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
from .entity import AciqEntity


@dataclass(frozen=True, kw_only=True)
class AciqBinaryDescription(BinarySensorEntityDescription):
    value_fn: Callable[[dict[str, Any], dict[str, Any]], bool | None]
    needs_equipment: bool = False


BINARY: tuple[AciqBinaryDescription, ...] = (
    AciqBinaryDescription(key="compressor", translation_key="compressor",
                          device_class=BinarySensorDeviceClass.RUNNING,
                          value_fn=lambda d, eq: bool(eq.get("compressor_hz")) or bool(d.get("compressor_running"))),
    # Verified 2026-10-19: aux_load_state = 1 exactly while the gas burners were lit
    AciqBinaryDescription(key="furnace", translation_key="furnace",
                          device_class=BinarySensorDeviceClass.HEAT, needs_equipment=True,
                          value_fn=lambda d, eq: bool(eq.get("furnace_on"))),
    AciqBinaryDescription(key="blower", translation_key="blower",
                          device_class=BinarySensorDeviceClass.RUNNING, needs_equipment=True,
                          value_fn=lambda d, eq: bool(eq.get("blower_on"))),
    AciqBinaryDescription(key="fault", translation_key="fault",
                          device_class=BinarySensorDeviceClass.PROBLEM, needs_equipment=True,
                          value_fn=lambda d, eq: any(eq.get("fault_codes", []))
                          or bool(eq.get("fault_state")) or bool(eq.get("protect_state"))),
    AciqBinaryDescription(key="aux_heat_allowed", translation_key="aux_heat_allowed",
                          value_fn=lambda d, eq: bool(d.get("dev", {}).get("elec_heat"))),
    AciqBinaryDescription(key="emergency_heat", translation_key="emergency_heat",
                          value_fn=lambda d, eq: bool(d.get("dev", {}).get("elec_heat_only"))),
    AciqBinaryDescription(key="power", translation_key="power",
                          device_class=BinarySensorDeviceClass.POWER,
                          value_fn=lambda d, eq: bool(d.get("dev", {}).get("power"))),
    AciqBinaryDescription(key="control_enabled", translation_key="control_enabled",
                          entity_category=EntityCategory.DIAGNOSTIC,
                          value_fn=lambda d, eq: d.get("control_enabled")),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: AciqConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities(AciqBinarySensor(entry.runtime_data, desc) for desc in BINARY)


class AciqBinarySensor(AciqEntity, BinarySensorEntity):
    entity_description: AciqBinaryDescription

    def __init__(self, coordinator, desc: AciqBinaryDescription) -> None:
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
