"""Constants for EasyTouch Local."""

from __future__ import annotations

from datetime import timedelta
import logging

DOMAIN = "easytouch_local"
LOGGER = logging.getLogger(__package__)
MANUFACTURER = "Pentair"
MODEL = "EasyTouch"

DEFAULT_PORT = 8899  # Elfin EW11 default TCP server port

# Options
CONF_READ_ONLY = "read_only"
CONF_SOURCE_ADDR = "source_addr"
CONF_POOL_CIRCUIT = "pool_circuit"
CONF_HIGH_SPEED_CIRCUIT = "high_speed_circuit"
CONF_EXTRA_CIRCUITS = "extra_circuits"
CONF_POWER_ENTITY = "power_entity"
CONF_POWER_THRESHOLD = "power_threshold"
CONF_SCHEDULE_MODE = "schedule_mode"
CONF_SUMMER_MONTHS = "summer_months"
CONF_SCHEDULE_ENABLED_ENTITY = "schedule_enabled_entity"
CONF_SUMMER_WEEKDAY_START = "summer_weekday_start"
CONF_SUMMER_WEEKDAY_STOP = "summer_weekday_stop"
CONF_SUMMER_WEEKEND_START = "summer_weekend_start"
CONF_SUMMER_WEEKEND_STOP = "summer_weekend_stop"
CONF_WINTER_START = "winter_start"
CONF_WINTER_STOP = "winter_stop"

SCHEDULE_MODE_HA = "home_assistant"
SCHEDULE_MODE_CONTROLLER = "controller"
SCHEDULE_MODES = [SCHEDULE_MODE_HA, SCHEDULE_MODE_CONTROLLER]

TIME_HELPER_KEYS = (
    CONF_SUMMER_WEEKDAY_START,
    CONF_SUMMER_WEEKDAY_STOP,
    CONF_SUMMER_WEEKEND_START,
    CONF_SUMMER_WEEKEND_STOP,
    CONF_WINTER_START,
    CONF_WINTER_STOP,
)

DEFAULT_OPTIONS: dict = {
    CONF_READ_ONLY: True,
    CONF_SOURCE_ADDR: 33,
    CONF_POOL_CIRCUIT: 6,
    CONF_HIGH_SPEED_CIRCUIT: 2,
    CONF_EXTRA_CIRCUITS: "",
    CONF_POWER_ENTITY: "sensor.span_panel_span_cloud_pool_equipment_power",
    CONF_POWER_THRESHOLD: 300,
    CONF_SCHEDULE_MODE: SCHEDULE_MODE_HA,
    CONF_SUMMER_MONTHS: "4-10",
    CONF_SCHEDULE_ENABLED_ENTITY: "input_boolean.pool_pump_schedule_enabled",
    CONF_SUMMER_WEEKDAY_START: "input_datetime.pool_summer_weekday_start",
    CONF_SUMMER_WEEKDAY_STOP: "input_datetime.pool_summer_weekday_stop",
    CONF_SUMMER_WEEKEND_START: "input_datetime.pool_summer_weekend_start",
    CONF_SUMMER_WEEKEND_STOP: "input_datetime.pool_summer_weekend_stop",
    CONF_WINTER_START: "input_datetime.pool_winter_start",
    CONF_WINTER_STOP: "input_datetime.pool_winter_stop",
}

# Timing
REFRESH_INTERVAL = timedelta(seconds=5)  # cheap: just re-checks link health
SETUP_WAIT = 10.0  # seconds to wait for the first status at startup
SCHEDULE_CHECK_INTERVAL = timedelta(minutes=30)
SCHEDULE_RETRY_DELAY = 300.0  # seconds after a failed schedule sync
SCHEDULE_DEBOUNCE = 5.0  # seconds after a helper change
RECONNECT_SETTLE = 10.0  # seconds after the link comes back before writing
CLOCK_DRIFT_LIMIT = 2  # minutes
CLOCK_MIN_RESYNC = 1800.0  # seconds between drift corrections
POWER_LIVE_WINDOW = timedelta(minutes=2)
POWER_GRACE = 90.0  # seconds after a relay change before power can veto "on"
