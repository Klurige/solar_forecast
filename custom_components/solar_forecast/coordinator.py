"""Coordinator for Solar Forecast Refinement.

Responsibilities
----------------
* Collect (OM-forecast-W, actual-inverter-W) pairs every 15 minutes.
* Persist them in a local SQLite database (up to MAX_HISTORY_YEARS).
* Compute per-slot exponentially-weighted bias-correction factors.
* Assemble a refined 24-hour forecast at 15-minute resolution.
* Notify registered sensor entities whenever the forecast changes.

Correction model
----------------
For each of the 96 daily time-slots (UTC-based, 0 = 00:00–00:15 UTC):

    correction_factor[s] = Σ(ratio_i × weight_i) / Σ(weight_i)

where
    ratio_i  = actual_W[i] / om_W[i]           (clamped to [MIN_RATIO, MAX_RATIO])
    weight_i = exp(−ln(2) / half_life × days_ago_i)

Applied to the current forecast:

    refined_W[s] = om_W[s] × correction_factor[s]

Slots with fewer than MIN_CORRECTION_SAMPLES observations fall back to the
overall weighted mean across all slots.
"""

from __future__ import annotations

import logging
import math
import os
import sqlite3
from collections import defaultdict
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
    MAX_HISTORY_YEARS,
    MAX_RATIO,
    MIN_CORRECTION_SAMPLES,
    MIN_RATIO,
    NIGHT_THRESHOLD_W,
    SLOTS_PER_DAY,
)

_LOGGER = logging.getLogger(__name__)


class SolarForecastCoordinator:
    """Manages data collection, storage and forecast computation."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry

        self._db: sqlite3.Connection | None = None
        self._listeners: list[Callable] = []
        self._update_callbacks: list[Callable] = []

        # Publicly readable state (read by sensor entity)
        self.forecast: list[dict] = []
        self.correction_factors: dict[int, float] = {}
        self.total_samples: int = 0
        self.data_since: str | None = None

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
                slot     INTEGER NOT NULL,   -- 0-95, UTC-based 15-min slot
                om_w     REAL    NOT NULL,   -- Open Meteo forecast watts
                actual_w REAL    NOT NULL,   -- Inverter actual watts
                PRIMARY KEY (date, slot)
            )
            """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_slot ON readings(slot)")
        conn.commit()
        return conn

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

        Returns
        -------
        factors   : dict  slot → correction factor
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

        # Overall weighted mean (fallback for sparse slots)
        all_pairs = [(r, w) for vals in slot_data.values() for r, w in vals]
        if all_pairs:
            total_w = sum(w for _, w in all_pairs)
            overall_mean = (
                sum(r * w for r, w in all_pairs) / total_w if total_w > 0 else 1.0
            )
        else:
            overall_mean = 1.0

        factors: dict[int, float] = {}
        for slot in range(SLOTS_PER_DAY):
            data = slot_data.get(slot, [])
            if len(data) >= MIN_CORRECTION_SAMPLES:
                total_w = sum(w for _, w in data)
                factors[slot] = (
                    sum(r * w for r, w in data) / total_w
                    if total_w > 0
                    else overall_mean
                )
            else:
                factors[slot] = overall_mean

        n_samples = sum(len(v) for v in slot_data.values())
        oldest = min(dates_seen) if dates_seen else None
        return factors, n_samples, oldest

    def _purge_old_data_sync(self) -> None:
        db = self._ensure_db()
        cutoff = (date.today() - timedelta(days=MAX_HISTORY_YEARS * 365)).isoformat()
        db.execute("DELETE FROM readings WHERE date < ?", (cutoff,))
        db.commit()

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

        Returns a dict mapping UTC datetime → watts for the next 48 h window.
        """
        now_utc = datetime.now(timezone.utc)
        horizon = now_utc + timedelta(hours=49)  # slightly over 48 h
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
                if dt is not None and now_utc <= dt < horizon:
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
                    if dt is None or not (now_utc <= dt < horizon):
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

    def _slot_from_utc(self, dt: datetime) -> int:
        """Return the 0-95 slot index for a UTC datetime."""
        return (dt.hour * 60 + dt.minute) // 15

    # ── Forecast assembly ─────────────────────────────────────────────────────

    def _build_forecast(self) -> list[dict]:
        """
        Build 192 corrected 15-min forecast entries covering the next 48 h.

        48 h ensures tomorrow's full calendar day is always present regardless
        of the current time of day. The sensor state (next-24 h kWh) is
        computed from the first 96 entries; the remaining 96 entries are used
        by the tomorrow Lovelace card.

        Each entry:
            period_end          ISO string (UTC)
            pv_estimate         float kW  (corrected)
            pv_estimate_raw     float kW  (from Open Meteo, uncorrected)
            correction_factor   float
        """
        om_data = self._collect_om_data()
        if not om_data:
            _LOGGER.warning(
                "No OM forecast data available from %s", self._forecast_entity
            )
            return []

        now_utc = datetime.now(timezone.utc)

        # Round up to next 15-min boundary
        minutes_into_hour = now_utc.minute
        next_boundary_minutes = ((minutes_into_hour // 15) + 1) * 15
        first_period_end = now_utc.replace(
            minute=0, second=0, microsecond=0
        ) + timedelta(minutes=next_boundary_minutes)

        entries: list[dict] = []
        for i in range(2 * SLOTS_PER_DAY):  # 192 slots = 48 h
            period_end = first_period_end + timedelta(minutes=15 * i)
            slot = self._slot_from_utc(period_end)

            # Find the OM forecast value closest to this period boundary
            raw_w = self._nearest_om_value(om_data, period_end)
            factor = self.correction_factors.get(slot, 1.0)

            # Only apply correction during the day; at night keep zero
            if raw_w >= NIGHT_THRESHOLD_W:
                refined_w = max(0.0, raw_w * factor)
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

    async def _record_current_slot(self, now_utc: datetime) -> None:
        """Sample and store one (OM-forecast, actual) pair for the current slot."""
        # Actual inverter power
        power_state = self.hass.states.get(self._power_entity)
        if power_state is None or power_state.state in ("unknown", "unavailable"):
            return
        try:
            actual_w = float(power_state.state)
        except ValueError:
            return

        # OM forecast for the current slot
        om_data = self._collect_om_data()
        om_w = self._nearest_om_value(om_data, now_utc, max_gap_minutes=10)

        # Skip nighttime / fully overcast (both near zero)
        if om_w < NIGHT_THRESHOLD_W and actual_w < NIGHT_THRESHOLD_W:
            return

        slot = self._slot_from_utc(now_utc)
        # Use local calendar date so daily patterns align across DST changes
        local_date = now_utc.astimezone(self._local_tz).date().isoformat()

        await self.hass.async_add_executor_job(
            self._upsert_reading, local_date, slot, om_w, actual_w
        )
        _LOGGER.debug(
            "Recorded %s slot %d: om=%.1f W  actual=%.1f W",
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
