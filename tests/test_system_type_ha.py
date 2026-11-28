"""System type sensor end to end: daemon JSON -> coordinator -> entity, attributes, diagnostics."""

from __future__ import annotations

import copy

from homeassistant.helpers import entity_registry as er

from custom_components.aciq_thermostat.diagnostics import async_get_config_entry_diagnostics

from .test_24v import _24v, _setup

SENSOR = "sensor.thermostat_system_type"


def _comm(state: dict, aux: int) -> dict:
    s = copy.deepcopy(state)
    s.update(comm_mode="communicating")
    s["ui"]["aux_heat_open"] = aux
    return s


async def test_dual_fuel(hass, aioclient_mock, mock_deployer, state) -> None:
    e = await _setup(hass, aioclient_mock, _comm(state, 1))
    st = hass.states.get(SENSOR)
    assert st.state == "heat_pump_furnace"
    assert st.attributes["outdoor_unit"] == "heat_pump" and st.attributes["heat_source"] == "furnace"
    assert st.attributes["indoor_unit"] == "furnace" and st.attributes["detected_from"] == "aux_heat_open=1"
    reg = er.async_get(hass).async_get(SENSOR)
    assert reg.entity_category == "diagnostic" and reg.disabled_by is None and reg.translation_key == "system_type"
    diag = await async_get_config_entry_diagnostics(hass, e)
    assert diag["system"]["system"] == "heat_pump_furnace"


async def test_heat_pump_only_and_a_setting_change(hass, aioclient_mock, mock_deployer, state) -> None:
    e = await _setup(hass, aioclient_mock, _comm(state, 0))
    assert hass.states.get(SENSOR).state == "heat_pump"
    assert hass.states.get(SENSOR).attributes["indoor_unit"] == "air_handler"
    # the installer turns backup heat on: the next update follows
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"http://{e.data['host']}:8080/api/state",
                       json=_comm(state, 1))
    await e.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(SENSOR).state == "heat_pump_furnace"


async def test_not_reported_yet(hass, aioclient_mock, mock_deployer, state) -> None:
    s = _comm(state, 0)
    s["ui"].pop("aux_heat_open")
    await _setup(hass, aioclient_mock, s)
    assert hass.states.get(SENSOR).state == "unknown"


async def test_24v_issue5(hass, aioclient_mock, mock_deployer, state) -> None:
    e = await _setup(hass, aioclient_mock, _24v(state))
    st = hass.states.get(SENSOR)
    assert st.state == "heat_pump_boiler"
    assert (st.attributes["wiring"], st.attributes["compressor_stages"], st.attributes["heat_stages"],
            st.attributes["blower"]) == ("24v", 2, 1, True)
    assert st.attributes["detected_from"] == "wired C, G, O/B, RC, W1, Y1, Y2; Taux=3"
    diag = await async_get_config_entry_diagnostics(hass, e)
    assert diag["system"]["heat_source"] == "boiler"
