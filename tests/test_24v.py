"""Issue #5: thermostat wired for conventional 24 V (dev_24v_decorder, no AHU3 lines): read-only."""
from __future__ import annotations

import copy
import logging

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.climate import HVACAction, HVACMode
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir

from custom_components.aciq_thermostat import coordinator
from custom_components.aciq_thermostat.const import CONF_ADB_PORT, CONF_AUTO_DEPLOY, DOMAIN
from custom_components.aciq_thermostat.diagnostics import async_get_config_entry_diagnostics
from custom_components.aciq_thermostat.v24 import decode, normalize
from .conftest import HOST, SERIAL

BASE = f"http://{HOST}:8080"
CLIMATE = "climate.thermostat"

# What the daemon reports for the issue #5 thermostat (values from its 2026-10-27 capture)
V24 = {"seen": True, "unit": "F", "room": 72.3, "setpoint": 72.0, "low": 67.0, "high": 79.0,
       "current_mode": 4, "op_mode": "77", "heat_mode": "6", "wind_fun": "1", "terminal_set_mode": "1",
       "terminal_state": "76", "terminal_state_a3": "1f6", "terminal_config": "200", "emer_heat": "0",
       "aux": "1", "ob_dir": "2", "afterheat": "0",
       "terminal_io": "PEK:0 O/B:1 G:1 W2:0 W1:1 Y2:1 Y1:1 C:1 RC:1 RH:0",
       "power": 1, "kv_mode": 6, "kv_emer_heat": 0, "taux": 3}

# (wall step, UI mode, terminal_config, auto_state, setpoint, expected HA mode, preset, action)
STEPS = [
    ("heat idle", 1, "200", 0, 72, HVACMode.HEAT, "none", HVACAction.IDLE),
    ("heat fan", 1, "204", 0, 72, HVACMode.HEAT, "none", HVACAction.FAN),
    ("heat call", 1, "266", 0, 75, HVACMode.HEAT, "none", HVACAction.HEATING),
    ("cool call", 2, "264", 0, 66, HVACMode.COOL, "none", HVACAction.COOLING),
    ("aux call", 3, "214", 0, 74, HVACMode.HEAT, "emergency_heat", HVACAction.HEATING),
    ("auto heat call", 0, "246", 2, 72, HVACMode.AUTO, "none", HVACAction.HEATING),
    ("auto aux call", 0, "216", 2, 72, HVACMode.AUTO, "none", HVACAction.HEATING),
    ("auto no demand", 0, "206", 0, 72, HVACMode.AUTO, "none", HVACAction.FAN),
    ("auto cool call", 0, "264", 1, 72, HVACMode.AUTO, "none", HVACAction.COOLING),
    ("off", 4, "0", 0, 72, HVACMode.OFF, "none", HVACAction.OFF),
]


def _24v(state: dict, ui_mode: int = 1, config: str = "200", auto_state: int = 0, sp: float = 72,
         low: float = 67, high: float = 79, seen: bool = True) -> dict:
    s = copy.deepcopy(state)
    v = dict(V24, terminal_config=config, setpoint=float(sp), low=float(low), high=float(high),
             power=0 if ui_mode == 4 else 1)
    s.update(mode="unknown", comm_mode="24v", v24=v if seen else {"seen": False}, ui_mode_raw=ui_mode,
             setpoint=0, equipment={"available": False})
    s["dev"]["seen"] = False
    s["ui"].update(deadband=4, temp_zone_mode=1, auto_state=auto_state, fan_state=1,
                   heating_max=95, cooling_min=39)
    return s


@pytest.fixture(autouse=True)
def fresh_log_memory():
    coordinator._V24_LOGGED.clear()


async def _setup(hass, aioclient_mock, st) -> MockConfigEntry:
    aioclient_mock.get(f"{BASE}/api/state", json=st)
    aioclient_mock.post(f"{BASE}/api/control", json={"success": True})
    e = MockConfigEntry(domain=DOMAIN, unique_id=SERIAL, title="Thermostat",
                        data={"host": HOST, "port": 8080, CONF_ADB_PORT: 5555}, options={CONF_AUTO_DEPLOY: True})
    e.add_to_hass(hass)
    await hass.config_entries.async_setup(e.entry_id)
    await hass.async_block_till_done()
    return e


def test_terminal_bitmasks() -> None:
    assert decode("1f6") == ["O/B", "G", "W1", "Y2", "Y1", "C", "RC"]          # = the terminal io line
    assert decode("76") == ["O/B", "G", "W1", "Y2", "Y1"]
    assert decode("266", config=True) == ["O/B", "G", "Y2", "Y1"]             # power bit left out
    assert decode("0", config=True) == [] and decode("", config=True) is None


@pytest.mark.parametrize(("step", "ui_mode", "config", "auto_state", "sp", "mode", "preset", "action"), STEPS)
async def test_24v_read_only_entities(hass, aioclient_mock, mock_deployer, state,
                                      step, ui_mode, config, auto_state, sp, mode, preset, action) -> None:
    e = await _setup(hass, aioclient_mock, _24v(state, ui_mode, config, auto_state, sp))
    assert e.state is ConfigEntryState.LOADED, step
    st = hass.states.get(CLIMATE)
    assert st.state == mode and st.attributes["hvac_action"] == action, step
    assert st.attributes["preset_mode"] == preset and "fan_mode" not in st.attributes
    if mode == HVACMode.AUTO:                                                # Auto holds a range here
        assert (st.attributes["target_temp_low"], st.attributes["target_temp_high"]) == (67, 79)
    elif mode != HVACMode.OFF:
        assert st.attributes["temperature"] == sp
    assert st.attributes["current_temperature"] == 72.3
    calls = {"Y": "binary_sensor.thermostat_compressor_call", "W": "binary_sensor.thermostat_aux_heat_call",
             "G": "binary_sensor.thermostat_fan_call"}
    active = set(normalize(_24v(state, ui_mode, config))["v24_outputs"]["active"])
    for letter, ent in calls.items():
        assert hass.states.get(ent).state == ("on" if any(t.startswith(letter) for t in active) else "off"), step
    out = hass.states.get("sensor.thermostat_24_v_outputs")
    assert out.state == (", ".join(sorted(active, key=lambda t: "PEK O/B G W2 W1 Y2 Y1".split().index(t)))
                         or "none")
    assert out.attributes["raw_terminal_config"] == config


async def test_24v_entities_and_issue(hass, aioclient_mock, mock_deployer, state) -> None:
    e = await _setup(hass, aioclient_mock, _24v(state))
    wiring = hass.states.get("sensor.thermostat_24_v_wiring")
    assert wiring.state == "O/B, G, W1, Y2, Y1, C, RC"
    assert wiring.attributes["heating_max"] == 95 and wiring.attributes["ob_dir"] == "2"
    # communicating-only entities (dev_app memory) aren't created on a 24 V unit
    for gone in ("binary_sensor.thermostat_furnace", "binary_sensor.thermostat_compressor",
                 "sensor.thermostat_outdoor_temperature", "binary_sensor.thermostat_aux_heat_allowed"):
        assert hass.states.get(gone) is None, gone
    assert hass.states.get("binary_sensor.thermostat_local_control_enabled").state == "off"
    issues = ir.async_get(hass)
    ro = issues.async_get_issue(DOMAIN, f"read_only_24v_{e.entry_id}")
    assert ro is not None and ro.severity == ir.IssueSeverity.WARNING
    assert issues.async_get_issue(DOMAIN, f"conventional_24v_{e.entry_id}") is None


async def test_communicating_unit_has_no_24v_entities(hass, aioclient_mock, mock_deployer, state) -> None:
    s = copy.deepcopy(state)
    s.update(comm_mode="communicating", v24={"seen": False})
    e = await _setup(hass, aioclient_mock, s)
    assert e.state is ConfigEntryState.LOADED
    assert hass.states.get("sensor.thermostat_24_v_outputs") is None
    assert hass.states.get("binary_sensor.thermostat_fan_call") is None
    assert hass.states.get("binary_sensor.thermostat_furnace") is not None
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"read_only_24v_{e.entry_id}") is None


async def test_24v_control_is_blocked(hass, aioclient_mock, mock_deployer, state) -> None:
    e = await _setup(hass, aioclient_mock, _24v(state))
    for service, data in (("set_temperature", {"temperature": 70}), ("set_hvac_mode", {"hvac_mode": "cool"}),
                          ("turn_off", {})):
        with pytest.raises(HomeAssistantError) as err:
            await hass.services.async_call("climate", service, {"entity_id": CLIMATE, **data}, blocking=True)
        assert err.value.translation_key == "read_only_24v", service
    assert not [c for c in aioclient_mock.mock_calls if c[0] == "POST"]
    assert e.runtime_data.commands[-1]["result"] == "blocked_read_only"


async def test_24v_changes_logged_and_in_diagnostics(hass, aioclient_mock, mock_deployer, state, caplog) -> None:
    caplog.set_level(logging.INFO)
    e = await _setup(hass, aioclient_mock, _24v(state))
    assert "is in conventional 24 V mode: read-only" in caplog.text
    c = e.runtime_data
    c.async_set_updated_data(normalize(_24v(state, 1, "266", 0, 75)))   # setpoint up, heat call
    c._track_times(c.data)
    await hass.async_block_till_done()
    assert "24 V state on" in caplog.text and "outputs none -> O/B+G+Y2+Y1" in caplog.text
    diag = await async_get_config_entry_diagnostics(hass, e)
    ev = diag["v24_events"]
    assert "initial" in ev[0] and ev[-1]["changes"]["setpoint"] == [72.0, 75.0]
    assert diag["state"]["v24_outputs"]["active"] == ["O/B", "G", "Y2", "Y1"]


async def test_24v_before_state_lines(hass, aioclient_mock, mock_deployer, state, caplog) -> None:
    caplog.set_level(logging.WARNING)
    e = await _setup(hass, aioclient_mock, _24v(state, seen=False))   # only meiju's comm_mode=1 so far
    assert e.state is ConfigEntryState.SETUP_RETRY and "conventional 24 V mode" in e.reason
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"conventional_24v_{e.entry_id}")
    assert issue.translation_placeholders["summary"] == "no 24 V state lines yet"
    assert issue.learn_more_url.endswith("/issues/5")
    caplog.clear()
    await hass.config_entries.async_reload(e.entry_id)                 # retry: no repeated warning
    await hass.async_block_till_done()
    assert "runs in conventional 24 V mode" not in caplog.text


async def test_unknown_state_is_not_called_24v(hass, aioclient_mock, mock_deployer, state) -> None:
    s = copy.deepcopy(state)
    s["dev"]["seen"] = False                          # older daemon, or no state lines yet
    e = await _setup(hass, aioclient_mock, s)
    assert e.state is ConfigEntryState.SETUP_RETRY and "has not seen" in e.reason
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"conventional_24v_{e.entry_id}") is None
