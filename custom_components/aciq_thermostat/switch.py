"""Switches: block the thermostat's cloud/internet traffic (opt-in); Dehumidification."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
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
    async_add_entities([BlockCloudSwitch(c)])

    # The daemon learns whether the indoor unit has Dehumidification only from its first AHU3
    # status line, which can come after setup (e.g. right after a daemon update), so the switch
    # is added as soon as support shows up rather than only at setup.
    added = False

    @callback
    def _add_dehumidification() -> None:
        nonlocal added
        if not added and dehumidification_supported(c.data or {}):
            added = True
            async_add_entities([DehumidificationSwitch(c)])

    _add_dehumidification()
    entry.async_on_unload(c.async_add_listener(_add_dehumidification))


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
    report the feature. Unavailable while it can't be turned on (off and not in Cool): HA's toggle
    flips itself on press and doesn't flip back when the action is refused."""

    _attr_translation_key = "dehumidification"

    def __init__(self, coordinator: AciqCoordinator) -> None:
        super().__init__(coordinator, "dehumidification")

    @property
    def available(self) -> bool:
        if self.data.get("control_enabled") is False:      # read-only firmware build: shown, not pressable
            return False
        return super().available and (self.data.get("mode") == DEV_COOL or self.is_on is True)

    @property
    def is_on(self) -> bool | None:
        return {0: False, 1: True}.get(self.data.get("dev", {}).get("humi_ctrl"))

    async def async_turn_on(self, **kwargs: Any) -> None:
        if self.data.get("mode") != DEV_COOL:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="dehumidification_cool")
        await self.coordinator.async_control({"dehumidification": True})

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_control({"dehumidification": False})
