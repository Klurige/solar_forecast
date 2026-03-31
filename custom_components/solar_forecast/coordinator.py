"""Coordinator for Solar Forecast Refinement.

Responsibilities
----------------
* Collect (OM-forecast-W, actual-inverter-W) pairs every 15 minutes.
* Persist them in a local SQLite database (up to MAX_HISTORY_YEARS).
* Compute per-slot exponentially-weighted bias-correction factors.
* Apply an intra-day real-time scaling based on how today is tracking.
* Assemble a refined 48-hour forecast (today + tomorrow) at 15-min resolution.
* Notify registered sensor entities whenever the forecast changes.

Correction model
----------------
Slots are LOCAL-time-of-day based (0 = 00:00–00:15 local, 95 = 23:45–00:00
local).  Using local time aligns the model with the actual solar cycle and
avoids DST drift that would corrupt per-slot learning.

For each of the 96 local-time daily slots:

    correction_factor[s] = Σ(ratio_i × weight_i) / Σ(weight_i)

where
    ratio_i  = actual_W[i] / om_W[i]           (clamped to [MIN_RATIO, MAX_RATIO])
    weight_i = exp(−ln(2) / half_life × days_ago_i)

Slots with fewer than MIN_CORRECTION_SAMPLES observations use factor=1.0
(no correction) until sufficient data is available.

Intra-day scaling
-----------------
Separately, a real-time "today's scaling" is computed from completed slots
of the current day:

    intraday_scaling = mean(actual_W / (om_W × correction_factor))

This captures whether today's actual conditions are systematically above or
below the refined forecast (e.g. unexpected cloud cover or unusual clarity)
and scales the remaining future slots of today accordingly.  Tomorrow's
slots are not affected.

DB schema version
-----------------
Version 2 introduced local-time slots (breaking change from v1 UTC slots).
On upgrade the readings table is cleared automatically.
"""

from __future__ import annotations

import logging
import math
import os
import sqlite3
from collections import defaultdict, deque
from datetime import date, datetime, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_utc_time_change,
)

from .const import (
    CONF_FORECAST_ENTITY,
    CONF_FORECAST_TOMORROW_ENTITY,
    CONF_POWER_ENTITY,
    CORRECTION_HALF_LIFE_DAYS,
    DB_SCHEMA_VERSION,
    INTRADAY_MAX_SCALING,
    INTRADAY_MIN_SAMPLES,
    INTRADAY_MIN_SCALING,
    MAX_HISTORY_YEARS,
    MAX_RATIO,
    MIN_CORRECTION_SAMPLES,
    MIN_RATIO,
    NIGHT_THRESHOLD_W,
    SLOT_AZIMUTH_BINS,
    SLOT_ELEVATION_STEP,
    SLOTS_PER_DAY,
)

_LOGGER = logging.getLogger(__name__)


# ── Solar position calculation ────────────────────────────────────────────────
# Pure-Python implementation; no external dependencies required.


def _solar_position(
    lat_deg: float, lon_deg: float, dt_utc: datetime
) -> tuple[float, float]:
    """
    Return (elevation_deg, azimuth_deg) of the sun for the given UTC datetime
    and geographic coordinates.

    Azimuth is measured clockwise from North (0° = N, 90° = E, 180° = S, 270° = W).
    Elevation is the angle above the horizon (negative = below horizon).

    Uses the Spencer / Iqbal algorithm accurate to ±0.01° for most dates.
    """
    lat = math.radians(lat_deg)

    # Day of year (1-based)
    doy = dt_utc.timetuple().tm_yday

    # Solar declination (radians) via Spencer's formula
    b = 2 * math.pi * (doy - 1) / 365
    decl = (
        0.006918
        - 0.399912 * math.cos(b)
        + 0.070257 * math.sin(b)
        - 0.006758 * math.cos(2 * b)
        + 0.000907 * math.sin(2 * b)
        - 0.002697 * math.cos(3 * b)
        + 0.00148 * math.sin(3 * b)
    )

    # Equation of time (minutes) via Spencer's formula
    eot_min = 229.18 * (
        0.000075
        + 0.001868 * math.cos(b)
        - 0.032077 * math.sin(b)
        - 0.014615 * math.cos(2 * b)
        - 0.04089 * math.sin(2 * b)
    )

    # True solar time (hours)
    utc_hour = dt_utc.hour + dt_utc.minute / 60 + dt_utc.second / 3600
    solar_time = utc_hour + lon_deg / 15 + eot_min / 60

    # Hour angle (radians): 0 at solar noon, negative AM, positive PM
    hour_angle = math.radians((solar_time - 12) * 15)

    # Solar elevation
    sin_elev = math.sin(lat) * math.sin(decl) + math.cos(lat) * math.cos(
        decl
    ) * math.cos(hour_angle)
    elevation = math.degrees(math.asin(max(-1.0, min(1.0, sin_elev))))

    # Solar azimuth (clockwise from North)
    cos_az = (math.sin(decl) - math.sin(lat) * sin_elev) / (
        math.cos(lat) * math.cos(math.radians(elevation))
    )
    cos_az = max(-1.0, min(1.0, cos_az))
    az = math.degrees(math.acos(cos_az))
    if math.sin(hour_angle) > 0:  # afternoon → azimuth > 180°
        az = 360 - az

    return elevation, az


def _solar_slot(
    lat: float, lon: float, dt_utc: datetime, elev_step: int, azim_bins: int
) -> int:
    """
    Map a UTC datetime to a solar-position slot integer.

    Slot encodes: elevation_bin * azim_bins + azimuth_bin
    where elevation_bin = floor(elevation / elev_step)
          azimuth_bin   = floor(azimuth / (360 / azim_bins))

    Returns -1 when the sun is below the horizon (nighttime).
    """
    elevation, azimuth = _solar_position(lat, lon, dt_utc)
    if elevation < 0:
        return -1  # night
    elev_bin = min(int(elevation // elev_step), (90 // elev_step) - 1)
    azim_bin = int(azimuth // (360 / azim_bins)) % azim_bins
    return elev_bin * azim_bins + azim_bin


class SolarForecastCoordinator:
    """Manages data collection, storage and forecast computation."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry

        self._db: sqlite3.Connection | None = None
        self._listeners: list[Callable] = []
        self._update_callbacks: list[Callable] = []

        # Rolling buffer of (utc_datetime, watts) from the power sensor.
        # Every state change is appended here; the 15-min tick drains old entries
        # and computes a time-weighted average for the completed slot.
        self._power_buffer: deque[tuple[datetime, float]] = deque()

        # Publicly readable state (read by sensor entity)
        self.forecast: list[dict] = []
        self.correction_factors: dict[int, float] = {}
        self.total_samples: int = 0
        self.data_since: str | None = None
        self.intraday_scaling: float = 1.0

    # ── Config helpers ────────────────────────────────────────────────────────

    @property
    def _forecast_entity(self) -> str:
        return self.entry.data[CONF_FORECAST_ENTITY]

    @property
    def _power_entity(self) -> str:
        return self.entry.data[CONF_POWER_ENTITY]

    @property
    def _forecast_tomorrow_entity(self) -> str | None:
        return self.entry.data.get(CONF_FORECAST_TOMORROW_ENTITY)

    @property
    def _local_tz(self) -> ZoneInfo:
        return ZoneInfo(self.hass.config.time_zone)

    @property
    def _latitude(self) -> float:
        return self.hass.config.latitude

    @property
    def _longitude(self) -> float:
        return self.hass.config.longitude

    # ── Database ──────────────────────────────────────────────────────────────

    def _db_path(self) -> str:
        storage_dir = os.path.join(self.hass.config.config_dir, ".storage")
        os.makedirs(storage_dir, exist_ok=True)
        return os.path.join(storage_dir, f"solar_forecast_{self.entry.entry_id}.db")

    def _open_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path(), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS readings (
                date     TEXT    NOT NULL,   -- ISO date, local calendar day
                slot     INTEGER NOT NULL,   -- solar-position bin (elev×azim)
                om_w     REAL    NOT NULL,   -- Open Meteo forecast watts
                actual_w REAL    NOT NULL,   -- Inverter actual watts (15-min avg)
                PRIMARY KEY (date, slot)
            )
            """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_slot ON readings(slot)")

        # Schema migration: apply version-specific transforms, or clear
        # incompatible data if no migration path is available.
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version < DB_SCHEMA_VERSION:
            _LOGGER.info(
                "Solar Forecast DB: migrating schema v%d → v%d",
                version,
                DB_SCHEMA_VERSION,
            )
            if version < 2:
                # v0/v1 used UTC-based slots and had a bug where om_w was
                # always 0 (< NIGHT_THRESHOLD_W), so no valid data was ever
                # stored.  Safe to clear.
                conn.execute("DELETE FROM readings")
                _LOGGER.info("Schema v0/v1: no valid data, cleared readings table")
            if version == 2:
                # v2 used local-time slots (0-95).  Migrate to solar-position
                # bins by reconstructing the local datetime for each row and
                # computing the sun's elevation + azimuth.  Rows that fall in
                # the same new bin are averaged together.
                self._migrate_v2_to_v3(conn)
            conn.execute(f"PRAGMA user_version = {DB_SCHEMA_VERSION}")

        conn.commit()
        return conn

    def _migrate_v2_to_v3(self, conn: sqlite3.Connection) -> None:
        """
        Migrate schema v2 (local-time slots 0-95) to v3 (solar-position bins).

        For each stored (date, local_slot) row:
          1. Reconstruct the local datetime at the midpoint of the 15-min slot.
          2. Convert to UTC and compute the solar-position bin.
          3. Discard nighttime rows (bin == -1).
          4. Average om_w / actual_w for rows that map to the same new bin
             on the same date (multiple 15-min local slots often fall in the
             same 10°×30° solar bin).
        """
        tz = ZoneInfo(self.hass.config.time_zone)
        lat = self.hass.config.latitude
        lon = self.hass.config.longitude

        rows = conn.execute(
            "SELECT date, slot, om_w, actual_w FROM readings"
        ).fetchall()

        if not rows:
            _LOGGER.info("Schema v2→v3 migration: no rows to migrate")
            return

        # Group by (date, new_solar_slot) → list of (om_w, actual_w)
        buckets: dict[tuple[str, int], list[tuple[float, float]]] = defaultdict(list)
        for date_str, old_slot, om_w, actual_w in rows:
            try:
                d = date.fromisoformat(date_str)
            except ValueError:
                continue
            # Midpoint of the 15-min local-time slot
            local_dt = datetime(d.year, d.month, d.day, tzinfo=tz) + timedelta(
                minutes=old_slot * 15 + 7
            )
            utc_dt = local_dt.astimezone(timezone.utc)
            new_slot = _solar_slot(
                lat, lon, utc_dt, SLOT_ELEVATION_STEP, SLOT_AZIMUTH_BINS
            )
            if new_slot < 0:
                continue  # nighttime; discard
            buckets[(date_str, new_slot)].append((om_w, actual_w))

        # Re-write the table with merged rows
        conn.execute("DELETE FROM readings")
        for (date_str, new_slot), values in buckets.items():
            avg_om = sum(v[0] for v in values) / len(values)
            avg_actual = sum(v[1] for v in values) / len(values)
            conn.execute(
                "INSERT INTO readings (date, slot, om_w, actual_w) VALUES (?, ?, ?, ?)",
                (date_str, new_slot, round(avg_om, 2), round(avg_actual, 2)),
            )
        _LOGGER.info(
            "Schema v2→v3 migration: %d rows → %d solar-position bins",
            len(rows),
            len(buckets),
        )

    def _ensure_db(self) -> sqlite3.Connection:
        if self._db is None:
            self._db = self._open_db()
        return self._db

    # All DB operations below are designed to be called via async_add_executor_job

    def _upsert_reading(
        self, date_str: str, slot: int, om_w: float, actual_w: float
    ) -> None:
        db = self._ensure_db()
        db.execute(
            "INSERT OR REPLACE INTO readings (date, slot, om_w, actual_w) "
            "VALUES (?, ?, ?, ?)",
            (date_str, slot, round(om_w, 2), round(actual_w, 2)),
        )
        db.commit()

    def _compute_correction_factors_sync(
        self,
    ) -> tuple[dict[int, float], int, str | None]:
        """
        Compute per-slot correction factors from the full history.

        Each 15-min UTC slot (0-95) gets its own factor once it has at least
        MIN_CORRECTION_SAMPLES readings.  Slots with fewer readings default to
        1.0 (no correction) so the refined line equals the raw line for
        time-of-day slots that haven't been observed enough yet.

        The former "overall_mean" fallback was removed because it caused a
        bootstrap problem: just a handful of noisy twilight samples would
        produce a very high mean that got incorrectly applied to all midday
        slots.

        Returns
        -------
        factors   : dict  slot → correction factor (only slots with enough data)
        n_samples : int   total usable readings
        oldest    : str   ISO date of oldest reading, or None
        """
        db = self._ensure_db()
        decay = math.log(2) / CORRECTION_HALF_LIFE_DAYS
        today = date.today()

        cursor = db.execute(
            "SELECT date, slot, om_w, actual_w FROM readings WHERE om_w >= ? "
            "ORDER BY date DESC",
            (NIGHT_THRESHOLD_W,),
        )

        # slot → list of (ratio, weight)
        slot_data: dict[int, list[tuple[float, float]]] = defaultdict(list)
        dates_seen: set[str] = set()

        for date_str, slot, om_w, actual_w in cursor:
            try:
                day = date.fromisoformat(date_str)
            except ValueError:
                continue
            days_ago = max(0, (today - day).days)
            weight = math.exp(-decay * days_ago)
            ratio = max(MIN_RATIO, min(MAX_RATIO, actual_w / om_w))
            slot_data[slot].append((ratio, weight))
            dates_seen.add(date_str)

        factors: dict[int, float] = {}
        for slot, data in slot_data.items():
            if len(data) >= MIN_CORRECTION_SAMPLES:
                total_w = sum(w for _, w in data)
                factors[slot] = (
                    sum(r * w for r, w in data) / total_w if total_w > 0 else 1.0
                )

        n_samples = sum(len(v) for v in slot_data.values())
        oldest = min(dates_seen) if dates_seen else None
        return factors, n_samples, oldest

    def _purge_old_data_sync(self) -> None:
        db = self._ensure_db()
        cutoff = (date.today() - timedelta(days=MAX_HISTORY_YEARS * 365)).isoformat()
        db.execute("DELETE FROM readings WHERE date < ?", (cutoff,))
        db.commit()

    def _compute_intraday_scaling_sync(self) -> float:
        """
        Compute a real-time "today's scaling" from completed slots of today.

        Compares actual production against the already-corrected (refined)
        forecast for each completed daytime slot today.  If actual is
        consistently above/below refined, the ratio is applied to remaining
        future slots of today to propagate the current conditions.

        Returns 1.0 when fewer than INTRADAY_MIN_SAMPLES are available.
        """
        db = self._ensure_db()
        today_str = date.today().isoformat()

        cursor = db.execute(
            "SELECT slot, om_w, actual_w FROM readings WHERE date = ? AND om_w >= ?",
            (today_str, NIGHT_THRESHOLD_W),
        )

        ratios: list[float] = []
        for slot, om_w, actual_w in cursor:
            factor = self.correction_factors.get(slot, 1.0)
            refined_w = om_w * factor
            if refined_w > 0:
                ratios.append(actual_w / refined_w)

        if len(ratios) < INTRADAY_MIN_SAMPLES:
            return 1.0

        raw_scaling = sum(ratios) / len(ratios)
        clamped = max(INTRADAY_MIN_SCALING, min(INTRADAY_MAX_SCALING, raw_scaling))
        _LOGGER.debug(
            "Intra-day scaling: %.3f (from %d slots, clamped to %.3f)",
            raw_scaling,
            len(ratios),
            clamped,
        )
        return clamped

    # ── Forecast parsing ──────────────────────────────────────────────────────

    def _parse_ts(self, ts_str: str) -> datetime | None:
        """Parse an ISO timestamp string, treating naive times as local."""
        try:
            dt = datetime.fromisoformat(str(ts_str).replace(".0000000", ""))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=self._local_tz)
            return dt.astimezone(timezone.utc)
        except (ValueError, TypeError):
            return None

    def _collect_om_data(self) -> dict[datetime, float]:
        """
        Collect Open Meteo forecast data for the next 48 h from the
        configured forecast entity (and optionally the tomorrow entity).

        Supports two attribute formats:
          * ``watts``     – dict  {ISO-timestamp: W}  (Open Meteo style)
          * ``forecasts`` – list  [{period_end, pv_estimate (kW)}]  (Solcast style)

        Returns a dict mapping UTC datetime → watts covering today + tomorrow.
        """
        # Anchor to local midnight so past slots of today are included.
        # The chart spans the full calendar day; we need historical values too.
        now_local = datetime.now(self._local_tz)
        local_midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        day_start_utc = local_midnight.astimezone(timezone.utc)
        horizon = day_start_utc + timedelta(hours=49)  # today + tomorrow + 1 h buffer
        result: dict[datetime, float] = {}

        entities = [self._forecast_entity]
        if self._forecast_tomorrow_entity:
            entities.append(self._forecast_tomorrow_entity)

        for entity_id in entities:
            state = self.hass.states.get(entity_id)
            if state is None:
                _LOGGER.debug("Entity %s not found", entity_id)
                continue

            # ── Open Meteo 'watts' dict format ────────────────────────────────
            watts: dict = state.attributes.get("watts") or {}
            for ts_str, w in watts.items():
                dt = self._parse_ts(ts_str)
                if dt is not None and day_start_utc <= dt < horizon:
                    try:
                        result[dt] = float(w)
                    except (ValueError, TypeError):
                        pass

            # ── Solcast / generic 'forecasts' list format ─────────────────────
            for entry in state.attributes.get("forecasts") or []:
                try:
                    ts_str = (
                        entry.get("period_end")
                        or entry.get("period_start")
                        or entry.get("datetime")
                        or ""
                    )
                    dt = self._parse_ts(ts_str)
                    if dt is None or not (day_start_utc <= dt < horizon):
                        continue
                    # pv_estimate is in kW → convert to W
                    w = (
                        float(
                            entry.get("pv_estimate")
                            or entry.get("pv_estimate_mean")
                            or 0
                        )
                        * 1000
                    )
                    result[dt] = w
                except (ValueError, TypeError, KeyError):
                    pass

        return result

    def _slot_for(self, dt_utc: datetime) -> int:
        """
        Return the solar-position slot index for the given UTC datetime.

        Slots encode (elevation_bin, azimuth_bin) as a single integer so the
        correction model learns factors tied to the physical sun position rather
        than clock time.  This means a tree that shadows the panels at a
        specific sun angle is learned once, regardless of which month or time
        of day that angle occurs.

        Returns -1 for nighttime (sun below horizon), which is excluded from
        recording and correction.
        """
        return _solar_slot(
            self._latitude,
            self._longitude,
            dt_utc,
            SLOT_ELEVATION_STEP,
            SLOT_AZIMUTH_BINS,
        )

    # ── Forecast assembly ─────────────────────────────────────────────────────

    def _build_forecast(self) -> list[dict]:
        """
        Build 192 corrected 15-min forecast entries covering today + tomorrow.

        The forecast is anchored to local midnight (00:15 first slot) so the
        full calendar day is always present in the attributes, regardless of the
        current time.  This ensures the ApexCharts card shows an unbroken line
        across the whole day even in the afternoon.

        Slots are keyed by LOCAL time of day (0 = 00:00–00:15 local) so the
        learned correction factors align with the actual shadow/solar cycle
        rather than UTC time.

        Intra-day scaling is applied only to future slots of today: if the
        current day is tracking above/below the refined forecast, that ratio
        is propagated into the remaining hours.  Tomorrow's slots are left
        unchanged because tomorrow's conditions are unknown.

        Entry layout:
            period_end          ISO string (UTC)
            pv_estimate         float kW  (corrected + intra-day scaled)
            pv_estimate_raw     float kW  (from Open Meteo, uncorrected)
            correction_factor   float     (per-slot learned factor, no intra-day)
        """
        om_data = self._collect_om_data()
        if not om_data:
            _LOGGER.warning(
                "No OM forecast data available from %s", self._forecast_entity
            )
            return []

        now_utc = datetime.now(timezone.utc)

        # Anchor to local midnight so today's past slots are included
        now_local = now_utc.astimezone(self._local_tz)
        local_midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        today_local = now_local.date()
        first_period_end = local_midnight.astimezone(timezone.utc) + timedelta(
            minutes=15
        )

        entries: list[dict] = []
        for i in range(2 * SLOTS_PER_DAY):  # 192 slots = today + tomorrow
            period_end = first_period_end + timedelta(minutes=15 * i)
            slot = self._slot_for(period_end)

            # Find the OM forecast value closest to this period boundary
            raw_w = self._nearest_om_value(om_data, period_end)

            # Slots not yet in correction_factors (too few samples, or nighttime
            # where slot==-1) default to factor=1.0.
            factor = self.correction_factors.get(slot, 1.0)

            # Only apply correction during the day; at night keep zero
            if raw_w >= NIGHT_THRESHOLD_W:
                refined_w = max(0.0, raw_w * factor)

                # Intra-day scaling: only for future slots of today.
                # Tomorrow is intentionally left unscaled — we don't know
                # whether today's weather pattern will persist.
                period_local_date = period_end.astimezone(self._local_tz).date()
                if period_end > now_utc and period_local_date == today_local:
                    refined_w = max(0.0, refined_w * self.intraday_scaling)
            else:
                refined_w = 0.0

            entries.append(
                {
                    "period_end": period_end.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
                    "pv_estimate": round(refined_w / 1000, 4),
                    "pv_estimate_raw": round(raw_w / 1000, 4),
                    "correction_factor": round(factor, 4),
                }
            )

        return entries

    @staticmethod
    def _nearest_om_value(
        om_data: dict[datetime, float], target: datetime, max_gap_minutes: int = 20
    ) -> float:
        """Return the OM watts value whose timestamp is nearest to *target*."""
        if not om_data:
            return 0.0
        best_dt = min(om_data, key=lambda dt: abs((dt - target).total_seconds()))
        if abs((best_dt - target).total_seconds()) <= max_gap_minutes * 60:
            return om_data[best_dt]
        return 0.0

    # ── Data collection ───────────────────────────────────────────────────────

    @callback
    def _on_power_state_change(self, event) -> None:
        """Append every inverter power reading to the rolling buffer."""
        new_state = event.data.get("new_state")
        if new_state is None or new_state.state in ("unknown", "unavailable"):
            return
        try:
            val = float(new_state.state)
        except ValueError:
            return
        self._power_buffer.append((datetime.now(timezone.utc), val))

    def _slot_average_w(self, slot_end_utc: datetime) -> float | None:
        """
        Compute the time-weighted average power (W) for the 15-min slot that
        just finished ending at *slot_end_utc*.

        Uses trapezoidal integration over all readings in the window, with the
        first and last values extended to the slot boundaries so the full 15
        minutes is always covered even if the sensor didn't update exactly at
        the boundary.

        Returns None if there are no readings in the window at all (e.g. HA
        just started and the buffer is empty).
        """
        slot_start = slot_end_utc - timedelta(minutes=15)

        # Collect readings that fall inside the slot window
        window = [
            (ts, w) for ts, w in self._power_buffer if slot_start <= ts <= slot_end_utc
        ]

        # Prune buffer entries older than the previous slot (keep a little extra)
        prune_before = slot_start - timedelta(minutes=5)
        while self._power_buffer and self._power_buffer[0][0] < prune_before:
            self._power_buffer.popleft()

        if not window:
            # Fall back to the most recent reading in the buffer if any
            if self._power_buffer:
                return self._power_buffer[-1][1]
            return None

        if len(window) == 1:
            return window[0][1]

        # Extend to slot boundaries using nearest edge value
        points = [(slot_start, window[0][1])] + window + [(slot_end_utc, window[-1][1])]

        total_watt_seconds = 0.0
        total_seconds = 0.0
        for i in range(len(points) - 1):
            t1, v1 = points[i]
            t2, v2 = points[i + 1]
            dt = (t2 - t1).total_seconds()
            total_watt_seconds += (v1 + v2) / 2 * dt
            total_seconds += dt

        return total_watt_seconds / total_seconds if total_seconds > 0 else window[0][1]

    async def _record_current_slot(self, now_utc: datetime) -> None:
        """Store the time-weighted average (OM-forecast, actual) pair for the slot."""
        # Time-weighted average power over the completed 15-min slot
        actual_w = self._slot_average_w(now_utc)
        if actual_w is None:
            _LOGGER.debug("No power readings in buffer, skipping slot recording")
            return

        # OM forecast for the current slot
        om_data = self._collect_om_data()
        om_w = self._nearest_om_value(om_data, now_utc, max_gap_minutes=10)

        # Skip nighttime / fully overcast (both near zero)
        if om_w < NIGHT_THRESHOLD_W and actual_w < NIGHT_THRESHOLD_W:
            return

        # Determine solar-position slot; skip nighttime (slot == -1)
        slot = self._slot_for(now_utc)
        if slot < 0:
            return
        # Use local calendar date so daily patterns align across DST changes
        local_date = now_utc.astimezone(self._local_tz).date().isoformat()

        await self.hass.async_add_executor_job(
            self._upsert_reading, local_date, slot, om_w, actual_w
        )
        _LOGGER.debug(
            "Recorded %s slot %d: om=%.1f W  actual=%.1f W (15-min avg)",
            local_date,
            slot,
            om_w,
            actual_w,
        )

    async def _refresh(self, now_utc: datetime | None = None) -> None:
        """Recompute the correction model and rebuild the output forecast."""
        if now_utc is not None:
            await self._record_current_slot(now_utc)
            await self.hass.async_add_executor_job(self._purge_old_data_sync)

        factors, total, oldest = await self.hass.async_add_executor_job(
            self._compute_correction_factors_sync
        )
        self.correction_factors = factors
        self.total_samples = total
        self.data_since = oldest

        # Intra-day scaling uses correction_factors, so compute it after
        self.intraday_scaling = await self.hass.async_add_executor_job(
            self._compute_intraday_scaling_sync
        )

        self.forecast = self._build_forecast()

        for cb in self._update_callbacks:
            cb()

    # ── Listeners ─────────────────────────────────────────────────────────────

    @callback
    def _on_15min_tick(self, now: datetime) -> None:
        """Triggered at xx:00:30, xx:15:30, xx:30:30, xx:45:30 UTC."""
        self.hass.async_create_task(self._refresh(now))

    @callback
    def _on_forecast_updated(self, event) -> None:
        """Triggered when the OM forecast sensor publishes new data."""
        self.hass.async_create_task(self._refresh())

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def async_setup(self) -> None:
        """Initialise DB, compute first forecast, register listeners."""
        await self.hass.async_add_executor_job(self._ensure_db)
        await self._refresh()

        # 15-minute aligned ticks (30 s past the boundary so sensors have settled)
        self._listeners.append(
            async_track_utc_time_change(
                self.hass,
                self._on_15min_tick,
                minute=[0, 15, 30, 45],
                second=30,
            )
        )

        # Immediate refresh when the OM sensor receives new forecast data
        self._listeners.append(
            async_track_state_change_event(
                self.hass,
                [self._forecast_entity],
                self._on_forecast_updated,
            )
        )

        # Buffer every power sensor change for time-weighted averaging
        self._listeners.append(
            async_track_state_change_event(
                self.hass,
                [self._power_entity],
                self._on_power_state_change,
            )
        )

        _LOGGER.info(
            "Solar Forecast Refinement set up. " "Forecast=%s  Power=%s  Samples=%d",
            self._forecast_entity,
            self._power_entity,
            self.total_samples,
        )

    async def async_shutdown(self) -> None:
        """Remove listeners and close the database."""
        for remove in self._listeners:
            remove()
        self._listeners.clear()
        if self._db is not None:
            await self.hass.async_add_executor_job(self._db.close)
            self._db = None

    # ── Sensor registration ───────────────────────────────────────────────────

    def register_update_callback(self, cb: Callable) -> None:
        self._update_callbacks.append(cb)

    def unregister_update_callback(self, cb: Callable) -> None:
        try:
            self._update_callbacks.remove(cb)
        except ValueError:
            pass
