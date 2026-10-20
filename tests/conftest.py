"""Shared fixtures."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.aciq_thermostat.deployer import Check, PreflightResult

STATE = json.loads((Path(__file__).parent / "fixtures" / "state.json").read_text())
HOST = "10.0.2.183"
SERIAL = "TESTSERIAL123"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations, hass):
    from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
    hass.config.units = US_CUSTOMARY_SYSTEM
    yield


@pytest.fixture
def state() -> dict:
    return copy.deepcopy(STATE)


def passing_preflight() -> PreflightResult:
    return PreflightResult(
        checks=[Check("root_shell", True, "uid=0"), Check("firmware", True, "ok")],
        serial=SERIAL,
        firmware=STATE["firmware"],
    )


@pytest.fixture
def mock_deployer():
    with (
        patch("custom_components.aciq_thermostat.deployer.Deployer.preflight",
              AsyncMock(return_value=passing_preflight())) as pre,
        patch("custom_components.aciq_thermostat.deployer.Deployer.ensure_daemon",
              AsyncMock(return_value="already running")) as ens,
        patch("custom_components.aciq_thermostat.deployer.Deployer.read_versions",
              AsyncMock(return_value={"screen_firmware": "LD:1.2.60", "hardware": "V4.3",
                                      "indoor_unit_firmware": "00AC000111171171000066111008"})),
    ):
        yield pre, ens
