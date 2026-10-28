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
    def __init__(self, initial_log: str | None = None) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="tapi-"))
        self.log, self.old = self.dir / "lastlog", self.dir / "lastlog.old"
        (self.dir / "uptime").write_text("1234.56 999.00\n")
        (self.dir / "release").write_text("DISTRIB_ID='tina.tangly.20260314.152301'\n")
        self.log.write_text(AHU3.format(ts=ts(), power=1, mode=1, auto=4, sp=72) if initial_log is None
                            else initial_log)
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
    s, dt = timed(lambda: d.get(f"/api/state?wait={sid}&timeout=4"))   # whole-second deadline: 3-4 s
    assert s["state_id"] == sid and 2.5 <= dt <= 6, dt


def test_room_temp_change_does_not_wake(d: Daemon) -> None:
    sid = d.get("/api/state")["state_id"]
    threading.Timer(0.5, d.append, [TEMP.format(ts=ts(), t=70.3)]).start()
    s, dt = timed(lambda: d.get(f"/api/state?wait={sid}&timeout=4"))
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
    _, dt = timed(lambda: d.get(f"/api/state?wait={sid}&timeout=4"))
    assert dt >= 2.5, dt                                                # a real wait slot was free again


def _celsius_line(sp: int) -> str:
    return (AHU3.format(ts=ts(), power=1, mode=1, auto=4, sp=sp)
            .replace("temp_unit =1", "temp_unit =0"))


def test_celsius_unit_reported_and_wakes_long_poll(d: Daemon) -> None:
    s = d.get("/api/state")
    assert s["temp_unit"] == "F" and s["setpoint"] == 72, s
    sid = s["state_id"]
    threading.Timer(0.5, d.append, [_celsius_line(21)]).start()        # wall unit switched to C
    s, dt = timed(lambda: d.get(f"/api/state?wait={sid}&timeout=10"))
    assert s["temp_unit"] == "C" and s["setpoint"] == 21 and dt < 3.5, (s, dt)


def test_celsius_setpoint_validation_and_conversion(d: Daemon) -> None:
    d.append(_celsius_line(21))
    assert d.get("/api/state")["temp_unit"] == "C"
    for body in ({"temperature": 31}, {"temperature": 15}, {"temperature": 99, "unit": "F"}):
        code, r = d.post("/api/control", body)
        assert code == 400 and "16 and 30 C" in r["error"], (body, r)
    # valid requests pass validation and reach the send step (no IPC queue on the test host)
    for body in ({"temperature": 22}, {"temperature": 22, "unit": "C"}, {"temperature": 72, "unit": "F"}):
        code, r = d.post("/api/control", body)
        assert "between" not in r.get("error", ""), (body, r)


def test_fahrenheit_unit_conversion(d: Daemon) -> None:
    for body, ok in (({"temperature": 22, "unit": "C"}, True), ({"temperature": 31, "unit": "C"}, False)):
        code, r = d.post("/api/control", body)                          # thermostat in F: 22C -> 72F
        assert ("between" not in r.get("error", "")) is ok, (body, r)


# Real lines from a thermostat with Auto holding a range (issue #7, 2026-10-25)
ZONE = ("{ts} user.info dev_app: rac_dev_send_dev_state] <I>(dev_app/src/dev_ui_state.c:<735>)rac: "
        "RAC_DEV_AHU3_STATE, temp_zone_control: {zc}, temp_zone_lower_limit ={lo}.000000  "
        "temp_zone_upper_limit={hi}.000000\n")
UI = ("{ts} user.info meiju: thermostat_ui_dev_state_response] hour_type:1, volume:8, bluetooth_open:1, "
      "wifi_open:1, wifi_connected:1, wifi_rssi:53, deadband:{db}, temp_zone_mode:{zm}, heating_max=86,         "
      "cooling_min=60, setpoint_range_open=0, aux_heat_open=0, smart_dehum_open=0, comm_mode=0, aux_type=0, "
      "language=1,         dehum_time_left:30, dehum_interval:30, weather_temp:16.900000, available_mode:77, "
      "day_or_night:2\n")


def test_zone_unknown_until_seen(d: Daemon) -> None:
    s = d.get("/api/state")
    assert s["dev"]["zone_control"] == -1 and s["ui"]["deadband"] == -1 and s["ui"]["temp_zone_mode"] == -1, s


def test_zone_state_parsed_and_wakes_long_poll(d: Daemon) -> None:
    d.append(ZONE.format(ts=ts(), zc=0, lo=68, hi=79) + UI.format(ts=ts(), db=4, zm=0))
    s = d.get("/api/state")
    assert s["dev"]["zone_control"] == 0 and s["ui"]["deadband"] == 4 and s["ui"]["temp_zone_mode"] == 0, s
    sid = s["state_id"]
    threading.Timer(0.5, d.append, [ZONE.format(ts=ts(), zc=1, lo=68, hi=77) + UI.format(ts=ts(), db=5, zm=1)]).start()
    s, dt = timed(lambda: d.get(f"/api/state?wait={sid}&timeout=10"))
    assert s["dev"]["zone_control"] == 1 and s["dev"]["zone_upper"] == 77 and dt < 3.5, (s, dt)
    assert s["ui"]["deadband"] == 5 and s["ui"]["temp_zone_mode"] == 1, s
    sid = s["state_id"]                                                 # range change alone wakes too
    threading.Timer(0.5, d.append, [ZONE.format(ts=ts(), zc=1, lo=67, hi=77)]).start()
    s, dt = timed(lambda: d.get(f"/api/state?wait={sid}&timeout=10"))
    assert s["dev"]["zone_lower"] == 67 and dt < 3.5, (s, dt)


def test_zone_control_validation(d: Daemon) -> None:
    d.append(ZONE.format(ts=ts(), zc=1, lo=68, hi=77) + UI.format(ts=ts(), db=5, zm=1))
    assert d.get("/api/state")["ui"]["deadband"] == 5
    for body, err in (({"target_temp_low": 59, "target_temp_high": 77}, "between 60 and 86 F"),
                      ({"target_temp_low": 68, "target_temp_high": 87}, "between 60 and 86 F"),
                      ({"target_temp_low": 70, "target_temp_high": 74}, "deadband (5)"),
                      ({"target_temp_low": 75}, "deadband (5)"),          # other side kept at 77
                      ({"target_temp_low": 21, "target_temp_high": 23, "unit": "C"}, "deadband (5)")):
        code, r = d.post("/api/control", body)
        assert code == 400 and err in r["error"], (body, r)
    # valid ranges pass validation and reach the send step (no IPC queue on the test host)
    for body in ({"target_temp_low": 67, "target_temp_high": 78}, {"target_temp_high": 80},
                 {"target_temp_low": 20, "target_temp_high": 26, "unit": "C"},
                 {"mode": "auto", "target_temp_low": 66, "target_temp_high": 76}):
        code, r = d.post("/api/control", body)
        assert "between" not in r.get("error", "") and "deadband" not in r.get("error", ""), (body, r)


def test_zone_celsius_validation(d: Daemon) -> None:
    d.append(_celsius_line(21) + ZONE.format(ts=ts(), zc=1, lo=20, hi=25) + UI.format(ts=ts(), db=4, zm=1))
    for body, err in (({"target_temp_low": 15, "target_temp_high": 25}, "between 16 and 30 C"),
                      ({"target_temp_low": 22, "target_temp_high": 23}, "deadband (2)")):
        code, r = d.post("/api/control", body)
        assert code == 400 and err in r["error"], (body, r)
    code, r = d.post("/api/control", {"target_temp_low": 20, "target_temp_high": 24})
    assert "between" not in r.get("error", "") and "deadband" not in r.get("error", ""), r


# Conventional 24 V mode: real lines from a Carrier TL04-1 (issue #5, 2026-10-24)
V24 = ("{ts} user.info dev_app: process_24v_cmd_a3] (dev_24v_decorder.c:<595>) rac: detect_comm_mode:1\n"
       "{ts} user.info dev_app: process_24v_cmd_a3] (dev_24v_decorder.c:<596>) rac: cpTempUnit:1 Tt:{tt} "
       "Thigh:{hi} Tlow:67 Ts:{ts_} \n"
       "{ts} user.info dev_app: process_24v_cmd_a3] (dev_24v_decorder.c:<597>) rac: "
       "rc24v_state.terminal_check_state=0 terminal_state=1f6 terminal_config=202 cpEmerHeat=0 Aaux=1 OB_dir=2\n"
       "{ts} user.info dev_app: parse_24v_terminal] (dev_24v_decorder.c:<236>) rac: "
       "terminal_set_mode=1,manual_terminal_state=0,terminal_state=76\n"
       "{ts} user.info dev_app: process_24v_cmd_a3] (dev_24v_decorder.c:<623>) rac: afterheat:0\n"
       "{ts} user.info dev_app: process_24v_cmd_a3] (dev_24v_decorder.c:<637>) rac: 24V terminal io: PEK:0 O/B:1 "
       "G:1 W2:0 W1:1 Y2:1 Y1:1 C:1 RC:1 RH:0\n"
       "{ts} user.info dev_app: process_24v_cmd_a3] (dev_24v_decorder.c:<638>) rac: rc24v_state.op_mode=77 "
       "wind_fun=1 heat_mode=6 Tt={tt}\n")


def v24(tt: float = 71.238998, hi: int = 74, sp: int = 68) -> str:
    return V24.format(ts=ts(), tt=tt, hi=hi, ts_=sp)


def test_communicating_mode_reported(d: Daemon) -> None:
    s = d.get("/api/state")
    assert s["comm_mode"] == "communicating" and s["v24"] == {"seen": False}, s


def _t_24v_parsed(d: Daemon) -> None:
    s = d.get("/api/state")
    assert s["comm_mode"] == "24v" and s["dev"]["seen"] is False and s["mode"] == "unknown", s
    v = s["v24"]
    assert v["seen"] and v["unit"] == "F" and v["room"] == 71.2 and v["setpoint"] == 68, v
    assert v["low"] == 67 and v["high"] == 74 and v["current_mode"] is None, v
    assert (v["op_mode"], v["heat_mode"], v["wind_fun"]) == ("77", "6", "1"), v
    assert (v["terminal_set_mode"], v["terminal_state"], v["terminal_state_a3"], v["terminal_config"]) == \
        ("1", "76", "1f6", "202"), v
    assert (v["emer_heat"], v["aux"], v["ob_dir"], v["afterheat"]) == ("0", "1", "2", "0"), v
    assert v["terminal_io"] == "PEK:0 O/B:1 G:1 W2:0 W1:1 Y2:1 Y1:1 C:1 RC:1 RH:0", v


def _t_24v_long_poll(d: Daemon) -> None:
    sid = d.get("/api/state")["state_id"]
    threading.Timer(0.5, d.append, [v24(tt=71.5)]).start()             # room only: no wake
    s, dt = timed(lambda: d.get(f"/api/state?wait={sid}&timeout=4"))
    assert s["state_id"] == sid and dt >= 2.5 and s["v24"]["room"] == 71.5, (dt, s["v24"])
    threading.Timer(0.5, d.append, [v24(sp=70)]).start()               # setpoint: wakes
    s, dt = timed(lambda: d.get(f"/api/state?wait={sid}&timeout=10"))
    assert s["v24"]["setpoint"] == 70 and dt < 3.5, (dt, s["v24"])
    d.append(f"{ts()} user.info dev_app: set24VMode] rc24v_state->last_mode=2,rc24v_state->current_mode=4\n")
    assert d.get("/api/state")["v24"]["current_mode"] == 4


def _t_meiju_comm_mode_only(d: Daemon) -> None:
    s = d.get("/api/state")
    assert s["comm_mode"] == "24v" and s["v24"] == {"seen": False}, s


# Real lines from the issue #5 capture (2026-10-27): wall set to Heat, then setpoint 72
HEAT_24V = ("{ts} user.info dev_app: set24VMode] <I>(dev_app/src/dev_control.c:<501>)rac: "
            "rc24v_state->last_mode=8,rc24v_state->current_mode=4\n"
            "{ts} user.info dev_app: set24VTemp] <I>(dev_app/src/dev_control.c:<638>)rac: rc24v_state mode =6 "
            "tempSet=72.000000\n"
            "{ts} user.info dev_app: set24VTemp] <I>(dev_app/src/dev_control.c:<641>)rac: rc24v_state->Ts =72\n"
            "{ts} user.info dev_app: rac_dev_24v_kv_parse] <I>(dev_app/src/dev_control.c:<1579>)rac: "
            "rc24v_state.powerState =1 mode =6 cpEmerHeat =0\n"
            "{ts} user.info dev_app: rac_dev_24v_kv_parse] <I>(dev_app/src/dev_control.c:<1580>)rac: "
            "rc24v_state.Aaux =1  Taux =3\n"
            "{ts} user.debug meiju: page_ui_refresh_background] mode=1, auto_state=0, fan_state=1, sensor_temp=7200, "
            "heatTemperValue=72, coolTemperValue=0, compressor_state=0\n"
            "{ts} user.info dev_app: process_24v_cmd_a3] <I>(dev_app/src/dev_24v_decorder.c:<597>)rac: "
            "rc24v_state.terminal_check_state=0 terminal_state=1f6 terminal_config=266 cpEmerHeat=0 Aaux=1 OB_dir=2\n")
OFF_24V = ("{ts} user.info dev_app: rac_dev_24v_kv_parse] <I>(dev_app/src/dev_control.c:<1579>)rac: "
           "rc24v_state.powerState =0 mode =6 cpEmerHeat =0\n"
           "{ts} user.debug meiju: page_ui_refresh_background] mode=4, auto_state=0, fan_state=0, sensor_temp=7300, "
           "heatTemperValue=0, coolTemperValue=0, compressor_state=0\n"
           "{ts} user.info dev_app: process_24v_cmd_a3] <I>(dev_app/src/dev_24v_decorder.c:<597>)rac: "
           "rc24v_state.terminal_check_state=0 terminal_state=1f6 terminal_config=0 cpEmerHeat=0 Aaux=1 OB_dir=2\n")


def _t_24v_heat_and_off(d: Daemon) -> None:
    d.append(HEAT_24V.format(ts=ts()) + UI.format(ts=ts(), db=4, zm=1)
             .replace("heating_max=86", "heating_max=95").replace("cooling_min=60", "cooling_min=39"))
    s = d.get("/api/state")
    v, ui = s["v24"], s["ui"]
    assert (v["power"], v["kv_mode"], v["kv_emer_heat"], v["taux"], v["current_mode"]) == (1, 6, 0, 3, 4), v
    assert v["setpoint"] == 72 and v["terminal_config"] == "266", v
    assert s["ui_mode_raw"] == 1 and (ui["auto_state"], ui["fan_state"]) == (0, 1), s
    assert (ui["heating_max"], ui["cooling_min"]) == (95, 39), ui
    sid = s["state_id"]
    threading.Timer(0.5, d.append, [OFF_24V.format(ts=ts())]).start()   # Off at the wall wakes long-polls
    s, dt = timed(lambda: d.get(f"/api/state?wait={sid}&timeout=10"))
    assert s["v24"]["power"] == 0 and s["v24"]["terminal_config"] == "0" and s["ui_mode_raw"] == 4, s
    assert dt < 3.5, dt


def _t_24v_control(d: Daemon) -> None:
    for body in ({"fan": "auto"}, {"mode": "cool", "fan": "low"}):
        code, r = d.post("/api/control", body)
        assert code == 400 and "fan speed" in r["error"], (body, r)
    for body in ({"mode": "cool"}, {"mode": "off"}, {"mode": "fan_only"}, {"mode": "emergency_heat"},
                 {"mode": "heat", "temperature": 70}, {"temperature": 70},
                 {"target_temp_low": 66, "target_temp_high": 72}, {"target_temp_high": 75}):
        code, r = d.post("/api/control", body)                          # passes validation (no IPC here)
        assert all(w not in r.get("error", "") for w in ("24 V", "between", "deadband", "mode must")), (body, r)
    code, r = d.post("/api/control", {"target_temp_low": 73})           # high kept from Thigh:74
    assert "deadband" in r["error"], r


def _t_24v_no_aux(d: Daemon) -> None:
    code, r = d.post("/api/control", {"mode": "emergency_heat"})
    assert code == 400 and "no aux heat" in r["error"], r
    code, r = d.post("/api/control", {"mode": "heat"})
    assert "24 V" not in r.get("error", ""), r


def _t_24v_celsius_setpoint(d: Daemon) -> None:
    code, r = d.post("/api/control", {"temperature": 70})               # 70 C: out of range
    assert code == 400 and "between 16 and 30 C" in r["error"], r
    code, r = d.post("/api/control", {"temperature": 70, "unit": "F"})  # 70 F -> 21 C
    assert "between" not in r.get("error", ""), r


V24_TESTS = [(_t_24v_control, lambda: v24()), (_t_24v_no_aux, lambda: v24().replace("Aaux=1", "Aaux=0")),
             (_t_24v_celsius_setpoint, lambda: v24().replace("cpTempUnit:1", "cpTempUnit:0")),
             (_t_24v_heat_and_off, lambda: v24()), (_t_24v_parsed, lambda: v24()), (_t_24v_long_poll, lambda: v24()),
             (_t_meiju_comm_mode_only, lambda: UI.format(ts=ts(), db=4, zm=1).replace("comm_mode=0", "comm_mode=1"))]


# Firmware tina.tangly.20260821.063106 (issue #8): same AHU3 lines at new source lines, plus a humidity line
AHU3_0821 = ("{ts} user.info dev_app: rac_dev_send_dev_state] <I>(dev_app/src/dev_ui_state.c:<743>)rac: "
             "RAC_DEV_AHU3_STATE, power: 1, mode: 4, op_mode =255 auto_state =4, temp_unit =1, temp_set: 72.000000, "
             "wind_speed: 3\n"
             "{ts} user.info dev_app: rac_dev_send_dev_state] <I>(dev_app/src/dev_ui_state.c:<744>)rac: "
             "RAC_DEV_AHU3_STATE, fan_state: 1,stop_fan_reach_temp=255,elec_heat: 0, elec_heat_only:0, "
             "compressor_state =1 FP=0\n"
             "{ts} user.info dev_app: rac_dev_send_dev_state] <I>(dev_app/src/dev_ui_state.c:<745>)rac: "
             "RAC_DEV_AHU3_STATE, temp_zone_control: 0, temp_zone_lower_limit =68.000000  temp_zone_upper_limit=79.000000\n"
             "{ts} user.info dev_app: rac_dev_send_dev_state] <I>(dev_app/src/dev_ui_state.c:<746>)rac: "
             "RAC_DEV_AHU3_STATE, humi_ctrl:0, humi_ctrl_fun:1, humi_set:0\n")


def _t_firmware_0821(d: Daemon) -> None:
    s = d.get("/api/state")
    assert s["comm_mode"] == "communicating" and s["mode"] == "heat" and s["setpoint"] == 72, s
    assert s["compressor_running"] and s["dev"]["zone_control"] == 0 and s["dev"]["zone_upper"] == 79, s


V24_TESTS.append((_t_firmware_0821, lambda: AHU3_0821.format(ts=ts())))


def main() -> int:
    tests: list = [v for k, v in globals().items() if k.startswith("test_")]
    tests += V24_TESTS                                      # (test, initial log) pairs
    failed = 0
    for t in tests:
        if isinstance(t, tuple):
            t, log = t
            d = Daemon(log())
        else:
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
