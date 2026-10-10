"""Base entity for IoT Open functions."""

from __future__ import annotations

from typing import Any

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .pyiotopen import Function, Status

from .const import entity_unique_id, signal_availability, signal_function, signal_value
from .hub import FunctionKey, IoTOpenHub
from .mapping import EntityPlan


class IoTOpenEntity(Entity):
    """One IoT Open function."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hub: IoTOpenHub, key: FunctionKey) -> None:
        self.hub = hub
        self.key = key
        self._topic_unsub: Any = None
        self._attr_unique_id = entity_unique_id(*key)
        plan = hub.plans[key]
        self._last_plan = plan
        fn = hub.function(key)
        assert fn is not None
        self._last_function: Function = fn
        self._attr_entity_registry_visible_default = not plan.hidden
        self._attr_device_info = DeviceInfo(identifiers={hub.device_identifier_for(key)})
        self._apply_function()

    # ------------------------------------------------------------------ helpers
    @property
    def function(self) -> Function:
        """Current function (the last known one while the entity is being removed)."""
        fn = self.hub.function(self.key)
        if fn is not None:
            self._last_function = fn
        return self._last_function

    @property
    def plan(self) -> EntityPlan:
        """Current entity plan (the last known one while the entity is being removed)."""
        plan = self.hub.plans.get(self.key)
        if plan is not None:
            self._last_plan = plan
        return self._last_plan

    @property
    def status(self) -> Status | None:
        """Latest value of the function's topic_read."""
        return self.hub.value(self.key[0], self.function.topic_read)

    @property
    def available(self) -> bool:
        """Available while the connection to IoT Open is up and the function exists."""
        return (
            self.hub.available
            and self.key in self.hub.plans
            and self.hub.function(self.key) is not None
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose IoT Open identifiers and the message that came with the last value."""
        fn = self.hub.function(self.key)
        if fn is None:
            return None
        attrs: dict[str, Any] = {
            "installation_id": self.key[0],
            "function_id": fn.id,
            "function_type": fn.type,
            "topic_read": fn.topic_read,
        }
        if fn.topic_write:
            attrs["topic_write"] = fn.topic_write
        if (status := self.status) is not None and status.msg:
            attrs["message"] = status.msg
        return attrs

    @callback
    def _apply_function(self) -> None:
        """Copy name and description from the function; platforms extend this."""
        self._attr_name = self.function.name

    # ------------------------------------------------------------------ lifecycle
    async def async_added_to_hass(self) -> None:
        """Listen for values, metadata changes and availability."""
        self.async_on_remove(
            async_dispatcher_connect(self.hass, signal_availability(self.hub.entry.entry_id), self._on_update)
        )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, signal_function(self.hub.entry.entry_id, *self.key), self._on_function
            )
        )
        self._subscribe_topic()
        self.async_on_remove(self._unsubscribe_topic)

    def _read_topics(self) -> list[str]:
        """Bare topics whose values affect this entity (platforms may add more)."""
        topic = self.function.topic_read
        return [topic] if topic else []

    @callback
    def _subscribe_topic(self) -> None:
        self._unsubscribe_topic()
        unsubs = [
            async_dispatcher_connect(
                self.hass, signal_value(self.hub.entry.entry_id, self.key[0], topic), self._on_update
            )
            for topic in self._read_topics()
        ]
        self._topic_unsub = unsubs or None

    @callback
    def _unsubscribe_topic(self) -> None:
        if self._topic_unsub is not None:
            for unsub in self._topic_unsub:
                unsub()
            self._topic_unsub = None

    @callback
    def _on_update(self) -> None:
        self.async_write_ha_state()

    @callback
    def _on_function(self) -> None:
        if self.hub.function(self.key) is None or self.key not in self.hub.plans:
            return  # being removed
        self._apply_function()
        self._subscribe_topic()  # topic_read may have changed
        self.async_write_ha_state()
