"""System type: which equipment the thermostat runs, from the signals real units report."""

from __future__ import annotations

import pytest

from custom_components.aciq_thermostat.system import SYSTEM_TYPES, describe

TERMINALS = ["O/B", "G", "W2", "W1", "Y2", "Y1", "C", "RC"]


def _comm(aux):
    return {"comm_mode": "communicating", "ui": {"aux_heat_open": aux}}


def _v24(wired, taux=-1):
    return {"comm_mode": "24v", "wiring": "24v", "v24": {"taux": taux}, "v24_outputs": {"wired": wired}}


@pytest.mark.parametrize(
    ("data", "system", "outdoor", "heat", "indoor"),
    [
        # communicating: the author's ACiQ dual fuel (aux 1), Midea / MrCool heat pumps (#8, #13, #16: aux 0)
        (_comm(1), "heat_pump_furnace", "heat_pump", "furnace", "furnace"),
        (_comm(0), "heat_pump", "heat_pump", "none", "air_handler"),
        (_comm(-1), "unknown", "heat_pump", "unknown", "air_handler"),
        # 24 V, #5: two-stage heat pump, hydronic boiler on W1, Taux 3
        (_v24(["O/B", "G", "W1", "Y2", "Y1", "C", "RC"], 3), "heat_pump_boiler", "heat_pump", "boiler", "air_handler"),
        (_v24(["O/B", "G", "W1", "Y1", "C", "RC"], 1), "heat_pump_electric", "heat_pump", "electric", "air_handler"),
        (_v24(["O/B", "G", "W1", "Y1", "C", "RC"], 2), "heat_pump_furnace", "heat_pump", "furnace", "furnace"),
        (_v24(["O/B", "G", "Y1", "C", "RC"]), "heat_pump", "heat_pump", "none", "air_handler"),
        (_v24(["G", "W1", "Y1", "C", "RC"]), "air_conditioner_furnace", "air_conditioner", "furnace", "furnace"),
        (_v24(["G", "Y1", "C", "RC"]), "air_conditioner", "air_conditioner", "none", "air_handler"),
        (_v24(["G", "W1", "W2", "C", "RC"]), "furnace", "none", "furnace", "furnace"),
        (_v24(["G", "W1", "C", "RC"], 1), "electric", "none", "electric", "air_handler"),
        (_v24(["W1", "C", "RC"], 3), "boiler", "none", "boiler", "none"),
        (_v24([]), "unknown", "unknown", "unknown", "unknown"),
        ({}, "unknown", "unknown", "unknown", "unknown"),
    ],
)
def test_describe(data, system, outdoor, heat, indoor) -> None:
    d = describe(data)
    assert (d["system"], d["outdoor_unit"], d["heat_source"], d["indoor_unit"]) == (system, outdoor, heat, indoor)
    assert d["system"] in SYSTEM_TYPES


def test_24v_stages_and_blower() -> None:
    d = describe(_v24(["O/B", "G", "W1", "W2", "Y2", "Y1", "C", "RC"], 1))
    assert (d["compressor_stages"], d["heat_stages"], d["blower"], d["wiring"]) == (2, 2, True, "24v")
    assert d["detected_from"] == "wired C, G, O/B, RC, W1, W2, Y1, Y2; Taux=1"
    d = describe(_v24(["W1", "C", "RC"]))
    assert (d["compressor_stages"], d["heat_stages"], d["blower"]) == (0, 1, False)
    assert d["heat_source"] == "furnace"           # no type setting: W1 reads as a furnace


def test_communicating_detail() -> None:
    d = describe(_comm(0))
    assert d["wiring"] == "communicating" and d["blower"] is True
    assert d["detected_from"] == "aux_heat_open=0"
    assert d["compressor_stages"] is None


@pytest.mark.parametrize(
    "data",
    [
        {"comm_mode": "communicating", "ui": None},
        {"comm_mode": "communicating", "ui": "x"},
        {"comm_mode": "communicating", "ui": {"aux_heat_open": True}},      # a bool is not the setting
        {"comm_mode": "communicating", "ui": {"aux_heat_open": "1"}},
        {"comm_mode": "communicating", "ui": {"aux_heat_open": 7}},
        {"wiring": "24v", "v24": None, "v24_outputs": None},
        {"wiring": "24v", "v24": {"taux": True}, "v24_outputs": {"wired": ["W1", 5, None]}},
        {"wiring": "24v", "v24": {"taux": "3"}, "v24_outputs": {"wired": "W1,G"}},
        {"wiring": "24v", "v24": {"taux": 9}, "v24_outputs": {"wired": ["W1"]}},
    ],
)
def test_odd_input_never_raises_or_guesses(data) -> None:
    d = describe(data)
    assert d["system"] in SYSTEM_TYPES
    assert d["heat_source"] in ("furnace", "unknown", "none")       # malformed type settings: no electric/boiler


def test_every_wiring_maps_to_a_known_system() -> None:
    """All 256 terminal combinations x every Taux value: always a listed state, consistent parts."""
    for mask in range(1 << len(TERMINALS)):
        wired = [t for i, t in enumerate(TERMINALS) if mask >> i & 1]
        for taux in (-1, 0, 1, 2, 3, 4, None):
            d = describe(_v24(wired, taux))
            assert d["system"] in SYSTEM_TYPES, (wired, taux)
            if not wired:
                continue
            has_out = d["outdoor_unit"] != "none"
            assert has_out == bool({"O/B", "Y1", "Y2"} & set(wired))
            assert (d["heat_source"] != "none") == bool({"W1", "W2"} & set(wired))
            assert d["blower"] == ("G" in wired)
            if d["heat_source"] == "furnace":
                assert d["indoor_unit"] == "furnace"
            assert d["system"] != "unknown" or (not has_out and d["heat_source"] == "none")


def test_translations_cover_every_state_and_attribute() -> None:
    import json
    from pathlib import Path

    root = Path(__file__).parents[1] / "custom_components" / "aciq_thermostat"
    for f in (root / "strings.json", root / "translations" / "en.json"):
        t = json.loads(f.read_text())["entity"]["sensor"]["system_type"]
        assert set(t["state"]) == set(SYSTEM_TYPES)
        sample = describe(_v24(["O/B", "G", "W1", "Y1"], 3))
        assert set(t["state_attributes"]) == set(sample) - {"system"}
