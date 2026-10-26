"""Constants for the ACiQ Thermostat (local) integration."""

from __future__ import annotations

DOMAIN = "aciq_thermostat"

CONF_ADB_PORT = "adb_port"
CONF_AUTO_DEPLOY = "auto_deploy"
CONF_EXTRA_ALLOWED_IPS = "extra_allowed_ips"
CONF_BLOCK_EGRESS = "block_egress"
CONF_SYNC_CLOCK = "sync_clock"

DEFAULT_NAME = "Thermostat"
DEFAULT_PORT = 8080
DEFAULT_ADB_PORT = 5555
DEFAULT_SCAN_INTERVAL = 30

API_HEALTH = "/health"
API_STATE = "/api/state"
API_CONTROL = "/api/control"

# Seconds to wait after a command before re-reading state (long-poll usually
# delivers the change sooner)
REFRESH_DELAY = 1
# After an Auto range write: how long to wait for the thermostat to report the new
# range (dev_app clamps or ignores values without an error)
RANGE_VERIFY_TIMEOUT = 6
# Long-poll: daemon holds GET /api/state?wait= up to this many seconds
LONG_POLL_TIMEOUT = 25
# Retry/backoff ceiling for the long-poll loop and outage re-checks
LONG_POLL_MAX_BACKOFF = 10
# TCP probe timeout used to classify outages
PROBE_TIMEOUT = 3
# Minimum time between automatic redeploys
DEPLOY_MIN_INTERVAL = 60
# How often to confirm the egress rule is still in place (also after every reboot)
EGRESS_VERIFY_INTERVAL = 1800
# Thermostat clock: checked at setup, after reboots/redeploys and this often; corrected
# when off by at least CLOCK_MAX_OFFSET seconds (date has 1 s resolution on the device)
CLOCK_CHECK_INTERVAL = 6 * 3600
CLOCK_MAX_OFFSET = 2.0

# ---- Device / firmware the control frames were verified on (2026-10-19) ----
VERIFIED_FIRMWARE = "tina.tangly.20260314.152301"
VERIFIED_DEV_APP_SHA256 = "63ff55d09f90b0ef896ed5cedccffac50e6a1a4b47efd220e2cb3235ffcabeb3"

# Bundled daemon (custom_components/aciq_thermostat/bin/)
DAEMON_FILENAME = "thermostat_api"
DAEMON_DEVICE_PATH = "/tmp/thermostat_api"

# Setpoint limits (device heating_max / cooling_min)
MIN_TEMP = 60
MAX_TEMP = 86
# When the thermostat is set to Celsius (its own heating_max / cooling_min)
MIN_TEMP_C = 16
MAX_TEMP_C = 30
TEMP_STEP = 1

# Daemon "mode" values
DEV_OFF = "off"
DEV_AUTO = "auto"
DEV_COOL = "cool"
DEV_HEAT = "heat"
DEV_FAN_ONLY = "fan_only"
DEV_EMERGENCY_HEAT = "emergency_heat"

# Fan speed is only adjustable in these modes (the screen/app lock it otherwise)
FAN_ADJUSTABLE_MODES = (DEV_COOL, DEV_HEAT)
FAN_MODES = ["auto", "low", "medium", "high"]

# dev_app auto_state_t (rac_msg.h): what Auto mode is currently doing
AUTO_STATE_NAMES = {0: "none", 1: "cool", 2: "heat", 3: "dry", 4: "fan"}
AUTO_STATE_COOL = 1
AUTO_STATE_HEAT = 2
AUTO_STATE_DRY = 3

# Fallback when auto_state is unknown: indoor coil vs room temperature (degF)
COIL_HEATING_DELTA = 15.0
COIL_COOLING_DELTA = 10.0

# Auto setpoint type (dev_app temp_zone_control): one setpoint or a heat/cool range
AUTO_SETPOINT_SINGLE = "single"
AUTO_SETPOINT_RANGE = "range"
AUTO_SETPOINT_UNKNOWN = "unknown"

PRESET_NONE = "none"
PRESET_EMERGENCY_HEAT = "emergency_heat"

ISSUE_FIRMWARE = "firmware_unverified"
ISSUE_24V = "conventional_24v"
ISSUE_DEPLOY = "deploy_failed"
