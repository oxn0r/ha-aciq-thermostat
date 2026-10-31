"""Option 'Hide fan speed on the thermostat' (off by default): no fan speed on the climate entity."""
from __future__ import annotations

import pytest

from homeassistant.components.climate import ClimateEntityFeature

from custom_components.aciq_thermostat.const import CONF_HIDE_FAN
from .test_dry import CLIMATE, FakeDaemon, _comm, _setup


@pytest.mark.parametrize(("options", "shown"), [({}, True), ({CONF_HIDE_FAN: False}, True),
                                                ({CONF_HIDE_FAN: True}, False)])
async def test_fan_speed_option(hass, aioclient_mock, mock_deployer, state, options, shown) -> None:
    await _setup(hass, aioclient_mock, FakeDaemon(_comm(state, "cool", 75)), **options)
    st = hass.states.get(CLIMATE)
    assert bool(st.attributes["supported_features"] & ClimateEntityFeature.FAN_MODE) is shown
    assert ("fan_mode" in st.attributes) is shown
    # setpoints are unaffected (unlike overriding supported_features in customize)
    assert st.attributes["supported_features"] & ClimateEntityFeature.TARGET_TEMPERATURE
    assert st.attributes["temperature"] == 75
    assert hass.states.get("sensor.thermostat_fan_setting") is not None


async def test_hidden_fan_speed_rejects_set_fan_mode(hass, aioclient_mock, mock_deployer, state) -> None:
    d = FakeDaemon(_comm(state, "cool", 75))
    await _setup(hass, aioclient_mock, d, **{CONF_HIDE_FAN: True})
    with pytest.raises(Exception):
        await hass.services.async_call("climate", "set_fan_mode", {"entity_id": CLIMATE, "fan_mode": "low"},
                                       blocking=True)
    assert d.posts == []
