"""Long-poll loop, outage classification/recovery, uptime tracking, egress option (v1.0.3)."""

from __future__ import annotations

import asyncio
import copy
import logging
from typing import Any
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant

from custom_components.aciq_thermostat import coordinator as coord_mod
from custom_components.aciq_thermostat.const import CONF_ADB_PORT, CONF_AUTO_DEPLOY, DOMAIN

from .conftest import HOST, SERIAL

BASE = f"http://{HOST}:8080"


class FakeResp:
    def __init__(self, status: int, data: dict | None) -> None:
        self.status, self._data = status, data
        self.request_info = aiohttp.RequestInfo(aiohttp.client.URL(BASE), "GET", {}, aiohttp.client.URL(BASE))

    async def json(self, content_type: str | None = None) -> dict:
        return self._data or {}

    async def __aenter__(self) -> "FakeResp":
        return self

    async def __aexit__(self, *a: Any) -> None:
        return None


class FakeSession:
    """Scripted responses for GET /api/state?wait=...; blocks forever when the script runs out."""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls: list[dict] = []
        self.idle = asyncio.Event()

    def get(self, url: str, params: dict | None = None) -> Any:
        self.calls.append(params or {})
        if not self.script:
            self.idle.set()
            return _Forever()
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        status, data = item
        return FakeResp(status, data)


class _Forever:
    async def __aenter__(self) -> None:
        await asyncio.Event().wait()

    async def __aexit__(self, *a: Any) -> None:
        return None


async def _setup(hass: HomeAssistant, aioclient_mock: Any, state: dict, options: dict | None = None) -> MockConfigEntry:
    aioclient_mock.get(f"{BASE}/api/state", json=state)
    aioclient_mock.post(f"{BASE}/api/control", json={"success": True})
    e = MockConfigEntry(domain=DOMAIN, unique_id=SERIAL, title="Thermostat",
                        data={CONF_HOST: HOST, CONF_PORT: 8080, CONF_ADB_PORT: 5555},
                        options=options or {CONF_AUTO_DEPLOY: True})
    e.add_to_hass(hass)
    await hass.config_entries.async_setup(e.entry_id)
    await hass.async_block_till_done()
    return e


async def _run_loop(c: Any, session: FakeSession) -> None:
    c.session = session
    task = asyncio.ensure_future(c._long_poll_loop())
    try:
        await asyncio.wait_for(session.idle.wait(), 5)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def _with(state: dict, **changes: Any) -> dict:
    s = copy.deepcopy(state)
    s.update(changes)
    return s


# ------------------------------------------------------------------ long-poll

async def test_long_poll_returns_on_change(hass, aioclient_mock, mock_deployer, state) -> None:
    e = await _setup(hass, aioclient_mock, _with(state, state_id="aaaa0001"))
    c = e.runtime_data
    changed = _with(state, state_id="aaaa0002", setpoint=70, mode="heat")
    session = FakeSession([(200, changed)])
    await _run_loop(c, session)
    await hass.async_block_till_done()
    assert session.calls[0] == {"wait": "aaaa0001", "timeout": "25"}
    assert session.calls[1]["wait"] == "aaaa0002"               # next wait uses the new id
    st = hass.states.get("climate.thermostat")
    assert st.state == "heat" and st.attributes["temperature"] == 70
    assert c.long_poll_supported is True


async def test_long_poll_timeout_without_change(hass, aioclient_mock, mock_deployer, state) -> None:
    e = await _setup(hass, aioclient_mock, _with(state, state_id="bbbb0001"))
    same = _with(state, state_id="bbbb0001")
    session = FakeSession([(200, same), (200, same)])
    await _run_loop(e.runtime_data, session)
    assert [p["wait"] for p in session.calls] == ["bbbb0001"] * 3


async def test_long_poll_old_daemon_404_falls_back(hass, aioclient_mock, mock_deployer, state, caplog) -> None:
    e = await _setup(hass, aioclient_mock, _with(state, state_id="cccc0001"))
    c = e.runtime_data
    session = FakeSession([(404, None)])
    with patch.object(coord_mod, "DEFAULT_SCAN_INTERVAL", 0):
        c.session = session
        task = asyncio.ensure_future(c._long_poll_loop())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert c.long_poll_supported is False
    assert len(session.calls) == 1                               # stops long-polling
    assert "no long-poll support" in caplog.text


async def test_long_poll_old_daemon_without_state_id(hass, aioclient_mock, mock_deployer, state) -> None:
    old = copy.deepcopy(state)
    old.pop("state_id", None)
    e = await _setup(hass, aioclient_mock, _with(state, state_id="dddd0001"))
    c = e.runtime_data
    with patch.object(coord_mod, "DEFAULT_SCAN_INTERVAL", 0):
        c.session = FakeSession([(200, old)])
        task = asyncio.ensure_future(c._long_poll_loop())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert c.long_poll_supported is False


async def test_long_poll_daemon_restart_mid_wait(hass, aioclient_mock, mock_deployer, state) -> None:
    """Connection dropped mid-wait: kick the watchdog, back off, resume."""
    e = await _setup(hass, aioclient_mock, _with(state, state_id="eeee0001"))
    c = e.runtime_data
    back = _with(state, state_id="eeee0002", setpoint=69)
    session = FakeSession([aiohttp.ServerDisconnectedError(), (200, back)])
    with (patch.object(c, "async_request_refresh", AsyncMock()) as refresh,
          patch.object(coord_mod.asyncio, "sleep", AsyncMock())):
        await _run_loop(c, session)
    refresh.assert_awaited()
    assert c.data["setpoint"] == 69


async def test_long_poll_waits_during_outage(hass, aioclient_mock, mock_deployer, state) -> None:
    e = await _setup(hass, aioclient_mock, _with(state, state_id="ffff0001"))
    c = e.runtime_data
    c.last_update_success = False
    c.session = FakeSession([])
    with patch.object(c, "async_request_refresh", AsyncMock()) as refresh:
        task = asyncio.ensure_future(c._long_poll_loop())
        await asyncio.sleep(1.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    refresh.assert_awaited()                                     # outage: re-check, no long-poll
    assert c.session.calls == []


async def test_control_then_immediate_refresh(hass, aioclient_mock, mock_deployer, state) -> None:
    e = await _setup(hass, aioclient_mock, state)
    with patch.object(coord_mod, "REFRESH_DELAY", 0):
        await hass.services.async_call("climate", "set_temperature",
                                       {"entity_id": "climate.thermostat", "temperature": 71}, blocking=True)
    methods = [c[0] for c in aioclient_mock.mock_calls]
    assert methods[-2:] == ["POST", "GET"]


async def test_long_poll_started_at_setup(hass, aioclient_mock, mock_deployer, state) -> None:
    with patch.object(coord_mod.AciqCoordinator, "async_start_long_poll") as start:
        await _setup(hass, aioclient_mock, state)
    start.assert_called_once()


async def test_start_long_poll_creates_one_task(hass, aioclient_mock, mock_deployer, state,
                                                no_background_long_poll) -> None:
    e = await _setup(hass, aioclient_mock, state)
    c = e.runtime_data
    real_start = no_background_long_poll.temp_original
    c.session = FakeSession([])
    real_start(c)
    first = c._lp_task
    real_start(c)                                   # second call: same task, no duplicate loop
    assert c._lp_task is first and not first.done()
    await hass.config_entries.async_unload(e.entry_id)
    await hass.async_block_till_done()
    assert first.done()                             # cancelled with the entry


# ------------------------------------------------------------------ outages

@pytest.mark.parametrize(
    ("probe", "kind", "deploys", "force"),
    [
        ({8080: "timeout", 5555: "timeout"}, "network_down", 0, None),
        ({8080: "refused", 5555: "open"}, "daemon_gone", 1, False),
        ({8080: "open", 5555: "open"}, "daemon_hung", 0, None),   # first failure: wait
        ({8080: "refused", 5555: "timeout"}, "network_down", 0, None),
    ],
)
async def test_outage_classification(hass, aioclient_mock, mock_deployer, state, tcp_probe, caplog,
                                     probe, kind, deploys, force) -> None:
    e = await _setup(hass, aioclient_mock, state)
    c = e.runtime_data
    _, ensure = mock_deployer
    before = ensure.await_count
    c.last_deploy_attempt = None
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{BASE}/api/state", exc=TimeoutError())
    tcp_probe.result = probe
    caplog.set_level(logging.WARNING)
    with patch.object(coord_mod.asyncio, "sleep", AsyncMock()):
        await c.async_refresh()
    assert not c.last_update_success
    assert c.outage is not None and c.outage.kind == kind
    assert ensure.await_count - before == deploys
    if force is not None:
        assert ensure.call_args.kwargs["force"] is force
    assert "TimeoutError" in caplog.text                          # reason is never empty
    assert f"TCP 8080 {probe[8080]}, ADB 5555 {probe[5555]}" in caplog.text


async def test_hung_daemon_force_restart_on_second_failure(hass, aioclient_mock, mock_deployer, state, tcp_probe) -> None:
    e = await _setup(hass, aioclient_mock, state)
    c = e.runtime_data
    _, ensure = mock_deployer
    c.last_deploy_attempt = None
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{BASE}/api/state", exc=TimeoutError())
    tcp_probe.result = {8080: "open", 5555: "open"}
    before = ensure.await_count
    with patch.object(coord_mod.asyncio, "sleep", AsyncMock()):
        await c.async_refresh()
        await c.async_refresh()
    assert ensure.await_count == before + 1 and ensure.call_args.kwargs["force"] is True


async def test_outage_logged_once_and_recovery(hass, aioclient_mock, mock_deployer, state, tcp_probe, caplog) -> None:
    e = await _setup(hass, aioclient_mock, state)
    c = e.runtime_data
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{BASE}/api/state", exc=TimeoutError())
    caplog.set_level(logging.INFO)
    for _ in range(3):
        await c.async_refresh()
    assert caplog.text.count("unavailable:") == 1
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{BASE}/api/state", json=state)
    await c.async_refresh()
    assert c.last_update_success and c.outage is None
    assert "back after" in caplog.text
    kinds = [ev["event"] for ev in c.events]
    assert "outage" in kinds and kinds[-1] == "recovered"


async def test_http_error_is_not_probed(hass, aioclient_mock, mock_deployer, state, tcp_probe) -> None:
    e = await _setup(hass, aioclient_mock, state)
    c = e.runtime_data
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{BASE}/api/state", status=500)
    with patch.object(c, "_tcp", AsyncMock()) as tcp:
        await c.async_refresh()
    tcp.assert_not_awaited()
    assert c.outage.kind == "api_error"


async def test_real_tcp_probe(hass, aioclient_mock, mock_deployer, state, socket_enabled) -> None:
    """_tcp itself against a local listener (open), a closed port (refused) and a timeout."""
    e = await _setup(hass, aioclient_mock, state)
    c = e.runtime_data
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    c.host = "127.0.0.1"
    assert await c._tcp(port) == "open"
    server.close()
    await server.wait_closed()
    assert await c._tcp(port) == "refused"
    with patch.object(coord_mod.asyncio, "open_connection", AsyncMock(side_effect=TimeoutError())):
        assert await c._tcp(port) == "timeout"


# ------------------------------------------------------------------ uptime / sensors / egress

async def test_boot_and_daemon_times(hass, aioclient_mock, mock_deployer, state) -> None:
    from homeassistant.helpers import entity_registry as er
    s = _with(state, uptime_s=3600, daemon_uptime_s=600)
    e = await _setup(hass, aioclient_mock, s)
    c = e.runtime_data
    first = c.boot_time
    assert first is not None and c.daemon_start_time is not None
    assert (c.daemon_start_time - first).total_seconds() == pytest.approx(3000, abs=2)
    c._track_times(_with(state, uptime_s=3660, daemon_uptime_s=660))   # 60 s later: same boot
    assert c.boot_time == first
    c._track_times(_with(state, uptime_s=30, daemon_uptime_s=5))        # rebooted
    assert c.boot_time != first
    ent = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{SERIAL}_last_boot")
    assert hass.states.get(ent).attributes["device_class"] == "timestamp"


async def test_auto_state_sensor_none_outside_auto(hass, aioclient_mock, mock_deployer, state) -> None:
    from homeassistant.helpers import entity_registry as er
    s = copy.deepcopy(state)
    s["mode"] = "heat"
    s["dev"]["auto_state"] = 1                       # stale "cool" left over from Auto
    s["ui"] = {"heat_setpoint": 72, "cool_setpoint": 71}
    e = await _setup(hass, aioclient_mock, s)
    reg = er.async_get(hass)
    ent = reg.async_get_entity_id("sensor", DOMAIN, f"{SERIAL}_auto_state")
    reg.async_update_entity(ent, disabled_by=None)
    await hass.config_entries.async_reload(e.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(ent).state == "none"
    clim = hass.states.get("climate.thermostat")
    assert clim.attributes["temperature"] == 72      # ui.cool_setpoint ignored outside Auto


async def test_egress_option_passed_to_deploy(hass, aioclient_mock, mock_deployer, state) -> None:
    _, ensure = mock_deployer
    await _setup(hass, aioclient_mock, state, options={CONF_AUTO_DEPLOY: True, "block_egress": True})
    assert ensure.call_args.kwargs["block_egress"] is True


async def test_block_cloud_switch_toggles_option(hass, aioclient_mock, mock_deployer, state) -> None:
    _, ensure = mock_deployer
    e = await _setup(hass, aioclient_mock, state)
    st = hass.states.get("switch.thermostat_block_cloud_traffic")
    assert st.state == "off"
    await hass.services.async_call("switch", "turn_on", {"entity_id": "switch.thermostat_block_cloud_traffic"},
                                   blocking=True)
    await hass.async_block_till_done()
    assert e.options["block_egress"] is True
    assert ensure.call_args.kwargs["block_egress"] is True          # reload applied it
    assert hass.states.get("switch.thermostat_block_cloud_traffic").state == "on"
    await hass.services.async_call("switch", "turn_on", {"entity_id": "switch.thermostat_block_cloud_traffic"},
                                   blocking=True)                     # no-op when already on
    await hass.services.async_call("switch", "turn_off", {"entity_id": "switch.thermostat_block_cloud_traffic"},
                                   blocking=True)
    await hass.async_block_till_done()
    assert e.options["block_egress"] is False and ensure.call_args.kwargs["block_egress"] is False


async def test_egress_verified_after_reboot_and_periodically(hass, aioclient_mock, mock_deployer, state, caplog) -> None:
    from datetime import timedelta
    from homeassistant.util import dt as dt_util
    with patch("custom_components.aciq_thermostat.deployer.Deployer.verify_egress",
               AsyncMock(return_value="ok")) as verify:
        e = await _setup(hass, aioclient_mock, _with(state, uptime_s=50000),
                         options={CONF_AUTO_DEPLOY: True, "block_egress": True})
        c = e.runtime_data
        await hass.async_block_till_done()
        verify.assert_not_awaited()                     # just applied by the deploy at setup
        assert c.egress_verified_at is not None

        caplog.set_level(logging.WARNING)
        c._track_times(_with(state, uptime_s=40))       # reboot detected
        await hass.async_block_till_done()
        assert verify.await_count == 1
        assert "rebooted" in caplog.text and any(ev["event"] == "reboot" for ev in c.events)

        c._track_times(_with(state, uptime_s=100))       # soon after: no new check
        await hass.async_block_till_done()
        assert verify.await_count == 1
        c.egress_verified_at = dt_util.utcnow() - timedelta(seconds=1900)
        c._track_times(_with(state, uptime_s=160))       # 30 min elapsed: periodic check
        await hass.async_block_till_done()
        assert verify.await_count == 2

        verify.return_value = "reapplied"
        c._egress_check_due = True
        c._track_times(_with(state, uptime_s=220))
        await hass.async_block_till_done()
        assert "was missing and has been re-applied" in caplog.text


async def test_egress_check_failure_retried(hass, aioclient_mock, mock_deployer, state, caplog) -> None:
    from custom_components.aciq_thermostat.deployer import DeployError
    with patch("custom_components.aciq_thermostat.deployer.Deployer.verify_egress",
               AsyncMock(side_effect=DeployError("adb busy"))):
        e = await _setup(hass, aioclient_mock, state, options={CONF_AUTO_DEPLOY: True, "block_egress": True})
        c = e.runtime_data
        c._egress_check_due = True
        c._maybe_verify_egress()
        await hass.async_block_till_done()
    assert "Could not verify the cloud-traffic block" in caplog.text
    assert c._egress_check_due is True                 # retried on the next poll


async def test_no_egress_checks_when_off(hass, aioclient_mock, mock_deployer, state) -> None:
    with patch("custom_components.aciq_thermostat.deployer.Deployer.verify_egress", AsyncMock()) as verify:
        e = await _setup(hass, aioclient_mock, _with(state, uptime_s=50000))
        e.runtime_data._track_times(_with(state, uptime_s=10))     # reboot, but block is off
        await hass.async_block_till_done()
    verify.assert_not_awaited()


async def test_switch_attributes(hass, aioclient_mock, mock_deployer, state) -> None:
    e = await _setup(hass, aioclient_mock, state, options={CONF_AUTO_DEPLOY: True, "block_egress": True})
    e.runtime_data.deployer.last_egress = {"enabled": True, "dropped_packets": 109}
    e.runtime_data.async_update_listeners()
    attrs = hass.states.get("switch.thermostat_block_cloud_traffic").attributes
    assert attrs["rule_active"] is True and attrs["dropped_packets"] == 109 and attrs["last_verified"]
