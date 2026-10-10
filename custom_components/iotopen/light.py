"""Lights for IoT Open ``dimmer`` functions (level on topic_read/topic_write between min and max).

Optional ``topic_read_onoff``/``topic_write_onoff`` with ``state_on``/``state_off`` carry the
on/off state separately; without them "on" means level > min.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import ATTR_BRIGHTNESS, ColorMode, LightEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import IoTOpenConfigEntry
from .entity import IoTOpenEntity
from .hub import FunctionKey
from .mapping import PLATFORM_LIGHT, command_value

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IoTOpenConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up dimmers now and whenever IoT Open gets new functions."""
    hub = entry.runtime_data

    @callback
    def _add(keys: list[FunctionKey]) -> None:
        for subentry_id, group in hub.group_by_subentry(keys).items():
            async_add_entities((IoTOpenLight(hub, key) for key in group), config_subentry_id=subentry_id)

    hub.register_platform(PLATFORM_LIGHT, _add)


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class IoTOpenLight(IoTOpenEntity, LightEntity):
    """IoT Open dimmer."""

    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _attr_color_mode = ColorMode.BRIGHTNESS

    # ---- helpers
    @property
    def _min(self) -> float:
        return _float(self.function.get_meta("min"), 0.0)

    @property
    def _max(self) -> float:
        value = _float(self.function.get_meta("max"), 100.0)
        return value if value > self._min else self._min + 100.0

    @property
    def _onoff_read(self) -> str | None:
        return (self.function.get_meta("topic_read_onoff") or "").strip() or None

    @property
    def _onoff_write(self) -> str | None:
        return (self.function.get_meta("topic_write_onoff") or "").strip() or None

    def _read_topics(self) -> list[str]:
        topics = super()._read_topics()
        if self._onoff_read:
            topics.append(self._onoff_read)
        return topics

    def _level(self) -> float | None:
        status = self.status
        return None if status is None else _float(status.value, self._min)

    # ---- state
    @property
    def brightness(self) -> int | None:
        level = self._level()
        if level is None:
            return None
        span = self._max - self._min
        return max(0, min(255, round((level - self._min) / span * 255)))

    @property
    def is_on(self) -> bool | None:
        if self._onoff_read:
            status = self.hub.value(self.key[0], self._onoff_read)
            if status is not None:
                on = (self.function.get_meta("state_on") or "1").strip()
                return str(status.value).strip() == on or _float(status.value, -1) == _float(on, -2)
        level = self._level()
        return None if level is None else level > self._min

    # ---- control
    async def async_turn_on(self, **kwargs: Any) -> None:
        fn = self.function
        if ATTR_BRIGHTNESS in kwargs:
            level = self._min + kwargs[ATTR_BRIGHTNESS] / 255 * (self._max - self._min)
            await self.hub.async_publish(self.key, fn.topic_write, round(level, 2))
        elif self._onoff_write:
            await self.hub.async_publish(
                self.key, self._onoff_write, command_value(fn.get_meta("state_on") or "1")
            )
        else:
            await self.hub.async_publish(self.key, fn.topic_write, self._max)

    async def async_turn_off(self, **kwargs: Any) -> None:
        fn = self.function
        if self._onoff_write:
            await self.hub.async_publish(
                self.key, self._onoff_write, command_value(fn.get_meta("state_off") or "0")
            )
        else:
            await self.hub.async_publish(self.key, fn.topic_write, self._min)
