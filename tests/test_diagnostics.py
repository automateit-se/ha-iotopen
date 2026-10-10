"""Diagnostics must not leak the token."""

from __future__ import annotations

from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)

from homeassistant.core import HomeAssistant

from .conftest import API_KEY, FakeMqtt


async def test_diagnostics(
    hass: HomeAssistant, hass_client, setup_integration: FakeMqtt, config_entry: MockConfigEntry
) -> None:
    diag = await get_diagnostics_for_config_entry(hass, hass_client, config_entry)
    assert API_KEY not in str(diag)
    assert diag["entry"]["data"]["token"] == "**REDACTED**"
    inst = diag["hub"]["installations"]["1001"]
    assert inst["client_id"] == 2001
    assert inst["functions"]["3003"]["plan"]["platform"] == "sensor"
    assert diag["hub"]["mqtt"]["state"] == "connected"
