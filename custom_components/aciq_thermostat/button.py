"""Maintenance buttons: redeploy the daemon, re-run pre-flight checks."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AciqConfigEntry
from .const import DOMAIN
from .coordinator import AciqCoordinator
from .deployer import DeployError
from .entity import AciqEntity

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant, entry: AciqConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    c = entry.runtime_data
    async_add_entities([RedeployButton(c), PreflightButton(c)])


class RedeployButton(AciqEntity, ButtonEntity):
    _attr_translation_key = "redeploy"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: AciqCoordinator) -> None:
        super().__init__(coordinator, "redeploy")

    @property
    def available(self) -> bool:
        return True  # must work exactly when the daemon is down

    async def async_press(self) -> None:
        try:
            await self.coordinator.async_deploy(force=True)
        except DeployError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="deploy_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        await self.coordinator.async_request_refresh()


class PreflightButton(AciqEntity, ButtonEntity):
    _attr_translation_key = "preflight"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: AciqCoordinator) -> None:
        super().__init__(coordinator, "preflight")

    @property
    def available(self) -> bool:
        return True

    async def async_press(self) -> None:
        try:
            res = await self.coordinator.async_run_preflight()
        except DeployError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="deploy_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        if not res["passed"]:
            failed = ", ".join(c["key"] for c in res["checks"] if c["fatal"] and not c["ok"])
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="checks_failed",
                translation_placeholders={"checks": failed},
            )
