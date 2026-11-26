"""Defrost from the indoor unit's protect_state 2, set at the start of every defrost on a TL04-1
(2026-11-22..26), 1-3 min before the coils swap."""

from __future__ import annotations

from typing import Any

from homeassistant.components.climate.const import HVACAction
from homeassistant.core import HomeAssistant

from .test_defrost import _feed, _setup
from .test_protection import _p

DEFROST = "binary_sensor.thermostat_defrost"

# (indoor coil, outdoor coil, compressor Hz, protect_state): the compressor stops and protect_state
# goes to 2, the coils swap, protect_state clears while the coil rule holds Defrost, then heat again
TIMELINE = [(100, 30, 60, 0), (90, 32, 0, 2), (70, 40, 20, 2), (48, 56, 60, 2), (40, 70, 70, 0),
            (62, 50, 30, 0), (100, 30, 60, 0)]


async def test_protect_state_starts_defrost_early(hass: HomeAssistant, aioclient_mock: Any, mock_deployer,
                                                  state) -> None:
    e = await _setup(hass, aioclient_mock, _p(state, 100, 30, 60, 0))
    c = e.runtime_data
    seen = []
    for coil, ocoil, hz, protect in TIMELINE:
        await _feed(hass, aioclient_mock, c, _p(state, coil, ocoil, hz, protect))
        seen.append(hass.states.get(DEFROST).state)
        if protect:
            assert hass.states.get("climate.thermostat").attributes["hvac_action"] == HVACAction.DEFROSTING
    assert seen == ["off", "on", "on", "on", "on", "off", "off"]
    assert c.last_defrost["trigger"] == "protect_state" and c.last_defrost["min_coil_f"] == 40
    start = next(ev for ev in c.events if ev["event"] == "defrost_start")
    assert start["trigger"] == "protect_state"
    assert hass.states.get("binary_sensor.thermostat_fault").state == "off"


async def test_short_reversal_the_coil_rule_misses(hass: HomeAssistant, aioclient_mock: Any, mock_deployer,
                                                   state) -> None:
    """2026-11-12 15:02: a short reversal after the furnace handed over; the indoor coil only got
    down to 53.6 F, above the coil rule's 50 F."""
    e = await _setup(hass, aioclient_mock, _p(state, 84, 24, 56, 0))
    c = e.runtime_data
    seen = []
    for coil, ocoil, hz, protect in [(80, 14, 0, 2), (66, 50, 50, 2), (53.6, 57, 0, 2), (57, 60, 15, 0),
                                      (75, 37, 56, 0)]:
        await _feed(hass, aioclient_mock, c, _p(state, coil, ocoil, hz, protect))
        seen.append(hass.states.get(DEFROST).state)
    assert seen == ["on", "on", "on", "on", "off"]       # held by the coils until the outdoor coil cools
    assert c.last_defrost["min_coil_f"] == 53.6


async def test_protect_state_outside_heating_or_other_values(hass: HomeAssistant, aioclient_mock: Any,
                                                             mock_deployer, state) -> None:
    e = await _setup(hass, aioclient_mock, _p(state, 100, 30, 60, 0))
    c = e.runtime_data
    for protect, mode in ((5, "heat"), (1, "heat"), (2, "cool"), (2, "off"), (2, "fan_only"),
                          (2, "emergency_heat")):
        s = _p(state, 100, 30, 60, protect)
        s["mode"] = mode
        await _feed(hass, aioclient_mock, c, s)
        assert hass.states.get(DEFROST).state == "off", (protect, mode)
    s = _p(state, 100, 30, 60, 2)
    s["mode"], s["dev"]["auto_state"] = "auto", 2       # auto, heating
    await _feed(hass, aioclient_mock, c, s)
    assert hass.states.get(DEFROST).state == "on"
    s = _p(state, 100, 30, 60, 2)
    s["equipment"]["indoor_coil_temp"] = None           # no coil reading: still starts, coil stays unknown
    await _feed(hass, aioclient_mock, c, s)
    assert hass.states.get(DEFROST).state == "on"
    await _feed(hass, aioclient_mock, c, _p(state, 100, 30, 60, 0))
    assert hass.states.get(DEFROST).state == "off"
