"""Dry duration: the thermostat's own 15/30/45/60 min setting (communicating systems).

The screen keeps this duration and counts down from it each time Dry starts, so the select
and the screen always show the same value.
"""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AciqConfigEntry
from .const import DRY_MINUTES
from .coordinator import AciqCoordinator
from .entity import AciqEntity

PARALLEL_UPDATES = 1  # one command at a time to the thermostat

OPTIONS = [str(m) for m in DRY_MINUTES]


async def async_setup_entry(
    hass: HomeAssistant, entry: AciqConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    if (entry.runtime_data.data or {}).get("wiring") != "24v":
        async_add_entities([AciqDryDuration(entry.runtime_data)])


class AciqDryDuration(AciqEntity, SelectEntity):
    _attr_translation_key = "dry_duration"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = OPTIONS

    def __init__(self, coordinator: AciqCoordinator) -> None:
        super().__init__(coordinator, "dry_duration")

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.dry_supported()

    @property
    def current_option(self) -> str | None:
        minutes = str(self.data.get("ui", {}).get("dry_minutes"))
        return minutes if minutes in OPTIONS else None

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_control({"dry_minutes": int(option)})
