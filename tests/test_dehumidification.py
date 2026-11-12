"""Dehumidification switch on the screen (issue #11): read-only, only where the indoor unit has it."""
from __future__ import annotations

import copy

from custom_components.aciq_thermostat.const import DOMAIN
from .test_dry import FakeDaemon, _push, _setup

ENTITY = "binary_sensor.thermostat_dehumidification"


def _humi(state: dict, on: int, fun: int = 1) -> dict:
    s = copy.deepcopy(state)
    s["comm_mode"] = "communicating"
    s["dev"].update(humi_ctrl=on, humi_ctrl_fun=fun)
    return s


async def test_dehumidification_follows_screen(hass, aioclient_mock, mock_deployer, state) -> None:
    e = await _setup(hass, aioclient_mock, FakeDaemon(_humi(state, 0)))
    assert hass.states.get(ENTITY).state == "off"
    _push(e, _humi(state, 1))
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).state == "on"
    _push(e, _humi(state, -1))                                     # not seen since a daemon restart
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).state == "unknown"


async def test_no_entity_without_support(hass, aioclient_mock, mock_deployer, state) -> None:
    await _setup(hass, aioclient_mock, FakeDaemon(_humi(state, 0, fun=0)))
    assert hass.states.get(ENTITY) is None
    assert hass.states.get("binary_sensor.thermostat_blower") is not None


async def test_no_entity_on_older_daemon(hass, aioclient_mock, mock_deployer, state) -> None:
    await _setup(hass, aioclient_mock, FakeDaemon(state))          # no humi fields at all
    assert hass.states.get(ENTITY) is None
    assert DOMAIN in hass.config_entries.async_domains()
