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

    def __init__(self, host: str, adb_port: int) -> None:
        self.host = host
        self.adb_port = adb_port
        self._lock = asyncio.Lock()
        self.last_allowlist: list[str] = []
        self.last_uptime_s: float | None = None
        self.last_egress: dict[str, Any] = {}

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

                port = await self._sh(dev, "netstat -ltn 2>/dev/null | grep -c ':8080 '")
                running = await self._sh(dev, "pidof thermostat_api")
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
        rules = ["-o lo -j RETURN", f"-d {lan} -j RETURN",
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
                want_cmd = f"{DAEMON_DEVICE_PATH} -d {args}"

                fw = await self._sh(dev, "sed -n \"s/^DISTRIB_ID='\\(.*\\)'/\\1/p\" /etc/openwrt_release")
                if fw != VERIFIED_FIRMWARE:
                    _LOGGER.warning("Firmware %s is not verified; daemon will run read-only", fw)
                up = (await self._sh(dev, "cut -d' ' -f1 /proc/uptime")).strip()
                try:
                    self.last_uptime_s = float(up)
                except ValueError:
                    self.last_uptime_s = None

                pid = await self._sh(dev, "pidof thermostat_api")
                have_sha = (await self._sh(dev, f"sha256sum {DAEMON_DEVICE_PATH} 2>/dev/null")).split(" ")[0]
                cmdline = ""
                if pid.isdigit():
                    cmdline = (await self._sh(dev, f"tr '\\0' ' ' < /proc/{pid}/cmdline")).strip()
                if (not force and pid.isdigit() and have_sha == want_sha and cmdline == want_cmd):
                    if block_egress is not None:
                        await self._apply_egress(dev, block_egress, ips)
                    return "already running"

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
                new_pid = await self._sh(dev, "pidof thermostat_api")
                if not new_pid.isdigit():
                    raise DeployError("daemon did not start")
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
