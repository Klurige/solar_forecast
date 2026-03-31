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
ATTR_INTRADAY_SCALING = "intraday_scaling"

# Algorithm tuning
SLOTS_PER_DAY = 96  # 15-min intervals per day
MAX_HISTORY_YEARS = 10
CORRECTION_HALF_LIFE_DAYS = 35  # recent data is weighted 2× vs 35-day-old data
MIN_CORRECTION_SAMPLES = 5  # min readings per LOCAL-time slot before its factor is used
NIGHT_THRESHOLD_W = 10.0  # W  – skip recording below this (night / deep cloud)
MAX_RATIO = 8.0  # cap correction factor to prevent runaway
MIN_RATIO = 0.05  # floor correction factor

# Intra-day real-time scaling
INTRADAY_MIN_SAMPLES = 3  # completed daytime slots needed before scaling kicks in
INTRADAY_MIN_SCALING = 0.2  # don't scale today's remaining forecast below 20%
INTRADAY_MAX_SCALING = 3.0  # don't scale today's remaining forecast above 300%

# DB schema version — bump when the storage format changes incompatibly
# v2: slot key changed from UTC to local time
# v3: slot key changed from local time to solar-position bin (elev × azim)
DB_SCHEMA_VERSION = 3

# Solar-position slot configuration
# Elevation bins: 10° steps from 0° to 90° → 9 bins
# Azimuth bins:   30° steps around the compass  → 12 sectors
# Total possible slots: 9 × 12 = 108
# Encoding: elev_bin * SLOT_AZIMUTH_BINS + azim_bin
SLOT_ELEVATION_STEP = 10  # degrees per elevation bin
SLOT_AZIMUTH_BINS = 12  # number of 30° azimuth sectors
