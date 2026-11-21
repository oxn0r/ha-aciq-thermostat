"""Config flow: user setup, DHCP discovery, reconfigure, and options.

Every path runs the read-only ADB pre-flight checks before an entry is
created or changed.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import time
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.core import callback
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ADB_PORT,
    CONF_AUTO_DEPLOY,
    CONF_BLOCK_EGRESS,
    CONF_DRY_RETURN,
    CONF_EXTRA_ALLOWED_IPS,
    CONF_HIDE_FAN,
    CONF_ADDRESS_CHANGE,
    CONF_PREVIOUS_HOST,
    CONF_SYNC_CLOCK,
    DEFAULT_ADB_PORT,
    DEFAULT_NAME,
    DEFAULT_PORT,
    DOMAIN,
    NEW_ISSUE_URL,
)
from .deployer import Deployer, DeployError, PreflightResult, tcp_open

_LOGGER = logging.getLogger(__name__)

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): str,
        vol.Optional(CONF_PORT, default=DEFAULT_PORT): vol.All(vol.Coerce(int), vol.Range(1, 65535)),
        vol.Optional(CONF_ADB_PORT, default=DEFAULT_ADB_PORT): vol.All(
            vol.Coerce(int), vol.Range(1, 65535)
        ),
    }
)


def _valid_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


class AciqConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for ACiQ Thermostat (Local)."""

    VERSION = 1

    def __init__(self) -> None:
        self._input: dict[str, Any] = {}
        self._result: PreflightResult | None = None

    async def _preflight(
        self, user_input: dict[str, Any], errors: dict[str, str], placeholders: dict[str, str]
    ) -> PreflightResult | None:
        """Validate input and run pre-flight; fill errors/placeholders on failure."""
        host = str(user_input[CONF_HOST]).strip()
        user_input[CONF_HOST] = host
        if not _valid_ip(host):
            errors[CONF_HOST] = "invalid_host"
            return None
        try:
            res = await Deployer(host, int(user_input[CONF_ADB_PORT]),
                                 int(user_input.get(CONF_PORT, DEFAULT_PORT))).preflight()
        except DeployError as err:
            _LOGGER.warning("Pre-flight failed for %s: %s", host, err)
            errors["base"] = "cannot_connect"
            placeholders["checks"] = str(err)
            return None
        if not res.passed:
            errors["base"] = "checks_failed"
            placeholders["checks"] = res.summary() + await self._save_debug_report(host, user_input, res)
            return None
        if not res.serial:
            errors["base"] = "no_serial"
            return None
        return res

    async def _save_debug_report(self, host: str, user_input: dict[str, Any], res: PreflightResult) -> str:
        """Unsupported build or setup: collect the read-only debug report into /config, so the
        issue can carry everything needed to support it. Returns a line for the error text."""
        try:
            report = await Deployer(host, int(user_input[CONF_ADB_PORT]),
                                    int(user_input.get(CONF_PORT, DEFAULT_PORT))).debug_report()
            report = {"preflight": res.as_dict(), **report}
        except DeployError as err:
            _LOGGER.warning("Debug report for %s failed: %s", host, err)
            return ""
        name = f"aciq_thermostat_debug_report_{time.strftime('%Y%m%d_%H%M%S')}.json"
        path = self.hass.config.path(name)

        def write() -> None:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=1)

        try:
            await self.hass.async_add_executor_job(write)
        except OSError as err:
            _LOGGER.warning("Could not save the debug report to %s: %s", path, err)
            return ""
        _LOGGER.info("Saved debug report for %s to %s", host, path)
        return (f"\n\nA debug report was saved to `/config/{name}` (no IPs or serial). "
                f"Please [open a new-firmware issue]({NEW_ISSUE_URL}) and attach it.")

    # ------------------------------------------------------------------ user

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        placeholders = {"checks": ""}
        if user_input is not None:
            res = await self._preflight(user_input, errors, placeholders)
            if res is not None:
                await self.async_set_unique_id(res.serial)
                self._abort_if_unique_id_configured(updates={CONF_HOST: user_input[CONF_HOST]})
                self._input, self._result = user_input, res
                return await self.async_step_confirm()
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(USER_SCHEMA, user_input or {}),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        assert self._result is not None
        if user_input is not None:
            data = {k: self._input[k] for k in (CONF_HOST, CONF_PORT, CONF_ADB_PORT)}
            return self.async_create_entry(
                title=self._input.get(CONF_NAME) or DEFAULT_NAME,
                data=data,
                options={CONF_AUTO_DEPLOY: True, CONF_EXTRA_ALLOWED_IPS: ""},
            )
        return self.async_show_form(
            step_id="confirm",
            description_placeholders={"checks": self._result.summary(), "host": self._input[CONF_HOST]},
        )

    # ------------------------------------------------------------- discovery

    async def async_step_dhcp(self, discovery_info: DhcpServiceInfo) -> ConfigFlowResult:
        """Thermostat seen on DHCP (hostname midea_*, Midea Wi-Fi module MAC)."""
        host = discovery_info.ip
        # Cheap path first: an entry already uses this IP -> nothing to do (no ADB traffic).
        for entry in self._async_current_entries(include_ignore=False):
            if entry.data.get(CONF_HOST) == host:
                return self.async_abort(reason="already_configured")
        try:
            res = await Deployer(host, DEFAULT_ADB_PORT).preflight()
        except DeployError:
            return self.async_abort(reason="cannot_connect")
        if not res.passed or not res.serial:
            return self.async_abort(reason="not_supported")
        await self.async_set_unique_id(res.serial)
        # Known thermostat with a new IP -> update the entry (discovery-update-info), but (#15) only
        # if its current address no longer answers: a thermostat that briefly joins another network
        # must not pull a working entry away. The old address is kept to fall back to.
        known = self.hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, res.serial)
        if known is not None and (old := known.data.get(CONF_HOST)) and old != host:
            ports = (known.data.get(CONF_PORT, DEFAULT_PORT), known.data.get(CONF_ADB_PORT, DEFAULT_ADB_PORT))
            answered = {str(p): await tcp_open(old, p) for p in ports}
            kept = any(answered.values())
            # where the address came from and how it was checked, for the log and diagnostics
            record = {"time": dt_util.utcnow().isoformat(timespec="seconds"), "source": "dhcp",
                      "hostname": discovery_info.hostname, "mac": discovery_info.macaddress, "ip": host,
                      "serial_check": "passed (same serial)", "old_host": old,
                      "old_host_answered": answered, "action": "kept old address" if kept else "moved"}
            detail = (f"seen on DHCP as {discovery_info.hostname} / {discovery_info.macaddress} at {host}; "
                      f"serial check passed (same serial); old address {old} answered: "
                      + ", ".join(f"port {p} {'yes' if ok else 'no'}" for p, ok in answered.items()))
            if kept:
                _LOGGER.info("Thermostat also seen at %s; keeping %s, which still answers (%s)", host, old, detail)
                if known.state is ConfigEntryState.LOADED:
                    known.runtime_data.address_seen(record)
                return self.async_abort(reason="already_configured")
            _LOGGER.warning("Thermostat moved from %s to %s (%s); %s is kept to fall back to if %s stops "
                            "answering", old, host, detail, old, host)
            self._abort_if_unique_id_configured(
                updates={CONF_HOST: host, CONF_PREVIOUS_HOST: old, CONF_ADDRESS_CHANGE: record})
        self._abort_if_unique_id_configured(updates={CONF_HOST: host})
        self._input = {CONF_HOST: host, CONF_NAME: DEFAULT_NAME, CONF_PORT: DEFAULT_PORT,
                       CONF_ADB_PORT: DEFAULT_ADB_PORT}
        self._result = res
        self.context["title_placeholders"] = {"host": host}
        return await self.async_step_confirm()

    # ----------------------------------------------------------- reconfigure

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        placeholders = {"checks": ""}
        if user_input is not None:
            res = await self._preflight(user_input, errors, placeholders)
            if res is not None:
                await self.async_set_unique_id(res.serial)
                self._abort_if_unique_id_mismatch(reason="wrong_device")
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={**{k: user_input[k] for k in (CONF_HOST, CONF_PORT, CONF_ADB_PORT)},
                                  CONF_PREVIOUS_HOST: None},          # set by hand: nothing to fall back to
                )
        schema = vol.Schema(
            {
                vol.Required(CONF_HOST): str,
                vol.Optional(CONF_PORT): vol.All(vol.Coerce(int), vol.Range(1, 65535)),
                vol.Optional(CONF_ADB_PORT): vol.All(vol.Coerce(int), vol.Range(1, 65535)),
            }
        )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(schema, user_input or dict(entry.data)),
            errors=errors,
            description_placeholders=placeholders,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return AciqOptionsFlow()


class AciqOptionsFlow(OptionsFlow):
    """Auto-deploy toggle and extra allowlisted IPs."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            raw = str(user_input.get(CONF_EXTRA_ALLOWED_IPS, ""))
            ips = [ip.strip() for ip in raw.replace(";", ",").split(",") if ip.strip()]
            if all(_valid_ip(ip) for ip in ips):
                user_input[CONF_EXTRA_ALLOWED_IPS] = ", ".join(ips)
                return self.async_create_entry(data=user_input)
            errors[CONF_EXTRA_ALLOWED_IPS] = "invalid_ip"
        opts = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Optional(CONF_AUTO_DEPLOY, default=opts.get(CONF_AUTO_DEPLOY, True)): bool,
                vol.Optional(CONF_EXTRA_ALLOWED_IPS, default=opts.get(CONF_EXTRA_ALLOWED_IPS, "")): str,
                vol.Optional(CONF_BLOCK_EGRESS, default=opts.get(CONF_BLOCK_EGRESS, False)): bool,
                vol.Optional(CONF_SYNC_CLOCK, default=opts.get(CONF_SYNC_CLOCK, True)): bool,
                vol.Optional(CONF_DRY_RETURN, default=opts.get(CONF_DRY_RETURN, True)): bool,
                vol.Optional(CONF_HIDE_FAN, default=opts.get(CONF_HIDE_FAN, False)): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
