"""Live end-to-end test against a real thermostat. Run only with ACIQ_LIVE_HOST set:

    ACIQ_LIVE_HOST=10.0.2.183 pytest tests/live -p no:cacheprovider

Changes nothing on the HVAC except one no-op command (current setpoint re-sent).
"""

from __future__ import annotations

import os

import pytest

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from custom_components.aciq_thermostat.const import CONF_EXTRA_ALLOWED_IPS, DOMAIN

HOST = os.environ.get("ACIQ_LIVE_HOST")
pytestmark = pytest.mark.skipif(not HOST, reason="set ACIQ_LIVE_HOST to run live tests")


async def test_live_end_to_end(hass: HomeAssistant, socket_enabled) -> None:
    import pytest_socket
    pytest_socket.socket_allow_hosts(["127.0.0.1", HOST], allow_unix_socket=True)
    assert await async_setup_component(hass, "network", {})
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": HOST, "name": "Thermostat", "port": 8080, "adb_port": 5555})
    print("\nPRE-FLIGHT:\n" + result["description_placeholders"]["checks"])
    assert result["step_id"] == "confirm", result
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    await hass.async_block_till_done()

    # keep HA (10.0.0.2) and the Mac's second IP allowed, like the hand-started daemon
    hass.config_entries.async_update_entry(entry, options={**entry.options,
                                           CONF_EXTRA_ALLOWED_IPS: "10.0.0.2, 10.0.0.40"})
    await hass.async_block_till_done()
    entry = hass.config_entries.async_get_entry(entry.entry_id)
    assert entry.state is config_entries.ConfigEntryState.LOADED
    coord = entry.runtime_data
    print("DEPLOY:", coord.last_deploy_result, "allowlist:", coord.allowed_ips())
    print("VERSIONS:", coord.versions)

    ents = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    print(f"ENTITIES ({len(ents)}):")
    for e in sorted(ents, key=lambda e: e.entity_id):
        st = hass.states.get(e.entity_id)
        val = st.state if st else "(disabled)"
        unit = st.attributes.get("unit_of_measurement", "") if st else ""
        print(f"  {e.entity_id:55s} {val} {unit}")
    clim = hass.states.get("climate.thermostat")
    print("CLIMATE ATTRS:", {k: clim.attributes.get(k) for k in
                             ("hvac_action", "temperature", "current_temperature", "current_humidity",
                              "fan_mode", "preset_mode", "hvac_modes")})
    assert clim.state in ("auto", "heat", "cool", "off", "fan_only")
    assert hass.states.get("sensor.thermostat_outdoor_temperature").state not in ("unavailable", "unknown")

    # no-op control: re-send the current setpoint through HA
    sp = clim.attributes["temperature"]
    await hass.services.async_call("climate", "set_temperature",
                                   {"entity_id": "climate.thermostat", "temperature": sp}, blocking=True)
    assert hass.states.get("climate.thermostat").attributes["temperature"] == sp
    print("NO-OP CONTROL OK, setpoint", sp)

    assert await hass.config_entries.async_unload(entry.entry_id)
    # daemon must still be running after HA unloads (it lives on the thermostat)
    from custom_components.aciq_thermostat.deployer import Deployer
    left = await Deployer(HOST, 5555).preflight()
    assert any(c.key == "api_port" and "thermostat_api" in c.detail for c in left.checks)
