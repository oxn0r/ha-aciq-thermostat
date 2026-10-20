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


async def test_uptime_recorded_at_deploy(thermo: FakeThermostat) -> None:
    d = Deployer("h", 5555)
    await d.ensure_daemon([])
    assert d.last_uptime_s == pytest.approx(1234.56)
    thermo.uptime = "garbage"
    await d.ensure_daemon([], force=True)
    assert d.last_uptime_s is None


async def test_egress_enable_builds_safe_chain(thermo: FakeThermostat) -> None:
    d = Deployer("h", 5555)
    await d.ensure_daemon(["10.0.0.40"], block_egress=True)
    chain = thermo.egress_chain
    assert chain[-1].endswith("-j DROP")                         # DROP only after every allow rule
    joined = " | ".join(chain)
    for allow in ("-o lo -j RETURN", "-d 10.0.0.0/22 -j RETURN", "-d 10.0.0.2 -j RETURN",
                  "-d 10.0.0.0/8 -j RETURN", "-d 172.16.0.0/12 -j RETURN", "-d 192.168.0.0/16 -j RETURN",
                  "-d 169.254.0.0/16 -j RETURN", "-d 100.64.0.0/10 -j RETURN",
                  "-d 10.0.0.40 -j RETURN", "-d 255.255.255.255 -j RETURN", "-d 224.0.0.0/4 -j RETURN"):
        assert allow in joined, allow
    assert d.last_egress == {"enabled": True, "lan": "10.0.0.0/22", "dropped_packets": 17}


async def test_egress_applied_when_already_running_and_disabled(thermo: FakeThermostat) -> None:
    d = Deployer("h", 5555)
    await d.ensure_daemon([])
    assert await d.ensure_daemon([], block_egress=True) == "already running"
    assert thermo.egress_chain
    await d.ensure_daemon([], block_egress=False)
    assert thermo.egress_chain == [] and d.last_egress == {"enabled": False}


async def test_egress_failure_cleans_up(thermo: FakeThermostat) -> None:
    thermo.egress_ok = False
    with pytest.raises(DeployError, match="egress filter failed"):
        await Deployer("h", 5555).ensure_daemon([], block_egress=True)
    assert any(c.startswith("iptables -D OUTPUT -j ACIQ_EGRESS") for c in thermo.commands)


async def test_egress_bad_lan_address(thermo: FakeThermostat) -> None:
    thermo.wlan_addr = ""
    with pytest.raises(DeployError, match="LAN"):
        await Deployer("h", 5555).ensure_daemon([], block_egress=True)
    assert not any(c.startswith("iptables -N") for c in thermo.commands)   # nothing applied


async def test_egress_never_blocks_local_destinations(thermo: FakeThermostat) -> None:
    """Simulate the chain: every local/private destination returns before DROP; public ones hit DROP."""
    import ipaddress
    await Deployer("h", 5555).ensure_daemon([], block_egress=True)
    nets = [ipaddress.ip_network(r.split("-d ")[1].split()[0]) for r in thermo.egress_chain if " -d " in r]

    def verdict(dst: str) -> str:
        ip = ipaddress.ip_address(dst)
        return "allow" if any(ip in n for n in nets) else "drop"

    for local in ("10.0.0.2", "10.0.0.4", "10.0.3.255", "10.20.30.40", "172.16.5.5", "192.168.1.10",
                  "169.254.1.1", "100.68.50.84", "255.255.255.255", "224.0.0.251", "239.255.255.250"):
        assert verdict(local) == "allow", local
    for cloud in ("8.8.8.8", "1.1.1.1", "114.114.114.114", "52.223.47.215", "54.213.20.210"):
        assert verdict(cloud) == "drop", cloud


async def test_verify_egress_states(thermo: FakeThermostat) -> None:
    d = Deployer("h", 5555)
    # enabled but missing (e.g. after a reboot) -> re-applied
    assert await d.verify_egress(True, []) == "reapplied"
    assert thermo.egress_chain and thermo.egress_chain[-1].endswith("-j DROP")
    # enabled and present -> ok, drop counter refreshed
    assert await d.verify_egress(True, []) == "ok"
    assert d.last_egress["dropped_packets"] == 17
    # disabled but present -> removed
    assert await d.verify_egress(False, []) == "removed"
    assert thermo.egress_chain == []
    # disabled and absent -> ok
    assert await d.verify_egress(False, []) == "ok"
