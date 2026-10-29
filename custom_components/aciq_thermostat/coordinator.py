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
    CONF_SYNC_CLOCK,
    CONF_EXTRA_ALLOWED_IPS,
    DEFAULT_ADB_PORT,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    CLOCK_CHECK_INTERVAL,
    CLOCK_MAX_OFFSET,
    DEPLOY_MIN_INTERVAL,
    DOMAIN,
    EGRESS_VERIFY_INTERVAL,
    ISSUE_24V,
    ISSUE_24V_READ_ONLY,
    ISSUE_DEPLOY,
    ISSUE_FIRMWARE,
    NEW_ISSUE_URL,
    LONG_POLL_MAX_BACKOFF,
    LONG_POLL_TIMEOUT,
    PROBE_TIMEOUT,
    RANGE_VERIFY_TIMEOUT,
    REFRESH_DELAY,
    VERIFIED_FIRMWARE,
)
from .deployer import Deployer, DeployError
from .v24 import normalize, signature

_LOGGER = logging.getLogger(__name__)
_V24_LOGGED: dict[str, str] = {}   # host -> last 24 V summary logged

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


RUNTIME_OPTIONS = frozenset({CONF_BLOCK_EGRESS, CONF_SYNC_CLOCK})
# What a 24 V unit accepts with setpoint control on (the daemon refuses mode and fan there too)
V24_CONTROL_KEYS = frozenset({"mode", "temperature", "target_temp_low", "target_temp_high", "unit"})  # no fan


def explain_deploy(detail: dict[str, Any], uptime: float | None, trigger: str) -> str:
    """Plain-language reason for a (re)deploy, for the log, diagnostics and sensors."""
    cause, action = detail.get("cause"), detail.get("action")
    if action == "already_running":
        return "daemon already running; kept"
    if cause == "not_running":
        if uptime is not None and uptime < 900:
            return (f"thermostat rebooted {_fmt_duration(uptime)} ago (power loss or restart); "
                    "daemon reinstalled")
        if trigger == "setup":
            return "daemon was not running at setup; started"
        up = f" although the thermostat has been up {_fmt_duration(uptime)}" if uptime is not None else ""
        return f"daemon had stopped{up}; restarted"
    if cause == "build_changed":
        return "upgraded to the bundled daemon build"
    if cause == "args_changed":
        return "restarted with a new allowlist"
    if cause == "forced":
        return "restarted (not answering)" if trigger == "daemon_hung" else "restarted on request"
    return "deployed"


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
        self.deployer = Deployer(self.host, self.adb_port, self.api_port)
        self.session = async_get_clientsession(hass)
        self.failures = 0
        self.last_deploy_attempt: float | None = None
        self.last_deploy_result: str | None = None
        self.preflight: dict[str, Any] | None = None
        self.versions: dict[str, str] = {}
        self.outage: Outage | None = None
        self.events: deque[dict[str, Any]] = deque(maxlen=20)   # outages + deploys, for diagnostics
        self.commands: deque[dict[str, Any]] = deque(maxlen=10)  # control commands + results, for diagnostics
        self.debug_report: dict[str, Any] | None = None           # last "Collect debug report", for diagnostics
        self.debug_report_time: datetime | None = None
        self.v24_events: deque[dict[str, Any]] = deque(maxlen=60)  # 24 V state changes, for diagnostics
        self._v24_last: dict[str, Any] | None = None
        self.long_poll_supported: bool | None = None
        self.boot_time: datetime | None = None
        self.daemon_start_time: datetime | None = None
        self._lp_task: asyncio.Task[None] | None = None
        self.egress_verified_at: datetime | None = None
        self.last_deploy_detail: dict[str, Any] = {}
        self.clock: dict[str, Any] = {}            # last check: offset_s, corrected, time, ...
        self._clock_check_due = True               # at setup, after reboots/redeploys, every 6 h
        self._clock_task: asyncio.Task[None] | None = None
        # options the running setup was built with; changes to only runtime options
        # (block_egress) are applied live instead of reloading the entry
        self.options_snapshot: dict[str, Any] = dict(entry.options)
        self.data_snapshot: dict[str, Any] = dict(entry.data)
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
        detail = dict(self.deployer.last_deploy_detail)
        explanation = explain_deploy(detail, up, reason)
        self.last_deploy_detail = {**detail, "trigger": reason, "explanation": explanation,
                                   "time": dt_util.utcnow().isoformat(timespec="seconds")}
        self._event("deploy", reason=reason, result=result, explanation=explanation,
                    thermostat_uptime_s=up, egress=dict(self.deployer.last_egress))
        ir.async_delete_issue(self.hass, DOMAIN, f"{ISSUE_DEPLOY}_{self.config_entry.entry_id}")
        if detail.get("action") == "already_running":
            _LOGGER.debug("Daemon on %s already running (pid %s)", self.host, detail.get("pid"))
        else:
            _LOGGER.info("Daemon on %s: %s (%s)", self.host, explanation, result)
        if result != "already running":
            self.long_poll_supported = None  # new daemon: re-detect
        if self.block_egress:
            self.egress_verified_at = dt_util.utcnow()   # applied as part of the deploy
            self._egress_check_due = False
        if detail.get("action") != "already_running":
            self._clock_check_due = True             # fresh boot/daemon: check the clock soon
        return result

    # --------------------------------------------------------------- clock

    @property
    def sync_clock_enabled(self) -> bool:
        return bool(self.config_entry.options.get(CONF_SYNC_CLOCK, True))

    def _maybe_check_clock(self) -> None:
        if not self.sync_clock_enabled or not self.auto_deploy:
            return
        last = self.clock.get("checked_at")
        due = self._clock_check_due or last is None or (
            (dt_util.utcnow() - last).total_seconds() > CLOCK_CHECK_INTERVAL)
        if not due or (self._clock_task is not None and not self._clock_task.done()):
            return
        self._clock_check_due = False
        self._clock_task = self.config_entry.async_create_background_task(
            self.hass, self.async_check_clock(), f"{DOMAIN} clock check {self.host}")

    async def async_check_clock(self) -> None:
        """Compare the thermostat clock with HA's and correct it if it drifted."""
        try:
            res = await self.deployer.sync_clock(CLOCK_MAX_OFFSET, correct=self.sync_clock_enabled)
        except DeployError as err:
            _LOGGER.debug("Clock check on %s skipped: %s", self.host, err)
            self._clock_check_due = True
            return
        self.clock = {**res, "checked_at": dt_util.utcnow(),
                      **({"last_correction": dt_util.utcnow(), "last_correction_offset_s": res["offset_s"]}
                         if res.get("corrected") else
                         {k: v for k, v in self.clock.items() if k.startswith("last_correction")})}
        if res.get("corrected"):
            self._event("clock_corrected", offset_s=res["offset_s"], after_s=res.get("offset_after_s"))
            _LOGGER.info("Thermostat clock on %s was off by %+.1f s; corrected to Home Assistant's time "
                         "(now %+.1f s)", self.host, res["offset_s"], res.get("offset_after_s", 0.0))
        self.async_update_listeners()

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

    async def async_set_block_egress(self, enable: bool) -> None:
        """Apply or remove the rule live over ADB, then persist the option (no reload).

        Raises HomeAssistantError (state unchanged) if the thermostat can't be reached;
        the periodic verifier reconciles later in that case."""
        try:
            result = await self.deployer.verify_egress(enable, self.extra_ips())
        except DeployError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="egress_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        self.egress_verified_at = dt_util.utcnow()
        self._event("egress_toggle", enabled=enable, result=result, egress=dict(self.deployer.last_egress))
        _LOGGER.info("Cloud-traffic block on %s %s (%s)", self.host, "enabled" if enable else "disabled", result)
        new_options = {**self.config_entry.options, CONF_BLOCK_EGRESS: enable}
        self.options_snapshot = dict(new_options)       # tells the update listener: already applied
        self.hass.config_entries.async_update_entry(self.config_entry, options=new_options)

    def needs_reload(self) -> bool:
        """True if the entry changed in a way that needs a restart of the setup.
        Applies runtime-only changes (block_egress) live and updates the snapshot."""
        new_opts, new_data = dict(self.config_entry.options), dict(self.config_entry.data)
        changed = {k for k in set(new_opts) | set(self.options_snapshot)
                   if new_opts.get(k) != self.options_snapshot.get(k)}
        if new_data != self.data_snapshot or changed - RUNTIME_OPTIONS:
            return True
        if CONF_SYNC_CLOCK in changed and new_opts.get(CONF_SYNC_CLOCK, True):
            self._clock_check_due = True              # turned on: check at the next update
        if CONF_BLOCK_EGRESS in changed:
            # changed from the Configure dialog (the switch updates the snapshot itself)
            self.config_entry.async_create_background_task(
                self.hass, self._apply_egress_from_options(bool(new_opts.get(CONF_BLOCK_EGRESS))),
                f"{DOMAIN} egress apply {self.host}")
        self.options_snapshot = new_opts
        return False

    async def _apply_egress_from_options(self, enable: bool) -> None:
        try:
            await self.deployer.verify_egress(enable, self.extra_ips())
        except DeployError as err:
            _LOGGER.warning("Could not %s the cloud-traffic block on %s now (%s); will retry",
                            "apply" if enable else "remove", self.host, err)
            self._egress_check_due = True
            return
        self.egress_verified_at = dt_util.utcnow()
        self._event("egress_toggle", enabled=enable, result="applied from options",
                    egress=dict(self.deployer.last_egress))
        self.async_update_listeners()

    async def _verify_egress(self) -> None:
        try:
            result = await self.deployer.verify_egress(True, self.extra_ips())
        except DeployError as err:
            _LOGGER.warning("Could not verify the cloud-traffic block on %s: %s", self.host, err)
            self._egress_check_due = True
            return
        self.egress_verified_at = dt_util.utcnow()
        self._event("egress_check", result=result, egress=dict(self.deployer.last_egress))
        if result == "repaired":
            _LOGGER.warning("Cloud-traffic block on %s had drifted (%s); rebuilt", self.host,
                            ", ".join(self.deployer.last_egress.get("repaired", [])))
        elif result != "ok":
            _LOGGER.warning("Cloud-traffic block on %s was missing and has been re-applied", self.host)
        else:
            _LOGGER.debug("Cloud-traffic block on %s verified: %s", self.host, self.deployer.last_egress)

    async def async_collect_debug_report(self) -> dict[str, Any]:
        """Pre-flight plus the deep debug report (read-only), kept for diagnostics."""
        preflight = await self.async_run_preflight()
        self.debug_report = {"preflight": preflight, **await self.deployer.debug_report()}
        self.debug_report_time = dt_util.utcnow()
        self.async_update_listeners()                 # "Last debug report" shows it right away
        return self.debug_report

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
                        data: dict[str, Any] = normalize(await resp.json(content_type=None))
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
                data: dict[str, Any] = normalize(await resp.json(content_type=None))
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
        issue_24v = f"{ISSUE_24V}_{self.config_entry.entry_id}"
        if not data.get("dev", {}).get("seen"):
            if data.get("comm_mode") == "24v":
                self._report_24v(issue_24v, data.get("v24", {}))
                raise UpdateFailed(translation_domain=DOMAIN, translation_key="mode_24v")
            raise UpdateFailed(translation_domain=DOMAIN, translation_key="no_state_yet")
        ir.async_delete_issue(self.hass, DOMAIN, issue_24v)
        self._track_times(data)
        ir.async_delete_issue(self.hass, DOMAIN, f"{ISSUE_24V_READ_ONLY}_{self.config_entry.entry_id}")

        issue_id = f"{ISSUE_FIRMWARE}_{self.config_entry.entry_id}"
        if data.get("control_enabled") is False:
            ir.async_create_issue(
                self.hass, DOMAIN, issue_id, is_fixable=False, severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_FIRMWARE, learn_more_url=NEW_ISSUE_URL,
                translation_placeholders={"firmware": str(data.get("firmware")),
                                          "verified": VERIFIED_FIRMWARE},
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
        return data

    def _report_24v(self, issue_id: str, v24: dict[str, Any]) -> None:
        """Conventional 24 V wiring (issue #5) without 24 V state lines yet: say so and log the
        raw 24 V fields, which are what's needed if it stays that way."""
        summary = (f"setpoint {v24.get('setpoint')}, Auto {v24.get('low')}-{v24.get('high')}, "
                   f"op_mode {v24.get('op_mode')}, heat_mode {v24.get('heat_mode')}, "
                   f"terminal_state {v24.get('terminal_state')}") if v24.get("seen") else "no 24 V state lines yet"
        ir.async_create_issue(
            self.hass, DOMAIN, issue_id, is_fixable=False, severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_24V, translation_placeholders={"summary": summary},
            learn_more_url=NEW_ISSUE_URL,
        )
        # setup retries build a new coordinator each time: remember per host, log on change only
        if _V24_LOGGED.get(self.host) != summary:
            _V24_LOGGED[self.host] = summary
            _LOGGER.warning("Thermostat on %s runs in conventional 24 V mode and hasn't reported its 24 V "
                            "state yet (see issue #5). Raw 24 V state: %s", self.host, v24)

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

    def _track_v24(self, data: dict[str, Any]) -> None:
        """24 V units: log each change of mode/setpoints/outputs and keep the last
        60 for diagnostics. That timeline is what's needed to confirm the mapping."""
        if data.get("wiring") != "24v":
            return
        sig = signature(data)
        last, self._v24_last = self._v24_last, sig
        if last is None:
            _LOGGER.info("Thermostat on %s is in conventional 24 V mode. State: %s", self.host, sig)
            self.v24_events.append({"time": dt_util.utcnow().isoformat(timespec="seconds"), "initial": sig})
            return
        changes = {k: [last.get(k), v] for k, v in sig.items() if last.get(k) != v}
        if changes:
            self.v24_events.append({"time": dt_util.utcnow().isoformat(timespec="seconds"),
                                    "changes": changes, "room": data.get("current_temp")})
            _LOGGER.info("24 V state on %s changed: %s (room %s)", self.host,
                         ", ".join(f"{k} {a} -> {b}" for k, (a, b) in changes.items()), data.get("current_temp"))

    def _track_times(self, data: dict[str, Any]) -> None:
        """Boot and daemon start times from uptimes; only move on real changes (>2 min)."""
        self._track_v24(data)
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
                    self._clock_check_due = True
        self._maybe_verify_egress()
        self._maybe_check_clock()

    # --------------------------------------------------------------- control

    def _command(self, payload: dict[str, Any], result: str, **fields: Any) -> None:
        self.commands.append({"time": dt_util.utcnow().isoformat(timespec="seconds"),
                              "payload": payload, "result": result, **fields})

    async def async_control(self, payload: dict[str, Any]) -> None:
        data = self.data or {}
        if data.get("wiring") == "24v" and not set(payload) <= V24_CONTROL_KEYS:
            self._command(payload, "blocked_24v")
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="fan_24v")
        if data.get("control_enabled") is False:
            self._command(payload, "blocked_firmware")
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="control_unverified",
                                     translation_placeholders={"firmware": str(data.get("firmware"))})
        try:
            async with asyncio.timeout(10):
                async with self.session.post(self.base + API_CONTROL, json=payload) as resp:
                    body = await resp.text()
                    if resp.status != 200:
                        self._command(payload, "rejected", status=resp.status, response=body[:300])
                        _LOGGER.warning("Thermostat on %s rejected %s: %s", self.host, payload, body)
                        raise HomeAssistantError(
                            translation_domain=DOMAIN,
                            translation_key="command_rejected",
                            translation_placeholders={"payload": str(payload), "error": body},
                        )
        except (aiohttp.ClientError, TimeoutError) as err:
            self._command(payload, "unreachable", error=str(err) or type(err).__name__)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="unreachable",
                translation_placeholders={"error": str(err) or type(err).__name__},
            ) from err
        self._command(payload, "sent")
        _LOGGER.debug("Sent %s to %s; daemon replied %s", payload, self.host, body.strip())
        await asyncio.sleep(REFRESH_DELAY)
        await self.async_refresh()
        # newer paths (24 V wiring, firmware other than the verified one): confirm each command landed
        if data.get("wiring") == "24v" or data.get("firmware") != VERIFIED_FIRMWARE:
            want: dict[str, Any] = {}
            if "mode" in payload:
                want["mode"] = payload["mode"]
            if "temperature" in payload:
                want["setpoint"] = float(payload["temperature"])
            if "fan" in payload:
                want["fan"] = payload["fan"]
            if want:
                await self._verify_readback(want)

    async def _verify_readback(self, want: dict[str, Any]) -> None:
        """Confirm the thermostat reports the commanded mode / setpoint / fan (the setpoint is
        sent in the thermostat's own unit, so it compares directly). Logged and kept in the
        command log; not an error, since the thermostat can legitimately adjust a value."""
        def reported() -> dict[str, Any]:
            return {k: (self.data or {}).get(k) for k in want}

        loop = asyncio.get_running_loop()
        deadline = loop.time() + RANGE_VERIFY_TIMEOUT
        while reported() != want and loop.time() < deadline:
            await asyncio.sleep(1)
            await self.async_refresh()
        got = reported()
        record: dict[str, Any] = {"readback": "applied" if got == want else "not_applied"}
        if "setpoint" in got:
            record["reported"] = got["setpoint"]
        if "mode" in got:
            record["reported_mode"] = got["mode"]
        if "fan" in got:
            record["reported_fan"] = got["fan"]
        self.commands[-1].update(record)
        def fmt(d: dict[str, Any]) -> str:
            return ", ".join(f"{k} {v:g}" if isinstance(v, float) else f"{k} {v}" for k, v in d.items())
        if got != want:
            _LOGGER.warning("Thermostat on %s did not report %s (reports %s)", self.host, fmt(want), fmt(got))
        else:
            _LOGGER.info("Thermostat on %s took %s", self.host, fmt(want))

    def auto_range(self) -> tuple[float | None, float | None]:
        dev = (self.data or {}).get("dev", {})
        def num(v: Any) -> float | None:
            return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
        return num(dev.get("zone_lower")), num(dev.get("zone_upper"))

    async def async_set_auto_range(self, payload: dict[str, Any]) -> None:
        """Send an Auto heat/cool range and confirm the thermostat applied it.

        dev_app clamps values to its own limits and drops some without an error, so
        the new range is checked in the reported state instead of trusting the send.
        """
        want = (float(payload["target_temp_low"]), float(payload["target_temp_high"]))
        await self.async_control(payload)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + RANGE_VERIFY_TIMEOUT
        while self.auto_range() != want and loop.time() < deadline:
            await asyncio.sleep(1)
            await self.async_refresh()
        got = self.auto_range()
        if got == want:
            self.commands[-1]["readback"] = "applied"
            _LOGGER.debug("Auto range on %s is now %s-%s", self.host, *want)
            return
        self.commands[-1].update(readback="not_applied", reported_low=got[0], reported_high=got[1])
        _LOGGER.warning("Thermostat on %s did not apply Auto range %s-%s (reports %s-%s)",
                        self.host, *want, *got)
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="range_not_applied",
            translation_placeholders={"low": f"{want[0]:g}", "high": f"{want[1]:g}",
                                      "got_low": f"{got[0]:g}" if got[0] is not None else "?",
                                      "got_high": f"{got[1]:g}" if got[1] is not None else "?"},
        )
