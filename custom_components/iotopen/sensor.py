"""Sensors for IoT Open functions."""

from __future__ import annotations

import contextlib
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import IoTOpenConfigEntry
from .entity import IoTOpenEntity
from .hub import FunctionKey
from .mapping import PLATFORM_SENSOR, sensor_value

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IoTOpenConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up sensors now and whenever IoT Open gets new functions."""
    hub = entry.runtime_data

    @callback
    def _add(keys: list[FunctionKey]) -> None:
        for subentry_id, group in hub.group_by_subentry(keys).items():
            async_add_entities((IoTOpenSensor(hub, key) for key in group), config_subentry_id=subentry_id)

    hub.register_platform(PLATFORM_SENSOR, _add)


class IoTOpenSensor(IoTOpenEntity, SensorEntity):
    """Read-only value of an IoT Open function."""

    @callback
    def _apply_function(self) -> None:
        super()._apply_function()
        plan = self.plan
        self._attr_device_class = None
        if plan.device_class:
            with contextlib.suppress(ValueError):
                self._attr_device_class = SensorDeviceClass(plan.device_class)
        self._attr_state_class = None
        if plan.state_class:
            with contextlib.suppress(ValueError):
                self._attr_state_class = SensorStateClass(plan.state_class)
        self._attr_native_unit_of_measurement = plan.unit
        self._attr_suggested_display_precision = plan.precision

    @property
    def native_value(self) -> Any:
        """Current value."""
        status = self.status
        if status is None:
            return None
        return sensor_value(self.function, self.plan, status.value)
