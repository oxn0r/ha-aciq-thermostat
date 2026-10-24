"""Climate entity.

Mode mapping (daemon -> HA): off->OFF, auto->AUTO (single setpoint, device
deadband), heat->HEAT, cool->COOL, fan_only->FAN_ONLY, emergency_heat->HEAT
with preset "emergency_heat".
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import ClimateEntityFeature, HVACAction, HVACMode
from homeassistant.const import ATTR_TEMPERATURE, PRECISION_TENTHS, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AciqConfigEntry
from .const import (
    AUTO_STATE_COOL,
    AUTO_STATE_DRY,
    AUTO_STATE_HEAT,
    COIL_COOLING_DELTA,
    COIL_HEATING_DELTA,
    DOMAIN,
    DEV_AUTO,
    DEV_COOL,
    DEV_EMERGENCY_HEAT,
    DEV_FAN_ONLY,
    DEV_HEAT,
    DEV_OFF,
    FAN_ADJUSTABLE_MODES,
    FAN_MODES,
    MAX_TEMP,
    MAX_TEMP_C,
    MIN_TEMP,
    MIN_TEMP_C,
    PRESET_EMERGENCY_HEAT,
    PRESET_NONE,
    TEMP_STEP,
)
from .coordinator import AciqCoordinator
from .entity import AciqEntity


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

DEV_TO_HVAC = {
    DEV_OFF: HVACMode.OFF,
    DEV_AUTO: HVACMode.AUTO,
    DEV_COOL: HVACMode.COOL,
    DEV_HEAT: HVACMode.HEAT,
    DEV_EMERGENCY_HEAT: HVACMode.HEAT,
    DEV_FAN_ONLY: HVACMode.FAN_ONLY,
}
HVAC_TO_DEV = {v: k for k, v in DEV_TO_HVAC.items() if k != DEV_EMERGENCY_HEAT}

PARALLEL_UPDATES = 1  # one command at a time to the thermostat


async def async_setup_entry(
    hass: HomeAssistant, entry: AciqConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([AciqClimate(entry.runtime_data)])


class AciqClimate(AciqEntity, ClimateEntity):
    _attr_name = None  # use the device name
    _attr_translation_key = "thermostat"
    _attr_precision = PRECISION_TENTHS
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.AUTO, HVACMode.HEAT, HVACMode.COOL, HVACMode.FAN_ONLY]
    _attr_fan_modes = FAN_MODES
    _attr_preset_modes = [PRESET_NONE, PRESET_EMERGENCY_HEAT]
    _attr_target_temperature_step = TEMP_STEP

    def __init__(self, coordinator: AciqCoordinator) -> None:
        super().__init__(coordinator, "climate")
        self._last_on_mode = DEV_AUTO
        # last confirmed direction while the compressor runs in Auto (held when unknown)
        self._auto_direction: HVACAction | None = None
        self._action = HVACAction.IDLE
        self._track_on_mode()
        self._action = self._compute_action()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._track_on_mode()
        self._action = self._compute_action()
        super()._handle_coordinator_update()

    def _track_on_mode(self) -> None:
        if self._dev_mode in (DEV_AUTO, DEV_HEAT, DEV_COOL, DEV_FAN_ONLY):
            self._last_on_mode = self._dev_mode

    @property
    def _dev_mode(self) -> str:
        return str(self.data.get("mode", "unknown"))

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

    def _celsius(self) -> bool:
        """The thermostat's own unit (its setpoint is reported and set in it)."""
        return self.data.get("temp_unit") == "C"

    @property
    def temperature_unit(self) -> str:
        return UnitOfTemperature.CELSIUS if self._celsius() else UnitOfTemperature.FAHRENHEIT

    @property
    def min_temp(self) -> float:
        return MIN_TEMP_C if self._celsius() else MIN_TEMP

    @property
    def max_temp(self) -> float:
        return MAX_TEMP_C if self._celsius() else MAX_TEMP

    @property
    def current_temperature(self) -> float | None:
        t = _num(self.data.get("current_temp"))          # always degF from the daemon
        if t is not None and self._celsius():
            return round((t - 32) * 5 / 9, 1)
        return t

    @property
    def current_humidity(self) -> float | None:
        return _num(self.data.get("humidity"))

    @property
    def target_temperature(self) -> float | None:
        if self.supported_features & ClimateEntityFeature.TARGET_TEMPERATURE:
            return _num(self.data.get("setpoint"))
        return None

    @property
    def fan_mode(self) -> str | None:
        fan = self.data.get("fan")
        return fan if isinstance(fan, str) and fan in FAN_MODES else None

    @property
    def hvac_action(self) -> HVACAction:
        return self._action

    def _compute_action(self) -> HVACAction:
        """Derive what the equipment is doing from real equipment state.

        Furnace burning -> heating. Compressor running -> direction from the mode;
        in Auto from the thermostat's own auto_state, else from the indoor coil
        temperature, else the last confirmed direction. Never from room vs setpoint
        (Auto's deadband keeps the room on either side of the setpoint while running).
        """
        mode = self._dev_mode
        if mode == DEV_OFF:
            return HVACAction.OFF
        eq = self.equipment
        if eq.get("furnace_on"):
            return HVACAction.HEATING
        compressor = bool(eq.get("compressor_hz")) or bool(self.data.get("compressor_running"))
        if compressor:
            if mode == DEV_COOL:
                return HVACAction.COOLING
            if mode in (DEV_HEAT, DEV_EMERGENCY_HEAT):
                return HVACAction.HEATING
            return self._auto_compressor_action(eq)
        if mode == DEV_FAN_ONLY or eq.get("blower_on"):
            return HVACAction.FAN
        return HVACAction.IDLE

    def _auto_compressor_action(self, eq: dict[str, Any]) -> HVACAction:
        auto_state = self.data.get("dev", {}).get("auto_state")
        direction: HVACAction | None = None
        if auto_state == AUTO_STATE_HEAT:
            direction = HVACAction.HEATING
        elif auto_state == AUTO_STATE_COOL:
            direction = HVACAction.COOLING
        elif auto_state == AUTO_STATE_DRY:
            direction = HVACAction.DRYING
        else:
            coil, room = _num(eq.get("indoor_coil_temp")), _num(self.data.get("current_temp"))
            if coil is not None and room is not None:
                if coil - room >= COIL_HEATING_DELTA:
                    direction = HVACAction.HEATING
                elif room - coil >= COIL_COOLING_DELTA:
                    direction = HVACAction.COOLING
        if direction is not None:
            self._auto_direction = direction
            return direction
        # unknown: keep the last confirmed direction rather than guessing
        return self._auto_direction or HVACAction.IDLE

    # ------------------------------------------------------------- control

    async def async_set_temperature(self, **kwargs: Any) -> None:
        payload: dict[str, Any] = {}
        if (hvac_mode := kwargs.get("hvac_mode")) is not None:
            payload["mode"] = HVAC_TO_DEV[hvac_mode]
        if (temperature := kwargs.get(ATTR_TEMPERATURE)) is not None:
            payload["temperature"] = int(round(temperature))
            payload["unit"] = "C" if self._celsius() else "F"
        if payload:
            await self.coordinator.async_control(payload)

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode not in HVAC_TO_DEV:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="unsupported_mode",
                translation_placeholders={"mode": str(hvac_mode)},
            )
        await self.coordinator.async_control({"mode": HVAC_TO_DEV[hvac_mode]})

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        if self._dev_mode not in FAN_ADJUSTABLE_MODES:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="fan_locked",
                translation_placeholders={"mode": self._dev_mode},
            )
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
