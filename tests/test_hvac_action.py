"""hvac_action in Auto must follow the equipment, never room-vs-setpoint (bug in v1.0.0)."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.climate.const import HVACAction
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant

from custom_components.aciq_thermostat.const import CONF_ADB_PORT, CONF_AUTO_DEPLOY, DOMAIN

from .conftest import HOST, SERIAL

BASE = f"http://{HOST}:8080"


def _auto(state: dict, *, room: float, auto_state: int, hz: int = 54, coil: float | None = 105.8,
          furnace: bool = False) -> dict:
    s = copy.deepcopy(state)
    s["mode"] = "auto"
    s["setpoint"] = 72
    s["current_temp"] = room
    s["dev"]["auto_state"] = auto_state
    s["equipment"].update(compressor_hz=hz, furnace_on=furnace, blower_on=True, blower_speed=2,
                          indoor_coil_temp=coil)
    return s


async def _setup(hass: HomeAssistant, aioclient_mock: Any, first: dict) -> MockConfigEntry:
    aioclient_mock.get(f"{BASE}/api/state", json=first)
    e = MockConfigEntry(domain=DOMAIN, unique_id=SERIAL, title="Thermostat",
                        data={CONF_HOST: HOST, CONF_PORT: 8080, CONF_ADB_PORT: 5555},
                        options={CONF_AUTO_DEPLOY: True})
    e.add_to_hass(hass)
    await hass.config_entries.async_setup(e.entry_id)
    await hass.async_block_till_done()
    return e


def _action(hass: HomeAssistant) -> str:
    return hass.states.get("climate.thermostat").attributes["hvac_action"]


async def _push(hass: HomeAssistant, e: MockConfigEntry, data: dict) -> str:
    e.runtime_data.async_set_updated_data(data)
    await hass.async_block_till_done()
    return _action(hass)


async def test_auto_heat_state_with_room_above_setpoint(hass, aioclient_mock, mock_deployer, state) -> None:
    """auto_state=heat wins even when the room is above the setpoint."""
    await _setup(hass, aioclient_mock, _auto(state, room=72.4, auto_state=2))
    assert _action(hass) == HVACAction.HEATING


async def test_auto_cool_state_with_room_below_setpoint(hass, aioclient_mock, mock_deployer, state) -> None:
    await _setup(hass, aioclient_mock, _auto(state, room=71.5, auto_state=1, coil=None))
    assert _action(hass) == HVACAction.COOLING


async def test_auto_unknown_state_hot_coil_room_above_setpoint(hass, aioclient_mock, mock_deployer, state) -> None:
    """Fallback: hot coil means heating regardless of room vs setpoint."""
    await _setup(hass, aioclient_mock, _auto(state, room=72.4, auto_state=0, coil=109.4))
    assert _action(hass) == HVACAction.HEATING


async def test_auto_unknown_state_cold_coil_room_below_setpoint(hass, aioclient_mock, mock_deployer, state) -> None:
    await _setup(hass, aioclient_mock, _auto(state, room=71.0, auto_state=0, coil=45.0))
    assert _action(hass) == HVACAction.COOLING


async def test_auto_dry(hass, aioclient_mock, mock_deployer, state) -> None:
    await _setup(hass, aioclient_mock, _auto(state, room=74, auto_state=3, coil=50))
    assert _action(hass) == HVACAction.DRYING


async def test_auto_furnace(hass, aioclient_mock, mock_deployer, state) -> None:
    await _setup(hass, aioclient_mock, _auto(state, room=72.5, auto_state=0, hz=0, coil=80, furnace=True))
    assert _action(hass) == HVACAction.HEATING


async def test_auto_unknown_at_startup_is_idle_not_a_guess(hass, aioclient_mock, mock_deployer, state) -> None:
    await _setup(hass, aioclient_mock, _auto(state, room=73.0, auto_state=0, coil=74.0))
    assert _action(hass) == HVACAction.IDLE


async def test_replay_reported_sequence_never_flips(hass, aioclient_mock, mock_deployer, state) -> None:
    """Exact readings from the v1.0.0 report (2026-10-20 01:14-01:17 UTC), with and without auto_state."""
    rows = [(71.9, 56, 105.8), (72.0, 54, 105.8), (72.1, 54, 107.6), (72.1, 54, 109.4), (72.4, 50, 109.4)]
    for auto_state in (2, 0, 4):
        e = await _setup(hass, aioclient_mock, _auto(state, room=rows[0][0], auto_state=auto_state,
                                                      hz=rows[0][1], coil=rows[0][2]))
        seen = [await _push(hass, e, _auto(state, room=r, auto_state=auto_state, hz=hz, coil=c))
                for r, hz, c in rows]
        assert seen == [HVACAction.HEATING] * len(rows), (auto_state, seen)
        await hass.config_entries.async_unload(e.entry_id)
        await hass.config_entries.async_remove(e.entry_id)
        aioclient_mock.clear_requests()


async def test_holds_last_direction_when_coil_near_room(hass, aioclient_mock, mock_deployer, state) -> None:
    """Once confirmed, an unknown state with an ambiguous coil keeps the last direction (no flip-flop)."""
    e = await _setup(hass, aioclient_mock, _auto(state, room=71.9, auto_state=2))
    assert _action(hass) == HVACAction.HEATING
    for room in (72.1, 71.8, 72.3):  # crosses the setpoint repeatedly
        assert await _push(hass, e, _auto(state, room=room, auto_state=0, coil=room + 3)) == HVACAction.HEATING
    # compressor stops -> blower only -> fan; then a cooling call is recognised
    assert await _push(hass, e, _auto(state, room=72.0, auto_state=4, hz=0, coil=72)) == HVACAction.FAN
    assert await _push(hass, e, _auto(state, room=75.0, auto_state=1, coil=50)) == HVACAction.COOLING


@pytest.mark.parametrize(("raw", "name"), [(0, "none"), (1, "cool"), (2, "heat"), (3, "dry"), (4, "fan")])
async def test_auto_state_sensor_names(hass, aioclient_mock, mock_deployer, state, raw, name) -> None:
    from homeassistant.helpers import entity_registry as er
    s = _auto(state, room=72, auto_state=raw)
    e = await _setup(hass, aioclient_mock, s)
    reg = er.async_get(hass)
    ent = reg.async_get_entity_id("sensor", DOMAIN, f"{SERIAL}_auto_state")
    reg.async_update_entity(ent, disabled_by=None)
    await hass.config_entries.async_reload(e.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(ent).state == name
