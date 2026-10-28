"""Deployer against the simulated thermostat."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from custom_components.aciq_thermostat.deployer import Deployer, DeployError, compact_log

from custom_components.aciq_thermostat.const import CONTROL_FIRMWARE, LOG_READ_LINES, VERIFIED_DEV_APP_SHA256

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


async def test_preflight_control_firmware(thermo: FakeThermostat) -> None:
    thermo.firmware = "tina.tangly.20260821.063106"                  # issue #8
    thermo.dev_app_sha = CONTROL_FIRMWARE[thermo.firmware]
    res = await Deployer("h", 5555).preflight()
    assert res.passed and "supported, no equipment data" in res.summary()
    thermo.dev_app_sha = VERIFIED_DEV_APP_SHA256                       # a different dev_app on that build
    assert not (await Deployer("h", 5555).preflight()).passed


async def test_recent_log(thermo: FakeThermostat) -> None:
    thermo.log_lines = ["a RAC_DEV_AHU3_STATE, power: 1", "b set24VTemp] Ts =72"]
    assert await Deployer("h", 5555).recent_log(10) == thermo.log_lines
    cmd = thermo.commands[-1] if thermo.commands else ""
    assert not cmd or ("/tmp/.lastlog.old /tmp/.lastlog" in cmd and f"tail -n {LOG_READ_LINES}" in cmd)


A3 = ("2026-10-28 10:02:{s:02d}.896 user.info dev_app: process_24v_cmd_a3] <I>(dev_app/src/dev_24v_decorder.c:<596>)"
      "rac: cpTempUnit:1 Tt:{tt} Thigh:{hi} Tlow:67 Ts:74")
IO = ("2026-10-28 10:02:{s:02d}.900 user.info dev_app: process_24v_cmd_a3] <I>(dev_app/src/dev_24v_decorder.c:<637>)"
      "rac: 24V terminal io: PEK:0 O/B:1 G:1")
UI = ("2026-10-28 10:02:{s:02d}.437 user.debug meiju: page_ui_refresh_background] mode={m}, auto_state=0, "
      "sensor_temp={t}, heatTemperValue=72")
RSSI = "2026-10-28 10:02:{s:02d}.166 user.info meiju: thermostat_ui_dev_state_response] wifi_rssi:{r}, deadband:4"
CMD = ("2026-10-28 10:02:{s:02d}.538 user.info dev_app: parse_tlv_24v_cmd] <I>(dev_app/src/dev_msmart_protocol.c:<715>)"
       "rac: send_cmd_flag =1")


def test_compact_log_keeps_changes_and_commands() -> None:
    lines = [A3.format(s=0, tt="73.12", hi=71), IO.format(s=0), UI.format(s=0, m=1, t=7300), RSSI.format(s=0, r=40),
             A3.format(s=1, tt="73.18", hi=71), IO.format(s=1), UI.format(s=1, m=1, t=7400), RSSI.format(s=1, r=44),
             CMD.format(s=2), CMD.format(s=2),                                # commands: always kept
             A3.format(s=3, tt="73.10", hi=74),                               # Thigh changed
             UI.format(s=3, m=2, t=7400),                                     # mode changed
             A3.format(s=4, tt="73.11", hi=74), IO.format(s=4), "", "   "]
    kept = compact_log(lines, 100)
    assert kept == [lines[0], lines[1], lines[2], lines[3], lines[8], lines[9], lines[10], lines[11]]
    assert compact_log(lines, 2) == kept[-2:] and compact_log(lines, 0) == []
    # a state that returns to an earlier value is a change again
    back = [UI.format(s=0, m=1, t=1), UI.format(s=1, m=2, t=1), UI.format(s=2, m=1, t=1)]
    assert compact_log(back, 10) == back


async def test_preflight_reports_24v_mode(thermo: FakeThermostat) -> None:
    thermo.ahu3_lines, thermo.v24_lines = 0, 412
    res = await Deployer("h", 5555).preflight()
    assert res.passed                                       # not fatal: setup explains it in Repairs
    assert "✅ communicating_mode: conventional 24 V mode (412 24 V state lines" in res.summary()


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


async def test_allowlist_behind_docker_nat(thermo: FakeThermostat) -> None:
    """#8: HA in Docker (bridge) sees 172.17.0.2; the thermostat sees the host's LAN IP."""
    thermo.local_ip = "172.17.0.2"
    thermo.adb_peers = ["10.0.0.169:61234"]                     # NAT changed the source port
    d = Deployer("h", 5555)
    await d.ensure_daemon([])
    assert thermo.daemon_cmdline == "/tmp/thermostat_api -d -a 172.17.0.2 -a 10.0.0.169"


async def test_allowlist_ignores_other_clients(thermo: FakeThermostat) -> None:
    thermo.adb_peers = ["10.0.0.2:50000"]                        # host networking: same address
    d = Deployer("h", 5555)
    await d.ensure_daemon([])
    assert d.last_allowlist == ["10.0.0.2"]
    thermo.adb_peers = ["10.0.0.7:41000", "10.0.0.8:42000"]      # other ADB clients, no port match
    await d.ensure_daemon([], force=True)
    assert d.last_allowlist == ["10.0.0.2"]
    thermo.adb_peers = ["10.0.0.7:41000", "::ffff:10.0.0.169:50000"]   # our port behind NAT
    await d.ensure_daemon([], force=True)
    assert d.last_allowlist == ["10.0.0.2", "10.0.0.169"]


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
    assert any(c.startswith("while iptables -D OUTPUT -j ACIQ_EGRESS") for c in thermo.commands)


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


async def test_healthy_daemon_with_longpoll_children_is_kept(thermo: FakeThermostat) -> None:
    """v1.0.3 bug: `pidof` returned daemon + long-poll child and the daemon got restarted."""
    d = Deployer("h", 5555)
    await d.ensure_daemon([])
    thermo.children = [5001, 5002]
    thermo.killed.clear()
    assert await d.ensure_daemon([]) == "already running"
    assert thermo.killed == [] and thermo.children == [5001, 5002]
    assert not any(c.startswith("P=$(pidof") for c in thermo.commands[-6:])   # no restart command
    assert d.last_deploy_detail["action"] == "already_running"


async def test_strays_are_killed_daemon_kept(thermo: FakeThermostat) -> None:
    d = Deployer("h", 5555)
    await d.ensure_daemon([])
    thermo.children, thermo.strays = [5001], [6001, 6002]   # orphaned children / leftovers
    thermo.killed.clear()
    assert await d.ensure_daemon([]) == "already running"
    assert thermo.killed == [6001, 6002] and thermo.daemon_pid == "4242"
    assert d.last_deploy_detail["strays_killed"] == [6001, 6002]


async def test_no_listener_but_processes_restarts_cleanly(thermo: FakeThermostat) -> None:
    d = Deployer("h", 5555)
    await d.ensure_daemon([])
    thermo.daemon_pid, thermo.strays = "", [7001, 7002]      # daemon died, orphans remain
    res = await d.ensure_daemon([])
    assert res.startswith("started pid 4242")
    assert 7001 in thermo.killed and 7002 in thermo.killed
    assert d.last_deploy_detail["cause"] == "not_running"


async def test_daemon_must_listen_after_start(thermo: FakeThermostat) -> None:
    thermo.start_fails = True
    with pytest.raises(DeployError, match="nothing listening"):
        await Deployer("h", 5555).ensure_daemon([])


async def test_custom_api_port_passed_to_daemon(thermo: FakeThermostat) -> None:
    d = Deployer("h", 5555, api_port=8099)
    await d.ensure_daemon([])
    assert thermo.daemon_cmdline == "/tmp/thermostat_api -d -p 8099 -a 10.0.0.2"
    assert await d.ensure_daemon([]) == "already running"


async def test_deploy_causes(thermo: FakeThermostat) -> None:
    d = Deployer("h", 5555)
    await d.ensure_daemon([])
    assert d.last_deploy_detail["cause"] == "not_running" and d.last_deploy_detail["action"] == "upgraded"
    await d.ensure_daemon(["10.0.0.9"])
    assert d.last_deploy_detail["cause"] == "args_changed" and d.last_deploy_detail["action"] == "started"
    thermo.daemon_sha = "old"
    await d.ensure_daemon(["10.0.0.9"])
    assert d.last_deploy_detail["cause"] == "build_changed" and d.last_deploy_detail["action"] == "upgraded"
    await d.ensure_daemon(["10.0.0.9"], force=True)
    assert d.last_deploy_detail["cause"] == "forced"


async def test_clock_in_sync_not_touched(thermo: FakeThermostat) -> None:
    res = await Deployer("h", 5555).sync_clock(2.0)
    assert res["corrected"] is False and abs(res["offset_s"]) < 1.6
    assert not any(c.startswith("date -u -s") for c in thermo.commands)


async def test_clock_corrected_when_off(thermo: FakeThermostat) -> None:
    thermo.clock_skew = -36000.0                       # 10 h behind (like the RTC after a reboot)
    res = await Deployer("h", 5555).sync_clock(2.0)
    assert res["corrected"] is True and res["offset_s"] < -35000
    assert abs(res["offset_after_s"]) < 1.6 and abs(thermo.clock_skew) < 1.6


async def test_clock_measure_only(thermo: FakeThermostat) -> None:
    thermo.clock_skew = 30.0
    res = await Deployer("h", 5555).sync_clock(2.0, correct=False)
    assert res["corrected"] is False and res["offset_s"] > 28 and thermo.clock_skew == 30.0


async def test_clock_refuses_when_our_clock_unset(thermo: FakeThermostat) -> None:
    with patch("custom_components.aciq_thermostat.deployer.time.time", return_value=1000.0):
        with pytest.raises(DeployError, match="unset"):
            await Deployer("h", 5555).sync_clock(2.0)


async def test_egress_allows_ntp(thermo: FakeThermostat) -> None:
    await Deployer("h", 5555).ensure_daemon([], block_egress=True)
    i_ntp = next(i for i, r in enumerate(thermo.egress_chain) if "--dport 123" in r)
    assert i_ntp < len(thermo.egress_chain) - 1          # before the final DROP


async def _armed(thermo: FakeThermostat) -> Deployer:
    d = Deployer("h", 5555)
    await d.ensure_daemon([], block_egress=True)
    assert await d.verify_egress(True, []) == "ok"
    return d


async def test_verify_detects_missing_rule_line(thermo: FakeThermostat) -> None:
    d = await _armed(thermo)
    thermo.egress_chain = [r for r in thermo.egress_chain if "--dport 123" not in r]   # e.g. older version's chain
    assert await d.verify_egress(True, []) == "repaired"
    assert any("missing 1" in x for x in d.last_egress["repaired"])
    assert any("--dport 123" in r for r in thermo.egress_chain)


async def test_verify_detects_extra_rule(thermo: FakeThermostat) -> None:
    d = await _armed(thermo)
    thermo.egress_chain.insert(0, "iptables -A ACIQ_EGRESS -j RETURN")     # someone neutered the chain
    assert await d.verify_egress(True, []) == "repaired"
    assert "iptables -A ACIQ_EGRESS -j RETURN" not in thermo.egress_chain[:1]


async def test_verify_detects_reordered_chain(thermo: FakeThermostat) -> None:
    d = await _armed(thermo)
    thermo.egress_chain.insert(0, thermo.egress_chain.pop())               # DROP moved to the top
    assert await d.verify_egress(True, []) == "repaired"
    assert any(x.endswith("order") for x in d.last_egress["repaired"])
    assert thermo.egress_chain[-1].endswith("-j DROP")


async def test_verify_heals_jump_position_and_duplicates_keeps_foreign(thermo: FakeThermostat) -> None:
    d = await _armed(thermo)
    foreign = "-A OUTPUT -d 1.2.3.4/32 -j ACCEPT"
    thermo.output_rules = [foreign, "-A OUTPUT -j ACIQ_EGRESS", "-A OUTPUT -j ACIQ_EGRESS"]
    assert await d.verify_egress(True, []) == "repaired"
    assert thermo.output_rules == ["-A OUTPUT -j ACIQ_EGRESS", foreign]        # one jump, first; foreign kept
    thermo.output_rules = [foreign, "-A OUTPUT -j ACIQ_EGRESS"]
    assert await d.verify_egress(True, []) == "repaired"
    assert "not first" in " ".join(d.last_egress["repaired"])


async def test_verify_ok_tolerates_iptables_formatting(thermo: FakeThermostat) -> None:
    d = await _armed(thermo)      # -S adds /32 and -m udp; must still compare equal
    assert await d.verify_egress(True, ["10.0.0.40"]) == "repaired"   # allowlist changed -> rebuilt
    assert await d.verify_egress(True, ["10.0.0.40"]) == "ok"
    assert "repaired" not in d.last_egress


async def test_disable_removes_all_jumps(thermo: FakeThermostat) -> None:
    d = await _armed(thermo)
    thermo.output_rules.append("-A OUTPUT -j ACIQ_EGRESS")                    # duplicate
    assert await d.verify_egress(False, []) == "removed"
    assert "-A OUTPUT -j ACIQ_EGRESS" not in thermo.output_rules
