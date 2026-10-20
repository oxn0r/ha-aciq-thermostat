"""Data coordinator: polls the on-device daemon and self-heals by redeploying it."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
import time
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    API_CONTROL,
    API_STATE,
    CONF_ADB_PORT,
    CONF_AUTO_DEPLOY,
    CONF_EXTRA_ALLOWED_IPS,
    DEFAULT_ADB_PORT,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DEPLOY_AFTER_FAILURES,
    DEPLOY_MIN_INTERVAL,
    DOMAIN,
    ISSUE_DEPLOY,
    ISSUE_FIRMWARE,
    REFRESH_DELAY,
    VERIFIED_FIRMWARE,
)
from .deployer import Deployer, DeployError

_LOGGER = logging.getLogger(__name__)


class AciqCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Polls GET /api/state; sends POST /api/control; redeploys the daemon when it's gone."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.data[CONF_HOST]}",
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self.host: str = entry.data[CONF_HOST]
        self.base = f"http://{self.host}:{entry.data.get(CONF_PORT, DEFAULT_PORT)}"
        self.deployer = Deployer(self.host, entry.data.get(CONF_ADB_PORT, DEFAULT_ADB_PORT))
        self.session = async_get_clientsession(hass)
        self.failures = 0
        self.last_deploy_attempt = 0.0
        self.last_deploy_result: str | None = None
        self.preflight: dict[str, Any] | None = None
        self.versions: dict[str, str] = {}
        self.device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.unique_id or self.host)},
            name=entry.title,
            manufacturer="ACiQ (Midea)",
            model="TL04-1",
            serial_number=entry.unique_id,
            configuration_url=f"{self.base}/health",
        )

    def set_versions(self, versions: dict[str, str]) -> None:
        self.versions = versions
        self.device_info["sw_version"] = versions.get("screen_firmware")
        self.device_info["hw_version"] = versions.get("hardware")

    # ------------------------------------------------------------- options

    @property
    def auto_deploy(self) -> bool:
        return self.config_entry.options.get(CONF_AUTO_DEPLOY, True)

    def extra_ips(self) -> list[str]:
        extra = self.config_entry.options.get(CONF_EXTRA_ALLOWED_IPS, "")
        return [ip.strip() for ip in extra.replace(";", ",").split(",") if ip.strip()]

    def allowed_ips(self) -> list[str]:
        """Allowlist the daemon was last started with (HA's own IP is added at deploy time)."""
        return self.deployer.last_allowlist or self.extra_ips()

    # -------------------------------------------------------------- deploy

    async def async_deploy(self, force: bool = False) -> str:
        self.last_deploy_attempt = time.monotonic()
        try:
            result = await self.deployer.ensure_daemon(self.extra_ips(), force=force)
        except DeployError as err:
            self.last_deploy_result = f"failed: {err}"
            ir.async_create_issue(
                self.hass, DOMAIN, f"{ISSUE_DEPLOY}_{self.config_entry.entry_id}",
                is_fixable=False, severity=ir.IssueSeverity.ERROR,
                translation_key=ISSUE_DEPLOY, translation_placeholders={"error": str(err)},
            )
            raise
        self.last_deploy_result = result
        ir.async_delete_issue(self.hass, DOMAIN, f"{ISSUE_DEPLOY}_{self.config_entry.entry_id}")
        _LOGGER.info("Daemon on %s: %s", self.host, result)
        return result

    async def async_run_preflight(self) -> dict[str, Any]:
        res = await self.deployer.preflight()
        self.preflight = res.as_dict()
        return self.preflight

    # --------------------------------------------------------------- polling

    async def _fetch(self) -> dict[str, Any]:
        async with asyncio.timeout(10):
            async with self.session.get(self.base + API_STATE) as resp:
                if resp.status != 200:
                    raise aiohttp.ClientError(f"HTTP {resp.status}")
                return await resp.json(content_type=None)

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            data = await self._fetch()
        except (aiohttp.ClientError, TimeoutError) as err:
            self.failures += 1
            if (self.auto_deploy and self.failures >= DEPLOY_AFTER_FAILURES
                    and time.monotonic() - self.last_deploy_attempt > DEPLOY_MIN_INTERVAL):
                _LOGGER.warning("Daemon unreachable (%s); redeploying via ADB", err)
                try:
                    await self.async_deploy()
                    await asyncio.sleep(2)
                    data = await self._fetch()
                except (DeployError, aiohttp.ClientError, TimeoutError) as err2:
                    raise UpdateFailed(f"Thermostat unreachable, redeploy failed: {err2}") from err2
            else:
                raise UpdateFailed(f"Thermostat API unreachable: {err}") from err

        self.failures = 0
        if not data.get("dev", {}).get("seen"):
            raise UpdateFailed("Daemon has not seen thermostat state yet")

        issue_id = f"{ISSUE_FIRMWARE}_{self.config_entry.entry_id}"
        if data.get("control_enabled") is False:
            ir.async_create_issue(
                self.hass, DOMAIN, issue_id, is_fixable=False, severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_FIRMWARE,
                translation_placeholders={"firmware": str(data.get("firmware")),
                                          "verified": VERIFIED_FIRMWARE},
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
        return data

    # --------------------------------------------------------------- control

    async def async_control(self, payload: dict[str, Any]) -> None:
        try:
            async with asyncio.timeout(10):
                async with self.session.post(self.base + API_CONTROL, json=payload) as resp:
                    body = await resp.text()
                    if resp.status != 200:
                        raise HomeAssistantError(f"Thermostat rejected {payload}: {body}")
        except (aiohttp.ClientError, TimeoutError) as err:
            raise HomeAssistantError(f"Thermostat unreachable: {err}") from err
        _LOGGER.debug("Sent %s", payload)
        await asyncio.sleep(REFRESH_DELAY)
        await self.async_refresh()
