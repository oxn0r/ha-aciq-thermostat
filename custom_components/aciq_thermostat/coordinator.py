"""Data coordinator.

* Long-poll loop: GET /api/state?wait=<state_id> returns as soon as the thermostat's
  state changes (wall-unit edits reach HA in ~1-2 s). Falls back to plain polling on
  daemons without long-poll support.
* Watchdog poll every 30 s, which also handles outages: it classifies them with quick
  TCP checks (network down vs daemon gone vs daemon hung), logs the reason once per
  outage, and redeploys over ADB only when that can help.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
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
from homeassistant.util import dt as dt_util

from .const import (
    API_CONTROL,
    API_STATE,
    CONF_ADB_PORT,
    CONF_AUTO_DEPLOY,
    CONF_BLOCK_EGRESS,
    CONF_EXTRA_ALLOWED_IPS,
    DEFAULT_ADB_PORT,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DEPLOY_MIN_INTERVAL,
    DOMAIN,
    EGRESS_VERIFY_INTERVAL,
    ISSUE_DEPLOY,
    ISSUE_FIRMWARE,
    LONG_POLL_MAX_BACKOFF,
    LONG_POLL_TIMEOUT,
    PROBE_TIMEOUT,
    REFRESH_DELAY,
    VERIFIED_FIRMWARE,
)
from .deployer import Deployer, DeployError

_LOGGER = logging.getLogger(__name__)

# outage classes
NET_DOWN = "network_down"        # thermostat not reachable at all (Wi-Fi drop, power loss)
DAEMON_GONE = "daemon_gone"      # host up, API port refused (thermostat rebooted / daemon died)
DAEMON_HUNG = "daemon_hung"      # API port accepts but doesn't answer
API_ERROR = "api_error"          # answered with an HTTP error


class HttpStatusError(aiohttp.ClientError):
    """The daemon answered with a non-200 status."""

    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status


@dataclass
class Outage:
    started: float
    kind: str
    detail: str


def _fmt_duration(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h else (f"{m}m{sec:02d}s" if m else f"{sec}s")


class AciqCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Polls / long-polls the on-device daemon, sends commands, self-heals."""

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
        self.api_port: int = int(entry.data.get(CONF_PORT, DEFAULT_PORT))
        self.adb_port: int = int(entry.data.get(CONF_ADB_PORT, DEFAULT_ADB_PORT))
        self.base = f"http://{self.host}:{self.api_port}"
        self.deployer = Deployer(self.host, self.adb_port)
        self.session = async_get_clientsession(hass)
        self.failures = 0
        self.last_deploy_attempt: float | None = None
        self.last_deploy_result: str | None = None
        self.preflight: dict[str, Any] | None = None
        self.versions: dict[str, str] = {}
        self.outage: Outage | None = None
        self.events: deque[dict[str, Any]] = deque(maxlen=20)   # outages + deploys, for diagnostics
        self.long_poll_supported: bool | None = None
        self.boot_time: datetime | None = None
        self.daemon_start_time: datetime | None = None
        self._lp_task: asyncio.Task[None] | None = None
        self.egress_verified_at: datetime | None = None
        self._egress_check_due = True       # verify after setup/redeploy/reboot, then every 30 min
        self._egress_task: asyncio.Task[None] | None = None
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

    def _event(self, event: str, **fields: Any) -> None:
        self.events.append({"time": dt_util.utcnow().isoformat(timespec="seconds"), "event": event, **fields})

    # ------------------------------------------------------------- options

    @property
    def auto_deploy(self) -> bool:
        return bool(self.config_entry.options.get(CONF_AUTO_DEPLOY, True))

    def extra_ips(self) -> list[str]:
        extra = self.config_entry.options.get(CONF_EXTRA_ALLOWED_IPS, "")
        return [ip.strip() for ip in extra.replace(";", ",").split(",") if ip.strip()]

    def allowed_ips(self) -> list[str]:
        """Allowlist the daemon was last started with (HA's own IP is added at deploy time)."""
        return self.deployer.last_allowlist or self.extra_ips()

    # -------------------------------------------------------------- deploy

    async def async_deploy(self, force: bool = False, reason: str = "setup") -> str:
        self.last_deploy_attempt = time.monotonic()
        try:
            result = await self.deployer.ensure_daemon(
                self.extra_ips(), force=force,
                block_egress=bool(self.config_entry.options.get(CONF_BLOCK_EGRESS, False)))
        except DeployError as err:
            self.last_deploy_result = f"failed: {err}"
            self._event("deploy_failed", reason=reason, error=str(err))
            ir.async_create_issue(
                self.hass, DOMAIN, f"{ISSUE_DEPLOY}_{self.config_entry.entry_id}",
                is_fixable=False, severity=ir.IssueSeverity.ERROR,
                translation_key=ISSUE_DEPLOY, translation_placeholders={"error": str(err)},
            )
            raise
        self.last_deploy_result = result
        up = self.deployer.last_uptime_s
        self._event("deploy", reason=reason, result=result, thermostat_uptime_s=up,
                    egress=dict(self.deployer.last_egress))
        ir.async_delete_issue(self.hass, DOMAIN, f"{ISSUE_DEPLOY}_{self.config_entry.entry_id}")
        _LOGGER.info(
            "Daemon on %s (%s): %s; thermostat uptime %s%s",
            self.host, reason, result,
            _fmt_duration(up) if up is not None else "unknown",
            " (recently rebooted)" if up is not None and up < 900 else "",
        )
        if result != "already running":
            self.long_poll_supported = None  # new daemon: re-detect
        if self.block_egress:
            self.egress_verified_at = dt_util.utcnow()   # applied as part of the deploy
            self._egress_check_due = False
        return result

    # ------------------------------------------------------------- egress

    @property
    def block_egress(self) -> bool:
        return bool(self.config_entry.options.get(CONF_BLOCK_EGRESS, False))

    def _maybe_verify_egress(self) -> None:
        """Schedule a background check that the egress rule is (still) applied."""
        if not self.block_egress or not self.auto_deploy:
            return
        due = self._egress_check_due or self.egress_verified_at is None or (
            (dt_util.utcnow() - self.egress_verified_at).total_seconds() > EGRESS_VERIFY_INTERVAL)
        if not due or (self._egress_task is not None and not self._egress_task.done()):
            return
        self._egress_check_due = False
        self._egress_task = self.config_entry.async_create_background_task(
            self.hass, self._verify_egress(), f"{DOMAIN} egress check {self.host}")

    async def _verify_egress(self) -> None:
        try:
            result = await self.deployer.verify_egress(True, self.extra_ips())
        except DeployError as err:
            _LOGGER.warning("Could not verify the cloud-traffic block on %s: %s", self.host, err)
            self._egress_check_due = True
            return
        self.egress_verified_at = dt_util.utcnow()
        self._event("egress_check", result=result, egress=dict(self.deployer.last_egress))
        if result != "ok":
            _LOGGER.warning("Cloud-traffic block on %s was missing and has been re-applied", self.host)
        else:
            _LOGGER.debug("Cloud-traffic block on %s verified: %s", self.host, self.deployer.last_egress)

    async def async_run_preflight(self) -> dict[str, Any]:
        res = await self.deployer.preflight()
        self.preflight = res.as_dict()
        return self.preflight

    # ------------------------------------------------------------ long-poll

    def async_start_long_poll(self) -> None:
        if self._lp_task is None or self._lp_task.done():
            self._lp_task = self.config_entry.async_create_background_task(
                self.hass, self._long_poll_loop(), f"{DOMAIN} long-poll {self.host}")

    async def _long_poll_loop(self) -> None:
        backoff = 1.0
        while True:
            sid = (self.data or {}).get("state_id")
            if self.long_poll_supported is False:
                await asyncio.sleep(DEFAULT_SCAN_INTERVAL)   # old daemon: plain polling only
                continue
            if not self.last_update_success or not sid:
                # outage (or no state yet): re-check soon instead of waiting for the watchdog
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, LONG_POLL_MAX_BACKOFF)
                await self.async_request_refresh()
                continue
            try:
                async with asyncio.timeout(LONG_POLL_TIMEOUT + 10):
                    async with self.session.get(
                        self.base + API_STATE, params={"wait": sid, "timeout": str(LONG_POLL_TIMEOUT)}
                    ) as resp:
                        if resp.status != 200:
                            raise HttpStatusError(resp.status)
                        data: dict[str, Any] = await resp.json(content_type=None)
            except HttpStatusError as err:
                if err.status == 404:
                    self._long_poll_unsupported()
                    continue
                await self._long_poll_failed()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, LONG_POLL_MAX_BACKOFF)
                continue
            except (aiohttp.ClientError, TimeoutError, ValueError):
                await self._long_poll_failed()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, LONG_POLL_MAX_BACKOFF)
                continue
            if "state_id" not in data:
                self._long_poll_unsupported()
                continue
            backoff = 1.0
            if self.long_poll_supported is not True:
                self.long_poll_supported = True
                _LOGGER.debug("Long-poll active for %s", self.host)
            if data.get("dev", {}).get("seen"):
                self._recovered()
                self.failures = 0
                self._track_times(data)
                self.async_set_updated_data(data)

    def _long_poll_unsupported(self) -> None:
        if self.long_poll_supported is not False:
            _LOGGER.info("Daemon on %s has no long-poll support; using %ss polling",
                         self.host, DEFAULT_SCAN_INTERVAL)
        self.long_poll_supported = False

    async def _long_poll_failed(self) -> None:
        # the daemon or network just failed: let the watchdog classify it right away
        await self.async_request_refresh()

    # --------------------------------------------------------------- polling

    async def _fetch(self) -> dict[str, Any]:
        async with asyncio.timeout(10):
            async with self.session.get(self.base + API_STATE) as resp:
                if resp.status != 200:
                    raise HttpStatusError(resp.status)
                data: dict[str, Any] = await resp.json(content_type=None)
                return data

    async def _tcp(self, port: int) -> str:
        """'open', 'refused' or 'timeout' for a quick TCP connect."""
        try:
            async with asyncio.timeout(PROBE_TIMEOUT):
                _, writer = await asyncio.open_connection(self.host, port)
        except ConnectionRefusedError:
            return "refused"
        except (TimeoutError, OSError):
            return "timeout"
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
        return "open"

    async def _classify(self, err: BaseException) -> tuple[str, str]:
        if isinstance(err, HttpStatusError):
            return API_ERROR, f"HTTP {err.status}"
        api, adb = await asyncio.gather(self._tcp(self.api_port), self._tcp(self.adb_port))
        what = f"{type(err).__name__}{': ' + str(err) if str(err) else ''}; TCP {self.api_port} {api}, ADB {self.adb_port} {adb}"
        if api == "refused" and adb == "open":
            return DAEMON_GONE, f"{what} -> daemon not running (thermostat rebooted or daemon stopped)"
        if api == "open":
            return DAEMON_HUNG, f"{what} -> daemon not answering"
        if api == "timeout" and adb == "timeout":
            return NET_DOWN, f"{what} -> thermostat unreachable on the network (Wi-Fi drop or power loss)"
        return NET_DOWN, what

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            data = await self._fetch()
        except (aiohttp.ClientError, TimeoutError) as err:
            data = await self._handle_failure(err)
        self._recovered()
        self.failures = 0
        if not data.get("dev", {}).get("seen"):
            raise UpdateFailed(translation_domain=DOMAIN, translation_key="no_state_yet")
        self._track_times(data)

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

    async def _handle_failure(self, err: BaseException) -> dict[str, Any]:
        """Classify, log once per outage, redeploy when it can help. Returns data or raises."""
        self.failures += 1
        kind, detail = await self._classify(err)
        if self.outage is None or self.outage.kind != kind:
            if self.outage is None:
                self.outage = Outage(time.monotonic(), kind, detail)
            else:
                self.outage.kind, self.outage.detail = kind, detail
            self._event("outage", kind=kind, detail=detail)
            _LOGGER.warning("Thermostat API on %s unavailable: %s", self.host, detail)

        can_deploy = self.auto_deploy and (
            self.last_deploy_attempt is None
            or time.monotonic() - self.last_deploy_attempt > DEPLOY_MIN_INTERVAL)
        redeploy = (kind == DAEMON_GONE) or (kind == DAEMON_HUNG and self.failures >= 2)
        if redeploy and can_deploy:
            _LOGGER.warning("Redeploying the daemon on %s via ADB (%s)", self.host, kind)
            try:
                await self.async_deploy(force=kind == DAEMON_HUNG, reason=kind)
                await asyncio.sleep(1)
                return await self._fetch()
            except (DeployError, aiohttp.ClientError, TimeoutError) as err2:
                raise UpdateFailed(
                    translation_domain=DOMAIN, translation_key="redeploy_failed",
                    translation_placeholders={"error": str(err2) or type(err2).__name__},
                ) from err2
        raise UpdateFailed(
            translation_domain=DOMAIN, translation_key="unreachable",
            translation_placeholders={"error": detail},
        ) from err

    def _recovered(self) -> None:
        if self.outage is None:
            return
        took = time.monotonic() - self.outage.started
        self._event("recovered", after_s=round(took), kind=self.outage.kind)
        _LOGGER.info("Thermostat API on %s back after %s (%s)", self.host, _fmt_duration(took),
                     self.outage.kind)
        self.outage = None

    def _track_times(self, data: dict[str, Any]) -> None:
        """Boot and daemon start times from uptimes; only move on real changes (>2 min)."""
        now = dt_util.utcnow()
        for attr, key in (("boot_time", "uptime_s"), ("daemon_start_time", "daemon_uptime_s")):
            secs = data.get(key)
            if not isinstance(secs, (int, float)) or isinstance(secs, bool) or secs < 0:
                continue
            new = (now - timedelta(seconds=secs)).replace(microsecond=0)
            old: datetime | None = getattr(self, attr)
            if old is None or abs((new - old).total_seconds()) > 120:
                setattr(self, attr, new)
                if attr == "boot_time" and old is not None:
                    _LOGGER.warning("Thermostat %s rebooted (uptime %s)", self.host, _fmt_duration(secs))
                    self._event("reboot", uptime_s=secs)
                    self._egress_check_due = True
        self._maybe_verify_egress()

    # --------------------------------------------------------------- control

    async def async_control(self, payload: dict[str, Any]) -> None:
        try:
            async with asyncio.timeout(10):
                async with self.session.post(self.base + API_CONTROL, json=payload) as resp:
                    body = await resp.text()
                    if resp.status != 200:
                        raise HomeAssistantError(
                            translation_domain=DOMAIN,
                            translation_key="command_rejected",
                            translation_placeholders={"payload": str(payload), "error": body},
                        )
        except (aiohttp.ClientError, TimeoutError) as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="unreachable",
                translation_placeholders={"error": str(err) or type(err).__name__},
            ) from err
        _LOGGER.debug("Sent %s", payload)
        await asyncio.sleep(REFRESH_DELAY)
        await self.async_refresh()
