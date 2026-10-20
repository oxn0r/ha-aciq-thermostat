"""Config flow: enter the thermostat IP, run read-only pre-flight checks, confirm."""

from __future__ import annotations

import ipaddress
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.core import callback

from .const import (
    CONF_ADB_PORT,
    CONF_AUTO_DEPLOY,
    CONF_EXTRA_ALLOWED_IPS,
    DEFAULT_ADB_PORT,
    DEFAULT_PORT,
    DOMAIN,
)
from .deployer import Deployer, DeployError, PreflightResult

_LOGGER = logging.getLogger(__name__)

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Optional(CONF_NAME, default="Thermostat"): str,
        vol.Optional(CONF_PORT, default=DEFAULT_PORT): vol.Coerce(int),
        vol.Optional(CONF_ADB_PORT, default=DEFAULT_ADB_PORT): vol.Coerce(int),
    }
)


class AciqConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._input: dict[str, Any] = {}
        self._result: PreflightResult | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        placeholders = {"checks": ""}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            try:
                ipaddress.ip_address(host)
            except ValueError:
                errors[CONF_HOST] = "invalid_host"
            else:
                user_input[CONF_HOST] = host
                try:
                    res = await Deployer(host, user_input[CONF_ADB_PORT]).preflight()
                except DeployError as err:
                    _LOGGER.warning("Pre-flight failed: %s", err)
                    errors["base"] = "cannot_connect"
                    placeholders["checks"] = str(err)
                else:
                    self._input, self._result = user_input, res
                    if res.passed:
                        await self.async_set_unique_id(res.serial or host)
                        self._abort_if_unique_id_configured(updates={CONF_HOST: host})
                        return await self.async_step_confirm()
                    errors["base"] = "checks_failed"
                    placeholders["checks"] = res.summary()
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
            return self.async_create_entry(title=self._input[CONF_NAME], data=data,
                                           options={CONF_AUTO_DEPLOY: True})
        return self.async_show_form(
            step_id="confirm",
            description_placeholders={"checks": self._result.summary(),
                                      "host": self._input[CONF_HOST]},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return AciqOptionsFlow()


class AciqOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            ips = [ip.strip() for ip in user_input.get(CONF_EXTRA_ALLOWED_IPS, "").split(",") if ip.strip()]
            try:
                for ip in ips:
                    ipaddress.ip_address(ip)
            except ValueError:
                errors[CONF_EXTRA_ALLOWED_IPS] = "invalid_ip"
            else:
                user_input[CONF_EXTRA_ALLOWED_IPS] = ", ".join(ips)
                return self.async_create_entry(data=user_input)
        opts = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Optional(CONF_AUTO_DEPLOY, default=opts.get(CONF_AUTO_DEPLOY, True)): bool,
                vol.Optional(CONF_EXTRA_ALLOWED_IPS,
                             default=opts.get(CONF_EXTRA_ALLOWED_IPS, "")): str,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
