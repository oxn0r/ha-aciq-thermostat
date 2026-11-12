"""Switches: block the thermostat's cloud/internet traffic (opt-in); Dehumidification."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AciqConfigEntry
from .const import CONF_BLOCK_EGRESS, DEV_COOL, DOMAIN
from .coordinator import AciqCoordinator
from .entity import AciqEntity

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant, entry: AciqConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    c = entry.runtime_data
    entities: list[SwitchEntity] = [BlockCloudSwitch(c)]
    if dehumidification_supported(c.data or {}):
        entities.append(DehumidificationSwitch(c))
    async_add_entities(entities)


def dehumidification_supported(data: dict[str, Any]) -> bool:
    """The indoor unit reports the feature (AHU3 humi_ctrl_fun); communicating systems only."""
    return bool(data.get("dev", {}).get("humi_ctrl_fun") == 1)


class BlockCloudSwitch(AciqEntity, SwitchEntity):
    """Mirrors the 'block_egress' option. Toggling applies or removes the runtime firewall
    rule on the thermostat immediately over ADB; the option is persisted for restarts."""

    _attr_translation_key = "block_cloud"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: AciqCoordinator) -> None:
        super().__init__(coordinator, "block_cloud")

    @property
    def available(self) -> bool:
        return True  # an option, usable even while the daemon is down

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.config_entry.options.get(CONF_BLOCK_EGRESS, False))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        eg = self.coordinator.deployer.last_egress
        verified = self.coordinator.egress_verified_at
        return {
            "rule_active": bool(eg.get("enabled")),
            "dropped_packets": eg.get("dropped_packets"),
            "last_verified": verified.isoformat() if verified else None,
            "last_repair": eg.get("repaired"),
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)

    async def _set(self, value: bool) -> None:
        """Apply live over ADB (no reload, daemon untouched); raises on failure, state unchanged."""
        if self.is_on == value:
            return
        await self.coordinator.async_set_block_egress(value)
        self.async_write_ha_state()


class DehumidificationSwitch(AciqEntity, SwitchEntity):
    """The screen's Dehumidification switch (issue #11): cooling tuned to remove more moisture,
    not Dry. On only in Cool, like the screen; off in any mode. Created only on indoor units that
    report the feature."""

    _attr_translation_key = "dehumidification"

    def __init__(self, coordinator: AciqCoordinator) -> None:
        super().__init__(coordinator, "dehumidification")

    @property
    def is_on(self) -> bool | None:
        return {0: False, 1: True}.get(self.data.get("dev", {}).get("humi_ctrl"))

    async def async_turn_on(self, **kwargs: Any) -> None:
        if self.data.get("mode") != DEV_COOL:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="dehumidification_cool")
        await self.coordinator.async_control({"dehumidification": True})

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_control({"dehumidification": False})
