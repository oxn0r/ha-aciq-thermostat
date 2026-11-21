"""Error and edge paths of the 1.4 changes: the TCP probe helper, ADB coming back with a failing
deploy, the previous address failing its check, and the indoor-coil-only defrost fallback."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest_socket

from custom_components.aciq_thermostat.const import CONF_AUTO_DEPLOY
from custom_components.aciq_thermostat.deployer import DeployError, tcp_open

from .test_adb_fallback import _issue, _setup as _setup_adb
from .test_defrost import SENSOR, _feed, _setup as _setup_defrost, _state
from .test_host_recovery import TCP, _outage, _setup as _setup_host
from .conftest import HOST


async def test_tcp_open_real(socket_enabled) -> None:
    pytest_socket.socket_allow_hosts(["127.0.0.1"])
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    assert await tcp_open("127.0.0.1", port) is True
    server.close()
    await server.wait_closed()
    assert await tcp_open("127.0.0.1", port) is False                      # refused
    with patch("custom_components.aciq_thermostat.deployer.asyncio.open_connection",
               AsyncMock(side_effect=TimeoutError())):
        assert await tcp_open("127.0.0.1", port, 0.1) is False             # timeout


async def test_adb_back_but_deploy_fails(hass, aioclient_mock, mock_deployer, state, tcp_probe) -> None:
    mock_deployer[1].side_effect = DeployError("Connection refused")
    tcp_probe.result = {8080: "open", 5555: "refused"}
    e = await _setup_adb(hass, aioclient_mock, state)
    c = e.runtime_data
    tcp_probe.result = {8080: "open", 5555: "open"}              # ADB port answers again...
    mock_deployer[1].side_effect = DeployError("pre-flight failed")   # ...but the deploy fails
    c._adb_next_check = 0
    await c.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert c.adb_down and _issue(hass, "adb_unavailable", e) is not None   # still flagged, notice kept


async def test_adb_back_without_auto_deploy(hass, aioclient_mock, mock_deployer, state, tcp_probe) -> None:
    mock_deployer[1].side_effect = DeployError("Connection refused")
    tcp_probe.result = {8080: "open", 5555: "refused"}
    e = await _setup_adb(hass, aioclient_mock, state)
    c = e.runtime_data
    hass.config_entries.async_update_entry(e, options={**e.options, CONF_AUTO_DEPLOY: False})
    await hass.async_block_till_done()
    c = e.runtime_data
    if not c.adb_down:                                            # options change may reload the entry
        c._adb_unavailable("test")
    tcp_probe.result = {8080: "open", 5555: "open"}
    c._adb_next_check = 0
    await c.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert not c.adb_down and _issue(hass, "adb_unavailable", e) is None


async def test_previous_address_check_fails(hass, aioclient_mock, mock_deployer, state, tcp_probe) -> None:
    e = await _setup_host(hass, aioclient_mock, state, previous=HOST)
    c = e.runtime_data
    mock_deployer[0].side_effect = DeployError("ADB auth failed")
    with patch(TCP, AsyncMock(return_value=True)):
        await _outage(hass, aioclient_mock, c, age=200)
    assert e.data["host"] != HOST                                 # stays: identity not confirmed


async def test_coil_only_fallback_holds_until_55(hass, aioclient_mock, mock_deployer, state) -> None:
    e = await _setup_defrost(hass, aioclient_mock, _state(state, 70, 40, 0))
    c = e.runtime_data
    for coil, expect in ((35, "on"), (50, "on"), (56, "off")):
        await _feed(hass, aioclient_mock, c, _state(state, coil, 40, 0))   # no outdoor coil reading
        assert hass.states.get(SENSOR).state == expect, coil
