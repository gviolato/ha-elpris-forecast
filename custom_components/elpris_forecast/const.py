"""Constants for the Elpris Forecast integration."""

from __future__ import annotations

DOMAIN = "elpris_forecast"

TIMEPRIS_URL = "https://elpriser.dk/data/timepris.json"
PROGNOSE_URL = "https://elpriser.dk/data/prognose.json"
ATTRIBUTION = "Data: Elpriser.dk (CC BY 4.0) / Energinet"

CONF_PLACE = "place"
CONF_FORGETTING_FACTOR = "forgetting_factor"
CONF_MIN_SCORED_DAYS = "min_scored_days"
CONF_MIN_EFFECTIVE_N = "min_effective_n"
CONF_SMOOTHING_HOURS = "smoothing_hours"
CONF_BAND_SOURCE = "band_source"
CONF_AGREEMENT = "agreement"

BAND_PUBLISHER = "publisher"
BAND_MODEL = "model"

DEFAULT_FORGETTING_FACTOR = 0.95
DEFAULT_MIN_SCORED_DAYS = 3
DEFAULT_MIN_EFFECTIVE_N = 5.0
DEFAULT_SMOOTHING_HOURS = 6
DEFAULT_BAND_SOURCE = BAND_PUBLISHER

# Firm prices: the source asks for at most one request per 5 minutes.
FIRM_REFRESH_MINUTES = 15
# The forecast is re-issued a few times a day; the CDN caches it for 5 minutes.
FORECAST_REFRESH_MINUTES = 30

MAX_HORIZON_HOURS = 168
TIMELINE_HOURS = 168
DANISH_VAT = 1.25

DB_FILENAME = "elpris_forecast.db"
