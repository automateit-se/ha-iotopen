"""Binary sensors for IoT Open functions with state_on/state_off."""

from __future__ import annotations

import contextlib

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import IoTOpenConfigEntry
from .entity import IoTOpenEntity
from .hub import FunctionKey
from .mapping import PLATFORM_BINARY_SENSOR, binary_state

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IoTOpenConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up binary sensors now and whenever IoT Open gets new functions."""
    hub = entry.runtime_data

    @callback
    def _add(keys: list[FunctionKey]) -> None:
        for subentry_id, group in hub.group_by_subentry(keys).items():
            async_add_entities((IoTOpenBinarySensor(hub, key) for key in group), config_subentry_id=subentry_id)

    hub.register_platform(PLATFORM_BINARY_SENSOR, _add)


class IoTOpenBinarySensor(IoTOpenEntity, BinarySensorEntity):
    """On/off state of an IoT Open function."""

    @callback
    def _apply_function(self) -> None:
        super()._apply_function()
        self._attr_device_class = None
        if self.plan.device_class:
            with contextlib.suppress(ValueError):
                self._attr_device_class = BinarySensorDeviceClass(self.plan.device_class)

    @property
    def is_on(self) -> bool | None:
        """True when the value equals state_on."""
        status = self.status
        return None if status is None else binary_state(self.function, status.value)
