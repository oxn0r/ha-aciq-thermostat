"""Maintenance buttons: redeploy the daemon; collect a debug report (pre-flight + build details)."""

from __future__ import annotations

from homeassistant.components import persistent_notification
from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AciqConfigEntry
from .const import BUG_ISSUE_URL, DOMAIN, NEW_ISSUE_URL
from .coordinator import AciqCoordinator
from .deployer import DeployError
from .entity import AciqEntity

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant, entry: AciqConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    c = entry.runtime_data
    # "Run pre-flight checks" (before 1.1.2) is part of the debug report now
    ent_reg = er.async_get(hass)
    if old := ent_reg.async_get_entity_id("button", DOMAIN, f"{entry.unique_id}_preflight"):
        ent_reg.async_remove(old)
    async_add_entities([RedeployButton(c), DebugReportButton(c)])


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


class DebugReportButton(AciqEntity, ButtonEntity):
    """Read-only deep report for new firmware or problems: kept for the next diagnostics download."""

    _attr_translation_key = "debug_report"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: AciqCoordinator) -> None:
        super().__init__(coordinator, "debug_report")

    @property
    def available(self) -> bool:
        return True

    async def async_press(self) -> None:
        try:
            report = await self.coordinator.async_collect_debug_report()
        except DeployError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="deploy_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        pre = report["preflight"]
        failed = [c["key"] for c in pre["checks"] if not c["ok"]]
        status = "all pre-flight checks passed" if not failed else "pre-flight: " + ", ".join(failed) + " failed"
        url = NEW_ISSUE_URL if not pre["passed"] else BUG_ISSUE_URL
        persistent_notification.async_create(
            self.hass,
            f"Debug report collected ({status}).\n\n"
            "1. Download diagnostics: Settings → Devices & services → ACiQ Thermostat → ⋮ → "
            "**Download diagnostics**. The report is included (no IPs or serial).\n"
            f"2. [Open a GitHub issue]({url}) and attach the file.",
            title="ACiQ Thermostat debug report",
            notification_id=f"{DOMAIN}_debug_report_{self.coordinator.config_entry.entry_id}",
        )
