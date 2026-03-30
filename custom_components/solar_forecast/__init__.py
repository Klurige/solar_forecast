"""Solar Forecast Refinement – Home Assistant custom component.

This component collects pairs of (Open-Meteo forecast W, actual inverter W)
every 15 minutes, stores them in a local SQLite database, and uses that history
to produce a bias-corrected 24-hour solar forecast at 15-minute resolution.

Installation
------------
1. Copy this directory to  <config>/custom_components/solar_forecast/
2. Restart Home Assistant.
3. Settings → Integrations → Add → "Solar Forecast Refinement"
4. Select your forecast sensor and inverter power sensor.
"""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import SolarForecastCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Solar Forecast Refinement from a config entry."""
    coordinator = SolarForecastCoordinator(hass, entry)
    await coordinator.async_setup()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        coordinator: SolarForecastCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown()
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update (reload on change)."""
    await hass.config_entries.async_reload(entry.entry_id)
