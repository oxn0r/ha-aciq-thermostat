"""Which equipment the thermostat runs, from what it reports (the System type sensor, the system card).

Communicating (HA/HB bus to an inverter indoor unit):
- every one seen so far has an inverter heat pump outdoors;
- ui.aux_heat_open is the thermostat's installer setting for backup heat: 0 = none, so the indoor unit
  is an air handler with only its coil and blower (issue #8 Midea EVOX G3, #13 Midea, #16); 1 = backup
  heat installed. The only one seen is a gas furnace under the coil (dual fuel, the author's ACiQ), and
  the indoor unit doesn't say which kind it is, so 1 reads as a furnace. An air handler with electric
  heat strips looks the same: the card's `heat_source: electric` draws it instead.

Conventional 24 V (terminal block):
- the wired terminals (terminal_state) give the equipment: O/B = heat pump, Y1 without O/B = air
  conditioner, Y2 / W2 = second stage, W1/W2 = heat, G = a blower;
- Taux, the thermostat's aux heat type setting, uses dev_app's AUX_TYPE enum: 1 electric, 2 furnace,
  3 boiler (issue #5: a two-stage heat pump with a hydronic boiler on W1 reports Taux 3).
"""

from __future__ import annotations

from typing import Any

HEAT_PUMP = "heat_pump"
AIR_CONDITIONER = "air_conditioner"
FURNACE = "furnace"
ELECTRIC = "electric"
BOILER = "boiler"
AIR_HANDLER = "air_handler"
NONE = "none"
UNKNOWN = "unknown"

AUX_TYPES = {1: ELECTRIC, 2: FURNACE, 3: BOILER}   # dev_app AUX_TYPE_ELEC / _FURNACE / _BOILER

# System type sensor states: outdoor unit + heat source
SYSTEM_TYPES = [
    "heat_pump", "heat_pump_furnace", "heat_pump_electric", "heat_pump_boiler",
    "air_conditioner", "air_conditioner_furnace", "air_conditioner_electric", "air_conditioner_boiler",
    "furnace", "electric", "boiler", "unknown",
]


def _wired(data: dict[str, Any]) -> set[str]:
    return set((data.get("v24_outputs") or {}).get("wired") or [])


def describe(data: dict[str, Any]) -> dict[str, Any]:
    """Equipment summary: outdoor unit, heat source and the parts the card draws."""
    if data.get("wiring") == "24v":
        return _describe_24v(data)
    if data.get("comm_mode") == "communicating":
        aux = (data.get("ui") or {}).get("aux_heat_open")
        heat = NONE if aux == 0 else FURNACE if aux == 1 else UNKNOWN
        return _summary("communicating", HEAT_PUMP, heat, heat_stages=None, compressor_stages=None,
                        blower=True, source=f"aux_heat_open={aux if aux in (0, 1) else 'unknown'}")
    return _summary(None, UNKNOWN, UNKNOWN, heat_stages=None, compressor_stages=None, blower=None,
                    source="not reported yet")


def _describe_24v(data: dict[str, Any]) -> dict[str, Any]:
    wired = _wired(data)
    if not wired:
        return _summary("24v", UNKNOWN, UNKNOWN, heat_stages=None, compressor_stages=None, blower=None,
                        source="wiring not reported yet")
    outdoor = HEAT_PUMP if "O/B" in wired else AIR_CONDITIONER if wired & {"Y1", "Y2"} else NONE
    taux = (data.get("v24") or {}).get("taux")
    if wired & {"W1", "W2"}:
        # no type setting reported: a furnace, the usual W1 equipment
        heat = AUX_TYPES.get(taux, FURNACE) if isinstance(taux, int) else FURNACE
    else:
        heat = NONE
    return _summary(
        "24v", outdoor, heat,
        compressor_stages=(2 if "Y2" in wired else 1) if outdoor != NONE else 0,
        heat_stages=(2 if "W2" in wired else 1) if heat != NONE else 0,
        blower="G" in wired,
        source=f"wired {', '.join(sorted(wired))}; Taux={taux if isinstance(taux, int) and taux >= 0 else 'unknown'}",
    )


def _summary(wiring: str | None, outdoor: str, heat: str, *, compressor_stages: int | None,
             heat_stages: int | None, blower: bool | None, source: str) -> dict[str, Any]:
    if outdoor == UNKNOWN and heat == UNKNOWN:
        system = UNKNOWN
    elif outdoor in (HEAT_PUMP, AIR_CONDITIONER):
        system = outdoor if heat == NONE else UNKNOWN if heat == UNKNOWN else f"{outdoor}_{heat}"
    else:
        system = heat if heat in (FURNACE, ELECTRIC, BOILER) else UNKNOWN
    # what the heat and blower live in indoors
    if heat == FURNACE:
        indoor = FURNACE
    elif blower is False:
        indoor = NONE                     # e.g. a boiler with radiators: no air handler
    elif outdoor in (HEAT_PUMP, AIR_CONDITIONER) or heat in (ELECTRIC, BOILER):
        indoor = AIR_HANDLER
    else:
        indoor = UNKNOWN
    return {
        "system": system, "wiring": wiring, "outdoor_unit": outdoor, "heat_source": heat,
        "indoor_unit": indoor, "compressor_stages": compressor_stages, "heat_stages": heat_stages,
        "blower": blower, "detected_from": source,
    }
