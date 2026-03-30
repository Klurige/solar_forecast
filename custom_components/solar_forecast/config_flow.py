"""Config flow for Solar Forecast Refinement."""
from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers import selector

from .const import (
    CONF_FORECAST_ENTITY,
    CONF_FORECAST_TOMORROW_ENTITY,
    CONF_POWER_ENTITY,
    DOMAIN,
)

_ENTITY_SELECTOR = selector.EntitySelector(
    selector.EntitySelectorConfig(domain="sensor")
)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(
            CONF_FORECAST_ENTITY,
            description={"suggested_value": "sensor.energy_production_today_2"},
        ): _ENTITY_SELECTOR,
        vol.Required(
            CONF_POWER_ENTITY,
            description={"suggested_value": "sensor.input_power_with_efficiency_loss"},
        ): _ENTITY_SELECTOR,
        vol.Optional(CONF_FORECAST_TOMORROW_ENTITY): _ENTITY_SELECTOR,
    }
)


class SolarForecastConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the config flow for Solar Forecast Refinement."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict | None = None
    ) -> config_entries.FlowResult:
        """Handle the initial setup step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Verify the entities exist
            for key in (CONF_FORECAST_ENTITY, CONF_POWER_ENTITY):
                entity_id = user_input.get(key)
                if entity_id and self.hass.states.get(entity_id) is None:
                    errors[key] = "entity_not_found"

            if not errors:
                await self.async_set_unique_id(
                    f"{user_input[CONF_FORECAST_ENTITY]}_{user_input[CONF_POWER_ENTITY]}"
                )
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="Solar Forecast Refinement",
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
        )
