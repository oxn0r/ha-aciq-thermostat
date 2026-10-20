#!/usr/bin/env python3
"""Native end-to-end tests for thermostat_api: compile for the host, feed a simulated
thermostat log, exercise /api/state long-polling over real HTTP.

    python3 daemon/tests/native_test.py
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "thermostat_api.c"
AHU3 = ("{ts} user.info dev_app: rac_dev_send_dev_state] <I>(dev_app/src/dev_ui_state.c:<733>)rac: "
        "RAC_DEV_AHU3_STATE, power: {power}, mode: {mode}, op_mode =255 auto_state ={auto}, temp_unit =1, "
        "temp_set: {sp}.000000, wind_speed: 3\n")
TEMP = "{ts} user.info dev_app: comp_th_data] <I>(x)rac: t_after_comp = {t} h_after_comp = 50.0\n"


def ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S.000")


class Daemon:
    def __init__(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="tapi-"))
        self.log, self.old = self.dir / "lastlog", self.dir / "lastlog.old"
        (self.dir / "uptime").write_text("1234.56 999.00\n")
        (self.dir / "release").write_text("DISTRIB_ID='tina.tangly.20260314.152301'\n")
        self.log.write_text(AHU3.format(ts=ts(), power=1, mode=1, auto=4, sp=72))
        self.bin = self.dir / "tapi"
        defs = {"LOG_FILE": self.log, "LOG_FILE_OLD": self.old, "UPTIME_FILE": self.dir / "uptime",
                "OS_RELEASE_FILE": self.dir / "release", "DEV_MSG_PATH": self.dir / "nomsg"}
        cmd = ["cc", "-O1", "-Wall", "-o", str(self.bin), str(SRC)]
        cmd += [f'-D{k}="{v}"' for k, v in defs.items()]
        subprocess.run(cmd, check=True)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.proc = subprocess.Popen([str(self.bin), "-p", str(self.port), "-a", "127.0.0.1"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            try:
                self.get("/health")
                return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("daemon did not start")

    def get(self, path: str, timeout: float = 40) -> dict:
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=timeout) as r:
            return json.loads(r.read())

    def post(self, path: str, body: dict, timeout: float = 10) -> tuple[int, dict]:
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def append(self, text: str) -> None:
        with open(self.log, "a") as f:
            f.write(text)

    def stop(self) -> None:
        self.proc.terminate()
        self.proc.wait(5)
        shutil.rmtree(self.dir, ignore_errors=True)


def timed(fn):  # noqa: ANN001
    t = time.monotonic()
    out = fn()
    return out, time.monotonic() - t


def test_state_has_id_and_uptime(d: Daemon) -> None:
    s = d.get("/api/state")
    assert s["mode"] == "auto" and s["setpoint"] == 72, s
    assert len(s["state_id"]) == 8 and s["uptime_s"] == 1234 and s["daemon_uptime_s"] == 0, s


def test_timeout_without_change(d: Daemon) -> None:
    sid = d.get("/api/state")["state_id"]
    s, dt = timed(lambda: d.get(f"/api/state?wait={sid}&timeout=3"))
    assert s["state_id"] == sid and 2.5 <= dt <= 5, dt


def test_room_temp_change_does_not_wake(d: Daemon) -> None:
    sid = d.get("/api/state")["state_id"]
    threading.Timer(0.5, d.append, [TEMP.format(ts=ts(), t=70.3)]).start()
    s, dt = timed(lambda: d.get(f"/api/state?wait={sid}&timeout=3"))
    assert s["state_id"] == sid and dt >= 2.5 and s["current_temp"] == 70.3, (dt, s["current_temp"])


def test_returns_on_change(d: Daemon) -> None:
    sid = d.get("/api/state")["state_id"]
    threading.Timer(1.0, d.append, [AHU3.format(ts=ts(), power=1, mode=4, auto=2, sp=70)]).start()
    s, dt = timed(lambda: d.get(f"/api/state?wait={sid}&timeout=20"))
    assert s["state_id"] != sid and s["mode"] == "heat" and s["setpoint"] == 70, s
    assert dt < 3.5, dt


def test_stale_id_answers_now(d: Daemon) -> None:
    _, dt = timed(lambda: d.get("/api/state?wait=deadbeef&timeout=20"))
    assert dt < 1.5, dt


def test_partial_line_waits_for_newline(d: Daemon) -> None:
    line = AHU3.format(ts=ts(), power=1, mode=4, auto=2, sp=65)
    d.append(line[:40])
    assert d.get("/api/state")["setpoint"] != 65
    d.append(line[40:])
    assert d.get("/api/state")["setpoint"] == 65


def test_rotation(d: Daemon) -> None:
    sid = d.get("/api/state")["state_id"]
    d.append(AHU3.format(ts=ts(), power=1, mode=2, auto=1, sp=74))   # written just before rotation
    os.replace(d.log, d.old)
    d.log.write_text(TEMP.format(ts=ts(), t=71.0))
    s = d.get(f"/api/state?wait={sid}&timeout=5")
    assert s["mode"] == "cool" and s["setpoint"] == 74, s          # tail of rotated file not lost
    d.append(AHU3.format(ts=ts(), power=0, mode=2, auto=1, sp=74))
    assert d.get("/api/state")["mode"] == "off"


def test_main_loop_not_blocked_and_waiter_cap(d: Daemon) -> None:
    sid = d.get("/api/state")["state_id"]
    results: list[float] = []
    def waiter() -> None:
        t = time.monotonic()
        d.get(f"/api/state?wait={sid}&timeout=6")
        results.append(time.monotonic() - t)
    threads = [threading.Thread(target=waiter) for _ in range(5)]
    for th in threads:
        th.start()
        time.sleep(0.15)
    _, dt_health = timed(lambda: d.get("/health"))
    code, _ = d.post("/api/control", {"temperature": 99})              # validation path, still responsive
    _, dt_ctl = timed(lambda: d.post("/api/control", {"temperature": 99}))
    for th in threads:
        th.join()
    assert dt_health < 1 and dt_ctl < 1 and code == 400, (dt_health, dt_ctl, code)
    assert sorted(results)[0] < 1.5, results                            # 5th waiter answered at once
    assert sum(1 for r in results if r >= 5) == 4, results              # 4 real waiters


def test_client_disconnect_frees_waiter(d: Daemon) -> None:
    sid = d.get("/api/state")["state_id"]
    for _ in range(4):                                                  # open and abandon 4 waits
        s = socket.create_connection(("127.0.0.1", d.port))
        s.sendall(f"GET /api/state?wait={sid}&timeout=30 HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        s.close()
    time.sleep(2.5)                                                     # children notice and exit
    d.get("/health")                                                    # main loop reaps them
    _, dt = timed(lambda: d.get(f"/api/state?wait={sid}&timeout=3"))
    assert dt >= 2.5, dt                                                # a real wait slot was free again


def main() -> int:
    tests = [v for k, v in globals().items() if k.startswith("test_")]
    failed = 0
    for t in tests:
        d = Daemon()
        try:
            t(d)
            print(f"PASS {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}: {e!r}")
        finally:
            d.stop()
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
