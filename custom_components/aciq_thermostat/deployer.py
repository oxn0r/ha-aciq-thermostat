"""ADB-based pre-flight checks and daemon deployment for the TL04-1.

The thermostat exposes an unauthenticated root adbd on TCP 5555. We use it to
validate that the device matches the build our control frames were verified
on, then push the bundled `thermostat_api` daemon to /tmp (tmpfs, cleared on
reboot) and start it. Nothing persistent on the thermostat is modified.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
import hashlib
import ipaddress
import logging
from pathlib import Path
import re
import shlex
import time
from typing import Any

from adb_shell.adb_device_async import AdbDeviceTcpAsync
from adb_shell.exceptions import AdbConnectionError, AdbTimeoutError, TcpTimeoutException

from .const import (
    DAEMON_DEVICE_PATH,
    DAEMON_FILENAME,
    VERIFIED_DEV_APP_SHA256,
    VERIFIED_FIRMWARE,
)

_LOGGER = logging.getLogger(__name__)

DAEMON_LOCAL_PATH = Path(__file__).parent / "bin" / DAEMON_FILENAME

# Process table for self-healing: which PID listens on the API port ("L pid/name") and
# every thermostat_api process with its parent ("P pid ppid"). Long-poll children share
# the name, so plain `pidof` can return several PIDs.
def _procs_cmd(port: int) -> str:
    return (f"netstat -ltnp 2>/dev/null | awk '$4 ~ /:{port}$/ {{print \"L \" $7}}'; "
            "for p in $(pidof thermostat_api); do echo \"P $p $(cut -d' ' -f4 /proc/$p/stat 2>/dev/null)\"; done")


def _parse_procs(out: str) -> tuple[int | None, dict[int, int]]:
    # BusyBox netstat truncates program names ("thermostat_ap"), so the listener is
    # matched by PID against the thermostat_api processes, never by name.
    listen_pids: list[int] = []
    procs: dict[int, int] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "L":
            pid = parts[1].split("/")[0]
            if pid.isdigit():
                listen_pids.append(int(pid))
        elif len(parts) == 3 and parts[0] == "P" and parts[1].isdigit() and parts[2].isdigit():
            procs[int(parts[1])] = int(parts[2])
    listener = next((p for p in listen_pids if p in procs), None)
    return listener, procs

# Never filtered by the egress rule (see _apply_egress)
LOCAL_NETS = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "100.64.0.0/10")

ADB_ERRORS = (
    AdbConnectionError,
    AdbTimeoutError,
    TcpTimeoutException,
    ConnectionError,
    OSError,
    asyncio.TimeoutError,
)


class DeployError(Exception):
    """Raised when talking to or deploying on the thermostat fails."""


@dataclass
class Check:
    key: str
    ok: bool
    detail: str
    fatal: bool = True


@dataclass
class PreflightResult:
    checks: list[Check] = field(default_factory=list)
    serial: str | None = None
    firmware: str | None = None

    @property
    def passed(self) -> bool:
        return all(c.ok for c in self.checks if c.fatal)

    def summary(self) -> str:
        return "\n".join(
            f"{'✅' if c.ok else ('❌' if c.fatal else '⚠️')} {c.key}: {c.detail}"
            for c in self.checks
        )

    def as_dict(self) -> dict[str, Any]:
        return {"serial": self.serial, "firmware": self.firmware,
                "passed": self.passed, "checks": [asdict(c) for c in self.checks]}


def bundled_daemon_sha256() -> str:
    return hashlib.sha256(DAEMON_LOCAL_PATH.read_bytes()).hexdigest()


class Deployer:
    """One short-lived ADB session per operation (adbd here is single-client friendly)."""

    def __init__(self, host: str, adb_port: int, api_port: int = 8080) -> None:
        self.host = host
        self.adb_port = adb_port
        self.api_port = api_port
        self._lock = asyncio.Lock()
        self.last_allowlist: list[str] = []
        self.last_uptime_s: float | None = None
        self.last_egress: dict[str, Any] = {}
        # what the last ensure_daemon did and why (action, cause, pids, thermostat uptime)
        self.last_deploy_detail: dict[str, Any] = {}

    async def _session(self) -> AdbDeviceTcpAsync:
        # adbd on this device can stall a new client while another adb host holds
        # a session; one retry covers that.
        last: Exception | None = None
        for attempt in range(2):
            dev = AdbDeviceTcpAsync(self.host, self.adb_port, default_transport_timeout_s=10.0)
            try:
                ok = await dev.connect(rsa_keys=None, auth_timeout_s=5, read_timeout_s=10)
            except ADB_ERRORS as err:
                last = err
                await dev.close()
                if attempt == 0:
                    await asyncio.sleep(2)
                continue
            if not ok:
                raise DeployError("ADB connection refused (device requires authentication?)")
            return dev
        raise DeployError(f"ADB connect to {self.host}:{self.adb_port} failed: {last}")

    @staticmethod
    async def _sh(dev: AdbDeviceTcpAsync, cmd: str, timeout: float = 15) -> str:
        try:
            out = await dev.shell(cmd, timeout_s=timeout, read_timeout_s=timeout)
        except ADB_ERRORS as err:
            raise DeployError(f"ADB shell failed: {err}") from err
        return (out or "").strip()

    # ------------------------------------------------------------------ checks

    async def preflight(self) -> PreflightResult:
        """Read-only validation of the thermostat."""
        res = PreflightResult()
        async with self._lock:
            dev = await self._session()
            try:
                uid = await self._sh(dev, "id -u")
                res.checks.append(Check("root_shell", uid == "0", f"uid={uid}"))

                fw = await self._sh(dev, "sed -n \"s/^DISTRIB_ID='\\(.*\\)'/\\1/p\" /etc/openwrt_release")
                res.firmware = fw or None
                res.checks.append(Check("firmware", fw == VERIFIED_FIRMWARE,
                                        f"{fw or 'unknown'} (verified: {VERIFIED_FIRMWARE})"))

                sha = (await self._sh(dev, "sha256sum /usr/bin/dev_app 2>/dev/null")).split(" ")[0]
                res.checks.append(Check("dev_app_build", sha == VERIFIED_DEV_APP_SHA256,
                                        f"sha256 {sha[:16] or 'missing'}…"))

                pid = await self._sh(dev, "pidof dev_app")
                res.checks.append(Check("dev_app_running", pid.isdigit(), f"pid {pid or 'none'}"))

                queue = await self._sh(dev, "[ -e /tmp/dev_msg ] && grep -c . /proc/sysvipc/msg")
                res.checks.append(Check("ipc_queues", queue.isdigit() and int(queue) > 1,
                                        f"/tmp/dev_msg present, {queue or 0} lines in /proc/sysvipc/msg"))

                ahu = await self._sh(dev, "cat /tmp/.lastlog.old /tmp/.lastlog 2>/dev/null | grep -c RAC_DEV_AHU3_STATE")
                ahu_n = int(ahu) if ahu.isdigit() else 0
                res.checks.append(Check("communicating_mode", ahu_n > 0,
                                        f"{ahu_n} AHU3 state lines in log"
                                        + ("" if ahu_n else " (may appear after the next state change)"),
                                        fatal=False))

                port = await self._sh(dev, f"netstat -ltn 2>/dev/null | grep -c ':{self.api_port} '")
                listener, _ = _parse_procs(await self._sh(dev, _procs_cmd(self.api_port)))
                running = str(listener) if listener else ""
                res.checks.append(Check("api_port", port in ("0", "") or running.isdigit(),
                                        "free" if port in ("0", "") else f"in use by thermostat_api pid {running or '?'}"))

                serial = await self._sh(dev, "cat /tmp/.sn 2>/dev/null")
                res.serial = re.sub(r"[^A-Za-z0-9]", "", serial) or None
            finally:
                await dev.close()
        return res

    # ---------------------------------------------------------------- deploy

    @staticmethod
    def _local_ip(dev: AdbDeviceTcpAsync) -> str | None:
        """Our address as the thermostat sees it (from the live ADB socket)."""
        try:
            writer = dev._io_manager._transport._writer  # adb-shell 0.4.4 internals
            sockname = writer.get_extra_info("sockname")
            return str(sockname[0]) if sockname else None
        except (AttributeError, TypeError, IndexError):
            return None

    async def _apply_egress(self, dev: AdbDeviceTcpAsync, enable: bool, allow_ips: list[str]) -> None:
        """Runtime (non-persistent) OUTPUT filter: drop everything except LAN, allowlisted
        IPs, broadcast/multicast and replies. Stops the cloud client's endless DNS retries
        from leaving the thermostat. Idempotent; cleared by reboot and re-applied at deploy."""
        if not enable:
            await self._sh(dev, "iptables -D OUTPUT -j ACIQ_EGRESS 2>/dev/null; "
                                "iptables -F ACIQ_EGRESS 2>/dev/null; iptables -X ACIQ_EGRESS 2>/dev/null; true")
            self.last_egress = {"enabled": False}
            return
        addr = await self._sh(dev, "ip -4 -o addr show wlan0 | awk '{print $4}' | head -1")
        try:
            lan = str(ipaddress.ip_interface(addr).network)
        except ValueError as err:
            raise DeployError(f"cannot determine thermostat LAN for egress filter ({addr!r})") from err
        # Only public internet destinations are dropped: every private/local range stays
        # open (own LAN, any RFC1918 subnet/VLAN, link-local, CGNAT/Tailscale), plus
        # explicitly allowlisted IPs, broadcast and multicast.
        rules = ["-o lo -j RETURN",
                 "-p udp --dport 123 -j RETURN",      # NTP: let a router-side NTP intercept answer
                 *(f"-d {net} -j RETURN" for net in (lan, *LOCAL_NETS)),
                 *(f"-d {shlex.quote(ip)} -j RETURN" for ip in allow_ips),
                 "-d 255.255.255.255 -j RETURN", "-d 224.0.0.0/4 -j RETURN"]
        script = ("iptables -N ACIQ_EGRESS 2>/dev/null; iptables -F ACIQ_EGRESS && "
                  "{ iptables -A ACIQ_EGRESS -m state --state ESTABLISHED,RELATED -j RETURN 2>/dev/null; true; } && "
                  + " && ".join(f"iptables -A ACIQ_EGRESS {r}" for r in rules)
                  + " && iptables -A ACIQ_EGRESS -j DROP"
                  " && { iptables -C OUTPUT -j ACIQ_EGRESS 2>/dev/null || iptables -I OUTPUT 1 -j ACIQ_EGRESS; }"
                  " && echo EGRESS_OK")
        out = await self._sh(dev, script)
        if "EGRESS_OK" not in out:
            # never leave a partial filter behind
            await self._sh(dev, "iptables -D OUTPUT -j ACIQ_EGRESS 2>/dev/null; iptables -F ACIQ_EGRESS 2>/dev/null; true")
            raise DeployError(f"egress filter failed: {out[-200:]}")
        drops = await self._sh(dev, "iptables -L ACIQ_EGRESS -v -x -n 2>/dev/null | tail -1 | awk '{print $1}'")
        self.last_egress = {"enabled": True, "lan": lan, "dropped_packets": int(drops) if drops.isdigit() else None}

    async def _device_epoch(self, dev: AdbDeviceTcpAsync) -> tuple[float, float]:
        """(thermostat time, reference time at the same instant). `date +%s` truncates to whole
        seconds, so +0.5 s is the best estimate; the reference is the RTT midpoint."""
        t0 = time.time()
        out = await self._sh(dev, "date -u +%s")
        t1 = time.time()
        if not out.isdigit():
            raise DeployError(f"unexpected clock reading {out!r}")
        return int(out) + 0.5, (t0 + t1) / 2

    async def sync_clock(self, max_offset: float, correct: bool = True) -> dict[str, Any]:
        """Measure the thermostat clock against ours; set it (runtime only, `date -s`) if off by
        >= max_offset. Returns {"offset_s", "corrected", "offset_after_s"}."""
        if time.time() < 1735689600:   # 2025-01-01: our own clock isn't trustworthy
            raise DeployError("Home Assistant's clock looks unset; not touching the thermostat clock")
        async with self._lock:
            dev = await self._session()
            try:
                dev_t, ref_t = await self._device_epoch(dev)
                offset = round(dev_t - ref_t, 1)
                result: dict[str, Any] = {"offset_s": offset, "corrected": False}
                if correct and abs(offset) >= max_offset:
                    target = int(round(time.time() + 0.05))      # ~half an ADB round trip ahead
                    await self._sh(dev, f"date -u -s @{target} >/dev/null")
                    dev_t2, ref_t2 = await self._device_epoch(dev)
                    result.update(corrected=True, offset_after_s=round(dev_t2 - ref_t2, 1))
                return result
            finally:
                await dev.close()

    async def verify_egress(self, enable: bool, extra_ips: list[str]) -> str:
        """Confirm the runtime egress rule matches `enable`; (re)apply or remove if not.

        Returns 'ok', 'reapplied' or 'removed'. One short ADB session."""
        async with self._lock:
            dev = await self._session()
            try:
                present = (await self._sh(
                    dev, "iptables -C OUTPUT -j ACIQ_EGRESS 2>/dev/null && echo PRESENT || echo MISSING")) == "PRESENT"
                if enable and present:
                    drops = await self._sh(dev, "iptables -L ACIQ_EGRESS -v -x -n 2>/dev/null | tail -1 | awk '{print $1}'")
                    self.last_egress = {**self.last_egress, "enabled": True,
                                        "dropped_packets": int(drops) if drops.isdigit() else None}
                    return "ok"
                if not enable and not present:
                    self.last_egress = {"enabled": False}
                    return "ok"
                allow = [ip for ip in [self._local_ip(dev), *extra_ips] if ip]
                await self._apply_egress(dev, enable, list(dict.fromkeys(allow)))
                return "reapplied" if enable else "removed"
            finally:
                await dev.close()

    async def ensure_daemon(self, extra_ips: list[str], force: bool = False,
                            block_egress: bool | None = None) -> str:
        """Make sure the bundled daemon runs with the wanted allowlist.

        The allowlist is our own ADB source address (what the thermostat sees)
        plus `extra_ips`. Replacing a running daemon happens in a single shell
        command so an interruption can't leave the API down.
        Returns a short description of what was done.
        """
        want_sha = await asyncio.get_running_loop().run_in_executor(None, bundled_daemon_sha256)
        async with self._lock:
            dev = await self._session()
            try:
                ips = [ip for ip in [self._local_ip(dev), *extra_ips] if ip]
                ips = list(dict.fromkeys(ips))
                if not ips:
                    raise DeployError("could not determine Home Assistant's IP for the allowlist")
                self.last_allowlist = ips
                args = " ".join(f"-a {shlex.quote(ip)}" for ip in ips)
                port_arg = f"-p {self.api_port} " if self.api_port != 8080 else ""
                want_cmd = f"{DAEMON_DEVICE_PATH} -d {port_arg}{args}"

                fw = await self._sh(dev, "sed -n \"s/^DISTRIB_ID='\\(.*\\)'/\\1/p\" /etc/openwrt_release")
                if fw != VERIFIED_FIRMWARE:
                    _LOGGER.warning("Firmware %s is not verified; daemon will run read-only", fw)
                up = (await self._sh(dev, "cut -d' ' -f1 /proc/uptime")).strip()
                try:
                    self.last_uptime_s = float(up)
                except ValueError:
                    self.last_uptime_s = None

                listener, procs = _parse_procs(await self._sh(dev, _procs_cmd(self.api_port)))
                pid = str(listener) if listener else ""
                # anything named thermostat_api that is neither the listening daemon nor one of its
                # long-poll children: orphaned children or leftover instances
                strays = sorted(p for p, pp in procs.items() if p != listener and pp != listener)
                have_sha = (await self._sh(dev, f"sha256sum {DAEMON_DEVICE_PATH} 2>/dev/null")).split(" ")[0]
                cmdline = ""
                if pid.isdigit():
                    cmdline = (await self._sh(dev, f"tr '\\0' ' ' < /proc/{pid}/cmdline")).strip()
                if not pid.isdigit():
                    cause = "not_running"
                elif have_sha != want_sha:
                    cause = "build_changed"
                elif cmdline != want_cmd:
                    cause = "args_changed"
                elif force:
                    cause = "forced"
                else:
                    cause = ""
                detail: dict[str, Any] = {"cause": cause or None, "previous_pid": int(pid) if pid.isdigit() else None,
                                          "thermostat_uptime_s": self.last_uptime_s}
                if not cause:
                    if strays:
                        await self._sh(dev, "kill " + " ".join(map(str, strays)) + " 2>/dev/null; true")
                        detail["strays_killed"] = strays
                        _LOGGER.warning("Removed %d stray thermostat_api process(es) on %s: %s",
                                        len(strays), self.host, strays)
                    if block_egress is not None:
                        await self._apply_egress(dev, block_egress, ips)
                    self.last_deploy_detail = {**detail, "action": "already_running", "pid": int(pid)}
                    return "already running"
                if strays:
                    detail["strays_killed"] = strays    # the restart below kills every instance

                pushed = False
                if have_sha != want_sha:
                    tmp = DAEMON_DEVICE_PATH + ".new"
                    try:
                        await dev.push(str(DAEMON_LOCAL_PATH), tmp, st_mode=0o100755)
                    except ADB_ERRORS as err:
                        raise DeployError(f"push failed: {err}") from err
                    got = (await self._sh(dev, f"sha256sum {tmp}")).split(" ")[0]
                    if got != want_sha:
                        await self._sh(dev, f"rm -f {tmp}")
                        raise DeployError(f"pushed daemon checksum mismatch ({got[:16]}…)")
                    pushed = True

                swap = f"mv -f {DAEMON_DEVICE_PATH}.new {DAEMON_DEVICE_PATH}; " if pushed else ""
                await self._sh(dev, (
                    f"P=$(pidof thermostat_api); [ -n \"$P\" ] && kill $P && sleep 1; "
                    f"{swap}chmod 755 {DAEMON_DEVICE_PATH} && {want_cmd} >/dev/null 2>&1; sleep 1"
                ), timeout=20)
                new_listener: int | None = None
                for _ in range(3):
                    new_listener, _procs = _parse_procs(await self._sh(dev, _procs_cmd(self.api_port)))
                    if new_listener:
                        break
                    await asyncio.sleep(1)
                if not new_listener:
                    raise DeployError("daemon did not start (nothing listening on the API port)")
                new_pid = str(new_listener)
                self.last_deploy_detail = {**detail, "action": "upgraded" if pushed else "started",
                                           "pid": int(new_pid)}
                if block_egress is not None:
                    await self._apply_egress(dev, block_egress, ips)
                return f"started pid {new_pid}" + (" (pushed new build)" if pushed else "")
            finally:
                await dev.close()

    async def read_versions(self) -> dict[str, str]:
        """Version strings shown in the ACiQ app (read-only)."""
        out: dict[str, str] = {}
        async with self._lock:
            dev = await self._session()
            try:
                out["screen_firmware"] = await self._sh(dev, "cat /tmp/.firmware_version.txt_check 2>/dev/null")
                out["hardware"] = await self._sh(dev, "cat /tmp/.hw_version.txt_check 2>/dev/null")
                out["os_build"] = await self._sh(dev, "sed -n \"s/^DISTRIB_ID='\\(.*\\)'/\\1/p\" /etc/openwrt_release")
                # dev_version_info holds the indoor unit's firmware + parameter version strings
                raw = await self._sh(dev, "strings -n 4 /mnt/UDISK/cfg/dev_app/dev_version_info 2>/dev/null")
                parts = raw.split()
                if parts:
                    out["indoor_unit_firmware"] = parts[0]
                if len(parts) >= 3:
                    out["indoor_unit_parameters"] = parts[1] + parts[2]
            finally:
                await dev.close()
        return {k: v for k, v in out.items() if v}

    async def stop_daemon(self) -> None:
        async with self._lock:
            dev = await self._session()
            try:
                await self._sh(dev, "kill $(pidof thermostat_api) 2>/dev/null; true")
            finally:
                await dev.close()
