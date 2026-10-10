"""IoT Open integration setup (kept out of __init__.py so updates never touch that file).

Every IoT Open *function* becomes a Home Assistant entity. Installations become hub devices and
IoT Open devices become devices behind them (``via_device``). Values are pushed over MQTT.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_TOKEN, CONF_URL, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .pyiotopen import IoTOpenClient

from .const import DOMAIN, device_identifier, hub_identifier
from .hub import IoTOpenHub

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.LIGHT, Platform.SENSOR, Platform.SWITCH]

type IoTOpenConfigEntry = ConfigEntry[IoTOpenHub]


async def async_setup_entry(hass: HomeAssistant, entry: IoTOpenConfigEntry) -> bool:
    """Set up IoT Open from a config entry."""
    client = IoTOpenClient(async_get_clientsession(hass), entry.data[CONF_URL], entry.data[CONF_TOKEN])
    hub = IoTOpenHub(hass, entry, client)
    await hub.async_setup()
    entry.runtime_data = hub
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        await hub.async_unload()
        raise
    return True



async def async_unload_entry(hass: HomeAssistant, entry: IoTOpenConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_unload()
    return unloaded


async def _async_update_listener(hass: HomeAssistant, entry: IoTOpenConfigEntry) -> None:
    """Reload when the installation selection changes (not for bookkeeping-only updates)."""
    hub = entry.runtime_data
    if hub.options_key != hub.loaded_options_key:
        await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: IoTOpenConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Allow deleting a device that no longer exists in IoT Open."""
    hub = entry.runtime_data
    current: set[str] = set()
    for iid, snap in hub.snapshots.items():
        current.add(hub_identifier(iid))
        current.update(device_identifier(iid, d) for d in snap.devices)
    return not any(domain == DOMAIN and ident in current for domain, ident in device.identifiers)
