"""Conventional 24 V thermostats (issue #5): read-only view of the daemon's raw v24 block.

Mapped from a supervised capture on a Carrier TL04-1 (2026-10-27):
- meiju UI mode: 0 auto, 1 heat, 2 cool, 3 aux/emergency heat, 4 off (dev_app powerState=0)
- Ts = single setpoint; Tlow/Thigh = Auto heat/cool pair (Auto holds a range when temp_zone_mode=1)
- UI auto_state: 2 heating demand, 1 cooling demand, 0 none
- terminal_state / terminal_config are bitmasks in the "24V terminal io" order (bit 0 first):
  terminal_state = wired terminals, terminal_config = outputs the thermostat drives now;
  0x200 is set in terminal_config whenever the thermostat is on (RH wasn't wired there)

Not yet confirmed against equipment running for long: whether terminal_config stays on
through compressor/stage delays. Nothing here sends commands.
"""

from __future__ import annotations

from typing import Any

TERMINALS = ("PEK", "O/B", "G", "W2", "W1", "Y2", "Y1", "C", "RC", "RH")
POWER_BIT = 0x200
CALL_TERMINALS = ("Y1", "Y2", "W1", "W2", "G", "O/B")   # outputs that call equipment
UI_MODES = {0: "auto", 1: "heat", 2: "cool", 3: "emergency_heat", 4: "off"}
READ_ONLY_24V = "24v"


def _hex(raw: Any) -> int | None:
    try:
        return int(str(raw), 16)
    except ValueError:
        return None


def decode(raw: Any, *, config: bool = False) -> list[str] | None:
    """Terminal names set in a bitmask; for terminal_config the power bit is left out."""
    value = _hex(raw)
    if value is None:
        return None
    if config:
        value &= ~POWER_BIT
    return [name for bit, name in enumerate(TERMINALS) if value >> bit & 1]


def outputs(v24: dict[str, Any]) -> dict[str, Any]:
    active = decode(v24.get("terminal_config"), config=True)
    cfg = _hex(v24.get("terminal_config"))
    return {
        "active": active if active is not None else [],
        "known": active is not None,
        "powered": bool(cfg & POWER_BIT) if cfg is not None else None,
        "wired": decode(v24.get("terminal_state_a3")) or [],
        "raw_config": v24.get("terminal_config"),
        "raw_wiring": v24.get("terminal_state_a3"),
    }


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def normalize(data: dict[str, Any]) -> dict[str, Any]:
    """Fill the communicating-mode fields entities use from the 24 V block (read-only).

    Communicating thermostats, and 24 V ones before any 24 V state line was seen, are
    returned unchanged.
    """
    v = data.get("v24") or {}
    if data.get("comm_mode") != "24v" or not v.get("seen"):
        return data
    ui = data.get("ui") or {}
    mode = UI_MODES.get(data.get("ui_mode_raw"), "unknown")       # type: ignore[arg-type]
    if v.get("power") == 0:
        mode = "off"
    out = outputs(v)
    zm = ui.get("temp_zone_mode")
    auto_state = ui.get("auto_state")
    norm = dict(data)
    norm.update(
        mode=mode,
        setpoint=_num(v.get("setpoint")),
        current_temp=_num(v.get("room")) or data.get("current_temp"),
        temp_unit="C" if v.get("unit") == "C" else "F",
        fan="unknown",
        compressor_running=bool({"Y1", "Y2"} & set(out["active"])),
        equipment={"available": False},
        read_only=READ_ONLY_24V,
        v24_outputs=out,
    )
    dev = dict(data.get("dev") or {})
    dev.update(
        seen=True, source="24v", power=0 if mode == "off" else 1,
        auto_state=auto_state if isinstance(auto_state, int) and auto_state >= 0 else 0,
        zone_control=1 if zm == 1 else 0 if zm == 0 else -1,
        zone_lower=_num(v.get("low")), zone_upper=_num(v.get("high")),
        temp_set=_num(v.get("setpoint")), elec_heat_only=1 if mode == "emergency_heat" else 0,
    )
    norm["dev"] = dev
    return norm


def signature(data: dict[str, Any]) -> dict[str, Any]:
    """Fields worth a log line / diagnostics event when they change (room temperature excluded)."""
    v = data.get("v24") or {}
    ui = data.get("ui") or {}
    out = data.get("v24_outputs") or {}
    return {
        "mode": data.get("mode"), "setpoint": data.get("setpoint"),
        "auto_low": v.get("low"), "auto_high": v.get("high"),
        "outputs": "+".join(out.get("active", [])) or "none",
        "auto_state": ui.get("auto_state"), "fan_state": ui.get("fan_state"),
        "power": v.get("power"), "kv_mode": v.get("kv_mode"), "emer_heat": v.get("kv_emer_heat"),
        "set24v_mode": v.get("current_mode"), "op_mode": v.get("op_mode"), "heat_mode": v.get("heat_mode"),
        "wind_fun": v.get("wind_fun"), "afterheat": v.get("afterheat"),
    }
