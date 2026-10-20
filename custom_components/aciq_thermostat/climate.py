"""Climate entity.

Mode mapping (daemon -> HA): off->OFF, auto->AUTO (single setpoint, device
deadband), heat->HEAT, cool->COOL, fan_only->FAN_ONLY, emergency_heat->HEAT
with preset "emergency_heat".
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, PRECISION_TENTHS, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AciqConfigEntry
from .const import (
    DEV_AUTO,
    DEV_COOL,
    DEV_EMERGENCY_HEAT,
    DEV_FAN_ONLY,
    DEV_HEAT,
    DEV_OFF,
    FAN_ADJUSTABLE_MODES,
    FAN_MODES,
    MAX_TEMP,
    MIN_TEMP,
    PRESET_EMERGENCY_HEAT,
    PRESET_NONE,
    TEMP_STEP,
)
from .entity import AciqEntity

DEV_TO_HVAC = {
    DEV_OFF: HVACMode.OFF,
    DEV_AUTO: HVACMode.AUTO,
    DEV_COOL: HVACMode.COOL,
    DEV_HEAT: HVACMode.HEAT,
    DEV_EMERGENCY_HEAT: HVACMode.HEAT,
    DEV_FAN_ONLY: HVACMode.FAN_ONLY,
}
HVAC_TO_DEV = {v: k for k, v in DEV_TO_HVAC.items() if k != DEV_EMERGENCY_HEAT}


async def async_setup_entry(
    hass: HomeAssistant, entry: AciqConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([AciqClimate(entry.runtime_data)])


class AciqClimate(AciqEntity, ClimateEntity):
    _attr_name = None  # use the device name
    _attr_translation_key = "thermostat"
    _attr_temperature_unit = UnitOfTemperature.FAHRENHEIT
    _attr_precision = PRECISION_TENTHS
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.AUTO, HVACMode.HEAT, HVACMode.COOL, HVACMode.FAN_ONLY]
    _attr_fan_modes = FAN_MODES
    _attr_preset_modes = [PRESET_NONE, PRESET_EMERGENCY_HEAT]
    _attr_min_temp = MIN_TEMP
    _attr_max_temp = MAX_TEMP
    _attr_target_temperature_step = TEMP_STEP

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "climate")
        self._last_on_mode = DEV_AUTO
        self._track_on_mode()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._track_on_mode()
        super()._handle_coordinator_update()

    def _track_on_mode(self) -> None:
        if self._dev_mode in (DEV_AUTO, DEV_HEAT, DEV_COOL, DEV_FAN_ONLY):
            self._last_on_mode = self._dev_mode

    @property
    def _dev_mode(self) -> str:
        return self.data.get("mode", "unknown")

    @property
    def supported_features(self) -> ClimateEntityFeature:
        f = ClimateEntityFeature.TURN_ON | ClimateEntityFeature.TURN_OFF | ClimateEntityFeature.PRESET_MODE
        if self._dev_mode in (DEV_AUTO, DEV_HEAT, DEV_COOL, DEV_EMERGENCY_HEAT):
            f |= ClimateEntityFeature.TARGET_TEMPERATURE
        if self._dev_mode in FAN_ADJUSTABLE_MODES:
            f |= ClimateEntityFeature.FAN_MODE
        return f

    @property
    def hvac_mode(self) -> HVACMode | None:
        return DEV_TO_HVAC.get(self._dev_mode)

    @property
    def preset_mode(self) -> str:
        return PRESET_EMERGENCY_HEAT if self._dev_mode == DEV_EMERGENCY_HEAT else PRESET_NONE

    @property
    def current_temperature(self) -> float | None:
        return self.data.get("current_temp")

    @property
    def current_humidity(self) -> float | None:
        return self.data.get("humidity")

    @property
    def target_temperature(self) -> float | None:
        if self.supported_features & ClimateEntityFeature.TARGET_TEMPERATURE:
            return self.data.get("setpoint")
        return None

    @property
    def fan_mode(self) -> str | None:
        fan = self.data.get("fan")
        return fan if fan in FAN_MODES else None

    @property
    def hvac_action(self) -> HVACAction:
        mode = self._dev_mode
        if mode == DEV_OFF:
            return HVACAction.OFF
        eq = self.equipment
        furnace = bool(eq.get("furnace_on"))
        compressor = bool(eq.get("compressor_hz")) or bool(self.data.get("compressor_running"))
        if furnace:
            return HVACAction.HEATING
        if compressor:
            if mode == DEV_COOL:
                return HVACAction.COOLING
            if mode in (DEV_HEAT, DEV_EMERGENCY_HEAT):
                return HVACAction.HEATING
            cur, sp = self.data.get("current_temp"), self.data.get("setpoint")
            return HVACAction.COOLING if cur is not None and sp is not None and cur > sp else HVACAction.HEATING
        if mode == DEV_FAN_ONLY or eq.get("blower_on"):
            return HVACAction.FAN
        return HVACAction.IDLE

    # ------------------------------------------------------------- control

    async def async_set_temperature(self, **kwargs: Any) -> None:
        payload: dict[str, Any] = {}
        if (hvac_mode := kwargs.get("hvac_mode")) is not None:
            payload["mode"] = HVAC_TO_DEV[hvac_mode]
        if (temperature := kwargs.get(ATTR_TEMPERATURE)) is not None:
            payload["temperature"] = int(round(temperature))
        if payload:
            await self.coordinator.async_control(payload)

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode not in HVAC_TO_DEV:
            raise HomeAssistantError(f"Unsupported mode {hvac_mode}")
        await self.coordinator.async_control({"mode": HVAC_TO_DEV[hvac_mode]})

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        if self._dev_mode not in FAN_ADJUSTABLE_MODES:
            raise HomeAssistantError(f"Fan speed can only be changed in heat or cool (now {self._dev_mode})")
        await self.coordinator.async_control({"fan": fan_mode})

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        if preset_mode == PRESET_EMERGENCY_HEAT:
            await self.coordinator.async_control({"mode": DEV_EMERGENCY_HEAT})
        elif self._dev_mode == DEV_EMERGENCY_HEAT:
            await self.coordinator.async_control({"mode": DEV_HEAT})

    async def async_turn_on(self) -> None:
        await self.coordinator.async_control({"mode": self._last_on_mode})

    async def async_turn_off(self) -> None:
        await self.coordinator.async_control({"mode": DEV_OFF})
