"""Constants for Solar Forecast Refinement."""

DOMAIN = "solar_forecast"

# Config entry keys
CONF_FORECAST_ENTITY = "forecast_entity"
CONF_POWER_ENTITY = "power_entity"
CONF_FORECAST_TOMORROW_ENTITY = "forecast_tomorrow_entity"

# Sensor
ATTR_FORECASTS = "forecasts"
ATTR_CORRECTION_FACTORS = "correction_factors"
ATTR_TOTAL_SAMPLES = "total_samples"
ATTR_DATA_SINCE = "data_since"
ATTR_ENERGY_TODAY_KWH = "energy_today_kwh"
ATTR_ENERGY_TOMORROW_KWH = "energy_tomorrow_kwh"

# Algorithm tuning
SLOTS_PER_DAY = 96  # 15-min intervals per day
MAX_HISTORY_YEARS = 10
CORRECTION_HALF_LIFE_DAYS = 35  # recent data is weighted 2× vs 35-day-old data
MIN_CORRECTION_SAMPLES = 5  # min readings per UTC slot before its factor is used
NIGHT_THRESHOLD_W = 10.0  # W  – skip recording below this (night / deep cloud)
MAX_RATIO = 8.0  # cap correction factor to prevent runaway
MIN_RATIO = 0.05  # floor correction factor
