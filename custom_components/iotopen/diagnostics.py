"""Diagnostics for IoT Open (tokens, passwords and personal data are redacted)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_TOKEN, CONF_USERNAME
from homeassistant.core import HomeAssistant

from . import IoTOpenConfigEntry

TO_REDACT = {
    CONF_TOKEN,
    CONF_PASSWORD,
    CONF_USERNAME,
    "api_key",
    "email",
    "phone",
    "address",
    "latitude",
    "longitude",
    "lat",
    "lng",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: IoTOpenConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    return {
        "entry": {
            "title": "**REDACTED**",
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "hub": async_redact_data(entry.runtime_data.diagnostics(), TO_REDACT),
    }
