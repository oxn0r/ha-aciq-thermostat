"""Switch: block the thermostat's cloud/internet traffic at the source (opt-in)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AciqConfigEntry
from .const import CONF_BLOCK_EGRESS
from .coordinator import AciqCoordinator
from .entity import AciqEntity

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant, entry: AciqConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([BlockCloudSwitch(entry.runtime_data)])


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
