"""A simulated TL04-1 adbd: answers the shell commands the deployer sends."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
import hashlib
from typing import Any

from custom_components.aciq_thermostat.const import VERIFIED_DEV_APP_SHA256, VERIFIED_FIRMWARE
from custom_components.aciq_thermostat.debug_report import DEBUG_COMMANDS
from custom_components.aciq_thermostat.deployer import DAEMON_LOCAL_PATH

BUNDLED_SHA = hashlib.sha256(DAEMON_LOCAL_PATH.read_bytes()).hexdigest()


@dataclass
class FakeThermostat:
    firmware: str = VERIFIED_FIRMWARE
    dev_app_sha: str = VERIFIED_DEV_APP_SHA256
    serial: str = "00004452017200021B6111301619CKS4"
    uid: str = "0"
    daemon_pid: str = ""
    daemon_sha: str = ""
    daemon_cmdline: str = ""
    new_file_sha: str = ""
    connect_ok: bool = True
    connect_raises: int = 0  # number of connect attempts that raise
    push_corrupts: bool = False
    start_fails: bool = False
    local_ip: str = "10.0.0.2"
    adb_peers: list[str] = field(default_factory=list)   # thermostat's view of ADB clients, "ip:port"
    commands: list[str] = field(default_factory=list)
    pushes: int = 0
    uptime: str = "1234.56"
    wlan_addr: str = "10.0.0.65/22"
    egress_ok: bool = True
    egress_chain: list[str] = field(default_factory=list)
    children: list[int] = field(default_factory=list)   # long-poll children (ppid = daemon)
    strays: list[int] = field(default_factory=list)     # thermostat_api not under the daemon (ppid 1)
    killed: list[int] = field(default_factory=list)
    clock_skew: float = 0.0                              # thermostat clock minus real time (s)
    output_rules: list[str] = field(default_factory=list)  # "-A OUTPUT ..." lines (incl. foreign rules)
    ahu3_lines: int = 5                                  # 0 + v24_lines > 0 = conventional 24 V wiring
    v24_lines: int = 0
    log_lines: list[str] = field(default_factory=list)  # what the recent-log grep returns
    dev_app_bytes: bytes = b""                           # what `pull /usr/bin/dev_app` returns
    dev_app_mem: bytes = b""                             # what the g_self_state dd/hexdump reads

    def factory(self, *args: Any, **kwargs: Any) -> FakeAdb:
        return FakeAdb(self)


DEBUG_SHELL = {c for _, c in DEBUG_COMMANDS}


class _Writer:
    def __init__(self, ip: str) -> None:
        self.ip = ip

    def get_extra_info(self, key: str) -> tuple[str, int]:
        return (self.ip, 50000)


class FakeAdb:
    def __init__(self, t: FakeThermostat) -> None:
        self.t = t
        self._io_manager = type("IO", (), {})()
        self._io_manager._transport = type("T", (), {})()
        self._io_manager._transport._writer = _Writer(t.local_ip)

    async def connect(self, **kwargs: Any) -> bool:
        if self.t.connect_raises > 0:
            self.t.connect_raises -= 1
            raise ConnectionError("simulated stall")
        return self.t.connect_ok

    async def close(self) -> None:
        return None

    async def push(self, local: str, remote: str, **kwargs: Any) -> None:
        self.t.pushes += 1
        self.t.new_file_sha = "0" * 64 if self.t.push_corrupts else BUNDLED_SHA

    async def pull(self, remote: str, local: Any, **kwargs: Any) -> None:
        if not self.t.dev_app_bytes:
            raise OSError("no such file")
        local.write(self.t.dev_app_bytes)

    async def shell(self, cmd: str, **kwargs: Any) -> str:  # noqa: C901 - a dispatch table
        t = self.t
        t.commands.append(cmd)
        if cmd.startswith("dd if=/proc/"):
            return t.dev_app_mem.hex()
        if cmd == "pidof dev_app":
            return "1666"
        if cmd == "date -u +%s":
            import time as _t
            return str(int(_t.time() + t.clock_skew))
        if cmd.startswith("date -u -s @"):
            import time as _t
            t.clock_skew = int(cmd.split("@")[1].split()[0]) - _t.time()
            return ""
        if "/proc/uptime" in cmd:
            return t.uptime
        if cmd.startswith("ip -4 -o addr show wlan0"):
            return t.wlan_addr
        if cmd.startswith("iptables -N ACIQ_EGRESS"):
            if not t.egress_ok:
                return "iptables: No chain/target/match by that name."
            parts = re.findall(r"iptables -A ACIQ_EGRESS ([^;&]+?)(?: 2>/dev/null)?(?=\s*(?:&&|;|$))", cmd)
            t.egress_chain = [f"iptables -A ACIQ_EGRESS {x.strip()}" for x in parts]
            t.output_rules = [r for r in t.output_rules if r != "-A OUTPUT -j ACIQ_EGRESS"]
            t.output_rules.insert(0, "-A OUTPUT -j ACIQ_EGRESS")
            return "EGRESS_OK"
        if cmd.startswith("iptables -C OUTPUT -j ACIQ_EGRESS"):
            return "PRESENT" if "-A OUTPUT -j ACIQ_EGRESS" in t.output_rules else "MISSING"
        if cmd.startswith("iptables -S OUTPUT"):
            return "\n".join(["-P OUTPUT ACCEPT", *t.output_rules])
        if cmd.startswith("iptables -S ACIQ_EGRESS"):
            def fmt(rule: str) -> str:   # mimic iptables -S formatting
                body = rule.replace("iptables -A ACIQ_EGRESS ", "")
                body = re.sub(r"-d (\d+\.\d+\.\d+\.\d+)(?=\s)", r"-d \1/32", body)
                body = body.replace("-p udp --dport", "-p udp -m udp --dport")
                return f"-A ACIQ_EGRESS {body}"
            return "\n".join(["-N ACIQ_EGRESS", *(fmt(r) for r in t.egress_chain)])
        if cmd.startswith("iptables -L ACIQ_EGRESS"):
            return "17"
        if cmd.startswith("while iptables -D OUTPUT -j ACIQ_EGRESS"):
            t.output_rules = [r for r in t.output_rules if r != "-A OUTPUT -j ACIQ_EGRESS"]
            t.egress_chain = []
            return ""
        if cmd == "id -u":
            return t.uid
        if "DISTRIB_ID" in cmd:
            return t.firmware
        if "sha256sum /usr/bin/dev_app" in cmd:
            return f"{t.dev_app_sha}  /usr/bin/dev_app"
        if cmd == "pidof dev_app":
            return "1668"
        if "/proc/sysvipc/msg" in cmd:
            return "12"
        if "tail -n" in cmd and "lastlog" in cmd:
            return "\n".join(t.log_lines)
        if "RAC_DEV_AHU3_STATE" in cmd:
            return str(t.ahu3_lines)
        if "dev_24v_decorder" in cmd:
            return str(t.v24_lines)
        if cmd.startswith("netstat -ltnp"):
            lines = [f"L {t.daemon_pid}/thermostat_ap"] if t.daemon_pid else []   # BusyBox truncates
            if t.daemon_pid:
                lines.append(f"P {t.daemon_pid} 1")
                lines += [f"P {c} {t.daemon_pid}" for c in t.children]
            lines += [f"P {s_} 1" for s_ in t.strays]
            return "\n".join(lines)
        if cmd.startswith("netstat -tn"):
            return "\n".join(f"tcp        0      0 10.0.0.65:5555          {p:<22}  ESTABLISHED"
                             for p in t.adb_peers)
        if "netstat" in cmd:
            return "1" if t.daemon_pid else "0"
        if cmd.startswith("kill ") and "2>/dev/null; true" in cmd and "pidof" not in cmd:
            pids = [int(x) for x in cmd.split()[1:] if x.isdigit()]
            t.killed += pids
            t.strays = [x for x in t.strays if x not in pids]
            return ""
        if cmd == "cat /tmp/.sn 2>/dev/null":
            return t.serial
        if cmd == "pidof thermostat_api":
            return t.daemon_pid
        if "sha256sum /tmp/thermostat_api.new" in cmd:
            return f"{t.new_file_sha}  /tmp/thermostat_api.new"
        if "sha256sum /tmp/thermostat_api" in cmd:
            return f"{t.daemon_sha}  /tmp/thermostat_api" if t.daemon_sha else ""
        if "/cmdline" in cmd:
            return t.daemon_cmdline
        if cmd.startswith("rm -f"):
            return ""
        if cmd.startswith("P=$(pidof thermostat_api)"):
            t.killed += ([int(t.daemon_pid)] if t.daemon_pid else []) + t.children + t.strays
            t.children, t.strays = [], []
            if "mv -f" in cmd:
                t.daemon_sha = t.new_file_sha
            if t.start_fails:
                t.daemon_pid = ""
            else:
                t.daemon_pid = "4242"
                t.daemon_cmdline = cmd.rsplit("&& ", 1)[1].split(" >/dev/null")[0]
            return ""
        if "kill" in cmd:
            t.daemon_pid = ""
            return ""
        if "firmware_version" in cmd:
            return "LD:1.2.60"
        if "hw_version" in cmd:
            return "V4.3"
        if "dev_version_info" in cmd:
            return "00AC000111171171000066111008\n00AC000111171\n1000"
        if cmd in DEBUG_SHELL:
            return f"output of {cmd.split()[0]} from 10.0.0.9"
        raise AssertionError(f"unexpected command: {cmd}")
