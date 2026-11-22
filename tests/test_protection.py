"""Fault vs Protection: the indoor unit's protect_state is set around every defrost's reversal
(a TL04-1's history, 2026-11-22 15:07-15:10 UTC) and isn't a fault."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from custom_components.aciq_thermostat.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .test_defrost import _feed, _setup, _state

FAULT = "binary_sensor.thermostat_fault"
PROTECTION = "binary_sensor.thermostat_protection"
DEFROST = "binary_sensor.thermostat_defrost"


def _p(base: dict, coil: float, ocoil: float, hz: int, protect: int, *, codes=(0, 0, 0, 0),
       fault_state: int = 0) -> dict:
    s = _state(base, coil, hz, 0, ocoil=ocoil)
    s["equipment"].update(protect_state=protect, fault_state=fault_state, fault_codes=list(codes))
    return s


# (indoor coil, outdoor coil, compressor Hz, protect_state): heating, the compressor stops for the
# reversal and protect_state goes on, the defrost starts, protect_state clears, the defrost ends
TIMELINE = [(100, 30, 60, 0), (90, 32, 0, 5), (70, 40, 20, 5), (48, 56, 60, 5), (40, 70, 70, 0),
            (62, 50, 30, 0), (100, 30, 60, 0)]


async def test_defrost_protection_is_not_a_fault(hass: HomeAssistant, aioclient_mock: Any, mock_deployer,
                                                 state) -> None:
    e = await _setup(hass, aioclient_mock, _p(state, 100, 30, 60, 0))
    c = e.runtime_data
    fault, prot, defrost = [], [], []
    for coil, ocoil, hz, protect in TIMELINE:
        await _feed(hass, aioclient_mock, c, _p(state, coil, ocoil, hz, protect))
        fault.append(hass.states.get(FAULT).state)
        prot.append(hass.states.get(PROTECTION).state)
        defrost.append(hass.states.get(DEFROST).state)
    assert set(fault) == {"off"}
    assert prot == ["off", "on", "on", "on", "off", "off", "off"]
    assert defrost == ["off", "off", "off", "on", "on", "off", "off"]
    attrs = hass.states.get(PROTECTION).attributes
    assert attrs["protect_state"] == 0 and attrs["last_value"] == 5 and attrs["last_duration_s"] >= 0
    assert hass.states.get(FAULT).attributes["protect_state"] == 0
    # the defrost records the protection state it saw
    assert c.last_defrost["signals_start"]["protect_state"] == 5
    assert c.last_defrost["signals_seen"]["protect_state"] == 5
    diag = await async_get_config_entry_diagnostics(hass, e)
    changes = diag["protection"]["changes"]
    assert [(ch["from"], ch["to"]) for ch in changes] == [(0, 5), (5, 0)]
    assert changes[0]["compressor_hz"] == 0 and changes[0]["mode"] == "heat"
    assert diag["protection"]["last"]["value"] == 5 and diag["protection"]["cycle"] is None
    # protection changes stay out of the outage/deploy event list
    assert not any(ev["event"].startswith("protect") for ev in c.events)


async def test_fault_codes_and_fault_state(hass: HomeAssistant, aioclient_mock: Any, mock_deployer, state) -> None:
    e = await _setup(hass, aioclient_mock, _p(state, 100, 30, 60, 0))
    c = e.runtime_data
    assert hass.states.get(FAULT).state == "off" and hass.states.get(PROTECTION).state == "off"
    await _feed(hass, aioclient_mock, c, _p(state, 100, 30, 60, 0, codes=(0, 12, 0, 0)))
    st = hass.states.get(FAULT)
    assert st.state == "on" and st.attributes["fault_codes"] == [0, 12, 0, 0]
    await _feed(hass, aioclient_mock, c, _p(state, 100, 30, 60, 3, fault_state=1))
    st = hass.states.get(FAULT)
    assert st.state == "on" and st.attributes["fault_state"] == 1 and st.attributes["protect_state"] == 3
    assert hass.states.get(PROTECTION).state == "on"
    await _feed(hass, aioclient_mock, c, _p(state, 100, 30, 60, 0))
    assert hass.states.get(FAULT).state == "off"


async def test_protection_without_equipment(hass: HomeAssistant, aioclient_mock: Any, mock_deployer, state) -> None:
    s = _p(state, 100, 30, 60, 4)
    s["equipment"] = {"available": False}
    e = await _setup(hass, aioclient_mock, s)
    assert hass.states.get(PROTECTION).state == "unavailable"
    assert hass.states.get(FAULT).state == "unavailable"
    assert e.runtime_data.protect_state is None
    # a bad value is ignored rather than counted
    await _feed(hass, aioclient_mock, e.runtime_data, {**_p(state, 100, 30, 60, 0),
                                                     "equipment": {**_p(state, 100, 30, 60, 0)["equipment"],
                                                                   "protect_state": None}})
    assert e.runtime_data.protect_state is None
    diag = await async_get_config_entry_diagnostics(hass, e)
    assert diag["protection"] == {"value": None, "cycle": None, "last": None, "changes": []}
