"""The IoT Open integration.

Every IoT Open function becomes a Home Assistant entity, and labelled Home Assistant entities are
mirrored into IoT Open. The implementation lives in ``integration.py``; this file only re-exports
it and should never need to change.
"""

from __future__ import annotations

from .integration import (
    PLATFORMS,
    IoTOpenConfigEntry,
    async_remove_config_entry_device,
    async_setup_entry,
    async_unload_entry,
)

__all__ = [
    "PLATFORMS",
    "IoTOpenConfigEntry",
    "async_remove_config_entry_device",
    "async_setup_entry",
    "async_unload_entry",
]
