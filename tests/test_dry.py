"""Dry (dehumidify) on communicating systems: mode, duration, countdown, start action, return."""
from __future__ import annotations

import copy
from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMockResponse

from homeassistant.components.climate import HVACAction, HVACMode
from homeassistant.exceptions import ServiceValidationError

from custom_components.aciq_thermostat.const import CONF_ADB_PORT, CONF_AUTO_DEPLOY, CONF_DRY_RETURN, DOMAIN
from custom_components.aciq_thermostat.v24 import normalize
from .conftest import HOST, SERIAL

BASE = f"http://{HOST}:8080"
CLIMATE = "climate.thermostat"
SELECT = "select.thermostat_dry_duration"
LEFT = "sensor.thermostat_dry_time_left"


def _comm(state: dict, mode: str = "heat", setpoint: int = 70, minutes: int = 15, left: int = 15) -> dict:
    s = copy.deepcopy(state)
    s.update(mode=mode, setpoint=setpoint, comm_mode="communicating")
    s["ui"].update(dry_minutes=minutes, dry_time_left=left)
    return s


class FakeDaemon:
    """Like the daemon on a communicating TL04-1: Dry goes through Cool, dry_minutes sets the duration."""

    def __init__(self, state: dict) -> None:
        self.state = state
        self.posts: list[dict[str, Any]] = []

    async def get(self, method: str, url: Any, data: Any) -> AiohttpClientMockResponse:
        return AiohttpClientMockResponse(method, url, json=self.state)

    async def post(self, method: str, url: Any, data: Any) -> AiohttpClientMockResponse:
        self.posts.append(data)
        s = self.state = copy.deepcopy(self.state)
        if "dry_minutes" in data:
            s["ui"].update(dry_minutes=data["dry_minutes"], dry_time_left=data["dry_minutes"])
        if "mode" in data:
            s["mode"] = data["mode"]
        if "temperature" in data:
            s["setpoint"] = data["temperature"]
        return AiohttpClientMockResponse(method, url, json={"success": True})


async def _setup(hass, aioclient_mock, daemon: FakeDaemon, **options: Any) -> MockConfigEntry:
    aioclient_mock.get(f"{BASE}/api/state", side_effect=daemon.get)
    aioclient_mock.post(f"{BASE}/api/control", side_effect=daemon.post)
    e = MockConfigEntry(domain=DOMAIN, unique_id=SERIAL, title="Thermostat",
                        data={"host": HOST, "port": 8080, CONF_ADB_PORT: 5555},
                        options={CONF_AUTO_DEPLOY: True, **options})
    e.add_to_hass(hass)
    await hass.config_entries.async_setup(e.entry_id)
    await hass.async_block_till_done()
    return e


def _push(e: MockConfigEntry, state: dict) -> None:
    """A state update as the long-poll delivers it."""
    data = normalize(state)
    e.runtime_data._track_times(data)
    e.runtime_data.async_set_updated_data(data)


@pytest.fixture(autouse=True)
def fast():
    with patch("custom_components.aciq_thermostat.coordinator.REFRESH_DELAY", 0):
        yield


@pytest.fixture
def logbook():
    with patch("custom_components.aciq_thermostat.coordinator.async_log_entry") as log:
        yield log


async def test_dry_entities(hass, aioclient_mock, mock_deployer, state) -> None:
    await _setup(hass, aioclient_mock, FakeDaemon(_comm(state, minutes=30, left=30)))
    c = hass.states.get(CLIMATE)
    assert HVACMode.DRY in c.attributes["hvac_modes"]
    assert hass.states.get(SELECT).state == "30"
    assert hass.states.get(SELECT).attributes["options"] == ["15", "30", "45", "60"]
    left = hass.states.get(LEFT)
    assert left.state == "0" and left.attributes["ends_at"] is None   # not drying: 0, not the full duration


async def test_no_dry_without_communicating_state(hass, aioclient_mock, mock_deployer, state) -> None:
    await _setup(hass, aioclient_mock, FakeDaemon(state))            # comm_mode not reported
    assert HVACMode.DRY not in hass.states.get(CLIMATE).attributes["hvac_modes"]
    assert hass.states.get(SELECT).state == "unavailable"
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "start_dry", {"entity_id": CLIMATE}, blocking=True)


async def test_dry_from_heat_and_back(hass, aioclient_mock, mock_deployer, state, logbook) -> None:
    d = FakeDaemon(_comm(state, "heat", 70))
    e = await _setup(hass, aioclient_mock, d)
    await hass.services.async_call("climate", "set_hvac_mode", {"entity_id": CLIMATE, "hvac_mode": "dry"},
                                   blocking=True)
    assert d.posts == [{"mode": "dry"}]                  # the daemon goes through Cool itself
    assert e.runtime_data.dry_return == {"mode": "heat", "temperature": 70}
    assert "from heat to cool to start Dry" in logbook.call_args.args[2]
    assert hass.states.get(CLIMATE).state == HVACMode.DRY

    # running: compressor -> drying; the countdown and its end time
    s = copy.deepcopy(d.state)
    s["ui"]["dry_time_left"] = 12
    s["equipment"].update(available=True, compressor_hz=40, blower_on=True)
    _push(e, s)
    await hass.async_block_till_done()
    assert hass.states.get(CLIMATE).attributes["hvac_action"] == HVACAction.DRYING
    left = hass.states.get(LEFT)
    assert left.state == "12" and left.attributes["ends_at"] is not None

    # countdown done: the thermostat is in Cool -> Heat 70 is put back
    s = copy.deepcopy(s)
    s["mode"] = "cool"
    d.state = s
    _push(e, s)
    await hass.async_block_till_done()
    assert d.posts[-1] == {"mode": "heat", "temperature": 70}
    assert e.runtime_data.dry_return is None
    assert "returning to heat" in logbook.call_args.args[2]
    assert hass.states.get(CLIMATE).state == HVACMode.HEAT


async def test_dry_return_option_off(hass, aioclient_mock, mock_deployer, state, logbook) -> None:
    d = FakeDaemon(_comm(state, "auto", 72))
    e = await _setup(hass, aioclient_mock, d, **{CONF_DRY_RETURN: False})
    await hass.services.async_call(DOMAIN, "start_dry", {"entity_id": CLIMATE, "duration": 45}, blocking=True)
    assert d.posts == [{"mode": "dry", "dry_minutes": 45}]
    assert hass.states.get(SELECT).state == "45"
    s = copy.deepcopy(d.state)
    s["mode"] = "cool"
    _push(e, s)
    await hass.async_block_till_done()
    assert len(d.posts) == 1 and hass.states.get(CLIMATE).state == HVACMode.COOL   # stays in Cool


async def test_dry_ended_into_another_mode_is_left_alone(hass, aioclient_mock, mock_deployer, state,
                                                         logbook) -> None:
    d = FakeDaemon(_comm(state, "heat", 70))
    e = await _setup(hass, aioclient_mock, d)
    await hass.services.async_call(DOMAIN, "start_dry", {"entity_id": CLIMATE}, blocking=True)
    await hass.services.async_call("climate", "set_hvac_mode", {"entity_id": CLIMATE, "hvac_mode": "auto"},
                                   blocking=True)
    assert d.posts == [{"mode": "dry"}, {"mode": "auto"}]   # someone picked Auto: not overridden
    assert e.runtime_data.dry_return is None


async def test_dry_from_cool_needs_no_return(hass, aioclient_mock, mock_deployer, state, logbook) -> None:
    d = FakeDaemon(_comm(state, "cool", 75))
    e = await _setup(hass, aioclient_mock, d)
    await hass.services.async_call("climate", "set_temperature",
                                   {"entity_id": CLIMATE, "hvac_mode": "dry", "temperature": 74}, blocking=True)
    assert d.posts == [{"mode": "dry"}, {"temperature": 74, "unit": "F"}]
    assert e.runtime_data.dry_return is None and not logbook.called
    assert hass.states.get(CLIMATE).attributes["temperature"] == 74          # Dry keeps a setpoint


async def test_dry_return_keeps_auto_range(hass, aioclient_mock, mock_deployer, state, logbook) -> None:
    s = _comm(state, "auto", 72)
    s["dev"].update(zone_control=1, zone_lower=68.0, zone_upper=77.0)
    e = await _setup(hass, aioclient_mock, FakeDaemon(s))
    await hass.services.async_call(DOMAIN, "start_dry", {"entity_id": CLIMATE}, blocking=True)
    assert e.runtime_data.dry_return == {"mode": "auto"}                     # the range stays as it was


async def test_dry_duration_select(hass, aioclient_mock, mock_deployer, state) -> None:
    d = FakeDaemon(_comm(state))
    await _setup(hass, aioclient_mock, d)
    await hass.services.async_call("select", "select_option", {"entity_id": SELECT, "option": "60"}, blocking=True)
    assert d.posts == [{"dry_minutes": 60}] and hass.states.get(SELECT).state == "60"


async def test_start_dry_rejects_other_durations(hass, aioclient_mock, mock_deployer, state) -> None:
    await _setup(hass, aioclient_mock, FakeDaemon(_comm(state)))
    with pytest.raises(Exception, match="duration"):
        await hass.services.async_call(DOMAIN, "start_dry", {"entity_id": CLIMATE, "duration": 20}, blocking=True)


async def test_turn_on_after_dry_skips_dry(hass, aioclient_mock, mock_deployer, state, logbook) -> None:
    d = FakeDaemon(_comm(state, "cool", 75))
    await _setup(hass, aioclient_mock, d)
    await hass.services.async_call(DOMAIN, "start_dry", {"entity_id": CLIMATE}, blocking=True)
    await hass.services.async_call("climate", "turn_off", {"entity_id": CLIMATE}, blocking=True)
    await hass.services.async_call("climate", "turn_on", {"entity_id": CLIMATE}, blocking=True)
    assert d.posts[-1] == {"mode": "cool"}                    # back to the last regular mode, not Dry
