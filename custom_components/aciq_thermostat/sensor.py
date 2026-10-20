"""Sensors: room conditions, setpoint/mode, and equipment data from dev_app."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfFrequency,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AciqConfigEntry
from .const import FAN_MODES
from .entity import AciqEntity

MODES = ["off", "auto", "cool", "heat", "fan_only", "emergency_heat", "unknown"]


def _eq(key: str) -> Callable[[dict[str, Any], dict[str, Any]], Any]:
    return lambda d, eq: eq.get(key)


@dataclass(frozen=True, kw_only=True)
class AciqSensorDescription(SensorEntityDescription):
    value_fn: Callable[[dict[str, Any], dict[str, Any]], Any]
    needs_equipment: bool = False


TEMP = dict(
    device_class=SensorDeviceClass.TEMPERATURE,
    state_class=SensorStateClass.MEASUREMENT,
    native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
    suggested_display_precision=1,
)

SENSORS: tuple[AciqSensorDescription, ...] = (
    AciqSensorDescription(key="temperature", translation_key="temperature", **TEMP,
                          value_fn=lambda d, eq: d.get("current_temp")),
    AciqSensorDescription(key="humidity", translation_key="humidity",
                          device_class=SensorDeviceClass.HUMIDITY,
                          state_class=SensorStateClass.MEASUREMENT,
                          native_unit_of_measurement=PERCENTAGE, suggested_display_precision=0,
                          value_fn=lambda d, eq: d.get("humidity")),
    AciqSensorDescription(key="setpoint", translation_key="setpoint",
                          device_class=SensorDeviceClass.TEMPERATURE,
                          native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
                          value_fn=lambda d, eq: d.get("setpoint")),
    AciqSensorDescription(key="mode", translation_key="mode", device_class=SensorDeviceClass.ENUM,
                          options=MODES,
                          value_fn=lambda d, eq: d.get("mode") if d.get("mode") in MODES else "unknown"),
    AciqSensorDescription(key="fan_setting", translation_key="fan_setting",
                          device_class=SensorDeviceClass.ENUM, options=[*FAN_MODES, "unknown"],
                          value_fn=lambda d, eq: d.get("fan") if d.get("fan") in FAN_MODES else "unknown"),
    # ---- equipment (dev_app memory) ----
    AciqSensorDescription(key="outdoor_temperature", translation_key="outdoor_temperature", **TEMP,
                          needs_equipment=True, value_fn=_eq("outdoor_temp")),
    AciqSensorDescription(key="indoor_coil_temperature", translation_key="indoor_coil_temperature",
                          **TEMP, needs_equipment=True, value_fn=_eq("indoor_coil_temp")),
    AciqSensorDescription(key="outdoor_coil_temperature", translation_key="outdoor_coil_temperature",
                          **TEMP, needs_equipment=True, value_fn=_eq("outdoor_coil_temp")),
    AciqSensorDescription(key="indoor_unit_t1", translation_key="indoor_unit_t1", **TEMP,
                          entity_category=EntityCategory.DIAGNOSTIC,
                          needs_equipment=True, value_fn=_eq("indoor_unit_t1")),
    AciqSensorDescription(key="compressor_frequency", translation_key="compressor_frequency",
                          device_class=SensorDeviceClass.FREQUENCY,
                          state_class=SensorStateClass.MEASUREMENT,
                          native_unit_of_measurement=UnitOfFrequency.HERTZ,
                          needs_equipment=True, value_fn=_eq("compressor_hz")),
    AciqSensorDescription(key="blower_stage", translation_key="blower_stage",
                          state_class=SensorStateClass.MEASUREMENT,
                          needs_equipment=True, value_fn=_eq("blower_speed")),
    AciqSensorDescription(key="fault_codes", translation_key="fault_codes",
                          entity_category=EntityCategory.DIAGNOSTIC, needs_equipment=True,
                          value_fn=lambda d, eq: ",".join(str(c) for c in eq.get("fault_codes", []) if c)
                          or "none"),
    # ---- diagnostics ----
    AciqSensorDescription(key="wifi_signal", translation_key="wifi_signal",
                          entity_category=EntityCategory.DIAGNOSTIC,
                          state_class=SensorStateClass.MEASUREMENT,
                          native_unit_of_measurement=PERCENTAGE,
                          value_fn=lambda d, eq: d.get("wifi_rssi")),
    AciqSensorDescription(key="firmware", translation_key="firmware",
                          entity_category=EntityCategory.DIAGNOSTIC,
                          value_fn=lambda d, eq: d.get("firmware")),
    AciqSensorDescription(key="indoor_unit_firmware", translation_key="indoor_unit_firmware",
                          entity_category=EntityCategory.DIAGNOSTIC,
                          value_fn=lambda d, eq: None),  # filled from coordinator.versions
    AciqSensorDescription(key="auto_state", translation_key="auto_state",
                          entity_category=EntityCategory.DIAGNOSTIC,
                          entity_registry_enabled_default=False,
                          value_fn=lambda d, eq: d.get("dev", {}).get("auto_state")),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: AciqConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities(AciqSensor(entry.runtime_data, desc) for desc in SENSORS)


class AciqSensor(AciqEntity, SensorEntity):
    entity_description: AciqSensorDescription

    def __init__(self, coordinator, desc: AciqSensorDescription) -> None:
        super().__init__(coordinator, desc.key)
        self.entity_description = desc

    @property
    def available(self) -> bool:
        if self.entity_description.needs_equipment and not self.equipment:
            return False
        return super().available

    @property
    def native_value(self) -> Any:
        if self.entity_description.key == "indoor_unit_firmware":
            return self.coordinator.versions.get("indoor_unit_firmware")
        return self.entity_description.value_fn(self.data, self.equipment)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.key == "indoor_unit_firmware":
            return self.coordinator.versions or None
        return None
