"""Issue #7: Auto holding a heat/cool range (dev_app temp_zone_control = 1)."""
from __future__ import annotations

import copy
from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMockResponse

from homeassistant.components.climate import ClimateEntityFeature, HVACMode
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util.unit_system import METRIC_SYSTEM

from custom_components.aciq_thermostat.const import CONF_ADB_PORT, CONF_AUTO_DEPLOY, DOMAIN
from custom_components.aciq_thermostat.diagnostics import async_get_config_entry_diagnostics
from .conftest import HOST, SERIAL

BASE = f"http://{HOST}:8080"
CLIMATE = "climate.thermostat"


def _zone(state: dict, zc: int | None = 1, lo: float = 68, hi: float = 77, db: int = 5) -> dict:
    """What the daemon reports for the issue #7 thermostat (Auto, heat 68 / cool 77, deadband 5)."""
    s = copy.deepcopy(state)
    if zc is None:
        return s                                     # daemon older than 1.1.0
    s["dev"].update(zone_control=zc, zone_lower=float(lo), zone_upper=float(hi), temp_set=float(hi))
    s["ui"].update(heat_setpoint=lo, cool_setpoint=hi, deadband=db, temp_zone_mode=zc)
    s["setpoint"] = hi
    return s


class FakeDaemon:
    """GET returns the current state; POST applies Auto ranges (optionally clamped, like dev_app)."""

    def __init__(self, state: dict, clamp_high: float | None = None, lag_reads: int = 0) -> None:
        self.state = state
        self.clamp_high = clamp_high
        self.lag_reads = lag_reads          # GETs that still show the old range after a write
        self.old: dict | None = None
        self.posts: list[dict[str, Any]] = []

    async def get(self, method: str, url: Any, data: Any) -> AiohttpClientMockResponse:
        if self.old is not None and self.lag_reads > 0:
            self.lag_reads -= 1
            return AiohttpClientMockResponse(method, url, json=self.old)
        return AiohttpClientMockResponse(method, url, json=self.state)

    async def post(self, method: str, url: Any, data: Any) -> AiohttpClientMockResponse:
        self.posts.append(data)
        if "target_temp_low" in data:
            hi = data["target_temp_high"]
            if self.clamp_high is not None:
                hi = min(hi, self.clamp_high)
            self.old, self.state = self.state, copy.deepcopy(self.state)
            self.state["dev"].update(zone_lower=float(data["target_temp_low"]), zone_upper=float(hi))
        return AiohttpClientMockResponse(method, url, json={"success": True})


async def _setup(hass, aioclient_mock, daemon: FakeDaemon) -> MockConfigEntry:
    aioclient_mock.get(f"{BASE}/api/state", side_effect=daemon.get)
    aioclient_mock.post(f"{BASE}/api/control", side_effect=daemon.post)
    e = MockConfigEntry(domain=DOMAIN, unique_id=SERIAL, title="Thermostat",
                        data={"host": HOST, "port": 8080, CONF_ADB_PORT: 5555}, options={CONF_AUTO_DEPLOY: True})
    e.add_to_hass(hass)
    await hass.config_entries.async_setup(e.entry_id)
    await hass.async_block_till_done()
    return e


@pytest.fixture(autouse=True)
def fast():
    with (patch("custom_components.aciq_thermostat.coordinator.REFRESH_DELAY", 0),
          patch("custom_components.aciq_thermostat.coordinator.RANGE_VERIFY_TIMEOUT", 0)):
        yield


async def test_range_thermostat_shows_low_high(hass, aioclient_mock, mock_deployer, state) -> None:
    await _setup(hass, aioclient_mock, FakeDaemon(_zone(state)))
    st = hass.states.get(CLIMATE)
    assert st.state == HVACMode.AUTO
    assert st.attributes["target_temp_low"] == 68 and st.attributes["target_temp_high"] == 77
    assert st.attributes.get("temperature") is None
    feats = st.attributes["supported_features"]
    assert feats & ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
    assert not feats & ClimateEntityFeature.TARGET_TEMPERATURE
    assert hass.states.get("sensor.thermostat_auto_setpoint_type").state == "range"
    assert hass.states.get("sensor.thermostat_auto_heat_setpoint").state == "68.0"
    assert hass.states.get("sensor.thermostat_auto_cool_setpoint").state == "77.0"
    assert hass.states.get("sensor.thermostat_deadband").state == "5"


@pytest.mark.parametrize(("zc", "kind"), [(0, "single"), (None, "unknown")])
async def test_single_setpoint_thermostat_unchanged(hass, aioclient_mock, mock_deployer, state, zc, kind) -> None:
    await _setup(hass, aioclient_mock, FakeDaemon(_zone(state, zc=zc, lo=68, hi=79, db=4)))
    st = hass.states.get(CLIMATE)
    assert st.attributes["temperature"] == (79 if zc == 0 else 72)
    assert "target_temp_low" not in st.attributes or st.attributes["target_temp_low"] is None
    assert st.attributes["supported_features"] & ClimateEntityFeature.TARGET_TEMPERATURE
    assert hass.states.get("sensor.thermostat_auto_setpoint_type").state == kind
    # stored zone values are not used in single-setpoint Auto: not shown
    assert hass.states.get("sensor.thermostat_auto_heat_setpoint").state == "unknown"


async def test_heat_mode_keeps_single_setpoint(hass, aioclient_mock, mock_deployer, state) -> None:
    s = _zone(state)
    s.update(mode="heat", setpoint=68)
    d = FakeDaemon(s)
    await _setup(hass, aioclient_mock, d)
    st = hass.states.get(CLIMATE)
    assert st.state == HVACMode.HEAT and st.attributes["temperature"] == 68
    await hass.services.async_call("climate", "set_temperature",
                                   {"entity_id": CLIMATE, "temperature": 69}, blocking=True)
    assert d.posts == [{"temperature": 69, "unit": "F"}]


async def test_set_range_payload_and_readback(hass, aioclient_mock, mock_deployer, state) -> None:
    d = FakeDaemon(_zone(state))
    e = await _setup(hass, aioclient_mock, d)
    await hass.services.async_call("climate", "set_temperature",
                                   {"entity_id": CLIMATE, "target_temp_low": 67, "target_temp_high": 78},
                                   blocking=True)
    assert d.posts == [{"target_temp_low": 67, "target_temp_high": 78, "unit": "F"}]
    st = hass.states.get(CLIMATE)
    assert st.attributes["target_temp_low"] == 67 and st.attributes["target_temp_high"] == 78
    cmd = e.runtime_data.commands[-1]
    assert cmd["result"] == "sent" and cmd["readback"] == "applied"


async def test_set_range_with_mode_in_auto(hass, aioclient_mock, mock_deployer, state) -> None:
    d = FakeDaemon(_zone(state))
    await _setup(hass, aioclient_mock, d)
    await hass.services.async_call("climate", "set_temperature",
                                   {"entity_id": CLIMATE, "hvac_mode": "auto",
                                    "target_temp_low": 66, "target_temp_high": 76}, blocking=True)
    assert d.posts == [{"mode": "auto", "target_temp_low": 66, "target_temp_high": 76, "unit": "F"}]


async def test_single_temperature_when_switching_to_range_auto(hass, aioclient_mock, mock_deployer,
                                                               state) -> None:
    s = _zone(state)
    s["mode"] = "heat"                                    # single setpoint in Heat, range in Auto
    d = FakeDaemon(s)
    await _setup(hass, aioclient_mock, d)
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call("climate", "set_temperature",
                                       {"entity_id": CLIMATE, "hvac_mode": "auto", "temperature": 70},
                                       blocking=True)
    assert err.value.translation_key == "range_required" and d.posts == []


async def test_range_when_switching_away_from_auto(hass, aioclient_mock, mock_deployer, state) -> None:
    d = FakeDaemon(_zone(state))
    await _setup(hass, aioclient_mock, d)
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call("climate", "set_temperature",
                                       {"entity_id": CLIMATE, "hvac_mode": "heat",
                                        "target_temp_low": 66, "target_temp_high": 76}, blocking=True)
    assert err.value.translation_key == "range_unavailable" and d.posts == []


async def test_clamped_range_is_reported(hass, aioclient_mock, mock_deployer, state) -> None:
    d = FakeDaemon(_zone(state), clamp_high=80)
    e = await _setup(hass, aioclient_mock, d)
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call("climate", "set_temperature",
                                       {"entity_id": CLIMATE, "target_temp_low": 68, "target_temp_high": 84},
                                       blocking=True)
    assert err.value.translation_key == "range_not_applied"
    assert err.value.translation_placeholders == {"low": "68", "high": "84", "got_low": "68", "got_high": "80"}
    cmd = e.runtime_data.commands[-1]
    assert cmd["readback"] == "not_applied" and cmd["reported_high"] == 80
    diag = await async_get_config_entry_diagnostics(hass, e)
    assert diag["auto_range"] == {"zone_control": 1, "temp_zone_mode": 1, "deadband": 5, "low": 68.0, "high": 80.0}
    assert diag["commands"][-1]["readback"] == "not_applied"


@pytest.mark.parametrize(("zc", "data"), [
    (1, {"temperature": 70}),                                     # range Auto: no single setpoint
    (0, {"target_temp_low": 66, "target_temp_high": 76}),         # single Auto: no range
])
async def test_mismatched_setpoint_rejected(hass, aioclient_mock, mock_deployer, state, zc, data) -> None:
    d = FakeDaemon(_zone(state, zc=zc))
    await _setup(hass, aioclient_mock, d)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call("climate", "set_temperature", {"entity_id": CLIMATE, **data},
                                       blocking=True)
    assert d.posts == []


async def test_celsius_range(hass, aioclient_mock, mock_deployer, state) -> None:
    hass.config.units = METRIC_SYSTEM
    s = _zone(state, lo=20, hi=25, db=4)
    s.update(temp_unit="C", current_temp=70.0)
    d = FakeDaemon(s)
    await _setup(hass, aioclient_mock, d)
    st = hass.states.get(CLIMATE)
    assert st.attributes["target_temp_low"] == 20 and st.attributes["target_temp_high"] == 25
    assert hass.states.get("sensor.thermostat_auto_heat_setpoint").attributes["unit_of_measurement"] == "°C"
    await hass.services.async_call("climate", "set_temperature",
                                   {"entity_id": CLIMATE, "target_temp_low": 19, "target_temp_high": 24},
                                   blocking=True)
    assert d.posts == [{"target_temp_low": 19, "target_temp_high": 24, "unit": "C"}]


async def test_switching_auto_type_updates_entity_and_logs(hass, aioclient_mock, mock_deployer, state,
                                                          caplog) -> None:
    e = await _setup(hass, aioclient_mock, FakeDaemon(_zone(state, zc=0, hi=79)))
    assert hass.states.get(CLIMATE).attributes["temperature"] == 79
    e.runtime_data.async_set_updated_data(_zone(state, zc=1))          # range turned on at the wall
    await hass.async_block_till_done()
    st = hass.states.get(CLIMATE)
    assert st.attributes["target_temp_low"] == 68 and st.attributes.get("temperature") is None
    assert "now uses a heat/cool range; reload HomeKit Bridge" in caplog.text


async def test_range_reported_a_moment_later(hass, aioclient_mock, mock_deployer, state) -> None:
    d = FakeDaemon(_zone(state), lag_reads=1)
    e = await _setup(hass, aioclient_mock, d)
    with patch("custom_components.aciq_thermostat.coordinator.RANGE_VERIFY_TIMEOUT", 3):
        await hass.services.async_call("climate", "set_temperature",
                                       {"entity_id": CLIMATE, "target_temp_low": 66, "target_temp_high": 76},
                                       blocking=True)
    assert e.runtime_data.commands[-1]["readback"] == "applied"
    assert hass.states.get(CLIMATE).attributes["target_temp_low"] == 66
