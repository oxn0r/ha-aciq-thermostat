"""ADB unavailable while the daemon keeps running (#14): start and keep control, repair notice,
recovery once ADB answers again."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import aiohttp
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir

from custom_components.aciq_thermostat.const import CONF_ADB_PORT, CONF_AUTO_DEPLOY, CONF_BLOCK_EGRESS, DOMAIN
from custom_components.aciq_thermostat.deployer import DeployError
from custom_components.aciq_thermostat.diagnostics import async_get_config_entry_diagnostics

from .conftest import HOST, SERIAL

BASE = f"http://{HOST}:8080"


async def _setup(hass: HomeAssistant, aioclient_mock: Any, state: dict | None, **options: Any) -> MockConfigEntry:
    if state is None:
        aioclient_mock.get(f"{BASE}/api/state", exc=aiohttp.ClientConnectionError())
    else:
        aioclient_mock.get(f"{BASE}/api/state", json=state)
    aioclient_mock.post(f"{BASE}/api/control", json={"success": True})
    e = MockConfigEntry(domain=DOMAIN, unique_id=SERIAL, title="Thermostat",
                        data={CONF_HOST: HOST, CONF_PORT: 8080, CONF_ADB_PORT: 5555},
                        options={CONF_AUTO_DEPLOY: True, **options})
    e.add_to_hass(hass)
    await hass.config_entries.async_setup(e.entry_id)
    await hass.async_block_till_done()
    return e


def _issue(hass: HomeAssistant, kind: str, e: MockConfigEntry) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, f"{kind}_{e.entry_id}")


async def test_setup_on_running_daemon_when_adb_down(hass, aioclient_mock, mock_deployer, state, tcp_probe) -> None:
    mock_deployer[1].side_effect = DeployError("ADB connect failed: Connection refused")
    tcp_probe.result = {8080: "open", 5555: "refused"}
    e = await _setup(hass, aioclient_mock, state)
    assert e.state is ConfigEntryState.LOADED
    c = e.runtime_data
    assert c.adb_down
    assert _issue(hass, "adb_unavailable", e) is not None
    assert _issue(hass, "deploy_failed", e) is None          # replaced by the ADB notice
    assert hass.states.get("climate.thermostat").state == state["mode"]      # control on the running daemon
    assert [ev["event"] for ev in c.events][-1] == "adb_unavailable"
    with patch("custom_components.aciq_thermostat.deployer.Deployer.preflight") as pre:
        diag = await async_get_config_entry_diagnostics(hass, e)
    pre.assert_not_called()                                   # no waiting on missing ADB
    assert diag["adb_unavailable"] is True and diag["preflight"] == {"error": "ADB unavailable"}


async def test_setup_retries_when_daemon_down_too(hass, aioclient_mock, mock_deployer, tcp_probe) -> None:
    mock_deployer[1].side_effect = DeployError("ADB connect failed")
    tcp_probe.result = {8080: "refused", 5555: "refused"}
    e = await _setup(hass, aioclient_mock, None)
    assert e.state is ConfigEntryState.SETUP_RETRY
    assert _issue(hass, "adb_unavailable", e) is None
    assert _issue(hass, "deploy_failed", e) is not None


async def test_setup_retries_on_other_deploy_error(hass, aioclient_mock, mock_deployer, state, tcp_probe) -> None:
    """ADB answers (e.g. a failed check): not the #14 case, keep the deploy error."""
    mock_deployer[1].side_effect = DeployError("pre-flight failed")
    tcp_probe.result = {8080: "open", 5555: "open"}
    e = await _setup(hass, aioclient_mock, state)
    assert e.state is ConfigEntryState.SETUP_RETRY
    assert _issue(hass, "adb_unavailable", e) is None


async def test_adb_down_skips_adb_work_then_recovers(hass, aioclient_mock, mock_deployer, state, tcp_probe) -> None:
    mock_deployer[1].side_effect = DeployError("Connection refused")
    tcp_probe.result = {8080: "open", 5555: "refused"}
    e = await _setup(hass, aioclient_mock, state, **{CONF_BLOCK_EGRESS: True})
    c = e.runtime_data
    with (patch("custom_components.aciq_thermostat.deployer.Deployer.sync_clock") as clock,
          patch("custom_components.aciq_thermostat.deployer.Deployer.verify_egress") as egress):
        await c.async_refresh()
        await hass.async_block_till_done(wait_background_tasks=True)
        clock.assert_not_called()
        egress.assert_not_called()
    assert mock_deployer[1].await_count == 1                  # not retried before the interval

    c._adb_next_check = 0                                     # retry due, ADB still closed
    await c.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert c.adb_down and mock_deployer[1].await_count == 1   # only a TCP check

    tcp_probe.result = {8080: "open", 5555: "open"}           # thermostat power-cycled
    mock_deployer[1].side_effect = None
    mock_deployer[1].return_value = "started pid 12 (pushed new build)"
    c._adb_next_check = 0
    await c.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert not c.adb_down
    assert _issue(hass, "adb_unavailable", e) is None
    events = [ev["event"] for ev in c.events]
    assert events[-2:] == ["deploy", "adb_recovered"]


async def test_buttons_point_to_notice(hass, aioclient_mock, mock_deployer, state, tcp_probe) -> None:
    mock_deployer[1].side_effect = DeployError("Connection refused")
    tcp_probe.result = {8080: "open", 5555: "refused"}
    await _setup(hass, aioclient_mock, state)
    mock_deployer[0].side_effect = DeployError("Connection refused")
    for button in ("button.thermostat_redeploy_daemon", "button.thermostat_collect_debug_report"):
        with pytest.raises(HomeAssistantError) as err:
            await hass.services.async_call("button", "press", {"entity_id": button}, blocking=True)
        assert err.value.translation_key == "adb_unavailable", button


async def test_runtime_adb_loss_raises_notice(hass, aioclient_mock, mock_deployer, state, tcp_probe) -> None:
    """ADB goes away later: the next ADB job (clock check) notices it."""
    e = await _setup(hass, aioclient_mock, state)
    c = e.runtime_data
    assert not c.adb_down
    tcp_probe.result = {8080: "open", 5555: "refused"}
    with patch("custom_components.aciq_thermostat.deployer.Deployer.sync_clock",
               side_effect=DeployError("Connection refused")):
        await c.async_check_clock()
    assert c.adb_down and _issue(hass, "adb_unavailable", e) is not None
