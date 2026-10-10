"""Switches for controllable IoT Open functions (topic_write + state_on/state_off)."""

from __future__ import annotations

import contextlib
from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import IoTOpenConfigEntry
from .entity import IoTOpenEntity
from .hub import FunctionKey
from .mapping import PLATFORM_SWITCH, binary_state

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IoTOpenConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up switches now and whenever IoT Open gets new functions."""
    hub = entry.runtime_data

    @callback
    def _add(keys: list[FunctionKey]) -> None:
        for subentry_id, group in hub.group_by_subentry(keys).items():
            async_add_entities((IoTOpenSwitch(hub, key) for key in group), config_subentry_id=subentry_id)

    hub.register_platform(PLATFORM_SWITCH, _add)


class IoTOpenSwitch(IoTOpenEntity, SwitchEntity):
    """Publishes state_on/state_off to ``<client_id>/<topic_write>``.

    The state follows ``topic_read`` (the device's confirmation). Functions without a
    ``topic_read`` are optimistic.
    """

    _optimistic_state: bool | None = None

    @callback
    def _apply_function(self) -> None:
        super()._apply_function()
        self._attr_device_class = None
        if self.plan.device_class:
            with contextlib.suppress(ValueError):
                self._attr_device_class = SwitchDeviceClass(self.plan.device_class)
        self._attr_assumed_state = self.function.topic_read is None

    @property
    def is_on(self) -> bool | None:
        """State from topic_read, or the last command when there is no topic_read."""
        if self.function.topic_read is None:
            return self._optimistic_state
        status = self.status
        return None if status is None else binary_state(self.function, status.value)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Send state_on."""
        await self._send(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Send state_off."""
        await self._send(False)

    async def _send(self, on: bool) -> None:
        fn = self.function
        raw = fn.state_on if on else fn.state_off
        if raw is None:
            raw = "1" if on else "0"
        await self.hub.async_send(self.key, raw)
        if fn.topic_read is None:
            self._optimistic_state = on
            self.async_write_ha_state()
