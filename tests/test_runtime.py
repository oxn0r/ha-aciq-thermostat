"""Setup/unload/remove, coordinator error paths, buttons, diagnostics, climate actions."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.climate.const import HVACAction
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir

from custom_components.aciq_thermostat.const import CONF_ADB_PORT, CONF_AUTO_DEPLOY, DOMAIN
from custom_components.aciq_thermostat.deployer import DeployError, PreflightResult
from custom_components.aciq_thermostat.diagnostics import async_get_config_entry_diagnostics

from .conftest import HOST, SERIAL

BASE = f"http://{HOST}:8080"
NODELAY = patch("custom_components.aciq_thermostat.coordinator.REFRESH_DELAY", 0)


def _entry(hass: HomeAssistant, auto: bool = True) -> MockConfigEntry:
    e = MockConfigEntry(domain=DOMAIN, unique_id=SERIAL, title="Thermostat",
                        data={CONF_HOST: HOST, CONF_PORT: 8080, CONF_ADB_PORT: 5555},
                        options={CONF_AUTO_DEPLOY: auto})
    e.add_to_hass(hass)
    return e


async def _setup(hass: HomeAssistant, aioclient_mock: Any, state: dict, auto: bool = True) -> MockConfigEntry:
    aioclient_mock.get(f"{BASE}/api/state", json=state)
    aioclient_mock.post(f"{BASE}/api/control", json={"success": True})
    e = _entry(hass, auto)
    await hass.config_entries.async_setup(e.entry_id)
    await hass.async_block_till_done()
    return e


async def test_setup_deploy_failure_not_ready(hass, aioclient_mock, mock_deployer, state) -> None:
    mock_deployer[1].side_effect = DeployError("adb down")
    e = await _setup(hass, aioclient_mock, state)
    assert e.state is ConfigEntryState.SETUP_RETRY
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"deploy_failed_{e.entry_id}")


async def test_setup_without_auto_deploy(hass, aioclient_mock, mock_deployer, state) -> None:
    e = await _setup(hass, aioclient_mock, state, auto=False)
    assert e.state is ConfigEntryState.LOADED
    mock_deployer[1].assert_not_awaited()


async def test_versions_read_failure_tolerated(hass, aioclient_mock, mock_deployer, state) -> None:
    with patch("custom_components.aciq_thermostat.deployer.Deployer.read_versions",
               AsyncMock(side_effect=DeployError("x"))):
        e = await _setup(hass, aioclient_mock, state)
    assert e.state is ConfigEntryState.LOADED


async def test_unload_and_remove(hass, aioclient_mock, mock_deployer, state) -> None:
    e = await _setup(hass, aioclient_mock, state)
    with patch("custom_components.aciq_thermostat.deployer.Deployer.stop_daemon", AsyncMock()) as stop:
        await hass.config_entries.async_remove(e.entry_id)
    stop.assert_awaited_once()


async def test_remove_tolerates_adb_error(hass, aioclient_mock, mock_deployer, state) -> None:
    e = await _setup(hass, aioclient_mock, state)
    with patch("custom_components.aciq_thermostat.deployer.Deployer.stop_daemon",
               AsyncMock(side_effect=DeployError("gone"))):
        await hass.config_entries.async_remove(e.entry_id)


async def test_options_change_reloads(hass, aioclient_mock, mock_deployer, state) -> None:
    e = await _setup(hass, aioclient_mock, state)
    calls = mock_deployer[1].await_count
    hass.config_entries.async_update_entry(e, options={CONF_AUTO_DEPLOY: True, "extra_allowed_ips": "10.0.0.4"})
    await hass.async_block_till_done()
    assert mock_deployer[1].await_count == calls + 1
    assert mock_deployer[1].call_args.args[0] == ["10.0.0.4"]


async def test_state_http_error_and_no_state(hass, aioclient_mock, mock_deployer, state, tcp_probe) -> None:
    e = await _setup(hass, aioclient_mock, state, auto=False)
    c = e.runtime_data
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{BASE}/api/state", status=500)
    await c.async_refresh()
    assert not c.last_update_success
    aioclient_mock.clear_requests()
    state["dev"]["seen"] = False
    aioclient_mock.get(f"{BASE}/api/state", json=state)
    await c.async_refresh()
    assert not c.last_update_success


async def test_redeploy_failure(hass, aioclient_mock, mock_deployer, state, tcp_probe) -> None:
    tcp_probe.result = {8080: "refused", 5555: "open"}
    e = await _setup(hass, aioclient_mock, state)
    c = e.runtime_data
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{BASE}/api/state", exc=TimeoutError())
    mock_deployer[1].side_effect = DeployError("adb down")
    c.last_deploy_attempt = None
    with patch("custom_components.aciq_thermostat.coordinator.asyncio.sleep", AsyncMock()):
        await c.async_refresh()
    assert not c.last_update_success
    assert c.last_deploy_result == "failed: adb down"


async def test_control_errors(hass, aioclient_mock, mock_deployer, state) -> None:
    e = await _setup(hass, aioclient_mock, state)
    c = e.runtime_data
    aioclient_mock.clear_requests()
    aioclient_mock.post(f"{BASE}/api/control", status=400, text='{"error": "nope"}')
    with pytest.raises(HomeAssistantError):
        await c.async_control({"temperature": 70})
    aioclient_mock.clear_requests()
    aioclient_mock.post(f"{BASE}/api/control", exc=TimeoutError())
    with pytest.raises(HomeAssistantError):
        await c.async_control({"temperature": 70})


async def test_buttons(hass, aioclient_mock, mock_deployer, state) -> None:
    await _setup(hass, aioclient_mock, state)
    pre, ens = mock_deployer
    await hass.services.async_call("button", "press", {"entity_id": "button.thermostat_redeploy_daemon"}, blocking=True)
    assert ens.call_args.kwargs["force"] is True and ens.call_args.kwargs["block_egress"] is False
    ens.side_effect = DeployError("x")
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call("button", "press", {"entity_id": "button.thermostat_redeploy_daemon"},
                                       blocking=True)
    assert hass.states.get("button.thermostat_run_pre_flight_checks") is None      # merged into the report


async def test_debug_report_button(hass, aioclient_mock, mock_deployer, state) -> None:
    e = await _setup(hass, aioclient_mock, state)
    pre, _ = mock_deployer
    with patch("custom_components.aciq_thermostat.button.persistent_notification.async_create") as note:
        await hass.services.async_call("button", "press", {"entity_id": "button.thermostat_collect_debug_report"},
                                       blocking=True)
    assert "all pre-flight checks passed" in note.call_args.args[1] and "bug_report.yml" in note.call_args.args[1]
    last = hass.states.get("sensor.thermostat_last_debug_report")
    assert last.state not in ("unknown", "unavailable") and last.attributes["preflight_passed"] is True
    assert last.attributes["failed_checks"] == [] and last.attributes["state_layout_found"] is False
    d = await async_get_config_entry_diagnostics(hass, e)
    assert d["debug_report"]["preflight"]["passed"] and d["debug_report"]["dev_app"]["type"] == "EXEC"
    from custom_components.aciq_thermostat.deployer import Check
    pre.return_value = PreflightResult(checks=[Check("firmware", False, "other build")])
    await hass.services.async_call("button", "press", {"entity_id": "button.thermostat_collect_debug_report"},
                                   blocking=True)
    assert e.runtime_data.debug_report["preflight"]["passed"] is False
    assert hass.states.get("sensor.thermostat_last_debug_report").attributes["failed_checks"] == ["firmware"]
    pre.side_effect = DeployError("x")
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call("button", "press", {"entity_id": "button.thermostat_collect_debug_report"},
                                       blocking=True)


async def test_diagnostics(hass, aioclient_mock, mock_deployer, state) -> None:
    e = await _setup(hass, aioclient_mock, state)
    d = await async_get_config_entry_diagnostics(hass, e)
    assert d["state"]["mode"] == "auto" and d["preflight"]["passed"]
    assert d["host"] == d["allowed_ips"] == d["preflight"]["serial"] == "**REDACTED**"
    mock_deployer[0].side_effect = DeployError("x")
    assert (await async_get_config_entry_diagnostics(hass, e))["preflight"] == {"error": "x"}


@pytest.mark.parametrize(
    ("mode", "eq", "cur", "expected"),
    [
        ("off", {}, 70, HVACAction.OFF),
        ("fan_only", {}, 70, HVACAction.FAN),
        ("cool", {"compressor_hz": 40}, 75, HVACAction.COOLING),
        ("emergency_heat", {"furnace_on": True}, 70, HVACAction.HEATING),
        ("heat", {"compressor_hz": 30}, 70, HVACAction.HEATING),
        ("auto", {"blower_on": False}, 72, HVACAction.IDLE),
    ],
)
async def test_hvac_action(hass, aioclient_mock, mock_deployer, state, mode, eq, cur, expected) -> None:
    state["mode"] = mode
    state["current_temp"] = cur
    state["equipment"].update({"compressor_hz": 0, "furnace_on": False, "blower_on": False, **eq})
    await _setup(hass, aioclient_mock, state)
    st = hass.states.get("climate.thermostat")
    assert st.attributes["hvac_action"] == expected
    if mode == "emergency_heat":
        assert st.attributes["preset_mode"] == "emergency_heat"


async def test_climate_services(hass, aioclient_mock, mock_deployer, state) -> None:
    state["mode"] = "emergency_heat"
    await _setup(hass, aioclient_mock, state)
    with NODELAY:
        for svc, data in [
            ("set_preset_mode", {"preset_mode": "none"}),
            ("set_temperature", {"temperature": 71, "hvac_mode": "heat"}),
            ("turn_on", {}),
        ]:
            await hass.services.async_call("climate", svc, {"entity_id": "climate.thermostat", **data},
                                           blocking=True)
    posts = [call[2] for call in aioclient_mock.mock_calls if call[0] == "POST"]
    assert posts == [{"mode": "heat"}, {"mode": "heat", "temperature": 71, "unit": "F"}, {"mode": "auto"}]


async def test_fan_mode_in_heat(hass, aioclient_mock, mock_deployer, state) -> None:
    state["mode"] = "heat"
    await _setup(hass, aioclient_mock, state)
    with NODELAY:
        await hass.services.async_call("climate", "set_fan_mode",
                                       {"entity_id": "climate.thermostat", "fan_mode": "low"}, blocking=True)
    posts = [call[2] for call in aioclient_mock.mock_calls if call[0] == "POST"]
    assert posts == [{"fan": "low"}]
