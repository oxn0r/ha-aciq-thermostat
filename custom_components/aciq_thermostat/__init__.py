"""ACiQ Thermostat (local) — Midea TL04-1 control without the cloud.

On setup the integration makes sure its bundled `thermostat_api` daemon is
running on the thermostat (deploying it over ADB if needed), then polls it.
"""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.loader import async_get_integration

from .const import CARD_REGISTERED, CARD_URL, CONF_AUTO_DEPLOY, DOMAIN
from .coordinator import AciqCoordinator
from .deployer import DeployError

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.CLIMATE, Platform.SENSOR, Platform.BINARY_SENSOR, Platform.BUTTON, Platform.SWITCH]

type AciqConfigEntry = ConfigEntry[AciqCoordinator]


async def _async_register_card(hass: HomeAssistant) -> None:
    """Serve the ACiQ System Card and load it in every dashboard (once, for all entries).
    The ?v= query changes with each release, so browsers fetch the new card."""
    if hass.data.get(CARD_REGISTERED):
        return
    hass.data[CARD_REGISTERED] = True
    if getattr(hass, "http", None) is None:  # no web server (e.g. some test setups)
        return
    from homeassistant.components.frontend import add_extra_js_url
    from homeassistant.components.http import StaticPathConfig

    path = Path(__file__).parent / "frontend" / "aciq-system-card.js"
    await hass.http.async_register_static_paths([StaticPathConfig(CARD_URL, str(path), True)])
    version = (await async_get_integration(hass, DOMAIN)).version
    add_extra_js_url(hass, f"{CARD_URL}?v={version}")


async def async_setup_entry(hass: HomeAssistant, entry: AciqConfigEntry) -> bool:
    await _async_register_card(hass)
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
    coordinator.async_start_long_poll()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _async_reload(hass: HomeAssistant, entry: AciqConfigEntry) -> None:
    """Reload only when the connection or daemon arguments changed. Runtime-only
    options (Block cloud traffic) are applied live by the coordinator."""
    if entry.runtime_data.needs_reload():
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
