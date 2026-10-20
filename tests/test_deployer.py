"""Deployer against the simulated thermostat."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from custom_components.aciq_thermostat.deployer import Deployer, DeployError

from .fake_adb import BUNDLED_SHA, FakeThermostat


@pytest.fixture
def thermo():
    t = FakeThermostat()
    with (
        patch("custom_components.aciq_thermostat.deployer.AdbDeviceTcpAsync", t.factory),
        patch("custom_components.aciq_thermostat.deployer.asyncio.sleep", AsyncMock()),
    ):
        yield t


async def test_preflight_pass(thermo: FakeThermostat) -> None:
    res = await Deployer("10.0.2.183", 5555).preflight()
    assert res.passed and res.serial == thermo.serial
    assert "✅ firmware" in res.summary()
    assert res.as_dict()["passed"] is True


async def test_preflight_wrong_firmware(thermo: FakeThermostat) -> None:
    thermo.firmware = "tina.other"
    res = await Deployer("h", 5555).preflight()
    assert not res.passed
    assert "❌ firmware" in res.summary()


async def test_connect_retry_then_ok(thermo: FakeThermostat) -> None:
    thermo.connect_raises = 1
    assert (await Deployer("h", 5555).preflight()).passed


async def test_connect_fails_twice(thermo: FakeThermostat) -> None:
    thermo.connect_raises = 2
    with pytest.raises(DeployError, match="ADB connect"):
        await Deployer("h", 5555).preflight()


async def test_connect_refused(thermo: FakeThermostat) -> None:
    thermo.connect_ok = False
    with pytest.raises(DeployError, match="refused"):
        await Deployer("h", 5555).preflight()


async def test_deploy_fresh_pushes_and_starts(thermo: FakeThermostat) -> None:
    d = Deployer("h", 5555)
    res = await d.ensure_daemon(["10.0.0.40"])
    assert res == "started pid 4242 (pushed new build)"
    assert thermo.pushes == 1 and thermo.daemon_sha == BUNDLED_SHA
    assert d.last_allowlist == ["10.0.0.2", "10.0.0.40"]
    assert thermo.daemon_cmdline == "/tmp/thermostat_api -d -a 10.0.0.2 -a 10.0.0.40"


async def test_deploy_idempotent(thermo: FakeThermostat) -> None:
    d = Deployer("h", 5555)
    await d.ensure_daemon([])
    assert await d.ensure_daemon([]) == "already running"
    assert thermo.pushes == 1


async def test_deploy_restart_on_allowlist_change_without_push(thermo: FakeThermostat) -> None:
    d = Deployer("h", 5555)
    await d.ensure_daemon([])
    res = await d.ensure_daemon(["10.0.0.9"])
    assert res == "started pid 4242" and thermo.pushes == 1


async def test_deploy_checksum_mismatch(thermo: FakeThermostat) -> None:
    thermo.push_corrupts = True
    with pytest.raises(DeployError, match="checksum"):
        await Deployer("h", 5555).ensure_daemon([])


async def test_deploy_start_fails(thermo: FakeThermostat) -> None:
    thermo.start_fails = True
    with pytest.raises(DeployError, match="did not start"):
        await Deployer("h", 5555).ensure_daemon([])


async def test_deploy_unverified_firmware_still_deploys(thermo: FakeThermostat) -> None:
    thermo.firmware = "tina.other"  # daemon itself goes read-only on other builds
    assert (await Deployer("h", 5555).ensure_daemon([])).startswith("started")


async def test_deploy_no_ip(thermo: FakeThermostat) -> None:
    thermo.local_ip = ""
    with pytest.raises(DeployError, match="allowlist"):
        await Deployer("h", 5555).ensure_daemon([])


async def test_versions_and_stop(thermo: FakeThermostat) -> None:
    d = Deployer("h", 5555)
    v = await d.read_versions()
    assert v["indoor_unit_firmware"] == "00AC000111171171000066111008"
    assert v["indoor_unit_parameters"] == "00AC0001111711000"
    await d.ensure_daemon([])
    await d.stop_daemon()
    assert thermo.daemon_pid == ""


async def test_shell_error_wrapped(thermo: FakeThermostat) -> None:
    d = Deployer("h", 5555)
    with patch("tests.fake_adb.FakeAdb.shell", AsyncMock(side_effect=TimeoutError())):
        with pytest.raises(DeployError, match="shell failed"):
            await d.preflight()


async def test_push_error_wrapped(thermo: FakeThermostat) -> None:
    with patch("tests.fake_adb.FakeAdb.push", AsyncMock(side_effect=OSError("disk"))):
        with pytest.raises(DeployError, match="push failed"):
            await Deployer("h", 5555).ensure_daemon([])
