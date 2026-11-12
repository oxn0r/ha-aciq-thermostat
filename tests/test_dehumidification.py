"""Dehumidification switch (issue #11): on in Cool only, off anywhere, only where the indoor unit has it."""
from __future__ import annotations

import copy
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMockResponse

from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er

from custom_components.aciq_thermostat.const import CONF_ADB_PORT, CONF_AUTO_DEPLOY, DOMAIN
from .conftest import HOST, SERIAL
from .test_dry import FakeDaemon, _push, _setup

ENTITY = "switch.thermostat_dehumidification"


def _humi(state: dict, on: int, fun: int = 1, mode: str = "cool") -> dict:
    s = copy.deepcopy(state)
    s.update(comm_mode="communicating", mode=mode)
    s["dev"].update(humi_ctrl=on, humi_ctrl_fun=fun)
    return s


class HumiDaemon(FakeDaemon):
    async def post(self, method: str, url: Any, data: Any) -> AiohttpClientMockResponse:
        r = await super().post(method, url, data)
        if "dehumidification" in data:
            self.state["dev"]["humi_ctrl"] = int(data["dehumidification"])
        return r


async def _turn(hass, service: str) -> None:
    await hass.services.async_call("switch", service, {"entity_id": ENTITY}, blocking=True)


async def test_switch_on_and_off_in_cool(hass, aioclient_mock, mock_deployer, state) -> None:
    d = HumiDaemon(_humi(state, 0))
    await _setup(hass, aioclient_mock, d)
    assert hass.states.get(ENTITY).state == "off"
    await _turn(hass, "turn_on")
    assert d.posts == [{"dehumidification": True}]
    assert hass.states.get(ENTITY).state == "on"
    await _turn(hass, "turn_off")
    assert d.posts[-1] == {"dehumidification": False}
    assert hass.states.get(ENTITY).state == "off"


async def test_on_only_in_cool_off_anywhere(hass, aioclient_mock, mock_deployer, state) -> None:
    d = HumiDaemon(_humi(state, 1, mode="heat"))
    await _setup(hass, aioclient_mock, d)
    with pytest.raises(ServiceValidationError):
        await _turn(hass, "turn_on")
    assert d.posts == []
    await _turn(hass, "turn_off")
    assert d.posts == [{"dehumidification": False}]


async def test_follows_screen(hass, aioclient_mock, mock_deployer, state) -> None:
    e = await _setup(hass, aioclient_mock, HumiDaemon(_humi(state, 0)))
    _push(e, _humi(state, 1))
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).state == "on"
    _push(e, _humi(state, -1))                                     # not seen since a daemon restart
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).state == "unknown"


async def test_no_switch_without_support(hass, aioclient_mock, mock_deployer, state) -> None:
    await _setup(hass, aioclient_mock, HumiDaemon(_humi(state, 0, fun=0)))
    assert hass.states.get(ENTITY) is None
    assert hass.states.get("switch.thermostat_block_cloud_traffic") is not None


async def test_switch_added_when_support_appears(hass, aioclient_mock, mock_deployer, state) -> None:
    """Right after a daemon update the humi fields are -1 until the first AHU3 line: no restart needed."""
    e = await _setup(hass, aioclient_mock, HumiDaemon(_humi(state, -1, fun=-1)))
    assert hass.states.get(ENTITY) is None
    _push(e, _humi(state, 0))
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).state == "off"
    _push(e, _humi(state, 1))                                      # not added twice
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).state == "on"


async def test_unavailable_while_it_cannot_turn_on(hass, aioclient_mock, mock_deployer, state) -> None:
    """HA's toggle flips itself and stays flipped when an action is refused, so it's greyed out instead."""
    e = await _setup(hass, aioclient_mock, HumiDaemon(_humi(state, 0, mode="heat")))
    assert hass.states.get(ENTITY).state == "unavailable"
    _push(e, _humi(state, 1, mode="heat"))                         # on outside Cool: can still be turned off
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).state == "on"
    _push(e, _humi(state, 0))                                      # Cool
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).state == "off"


async def test_no_switch_on_older_daemon(hass, aioclient_mock, mock_deployer, state) -> None:
    await _setup(hass, aioclient_mock, FakeDaemon(state))          # no humi fields at all
    assert hass.states.get(ENTITY) is None


async def test_old_sensor_removed(hass, aioclient_mock, mock_deployer, state) -> None:
    """1.2.2's read-only binary sensor is replaced by the switch."""
    e = MockConfigEntry(domain=DOMAIN, unique_id=SERIAL, title="Thermostat",
                        data={"host": HOST, "port": 8080, CONF_ADB_PORT: 5555}, options={CONF_AUTO_DEPLOY: True})
    e.add_to_hass(hass)
    reg = er.async_get(hass)
    reg.async_get_or_create("binary_sensor", DOMAIN, f"{SERIAL}_dehumidification", config_entry=e)
    d = HumiDaemon(_humi(state, 0))
    aioclient_mock.get(f"http://{HOST}:8080/api/state", side_effect=d.get)
    await hass.config_entries.async_setup(e.entry_id)
    await hass.async_block_till_done()
    assert reg.async_get_entity_id("binary_sensor", DOMAIN, f"{SERIAL}_dehumidification") is None
    assert hass.states.get(ENTITY) is not None
