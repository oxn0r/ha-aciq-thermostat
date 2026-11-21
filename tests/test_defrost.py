"""Defrost sensor (#9), replaying the first real capture: the indoor coil falls from ~88 F to
~5 F while the heat pump runs reversed, then climbs back to ~100 F."""

from __future__ import annotations

import copy
from typing import Any

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.climate.const import HVACAction
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant

from custom_components.aciq_thermostat.const import CONF_ADB_PORT, CONF_AUTO_DEPLOY, DOMAIN
from custom_components.aciq_thermostat.diagnostics import async_get_config_entry_diagnostics

from .conftest import HOST, SERIAL

BASE = f"http://{HOST}:8080"
SENSOR = "binary_sensor.thermostat_defrost"
# (indoor coil F, outdoor coil F, compressor Hz, four_valve) every poll, after the #9 history
# (2026-11-21 08:38-08:46; outdoor coil 66 F in the report taken right after the reversed run)
REPLAY = [(88, 30, 39, 0), (60, 32, 0, 0), (32, 45, 40, 1), (12, 60, 80, 1), (5, 66, 80, 1), (12, 66, 0, 0),
          (45, 50, 20, 0), (70, 30, 43, 0), (100, 28, 43, 0)]
EXPECT = [False, False, True, True, True, True, True, False, False]
# A short mild-weather defrost from a TL04-1's history (2026-11-15 19:47-19:52, 37 F outside): the
# indoor coil only gets down to ~43 F while the outdoor coil heats to 77 F
SHORT = [(84.2, 21.2), (78.8, 33.8), (66.2, 48.2), (60.8, 39.2), (50.0, 39.2), (44.6, 46.4), (42.8, 75.2),
         (46.4, 77.0), (51.8, 64.4), (60.8, 44.6), (77.0, 35.6), (91.4, 30.2)]
SHORT_EXPECT = [False] * 6 + [True] * 3 + [False] * 3


async def _setup(hass: HomeAssistant, aioclient_mock: Any, state: dict) -> MockConfigEntry:
    aioclient_mock.get(f"{BASE}/api/state", json=state)
    e = MockConfigEntry(domain=DOMAIN, unique_id=SERIAL, title="Thermostat",
                        data={CONF_HOST: HOST, CONF_PORT: 8080, CONF_ADB_PORT: 5555},
                        options={CONF_AUTO_DEPLOY: True})
    e.add_to_hass(hass)
    await hass.config_entries.async_setup(e.entry_id)
    await hass.async_block_till_done()
    return e


def _state(base: dict, coil: float, hz: int, valve: int, mode: str = "heat", auto_state: int = 0,
           ocoil: float | None = None) -> dict:
    s = copy.deepcopy(base)
    s["mode"], s["current_temp"] = mode, 71.0
    s.setdefault("dev", {})["auto_state"] = auto_state
    eq = s["equipment"]
    eq.update({"available": True, "indoor_coil_temp": coil, "compressor_hz": hz, "furnace_on": False,
               "outdoor_temp": 44.0, "outdoor_coil_temp": ocoil})
    eq["defrost_signals"] = {"four_valve": valve, "outdoor_fan_low": 0, "outdoor_fan_high": 0,
                             "compressor_on": int(hz > 0), "oil_return": 0, "outdoor_fan_speed": 0}
    return s


async def _feed(hass: HomeAssistant, aioclient_mock: Any, c: Any, s: dict) -> None:
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{BASE}/api/state", json=s)
    await c.async_refresh()
    await hass.async_block_till_done()


async def test_defrost_replay(hass, aioclient_mock, mock_deployer, state) -> None:
    e = await _setup(hass, aioclient_mock, _state(state, 88, 39, 0, ocoil=30))
    c = e.runtime_data
    seen, actions = [], []
    for (coil, ocoil, hz, valve) in REPLAY:
        await _feed(hass, aioclient_mock, c, _state(state, coil, hz, valve, ocoil=ocoil))
        seen.append(hass.states.get(SENSOR).state == "on")
        actions.append(hass.states.get("climate.thermostat").attributes["hvac_action"])
    assert seen == EXPECT
    assert all(a == HVACAction.DEFROSTING for a, on in zip(actions, EXPECT) if on)
    assert actions[-1] == HVACAction.HEATING
    events = [ev for ev in c.events if ev["event"].startswith("defrost")]
    assert [ev["event"] for ev in events] == ["defrost_start", "defrost_end"]
    assert events[1]["min_coil_f"] == 5 and events[1]["signals_seen"] == {"four_valve": 1, "compressor_on": 1}
    attrs = hass.states.get(SENSOR).attributes
    assert attrs["last_min_coil_temp"] == 5 and attrs["last_duration_s"] >= 0
    diag = await async_get_config_entry_diagnostics(hass, e)
    assert diag["defrost"]["active"] is False and diag["defrost"]["last"]["signals_seen"]["four_valve"] == 1


async def test_cold_coil_while_cooling_is_not_defrost(hass, aioclient_mock, mock_deployer, state) -> None:
    e = await _setup(hass, aioclient_mock, _state(state, 70, 0, 0, mode="cool"))
    c = e.runtime_data
    for mode, auto_state in (("cool", 0), ("auto", 1), ("auto", 0), ("dry", 0), ("emergency_heat", 0)):
        await _feed(hass, aioclient_mock, c, _state(state, 35, 50, 0, mode=mode, auto_state=auto_state))
        assert hass.states.get(SENSOR).state == "off", mode


async def test_defrost_in_auto_heating(hass, aioclient_mock, mock_deployer, state) -> None:
    e = await _setup(hass, aioclient_mock, _state(state, 95, 40, 0, mode="auto", auto_state=2))
    c = e.runtime_data
    await _feed(hass, aioclient_mock, c, _state(state, 20, 70, 1, mode="auto", auto_state=2))
    assert hass.states.get(SENSOR).state == "on"
    assert hass.states.get("climate.thermostat").attributes["hvac_action"] == HVACAction.DEFROSTING
    await _feed(hass, aioclient_mock, c, _state(state, 20, 70, 1, mode="off"))        # turned off mid-cycle
    assert hass.states.get(SENSOR).state == "off"


async def test_no_equipment_data(hass, aioclient_mock, mock_deployer, state) -> None:
    s = _state(state, 20, 70, 1)
    s["equipment"] = {"available": False}
    e = await _setup(hass, aioclient_mock, s)
    assert hass.states.get(SENSOR).state == "unavailable"
    assert e.runtime_data.defrost is False


async def test_short_mild_weather_defrost(hass, aioclient_mock, mock_deployer, state) -> None:
    e = await _setup(hass, aioclient_mock, _state(state, 84.2, 56, 0, ocoil=21.2))
    c = e.runtime_data
    seen = []
    for coil, ocoil in SHORT:
        await _feed(hass, aioclient_mock, c, _state(state, coil, 30, 0, ocoil=ocoil))
        seen.append(hass.states.get(SENSOR).state == "on")
    assert seen == SHORT_EXPECT
    assert c.last_defrost is not None and c.last_defrost["min_coil_f"] == 42.8


async def test_warm_idle_and_missing_outdoor_coil(hass, aioclient_mock, mock_deployer, state) -> None:
    e = await _setup(hass, aioclient_mock, _state(state, 66, 0, 0, ocoil=72))     # warm day, idle in Heat
    c = e.runtime_data
    assert hass.states.get(SENSOR).state == "off"
    await _feed(hass, aioclient_mock, c, _state(state, 45, 50, 0, ocoil=None))    # no outdoor coil reading
    assert hass.states.get(SENSOR).state == "off"                                 # 45 F alone isn't enough
    await _feed(hass, aioclient_mock, c, _state(state, 35, 50, 0, ocoil=None))
    assert hass.states.get(SENSOR).state == "on"
