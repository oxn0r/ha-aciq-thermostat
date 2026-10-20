"""ACiQ Thermostat (local) — Midea TL04-1 control without the cloud.

On setup the integration makes sure its bundled `thermostat_api` daemon is
running on the thermostat (deploying it over ADB if needed), then polls it.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import CONF_AUTO_DEPLOY
from .coordinator import AciqCoordinator
from .deployer import DeployError

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.CLIMATE, Platform.SENSOR, Platform.BINARY_SENSOR, Platform.BUTTON]

type AciqConfigEntry = ConfigEntry[AciqCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: AciqConfigEntry) -> bool:
    coordinator = AciqCoordinator(hass, entry)

    if entry.options.get(CONF_AUTO_DEPLOY, True):
        try:
            await coordinator.async_deploy()
        except DeployError as err:
            raise ConfigEntryNotReady(f"Could not start daemon on thermostat: {err}") from err

    try:
        coordinator.set_versions(await coordinator.deployer.read_versions())
    except DeployError as err:
        _LOGGER.debug("Could not read version info: %s", err)

    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _async_reload(hass: HomeAssistant, entry: AciqConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: AciqConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: AciqConfigEntry) -> None:
    """Stop the daemon when the integration is deleted (the /tmp copy vanishes on reboot)."""
    from .const import CONF_ADB_PORT, DEFAULT_ADB_PORT
    from .deployer import Deployer
    from homeassistant.const import CONF_HOST

    try:
        await Deployer(entry.data[CONF_HOST], entry.data.get(CONF_ADB_PORT, DEFAULT_ADB_PORT)).stop_daemon()
    except DeployError as err:
        _LOGGER.warning("Could not stop daemon on removal: %s", err)
