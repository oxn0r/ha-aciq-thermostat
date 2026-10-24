"""Issue #4: thermostat set to Celsius (prototype)."""
from __future__ import annotations

import copy
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
from custom_components.aciq_thermostat.const import CONF_ADB_PORT, CONF_AUTO_DEPLOY, DOMAIN
from .conftest import HOST, SERIAL

BASE = f"http://{HOST}:8080"


def _c(state: dict, sp: int = 20) -> dict:
    """What the daemon reports with the thermostat in Celsius (values from a live TL04-1)."""
    s = copy.deepcopy(state)
    s.update(temp_unit="C", setpoint=sp, current_temp=68.5)      # room temp stays degF
    s["dev"]["temp_set"] = float(sp)
    return s


async def _setup(hass, aioclient_mock, st):
    aioclient_mock.get(f"{BASE}/api/state", json=st)
    aioclient_mock.post(f"{BASE}/api/control", json={"success": True})
    e = MockConfigEntry(domain=DOMAIN, unique_id=SERIAL, title="Thermostat",
                        data={"host": HOST, "port": 8080, CONF_ADB_PORT: 5555}, options={CONF_AUTO_DEPLOY: True})
    e.add_to_hass(hass)
    await hass.config_entries.async_setup(e.entry_id)
    await hass.async_block_till_done()
    return e


@pytest.mark.parametrize(("units", "sp", "cur", "lo", "hi"), [
    (METRIC_SYSTEM, 20, 20.3, 16, 30),
    (US_CUSTOMARY_SYSTEM, 68, 68.5, 61, 86),
])
async def test_celsius_thermostat_displays_correctly(hass, aioclient_mock, mock_deployer, state,
                                                    units, sp, cur, lo, hi) -> None:
    hass.config.units = units
    await _setup(hass, aioclient_mock, _c(state, 20))
    a = hass.states.get("climate.thermostat").attributes
    assert a["temperature"] == sp                                  # was -6.7 C in v1.0.6
    assert a["current_temperature"] == pytest.approx(cur, abs=0.1)
    assert round(a["min_temp"]) == lo and round(a["max_temp"]) == hi
    assert float(hass.states.get("sensor.thermostat_setpoint").state) == pytest.approx(sp, abs=0.1)


async def test_celsius_set_temperature_payload(hass, aioclient_mock, mock_deployer, state) -> None:
    hass.config.units = METRIC_SYSTEM
    await _setup(hass, aioclient_mock, _c(state, 20))
    with patch("custom_components.aciq_thermostat.coordinator.REFRESH_DELAY", 0):
        await hass.services.async_call("climate", "set_temperature",
                                       {"entity_id": "climate.thermostat", "temperature": 21}, blocking=True)
    posts = [c[2] for c in aioclient_mock.mock_calls if c[0] == "POST"]
    assert posts == [{"temperature": 21, "unit": "C"}]


async def test_unit_switch_while_running(hass, aioclient_mock, mock_deployer, state) -> None:
    hass.config.units = METRIC_SYSTEM
    e = await _setup(hass, aioclient_mock, state)               # thermostat in F, setpoint 72
    assert hass.states.get("climate.thermostat").attributes["temperature"] == pytest.approx(22.2, abs=0.1)
    e.runtime_data.async_set_updated_data(_c(state, 22))        # user switches the wall unit to C
    await hass.async_block_till_done()
    assert hass.states.get("climate.thermostat").attributes["temperature"] == 22


async def test_old_daemon_without_unit_is_fahrenheit(hass, aioclient_mock, mock_deployer, state) -> None:
    s = copy.deepcopy(state)
    s.pop("temp_unit", None)
    await _setup(hass, aioclient_mock, s)
    assert hass.states.get("climate.thermostat").attributes["temperature"] == 72
