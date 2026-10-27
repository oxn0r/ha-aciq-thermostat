"""Binary sensors: compressor, furnace, blower, faults, flags."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AciqConfigEntry
from .coordinator import AciqCoordinator
from .entity import AciqEntity

PARALLEL_UPDATES = 0  # coordinator-based, read-only


@dataclass(frozen=True, kw_only=True)
class AciqBinaryDescription(BinarySensorEntityDescription):
    value_fn: Callable[[dict[str, Any], dict[str, Any]], bool | None]
    needs_equipment: bool = False
    # communicating-only (dev_app memory / AHU3 fields) or 24 V-only (terminal outputs)
    v24: bool | None = None


def _out(d: dict[str, Any], *terminals: str) -> bool | None:
    out = d.get("v24_outputs") or {}
    if not out.get("known"):
        return None
    return bool(set(terminals) & set(out.get("active", [])))


def for_unit(desc: Any, is_24v: bool) -> bool:
    """Equipment-memory entities only exist on communicating units; terminal ones on 24 V."""
    if is_24v:
        return not desc.needs_equipment and desc.v24 is not False
    return desc.v24 is not True


BINARY: tuple[AciqBinaryDescription, ...] = (
    AciqBinaryDescription(key="compressor", translation_key="compressor",
                          device_class=BinarySensorDeviceClass.RUNNING,
                          v24=False,
                          value_fn=lambda d, eq: bool(eq.get("compressor_hz")) or bool(d.get("compressor_running"))),
    # Verified 2026-10-19: aux_load_state = 1 exactly while the gas burners were lit
    # No device class: HEAT would display off/on as "Normal"/"Hot"; translated as Off/Burning instead
    AciqBinaryDescription(key="furnace", translation_key="furnace", needs_equipment=True,
                          value_fn=lambda d, eq: bool(eq.get("furnace_on"))),
    AciqBinaryDescription(key="blower", translation_key="blower",
                          device_class=BinarySensorDeviceClass.RUNNING, needs_equipment=True,
                          value_fn=lambda d, eq: bool(eq.get("blower_on"))),
    AciqBinaryDescription(key="fault", translation_key="fault",
                          device_class=BinarySensorDeviceClass.PROBLEM, needs_equipment=True,
                          value_fn=lambda d, eq: any(eq.get("fault_codes", []))
                          or bool(eq.get("fault_state")) or bool(eq.get("protect_state"))),
    # dev_app elec_heat: backup (furnace) heat permitted in the current mode (on in Heat);
    # not "furnace burning" -- that's the furnace sensor. Diagnostic, still enabled.
    AciqBinaryDescription(key="aux_heat_allowed", translation_key="aux_heat_allowed",
                          entity_category=EntityCategory.DIAGNOSTIC, v24=False,
                          value_fn=lambda d, eq: bool(d.get("dev", {}).get("elec_heat"))),
    AciqBinaryDescription(key="emergency_heat", translation_key="emergency_heat",
                          value_fn=lambda d, eq: bool(d.get("dev", {}).get("elec_heat_only"))),
    AciqBinaryDescription(key="power", translation_key="power",
                          device_class=BinarySensorDeviceClass.POWER,
                          value_fn=lambda d, eq: bool(d.get("dev", {}).get("power"))),
    AciqBinaryDescription(key="control_enabled", translation_key="control_enabled",
                          entity_category=EntityCategory.DIAGNOSTIC,
                          value_fn=lambda d, eq: bool(d.get("control_enabled")) and not d.get("read_only")),
    # ---- conventional 24 V: outputs the thermostat drives (terminal_config), read-only ----
    AciqBinaryDescription(key="compressor_call", translation_key="compressor_call", v24=True,
                          device_class=BinarySensorDeviceClass.RUNNING,
                          value_fn=lambda d, eq: _out(d, "Y1", "Y2")),
    AciqBinaryDescription(key="compressor_stage_2", translation_key="compressor_stage_2", v24=True,
                          device_class=BinarySensorDeviceClass.RUNNING,
                          value_fn=lambda d, eq: _out(d, "Y2")),
    AciqBinaryDescription(key="aux_heat_call", translation_key="aux_heat_call", v24=True,
                          device_class=BinarySensorDeviceClass.RUNNING,
                          value_fn=lambda d, eq: _out(d, "W1", "W2")),
    AciqBinaryDescription(key="fan_call", translation_key="fan_call", v24=True,
                          device_class=BinarySensorDeviceClass.RUNNING,
                          value_fn=lambda d, eq: _out(d, "G")),
    AciqBinaryDescription(key="reversing_valve", translation_key="reversing_valve", v24=True,
                          entity_category=EntityCategory.DIAGNOSTIC,
                          value_fn=lambda d, eq: _out(d, "O/B")),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: AciqConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    c = entry.runtime_data
    is_24v = (c.data or {}).get("read_only") == "24v"
    async_add_entities(AciqBinarySensor(c, desc) for desc in BINARY if for_unit(desc, is_24v))


class AciqBinarySensor(AciqEntity, BinarySensorEntity):
    entity_description: AciqBinaryDescription

    def __init__(self, coordinator: AciqCoordinator, desc: AciqBinaryDescription) -> None:
        super().__init__(coordinator, desc.key)
        self.entity_description = desc

    @property
    def available(self) -> bool:
        if self.entity_description.needs_equipment and not self.equipment:
            return False
        return super().available

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.value_fn(self.data, self.equipment)
