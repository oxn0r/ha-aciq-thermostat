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
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AciqConfigEntry
from .const import (
    AUTO_SETPOINT_RANGE,
    AUTO_SETPOINT_SINGLE,
    AUTO_SETPOINT_UNKNOWN,
    AUTO_STATE_NAMES,
    FAN_MODES,
)
from .coordinator import AciqCoordinator
from .entity import AciqEntity

PARALLEL_UPDATES = 0  # coordinator-based, read-only

MODES = ["off", "auto", "cool", "heat", "fan_only", "emergency_heat", "unknown"]


def _eq(key: str) -> Callable[[dict[str, Any], dict[str, Any]], Any]:
    return lambda d, eq: eq.get(key)


@dataclass(frozen=True, kw_only=True)
class AciqSensorDescription(SensorEntityDescription):
    value_fn: Callable[[dict[str, Any], dict[str, Any]], Any]
    needs_equipment: bool = False
    # value from the coordinator itself (versions, boot/daemon times) instead of the state
    coord_fn: Callable[[AciqCoordinator], Any] | None = None


def _auto_state(d: dict[str, Any]) -> str:
    # The thermostat keeps its last Auto decision while in Heat/Cool; it only means
    # something in Auto, so report "none" otherwise.
    if d.get("mode") != "auto":
        return "none"
    return AUTO_STATE_NAMES.get(d.get("dev", {}).get("auto_state", 0), "none")


def _auto_setpoint(d: dict[str, Any]) -> str:
    zc = d.get("dev", {}).get("zone_control")
    return AUTO_SETPOINT_RANGE if zc == 1 else AUTO_SETPOINT_SINGLE if zc == 0 else AUTO_SETPOINT_UNKNOWN


def _zone(key: str) -> Callable[[dict[str, Any], dict[str, Any]], Any]:
    # stored on every thermostat, but only used while Auto holds a range
    return lambda d, eq: d.get("dev", {}).get(key) if _auto_setpoint(d) == AUTO_SETPOINT_RANGE else None


def _deadband(d: dict[str, Any], eq: dict[str, Any]) -> Any:
    db = d.get("ui", {}).get("deadband")
    return db if isinstance(db, int) and db >= 0 else None


# Reported in the thermostat's own unit (follow it when it is set to Celsius)
THERMOSTAT_UNIT_KEYS = ("setpoint", "auto_heat_setpoint", "auto_cool_setpoint", "deadband")

TEMP: dict[str, Any] = dict(
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
                          entity_registry_enabled_default=False,
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
                          entity_registry_enabled_default=False,
                          state_class=SensorStateClass.MEASUREMENT,
                          native_unit_of_measurement=PERCENTAGE,
                          value_fn=lambda d, eq: d.get("wifi_rssi")),
    AciqSensorDescription(key="firmware", translation_key="firmware",
                          entity_category=EntityCategory.DIAGNOSTIC,
                          value_fn=lambda d, eq: d.get("firmware")),
    AciqSensorDescription(key="indoor_unit_firmware", translation_key="indoor_unit_firmware",
                          entity_category=EntityCategory.DIAGNOSTIC,
                          value_fn=lambda d, eq: None,
                          coord_fn=lambda c: c.versions.get("indoor_unit_firmware")),
    AciqSensorDescription(key="auto_state", translation_key="auto_state",
                          device_class=SensorDeviceClass.ENUM,
                          options=list(AUTO_STATE_NAMES.values()),
                          entity_category=EntityCategory.DIAGNOSTIC,
                          entity_registry_enabled_default=False,
                          value_fn=lambda d, eq: _auto_state(d)),
    AciqSensorDescription(key="auto_setpoint_type", translation_key="auto_setpoint_type",
                          device_class=SensorDeviceClass.ENUM,
                          options=[AUTO_SETPOINT_SINGLE, AUTO_SETPOINT_RANGE, AUTO_SETPOINT_UNKNOWN],
                          entity_category=EntityCategory.DIAGNOSTIC,
                          value_fn=lambda d, eq: _auto_setpoint(d)),
    AciqSensorDescription(key="auto_heat_setpoint", translation_key="auto_heat_setpoint",
                          device_class=SensorDeviceClass.TEMPERATURE,
                          native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
                          entity_category=EntityCategory.DIAGNOSTIC,
                          value_fn=_zone("zone_lower")),
    AciqSensorDescription(key="auto_cool_setpoint", translation_key="auto_cool_setpoint",
                          device_class=SensorDeviceClass.TEMPERATURE,
                          native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
                          entity_category=EntityCategory.DIAGNOSTIC,
                          value_fn=_zone("zone_upper")),
    # a temperature difference: no temperature device class (HA would convert it as absolute)
    AciqSensorDescription(key="deadband", translation_key="deadband",
                          native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
                          entity_category=EntityCategory.DIAGNOSTIC,
                          value_fn=_deadband),
    AciqSensorDescription(key="clock_offset", translation_key="clock_offset",
                          device_class=SensorDeviceClass.DURATION,
                          native_unit_of_measurement=UnitOfTime.SECONDS,
                          state_class=SensorStateClass.MEASUREMENT,
                          suggested_display_precision=1,
                          entity_category=EntityCategory.DIAGNOSTIC,
                          value_fn=lambda d, eq: None,
                          coord_fn=lambda c: c.clock.get("offset_after_s", c.clock.get("offset_s"))),
    AciqSensorDescription(key="last_boot", translation_key="last_boot",
                          device_class=SensorDeviceClass.TIMESTAMP,
                          entity_category=EntityCategory.DIAGNOSTIC,
                          value_fn=lambda d, eq: None, coord_fn=lambda c: c.boot_time),
    AciqSensorDescription(key="daemon_started", translation_key="daemon_started",
                          device_class=SensorDeviceClass.TIMESTAMP,
                          entity_category=EntityCategory.DIAGNOSTIC,
                          value_fn=lambda d, eq: None, coord_fn=lambda c: c.daemon_start_time),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: AciqConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities(AciqSensor(entry.runtime_data, desc) for desc in SENSORS)


class AciqSensor(AciqEntity, SensorEntity):
    entity_description: AciqSensorDescription

    def __init__(self, coordinator: AciqCoordinator, desc: AciqSensorDescription) -> None:
        super().__init__(coordinator, desc.key)
        self.entity_description = desc

    @property
    def available(self) -> bool:
        if self.entity_description.needs_equipment and not self.equipment:
            return False
        return super().available

    @property
    def native_unit_of_measurement(self) -> str | None:
        # setpoints are reported in the thermostat's own unit
        if self.entity_description.key in THERMOSTAT_UNIT_KEYS and self.data.get("temp_unit") == "C":
            return UnitOfTemperature.CELSIUS
        return self.entity_description.native_unit_of_measurement

    @property
    def native_value(self) -> Any:
        if self.entity_description.coord_fn is not None:
            return self.entity_description.coord_fn(self.coordinator)
        return self.entity_description.value_fn(self.data, self.equipment)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        key = self.entity_description.key
        if key == "indoor_unit_firmware":
            return self.coordinator.versions or None
        if key == "clock_offset":
            c = self.coordinator.clock
            if not c:
                return None
            def _iso(v: Any) -> Any:
                return v.isoformat() if hasattr(v, "isoformat") else v
            return {"measured_offset_s": c.get("offset_s"), "last_checked": _iso(c.get("checked_at")),
                    "last_correction": _iso(c.get("last_correction")),
                    "last_correction_offset_s": c.get("last_correction_offset_s"),
                    "sync_enabled": self.coordinator.sync_clock_enabled}
        if key == "daemon_started":
            d = self.coordinator.last_deploy_detail
            if not d:
                return None
            return {"last_deploy": d.get("time"), "last_deploy_reason": d.get("explanation"),
                    "cause": d.get("cause"), "trigger": d.get("trigger"),
                    "thermostat_uptime_at_deploy_s": d.get("thermostat_uptime_s")}
        return None
