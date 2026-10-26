"""Issue #5: thermostat wired for conventional 24 V (dev_24v_decorder, no AHU3 lines)."""
from __future__ import annotations

import copy
import logging

import pytest

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import issue_registry as ir

from custom_components.aciq_thermostat.const import CONF_ADB_PORT, CONF_AUTO_DEPLOY, DOMAIN
from custom_components.aciq_thermostat import coordinator
from .conftest import HOST, SERIAL

BASE = f"http://{HOST}:8080"

# what the 1.1 daemon reports for the issue #5 thermostat
V24 = {"seen": True, "unit": "F", "room": 71.2, "setpoint": 68.0, "low": 67.0, "high": 74.0,
       "current_mode": None, "op_mode": "77", "heat_mode": "6", "wind_fun": "1", "terminal_set_mode": "1",
       "terminal_state": "76", "terminal_state_a3": "1f6", "terminal_config": "202", "emer_heat": "0",
       "aux": "1", "ob_dir": "2", "afterheat": "0",
       "terminal_io": "PEK:0 O/B:1 G:1 W2:0 W1:1 Y2:1 Y1:1 C:1 RC:1 RH:0"}


def _24v(state: dict) -> dict:
    s = copy.deepcopy(state)
    s.update(mode="unknown", comm_mode="24v", v24=V24)
    s["dev"]["seen"] = False
    s["equipment"] = {"available": False}
    return s


@pytest.fixture(autouse=True)
def fresh_log_memory():
    coordinator._V24_LOGGED.clear()


async def _setup(hass, aioclient_mock, st) -> MockConfigEntry:
    aioclient_mock.get(f"{BASE}/api/state", json=st)
    e = MockConfigEntry(domain=DOMAIN, unique_id=SERIAL, title="Thermostat",
                        data={"host": HOST, "port": 8080, CONF_ADB_PORT: 5555}, options={CONF_AUTO_DEPLOY: True})
    e.add_to_hass(hass)
    await hass.config_entries.async_setup(e.entry_id)
    await hass.async_block_till_done()
    return e


async def test_24v_thermostat_gets_clear_repair_issue(hass, aioclient_mock, mock_deployer, state, caplog) -> None:
    caplog.set_level(logging.WARNING)
    e = await _setup(hass, aioclient_mock, _24v(state))
    assert e.state is ConfigEntryState.SETUP_RETRY
    assert "conventional 24 V mode" in e.reason
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"conventional_24v_{e.entry_id}")
    assert issue is not None and issue.severity == ir.IssueSeverity.ERROR
    assert issue.learn_more_url.endswith("/issues/5")
    assert issue.translation_placeholders["summary"] == (
        "setpoint 68.0, Auto 67.0-74.0, op_mode 77, heat_mode 6, terminal_state 76")
    assert "runs in conventional 24 V mode" in caplog.text and "'terminal_io': 'PEK:0" in caplog.text
    # setup retries with the same state don't repeat the warning
    caplog.clear()
    await hass.config_entries.async_reload(e.entry_id)
    await hass.async_block_till_done()
    assert e.state is ConfigEntryState.SETUP_RETRY
    assert "runs in conventional 24 V mode" not in caplog.text


async def test_24v_before_state_lines(hass, aioclient_mock, mock_deployer, state) -> None:
    s = _24v(state)
    s["v24"] = {"seen": False}                       # only meiju's comm_mode=1 seen so far
    e = await _setup(hass, aioclient_mock, s)
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"conventional_24v_{e.entry_id}")
    assert issue.translation_placeholders["summary"] == "no 24 V state lines yet"


async def test_communicating_thermostat_has_no_24v_issue(hass, aioclient_mock, mock_deployer, state) -> None:
    s = copy.deepcopy(state)
    s.update(comm_mode="communicating", v24={"seen": False})
    e = await _setup(hass, aioclient_mock, s)
    assert e.state is ConfigEntryState.LOADED
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"conventional_24v_{e.entry_id}") is None


async def test_unknown_state_is_not_called_24v(hass, aioclient_mock, mock_deployer, state) -> None:
    s = copy.deepcopy(state)
    s["dev"]["seen"] = False                          # older daemon, or no state lines yet
    e = await _setup(hass, aioclient_mock, s)
    assert e.state is ConfigEntryState.SETUP_RETRY and "has not seen" in e.reason
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"conventional_24v_{e.entry_id}") is None
