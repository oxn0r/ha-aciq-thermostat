"""#15: DHCP discovery moved the entry to an address the thermostat only had briefly. While the
new address stays unreachable, the integration tries the previous one and moves back if the same
thermostat (serial) answers there."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, patch

import aiohttp
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant

from custom_components.aciq_thermostat.const import CONF_ADB_PORT, CONF_AUTO_DEPLOY, DOMAIN
from custom_components.aciq_thermostat.deployer import Check, PreflightResult

from .conftest import HOST, SERIAL

NEW = "192.168.50.117"
TCP = "custom_components.aciq_thermostat.coordinator.tcp_open"


async def _setup(hass: HomeAssistant, aioclient_mock: Any, state: dict, previous: str | None,
                 **extra: Any) -> MockConfigEntry:
    aioclient_mock.get(f"http://{NEW}:8080/api/state", json=state)
    e = MockConfigEntry(domain=DOMAIN, unique_id=SERIAL, title="Thermostat",
                        data={CONF_HOST: NEW, CONF_PORT: 8080, CONF_ADB_PORT: 5555, "previous_host": previous,
                              **extra},
                        options={CONF_AUTO_DEPLOY: True})
    e.add_to_hass(hass)
    await hass.config_entries.async_setup(e.entry_id)
    await hass.async_block_till_done()
    return e


async def _outage(hass: HomeAssistant, aioclient_mock: Any, c: Any, age: float) -> None:
    """New address unreachable, outage started `age` seconds ago."""
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"http://{NEW}:8080/api/state", exc=aiohttp.ClientConnectionError())
    await c.async_refresh()
    c.outage.started = time.monotonic() - age
    await c.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_moves_back_to_previous_address(hass, aioclient_mock, mock_deployer, state, tcp_probe) -> None:
    e = await _setup(hass, aioclient_mock, state, previous=HOST)
    c = e.runtime_data
    with patch(TCP, AsyncMock(return_value=True)) as tcp:
        await _outage(hass, aioclient_mock, c, age=10)               # too early: nothing tried
        tcp.assert_not_awaited()
        await _outage(hass, aioclient_mock, c, age=200)
    assert e.data[CONF_HOST] == HOST and e.data["previous_host"] == NEW
    assert any(ev["event"] == "host_changed" and ev["host"] == HOST for ev in c.events)
    rec = e.data["last_address_change"]
    assert (rec["source"], rec["action"], rec["ip"], rec["old_host"]) == ("fallback", "moved back", HOST, NEW)


async def test_address_seen_event_and_redacted_diagnostics(hass, aioclient_mock, mock_deployer, state,
                                                           tcp_probe) -> None:
    from custom_components.aciq_thermostat.diagnostics import async_get_config_entry_diagnostics
    record = {"source": "dhcp", "ip": "10.9.9.9", "mac": "54926a000000", "old_host": HOST, "hostname": "midea_1"}
    e = await _setup(hass, aioclient_mock, state, previous=HOST, last_address_change=record)
    c = e.runtime_data
    c.address_seen({"time": "t", "source": "dhcp", "ip": "10.9.9.9", "mac": "54926a000000", "action": "kept old address"})
    assert c.events[-1]["event"] == "address_seen" and c.events[-1]["ip"] == "10.9.9.9"
    diag = await async_get_config_entry_diagnostics(hass, e)
    rec = diag["last_address_change"]
    assert rec["hostname"] == "midea_1" and rec["source"] == "dhcp"
    assert rec["ip"] == rec["mac"] == rec["old_host"] == "**REDACTED**"


async def test_other_device_at_previous_address(hass, aioclient_mock, mock_deployer, state, tcp_probe) -> None:
    e = await _setup(hass, aioclient_mock, state, previous=HOST)
    c = e.runtime_data
    mock_deployer[0].return_value = PreflightResult(checks=[Check("root_shell", True, "")], serial="OTHER")
    with patch(TCP, AsyncMock(return_value=True)):
        await _outage(hass, aioclient_mock, c, age=200)
    assert e.data[CONF_HOST] == NEW


async def test_previous_address_silent_then_retry_interval(hass, aioclient_mock, mock_deployer, state,
                                                           tcp_probe) -> None:
    e = await _setup(hass, aioclient_mock, state, previous=HOST)
    c = e.runtime_data
    with patch(TCP, AsyncMock(return_value=False)) as tcp:
        await _outage(hass, aioclient_mock, c, age=200)
        await c.async_refresh()                                       # within the retry interval
        await hass.async_block_till_done(wait_background_tasks=True)
    assert tcp.await_count == 1 and e.data[CONF_HOST] == NEW


async def test_no_previous_address(hass, aioclient_mock, mock_deployer, state, tcp_probe) -> None:
    e = await _setup(hass, aioclient_mock, state, previous=None)
    with patch(TCP, AsyncMock(return_value=True)) as tcp:
        await _outage(hass, aioclient_mock, e.runtime_data, age=200)
    tcp.assert_not_awaited()
