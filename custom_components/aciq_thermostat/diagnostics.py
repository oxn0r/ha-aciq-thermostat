"""Diagnostics download: latest state, last deploy result, pre-flight checks (serial and IPs redacted)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import AciqConfigEntry
from .const import CONF_ADDRESS_CHANGE, LOG_TAIL_LINES, VERIFIED_DEV_APP_SHA256, VERIFIED_FIRMWARE
from .deployer import DeployError, bundled_daemon_sha256
from .system import describe as describe_system

# users post this download publicly on GitHub issues
TO_REDACT = {"serial", "host", "allowed_ips", "ip", "old_host", "previous_host", "mac"}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: AciqConfigEntry) -> dict[str, Any]:
    c = entry.runtime_data
    preflight: dict[str, Any]
    recent_log: list[str] | dict[str, str]
    if c.adb_down:                                   # #14: don't wait on ADB that isn't there
        preflight = recent_log = {"error": "ADB unavailable"}
    else:
        try:
            preflight = await c.async_run_preflight()
        except DeployError as err:
            preflight = {"error": str(err)}
        try:
            recent_log = await c.deployer.recent_log(LOG_TAIL_LINES)
        except DeployError as err:
            recent_log = {"error": str(err)}
    return async_redact_data({
        "host": c.host,
        "options": dict(entry.options),
        "verified_firmware": VERIFIED_FIRMWARE,
        "verified_dev_app_sha256": VERIFIED_DEV_APP_SHA256,
        "bundled_daemon_sha256": await hass.async_add_executor_job(bundled_daemon_sha256),
        "last_deploy_result": c.last_deploy_result,
        "last_deploy": c.last_deploy_detail,
        "clock": {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in c.clock.items()},
        "versions": c.versions,
        "long_poll_supported": c.long_poll_supported,
        "adb_unavailable": c.adb_down,
        # #15: the last address change (DHCP discovery or fallback), addresses redacted
        "last_address_change": entry.data.get(CONF_ADDRESS_CHANGE),
        "egress_filter": c.deployer.last_egress,
        "egress_verified_at": c.egress_verified_at.isoformat() if c.egress_verified_at else None,
        "thermostat_boot_time": c.boot_time.isoformat() if c.boot_time else None,
        "daemon_start_time": c.daemon_start_time.isoformat() if c.daemon_start_time else None,
        "events": list(c.events),
        "commands": list(c.commands),
        "dry_return": c.dry_return,
        "system": describe_system(c.data or {}),
        "defrost": {"active": c.defrost, "cycle": c.defrost_cycle, "last": c.last_defrost},
        "protection": {"value": c.protect_state, "cycle": c.protection_cycle, "last": c.last_protection,
                       "changes": list(c.protection_log)},
        "v24_events": list(c.v24_events),
        "auto_range": {
            "zone_control": (c.data or {}).get("dev", {}).get("zone_control"),
            "temp_zone_mode": (c.data or {}).get("ui", {}).get("temp_zone_mode"),
            "deadband": (c.data or {}).get("ui", {}).get("deadband"),
            "low": c.auto_range()[0],
            "high": c.auto_range()[1],
        },
        "allowed_ips": c.allowed_ips(),
        "preflight": preflight,
        "state": c.data,
        # state and command lines from the thermostat's own log window (1-3 minutes), so testers
        # don't need ADB: make the changes, then download diagnostics right away
        "recent_log": recent_log,
        # deep build details for supporting new firmware: press "Collect debug report" first
        "debug_report": c.debug_report or "not collected (press the Collect debug report button, then download again)",
    }, TO_REDACT)
