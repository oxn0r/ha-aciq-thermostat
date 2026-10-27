"""Diagnostics download: latest state, last deploy result, pre-flight checks."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from . import AciqConfigEntry
from .const import VERIFIED_DEV_APP_SHA256, VERIFIED_FIRMWARE
from .deployer import DeployError, bundled_daemon_sha256


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: AciqConfigEntry) -> dict[str, Any]:
    c = entry.runtime_data
    try:
        preflight = await c.async_run_preflight()
    except DeployError as err:
        preflight = {"error": str(err)}
    return {
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
    }
