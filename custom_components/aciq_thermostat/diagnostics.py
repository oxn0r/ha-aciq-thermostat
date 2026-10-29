"""Diagnostics download: latest state, last deploy result, pre-flight checks (serial and IPs redacted)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import AciqConfigEntry
from .const import LOG_TAIL_LINES, VERIFIED_DEV_APP_SHA256, VERIFIED_FIRMWARE
from .deployer import DeployError, bundled_daemon_sha256

# users post this download publicly on GitHub issues
TO_REDACT = {"serial", "host", "allowed_ips"}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: AciqConfigEntry) -> dict[str, Any]:
    c = entry.runtime_data
    try:
        preflight = await c.async_run_preflight()
    except DeployError as err:
        preflight = {"error": str(err)}
    try:
        recent_log: list[str] | dict[str, str] = await c.deployer.recent_log(LOG_TAIL_LINES)
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
        "egress_filter": c.deployer.last_egress,
        "egress_verified_at": c.egress_verified_at.isoformat() if c.egress_verified_at else None,
        "thermostat_boot_time": c.boot_time.isoformat() if c.boot_time else None,
        "daemon_start_time": c.daemon_start_time.isoformat() if c.daemon_start_time else None,
        "events": list(c.events),
        "commands": list(c.commands),
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
