"""Config flow paths: user errors, DHCP discovery, reconfigure, options."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo

from custom_components.aciq_thermostat.const import (
    CONF_ADB_PORT,
    CONF_AUTO_DEPLOY,
    CONF_EXTRA_ALLOWED_IPS,
    DOMAIN,
)
from custom_components.aciq_thermostat.deployer import Check, DeployError, PreflightResult

from .conftest import HOST, SERIAL

USER = {CONF_HOST: HOST, CONF_NAME: "Family Room", CONF_PORT: 8080, CONF_ADB_PORT: 5555}
DHCP = DhcpServiceInfo(ip="10.0.2.200", hostname="midea_44_1619", macaddress="54926ae55f14")
NO_SETUP = patch("custom_components.aciq_thermostat.async_setup_entry", AsyncMock(return_value=True))


def _entry(hass: HomeAssistant, host: str = HOST) -> MockConfigEntry:
    e = MockConfigEntry(domain=DOMAIN, unique_id=SERIAL, title="T",
                        data={CONF_HOST: host, CONF_PORT: 8080, CONF_ADB_PORT: 5555},
                        options={CONF_AUTO_DEPLOY: True})
    e.add_to_hass(hass)
    return e


async def _user(hass: HomeAssistant, data: dict) -> dict:
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    return await hass.config_entries.flow.async_configure(r["flow_id"], data)


async def test_user_invalid_host(hass: HomeAssistant, mock_deployer) -> None:
    r = await _user(hass, {**USER, CONF_HOST: "not-an-ip"})
    assert r["errors"] == {CONF_HOST: "invalid_host"}


async def test_user_no_serial(hass: HomeAssistant, mock_deployer) -> None:
    pre, _ = mock_deployer
    pre.return_value = PreflightResult(checks=[Check("root_shell", True, "")], serial=None)
    assert (await _user(hass, USER))["errors"] == {"base": "no_serial"}


async def test_user_already_configured_updates_host(hass: HomeAssistant, mock_deployer) -> None:
    e = _entry(hass, host="10.0.2.1")
    r = await _user(hass, USER)
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "already_configured"
    assert e.data[CONF_HOST] == HOST


async def test_user_title_is_name(hass: HomeAssistant, mock_deployer) -> None:
    r = await _user(hass, USER)
    with NO_SETUP:
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {})
    assert r["title"] == "Family Room"
    assert r["options"] == {CONF_AUTO_DEPLOY: True, CONF_EXTRA_ALLOWED_IPS: ""}


# ----------------------------------------------------------------------- DHCP

async def _dhcp(hass: HomeAssistant) -> dict:
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_DHCP}, data=DHCP)


async def test_dhcp_new_device(hass: HomeAssistant, mock_deployer) -> None:
    r = await _dhcp(hass)
    assert r["type"] is FlowResultType.FORM and r["step_id"] == "confirm"
    with NO_SETUP:
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {})
    assert r["type"] is FlowResultType.CREATE_ENTRY
    assert r["data"][CONF_HOST] == "10.0.2.200" and r["result"].unique_id == SERIAL


async def test_dhcp_same_ip_no_adb(hass: HomeAssistant, mock_deployer) -> None:
    pre, _ = mock_deployer
    _entry(hass, host="10.0.2.200")
    r = await _dhcp(hass)
    assert r["reason"] == "already_configured"
    pre.assert_not_awaited()


async def test_dhcp_known_device_new_ip(hass: HomeAssistant, mock_deployer) -> None:
    e = _entry(hass, host=HOST)
    r = await _dhcp(hass)
    assert r["reason"] == "already_configured"
    assert e.data[CONF_HOST] == "10.0.2.200"


async def test_dhcp_cannot_connect(hass: HomeAssistant, mock_deployer) -> None:
    mock_deployer[0].side_effect = DeployError("x")
    assert (await _dhcp(hass))["reason"] == "cannot_connect"


async def test_dhcp_not_supported(hass: HomeAssistant, mock_deployer) -> None:
    mock_deployer[0].return_value = PreflightResult(checks=[Check("firmware", False, "")], serial=SERIAL)
    assert (await _dhcp(hass))["reason"] == "not_supported"


# ---------------------------------------------------------------- reconfigure

async def test_reconfigure_success(hass: HomeAssistant, mock_deployer) -> None:
    e = _entry(hass)
    r = await e.start_reconfigure_flow(hass)
    assert r["step_id"] == "reconfigure"
    with NO_SETUP:
        r = await hass.config_entries.flow.async_configure(
            r["flow_id"], {CONF_HOST: "10.0.2.9", CONF_PORT: 8081, CONF_ADB_PORT: 5555})
    assert r["reason"] == "reconfigure_successful"
    assert e.data[CONF_HOST] == "10.0.2.9" and e.data[CONF_PORT] == 8081


async def test_reconfigure_wrong_device(hass: HomeAssistant, mock_deployer) -> None:
    e = _entry(hass)
    mock_deployer[0].return_value = PreflightResult(checks=[Check("root_shell", True, "")], serial="OTHER")
    r = await e.start_reconfigure_flow(hass)
    r = await hass.config_entries.flow.async_configure(
        r["flow_id"], {CONF_HOST: "10.0.2.9", CONF_PORT: 8080, CONF_ADB_PORT: 5555})
    assert r["reason"] == "wrong_device"
    assert e.data[CONF_HOST] == HOST


async def test_reconfigure_error_shows_form(hass: HomeAssistant, mock_deployer) -> None:
    e = _entry(hass)
    mock_deployer[0].side_effect = DeployError("down")
    r = await e.start_reconfigure_flow(hass)
    r = await hass.config_entries.flow.async_configure(
        r["flow_id"], {CONF_HOST: "10.0.2.9", CONF_PORT: 8080, CONF_ADB_PORT: 5555})
    assert r["errors"] == {"base": "cannot_connect"}


# -------------------------------------------------------------------- options

async def test_options(hass: HomeAssistant, mock_deployer) -> None:
    e = _entry(hass)
    with NO_SETUP:
        r = await hass.config_entries.options.async_init(e.entry_id)
        r = await hass.config_entries.options.async_configure(
            r["flow_id"], {CONF_AUTO_DEPLOY: False, CONF_EXTRA_ALLOWED_IPS: "10.0.0.4; bad"})
        assert r["errors"] == {CONF_EXTRA_ALLOWED_IPS: "invalid_ip"}
        r = await hass.config_entries.options.async_configure(
            r["flow_id"], {CONF_AUTO_DEPLOY: False, CONF_EXTRA_ALLOWED_IPS: " 10.0.0.4 ;10.0.0.40,"})
    assert r["type"] is FlowResultType.CREATE_ENTRY
    assert e.options == {CONF_AUTO_DEPLOY: False, CONF_EXTRA_ALLOWED_IPS: "10.0.0.4, 10.0.0.40",
                         "block_egress": False, "sync_clock": True, "v24_setpoint_control": False}
