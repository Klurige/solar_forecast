"""Sensor platform for Solar Forecast Refinement."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    ATTR_CORRECTION_FACTORS,
    ATTR_DATA_SINCE,
    ATTR_ENERGY_TODAY_KWH,
    ATTR_ENERGY_TOMORROW_KWH,
    ATTR_FORECASTS,
    ATTR_TOTAL_SAMPLES,
    DOMAIN,
    SLOTS_PER_DAY,
)
from .coordinator import SolarForecastCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the sensor entity."""
    coordinator: SolarForecastCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([SolarForecastSensor(coordinator, entry)])


class SolarForecastSensor(SensorEntity):
    """
    Refined 24-hour solar forecast sensor.

    State   : total kWh expected over the next 24 hours (corrected)
    Unit    : kWh
    Attributes:
        forecasts           – list of 96 dicts, each:
                                period_end          UTC ISO string
                                pv_estimate         kW  (corrected)
                                pv_estimate_raw     kW  (OM, uncorrected)
                                correction_factor   float
        correction_factors  – dict slot (0-95) → learned factor
        total_samples       – how many (om, actual) pairs are stored
        data_since          – ISO date of oldest stored reading
        energy_today_kwh    – total kWh for today's remaining forecast
        energy_tomorrow_kwh – total kWh for next calendar day's forecast

    The sensor updates:
      * every 15 minutes (aligned to clock boundaries)
      * immediately whenever the upstream OM forecast changes
    """

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "kWh"
    _attr_icon = "mdi:solar-power"
    _attr_should_poll = False

    def __init__(
        self, coordinator: SolarForecastCoordinator, entry: ConfigEntry
    ) -> None:
        self._coordinator = coordinator
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_forecast"
        self._attr_name = "Solar Forecast (Refined)"

    # ── HA lifecycle ──────────────────────────────────────────────────────────

    async def async_added_to_hass(self) -> None:
        self._coordinator.register_update_callback(self._handle_coordinator_update)
        self._handle_coordinator_update()

    async def async_will_remove_from_hass(self) -> None:
        self._coordinator.unregister_update_callback(self._handle_coordinator_update)

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()

    # ── State & attributes ────────────────────────────────────────────────────

    @property
    def native_value(self) -> float:
        """Total corrected kWh for the next 24 hours."""
        return round(
            sum(e["pv_estimate"] * 0.25 for e in self._coordinator.forecast), 2
        )
        # pv_estimate is in kW; each slot is 15 min = 0.25 h → kWh

    @property
    def extra_state_attributes(self) -> dict:
        forecasts = self._coordinator.forecast
        now_utc = datetime.now(timezone.utc)

        today_kwh = 0.0
        tomorrow_kwh = 0.0
        today_date = now_utc.date()

        for entry in forecasts:
            try:
                period_end = datetime.fromisoformat(entry["period_end"])
                if period_end.tzinfo is None:
                    period_end = period_end.replace(tzinfo=timezone.utc)
                period_date = period_end.astimezone(self._coordinator._local_tz).date()
                kwh = entry["pv_estimate"] * 0.25  # kW × 0.25h
                if period_date == today_date:
                    today_kwh += kwh
                elif period_date > today_date:
                    tomorrow_kwh += kwh
            except (ValueError, KeyError):
                continue

        # Include only a sample of correction factors to keep the attribute readable
        # (every 4th slot = one per hour)
        cf_sample = {
            slot: round(factor, 3)
            for slot, factor in self._coordinator.correction_factors.items()
            if slot % 4 == 0
        }

        return {
            ATTR_FORECASTS: forecasts,
            ATTR_ENERGY_TODAY_KWH: round(today_kwh, 2),
            ATTR_ENERGY_TOMORROW_KWH: round(tomorrow_kwh, 2),
            ATTR_CORRECTION_FACTORS: cf_sample,
            ATTR_TOTAL_SAMPLES: self._coordinator.total_samples,
            ATTR_DATA_SINCE: self._coordinator.data_since,
        }
