"""Base entity."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import AciqCoordinator


class AciqEntity(CoordinatorEntity[AciqCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: AciqCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.unique_id}_{key}"
        self._attr_device_info = coordinator.device_info

    @property
    def data(self) -> dict[str, Any]:
        return self.coordinator.data or {}

    @property
    def equipment(self) -> dict[str, Any]:
        eq = self.data.get("equipment") or {}
        return eq if eq.get("available") else {}
