"""The ACiQ System Card is served and loaded by the integration, once."""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant

from custom_components.aciq_thermostat import _async_register_card
from custom_components.aciq_thermostat.const import CARD_REGISTERED, CARD_URL

COMPONENT = Path(__file__).parent.parent / "custom_components" / "aciq_thermostat"
VERSION = json.loads((COMPONENT / "manifest.json").read_text())["version"]


async def test_card_registered_once(hass: HomeAssistant) -> None:
    hass.http = MagicMock(async_register_static_paths=AsyncMock())
    with patch("homeassistant.components.frontend.add_extra_js_url") as add_js:
        await _async_register_card(hass)
        await _async_register_card(hass)  # second config entry
    hass.http.async_register_static_paths.assert_awaited_once()
    (cfg,), = hass.http.async_register_static_paths.call_args.args
    assert cfg.url_path == CARD_URL == "/aciq_thermostat/aciq-system-card.js"
    assert Path(cfg.path).is_file() and cfg.cache_headers
    add_js.assert_called_once_with(hass, f"{CARD_URL}?v={VERSION}")


async def test_card_without_http(hass: HomeAssistant) -> None:
    hass.http = None
    await _async_register_card(hass)
    assert hass.data[CARD_REGISTERED]


def test_card_version_matches_manifest() -> None:
    src = (COMPONENT / "frontend" / "aciq-system-card.js").read_text()
    assert re.search(r'const VERSION = "([^"]+)"', src)[1] == VERSION  # type: ignore[index]
