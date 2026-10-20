"""Config flow, entity state mapping and control payloads (mocked thermostat)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant import config_entries
from homeassistant.components.climate import HVACAction, HVACMode
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir

from custom_components.aciq_thermostat.const import CONF_ADB_PORT, CONF_AUTO_DEPLOY, DOMAIN
from custom_components.aciq_thermostat.deployer import Check, DeployError, PreflightResult

from .conftest import HOST, SERIAL

BASE = f"http://{HOST}:8080"


async def _setup(hass: HomeAssistant, aioclient_mock, state: dict) -> MockConfigEntry:
    aioclient_mock.get(f"{BASE}/api/state", json=state)
    aioclient_mock.post(f"{BASE}/api/control", json={"success": True})
    entry = MockConfigEntry(
        domain=DOMAIN, title="Thermostat", unique_id=SERIAL,
        data={CONF_HOST: HOST, CONF_PORT: 8080, CONF_ADB_PORT: 5555},
        options={CONF_AUTO_DEPLOY: True},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


# ------------------------------------------------------------------ config flow

async def test_config_flow_happy_path(hass: HomeAssistant, mock_deployer) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: HOST, CONF_NAME: "Thermostat", CONF_PORT: 8080, CONF_ADB_PORT: 5555})
    assert result["step_id"] == "confirm"
    with patch("custom_components.aciq_thermostat.async_setup_entry", AsyncMock(return_value=True)):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == SERIAL
    assert result["data"] == {CONF_HOST: HOST, CONF_PORT: 8080, CONF_ADB_PORT: 5555}


async def test_config_flow_failed_checks(hass: HomeAssistant, mock_deployer) -> None:
    pre, _ = mock_deployer
    pre.return_value = PreflightResult(checks=[Check("firmware", False, "other build")], serial=SERIAL)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: HOST, CONF_NAME: "T", CONF_PORT: 8080, CONF_ADB_PORT: 5555})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "checks_failed"}


async def test_config_flow_cannot_connect(hass: HomeAssistant, mock_deployer) -> None:
    pre, _ = mock_deployer
    pre.side_effect = DeployError("refused")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: HOST, CONF_NAME: "T", CONF_PORT: 8080, CONF_ADB_PORT: 5555})
    assert result["errors"] == {"base": "cannot_connect"}


# --------------------------------------------------------------------- entities

async def test_entities_from_live_fixture(hass: HomeAssistant, aioclient_mock, mock_deployer, state) -> None:
    _, ensure = mock_deployer
    await _setup(hass, aioclient_mock, state)
    ensure.assert_awaited()  # daemon ensured at setup
    assert ensure.call_args.args[0] == []  # HA IP is taken from the ADB socket

    c = hass.states.get("climate.thermostat")
    assert c.state == HVACMode.AUTO
    assert c.attributes["temperature"] == 72
    assert c.attributes["current_temperature"] == state["current_temp"]
    assert c.attributes.get("fan_mode") is None  # fan locked in auto
    assert hass.states.get("sensor.thermostat_outdoor_temperature").state == str(state["equipment"]["outdoor_temp"])
    assert hass.states.get("binary_sensor.thermostat_furnace").state == "off"
    assert hass.states.get("sensor.thermostat_mode").state == "auto"
    assert hass.states.get("binary_sensor.thermostat_fault").state == "off"


async def test_furnace_and_compressor_actions(hass: HomeAssistant, aioclient_mock, mock_deployer, state) -> None:
    state["mode"] = "heat"
    state["equipment"].update(furnace_on=True, blower_on=True, blower_speed=4, compressor_hz=0)
    await _setup(hass, aioclient_mock, state)
    c = hass.states.get("climate.thermostat")
    assert c.attributes["hvac_action"] == HVACAction.HEATING
    assert hass.states.get("binary_sensor.thermostat_furnace").state == "on"
    assert hass.states.get("binary_sensor.thermostat_compressor").state == "off"
    assert "fan_mode" in c.attributes  # fan adjustable in heat


async def test_equipment_unavailable(hass: HomeAssistant, aioclient_mock, mock_deployer, state) -> None:
    state["equipment"] = {"available": False}
    await _setup(hass, aioclient_mock, state)
    assert hass.states.get("sensor.thermostat_outdoor_temperature").state == "unavailable"
    assert hass.states.get("climate.thermostat").state == HVACMode.AUTO


async def test_control_payloads(hass: HomeAssistant, aioclient_mock, mock_deployer, state) -> None:
    await _setup(hass, aioclient_mock, state)
    with patch("custom_components.aciq_thermostat.coordinator.REFRESH_DELAY", 0):
        await hass.services.async_call("climate", "set_temperature",
                                       {"entity_id": "climate.thermostat", "temperature": 70}, blocking=True)
        await hass.services.async_call("climate", "set_hvac_mode",
                                       {"entity_id": "climate.thermostat", "hvac_mode": "heat"}, blocking=True)
        await hass.services.async_call("climate", "set_preset_mode",
                                       {"entity_id": "climate.thermostat", "preset_mode": "emergency_heat"},
                                       blocking=True)
        await hass.services.async_call("climate", "turn_off", {"entity_id": "climate.thermostat"}, blocking=True)
    posts = [call[2] for call in aioclient_mock.mock_calls if call[0] == "POST"]
    assert posts == [{"temperature": 70}, {"mode": "heat"}, {"mode": "emergency_heat"}, {"mode": "off"}]


async def test_fan_blocked_in_auto(hass: HomeAssistant, aioclient_mock, mock_deployer, state) -> None:
    await _setup(hass, aioclient_mock, state)
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call("climate", "set_fan_mode",
                                       {"entity_id": "climate.thermostat", "fan_mode": "low"}, blocking=True)


async def test_firmware_issue(hass: HomeAssistant, aioclient_mock, mock_deployer, state) -> None:
    state["control_enabled"] = False
    state["firmware"] = "tina.other"
    entry = await _setup(hass, aioclient_mock, state)
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"firmware_unverified_{entry.entry_id}")
    assert issue is not None


async def test_redeploy_when_daemon_gone(hass: HomeAssistant, aioclient_mock, mock_deployer, state, tcp_probe) -> None:
    _, ensure = mock_deployer
    entry = await _setup(hass, aioclient_mock, state)
    coord = entry.runtime_data
    calls_before = ensure.await_count
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{BASE}/api/state", exc=TimeoutError())
    tcp_probe.result = {8080: "timeout", 5555: "timeout"}          # network down: never redeploy
    coord.last_deploy_attempt = None
    await coord.async_refresh()
    assert ensure.await_count == calls_before
    tcp_probe.result = {8080: "refused", 5555: "open"}             # daemon gone: redeploy at once
    with patch("custom_components.aciq_thermostat.coordinator.asyncio.sleep", AsyncMock()):
        await coord.async_refresh()
    assert ensure.await_count == calls_before + 1
    assert ensure.call_args.kwargs["force"] is False


async def test_metric_ha_converts(hass: HomeAssistant, aioclient_mock, mock_deployer, state) -> None:
    from homeassistant.util.unit_system import METRIC_SYSTEM
    hass.config.units = METRIC_SYSTEM
    await _setup(hass, aioclient_mock, state)
    assert hass.states.get("climate.thermostat").attributes["temperature"] == 22.2
    with patch("custom_components.aciq_thermostat.coordinator.REFRESH_DELAY", 0):
        await hass.services.async_call("climate", "set_temperature",
                                       {"entity_id": "climate.thermostat", "temperature": 21}, blocking=True)
    posts = [call[2] for call in aioclient_mock.mock_calls if call[0] == "POST"]
    assert posts == [{"temperature": 70}]  # 21 C -> 69.8 F -> 70


async def test_device_versions(hass: HomeAssistant, aioclient_mock, mock_deployer, state) -> None:
    from homeassistant.helpers import device_registry as dr
    await _setup(hass, aioclient_mock, state)
    dev = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, SERIAL)})
    assert dev.sw_version == "LD:1.2.60" and dev.hw_version == "V4.3" and dev.serial_number == SERIAL
    assert hass.states.get("sensor.thermostat_indoor_unit_firmware").state == "00AC000111171171000066111008"


async def test_aux_heat_allowed_is_enabled_diagnostic(hass: HomeAssistant, aioclient_mock, mock_deployer, state) -> None:
    from homeassistant.const import EntityCategory
    from homeassistant.helpers import entity_registry as er
    await _setup(hass, aioclient_mock, state)
    ent = er.async_get(hass).async_get("binary_sensor.thermostat_aux_heat_allowed")
    assert ent.entity_category is EntityCategory.DIAGNOSTIC
    assert ent.disabled_by is None
    assert hass.states.get("binary_sensor.thermostat_aux_heat_allowed").state == "off"


async def test_furnace_sensor_states_and_no_heat_class(hass: HomeAssistant, aioclient_mock, mock_deployer, state) -> None:
    state["equipment"]["furnace_on"] = True
    await _setup(hass, aioclient_mock, state)
    st = hass.states.get("binary_sensor.thermostat_furnace")
    assert st.state == "on" and "device_class" not in st.attributes  # UI label comes from translations: Burning
