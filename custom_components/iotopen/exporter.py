"""Mirror Home Assistant into an IoT Open installation.

* Which entities: every entity carrying the HA label (default "IoT Open"), plus all entities of
  devices carrying that label. Entities from this integration are never exported (no loops).
* Structure: HA device -> IoT Open device, HA entity -> IoT Open function (see export_mapping).
  Objects we own are marked with ``ha.source`` = HA instance id and reconciled (create / update /
  delete) whenever labels, entities or devices change, on start and on IoT Open structure events.
* Values: every state change is published to ``<client_id>/obj/ha/<domain>/<object_id>``.
* Control: ``<client_id>/set/obj/ha/...`` calls the matching HA service.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er, label_registry as lr
from homeassistant.helpers.event import async_call_later, async_track_state_change_event

from .const import DOMAIN
from .export_mapping import (
    META_ENTITY_ID,
    META_HA_DEVICE_ID,
    META_SOURCE,
    FunctionSpec,
    command_service,
    desired_device_meta,
    desired_update,
    device_meta,
    entity_for_topic,
    export_value,
    dimmer_command,
    dimmer_level,
    function_spec,
    is_onoff_topic,
    is_own,
    is_exportable,
    meta_differs,
)
from .pyiotopen import (
    Device,
    Function,
    Installation,
    IoTOpenAuthError,
    IoTOpenError,
    StructureEvent,
    ValueUpdate,
    full_topic,
    structure_topics,
)

if TYPE_CHECKING:
    from .hub import IoTOpenHub

_LOGGER = logging.getLogger(__name__)

RECONCILE_DEBOUNCE = 2.0
LABEL_ICON = "iotopen:badge"  # served by frontend/iotopen-icons.js
LABEL_COLOR = "indigo"  # closest HA palette colour to IoT Open's #0d3e53
LABEL_DESCRIPTION = "Speglas till IoT Open / Mirrored to IoT Open"
DEVICE_TYPE = "HomeAssistant"


class IoTOpenExporter:
    """Keeps one IoT Open installation in sync with labelled HA entities."""

    def __init__(
        self,
        hass: HomeAssistant,
        hub: IoTOpenHub,
        installation: Installation,
        label_name: str,
        allow_control: bool,
    ) -> None:
        self.hass = hass
        self.hub = hub
        self.installation = installation
        self.label_name = label_name
        self.allow_control = allow_control
        self.exported: dict[str, FunctionSpec] = {}
        self._lock = asyncio.Lock()
        self._timer: CALLBACK_TYPE | None = None
        self._state_unsub: CALLBACK_TYPE | None = None
        self._unsubs: list[Callable[[], None]] = []
        self._task: asyncio.Task[Any] | None = None
        self.last_error: str | None = None

    @property
    def client_id(self) -> int:
        return self.installation.client_id

    # ------------------------------------------------------------------ lifecycle
    async def async_start(self) -> None:
        """Ensure the label exists, listen for changes and do the first sync."""
        self._ensure_label()
        bus = self.hass.bus
        for event_type in (
            er.EVENT_ENTITY_REGISTRY_UPDATED,
            dr.EVENT_DEVICE_REGISTRY_UPDATED,
            lr.EVENT_LABEL_REGISTRY_UPDATED,
        ):
            self._unsubs.append(bus.async_listen(event_type, self._on_registry_event))
        self._unsubs.append(self.hub.add_message_hook(self._on_broker_message))
        self._unsubs.append(self.hub.add_connect_listener(self._publish_all))
        self.hub.extra_topics.update(structure_topics(self.client_id))
        # the first sync talks to IoT Open for every exported entity: never block HA start-up with it
        self._task = self.hub.entry.async_create_background_task(
            self.hass, self.async_reconcile(), "iotopen export initial sync"
        )

    @callback
    def _ensure_label(self) -> None:
        """Create the label, or fill in icon/colour/description the user has not set."""
        labels = lr.async_get(self.hass)
        label = labels.async_get_label_by_name(self.label_name)
        if label is None:
            labels.async_create(
                self.label_name, icon=LABEL_ICON, color=LABEL_COLOR, description=LABEL_DESCRIPTION
            )
            return
        changes: dict[str, Any] = {}
        if not label.icon or label.icon in ("mdi:cloud-sync", "iotopen:logo"):  # upgrade our earlier default
            changes["icon"] = LABEL_ICON
        if not label.color:
            changes["color"] = LABEL_COLOR
        if not label.description:
            changes["description"] = LABEL_DESCRIPTION
        if changes:
            labels.async_update(label.label_id, **changes)

    async def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._timer:
            self._timer()
            self._timer = None
        if self._state_unsub:
            self._state_unsub()
            self._state_unsub = None
        if self._task and not self._task.done():
            self._task.cancel()

    # ------------------------------------------------------------------ what to export
    def _label_id(self) -> str | None:
        label = lr.async_get(self.hass).async_get_label_by_name(self.label_name)
        return label.label_id if label else None

    def _wanted(self) -> dict[str, er.RegistryEntry | None]:
        """entity_id -> registry entry (None for entities without a registry entry)."""
        label_id = self._label_id()
        if label_id is None:
            return {}
        ent_reg = er.async_get(self.hass)
        dev_reg = dr.async_get(self.hass)
        wanted: dict[str, er.RegistryEntry | None] = {}
        for entry in er.async_entries_for_label(ent_reg, label_id):
            wanted[entry.entity_id] = entry
        for device in dr.async_entries_for_label(dev_reg, label_id):
            for entry in er.async_entries_for_device(ent_reg, device.id):
                if entry.hidden_by is None and entry.entity_category is None:
                    wanted.setdefault(entry.entity_id, entry)
        return {
            eid: entry
            for eid, entry in wanted.items()
            if is_exportable(eid)
            and (entry is None or (entry.platform != DOMAIN and entry.disabled_by is None))
        }

    # ------------------------------------------------------------------ reconcile
    @callback
    def schedule_reconcile(self, delay: float = RECONCILE_DEBOUNCE) -> None:
        if self._timer:
            self._timer()

        @callback
        def _fire(_now: datetime) -> None:
            self._timer = None
            self._task = self.hub.entry.async_create_background_task(
                self.hass, self.async_reconcile(), "iotopen export reconcile"
            )

        self._timer = async_call_later(self.hass, delay, _fire)

    @callback
    def _on_registry_event(self, _event: Event) -> None:
        self.schedule_reconcile()

    async def async_reconcile(self) -> None:
        """Make the IoT Open installation match the labelled HA entities."""
        async with self._lock:
            try:
                await self._reconcile()
                self.last_error = None
            except IoTOpenAuthError:
                self.last_error = "auth"
                self.hub._start_reauth()  # noqa: SLF001
            except IoTOpenError as err:
                self.last_error = str(err)
                _LOGGER.warning("IoT Open export failed, retrying later: %s", err)
                self.schedule_reconcile(60)

    async def _reconcile(self) -> None:
        client = self.hub.client
        iid = self.installation.id
        source_id = self.hub.source_id or ""
        source = self.hub.instance_name
        wanted = self._wanted()
        dev_reg = dr.async_get(self.hass)

        functions, devices = await asyncio.gather(client.functions(iid), client.devices(iid))
        own_fns: dict[str, list[Function]] = {}
        for fn in functions:
            if is_own(fn.get_meta, source_id) and fn.get_meta(META_ENTITY_ID):
                own_fns.setdefault(fn.get_meta(META_ENTITY_ID) or "", []).append(fn)
        own_devs: dict[str, list[Device]] = {}
        for dev in devices:
            if is_own(dev.get_meta, source_id) and dev.get_meta(META_HA_DEVICE_ID):
                own_devs.setdefault(dev.get_meta(META_HA_DEVICE_ID) or "", []).append(dev)

        # ---- devices
        needed_devices = {e.device_id for e in wanted.values() if e is not None and e.device_id}
        device_map: dict[str, int] = {}
        for ha_device_id in needed_devices:
            ha_device = dev_reg.async_get(ha_device_id)
            if ha_device is None:
                continue
            meta = device_meta(
                ha_device.name_by_user or ha_device.name or ha_device_id,
                ha_device.manufacturer,
                ha_device.model,
                ha_device_id,
                source,
                source_id,
            )
            existing = own_devs.pop(ha_device_id, [])
            if existing:
                keep, *dupes = existing
                for dupe in dupes:
                    await client.delete_device(iid, dupe.id)
                wanted_dev = desired_device_meta(keep.meta, meta)
                if meta_differs(keep.meta, wanted_dev):
                    keep = await client.update_device(keep, meta={**keep.meta, **wanted_dev})
                device_map[ha_device_id] = keep.id
            else:
                created = await client.create_device(iid, DEVICE_TYPE, meta)
                device_map[ha_device_id] = created.id
                _LOGGER.info("IoT Open: created device %s for %s", created.id, meta["name"])

        # ---- functions
        exported: dict[str, FunctionSpec] = {}
        for entity_id, entry in wanted.items():
            state = self.hass.states.get(entity_id)
            name = (state.name if state else None) or (entry.name or entry.original_name if entry else None) or entity_id
            attrs = dict(state.attributes) if state else {}
            if entry is not None and "device_class" not in attrs and (entry.device_class or entry.original_device_class):
                attrs["device_class"] = entry.device_class or entry.original_device_class
            spec = function_spec(
                entity_id,
                str(name),
                attrs,
                source,
                source_id=source_id,
                allow_control=self.allow_control,
                iot_device_id=device_map.get(entry.device_id) if entry and entry.device_id else None,
            )
            existing = own_fns.pop(entity_id, [])
            if existing:
                keep, *dupes = existing
                for dupe in dupes:
                    await client.delete_function(iid, dupe.id)
                # IoT Open is master for the type and presentation keys (name, visible, unit, text_*):
                # manual edits in IoT Open are not reverted here (see desired_update)
                type_, wanted_meta, removed = desired_update(keep.meta, keep.type, spec)
                if removed or keep.type != type_ or meta_differs(keep.meta, wanted_meta):
                    merged = {k: v for k, v in keep.meta.items() if k not in removed}
                    merged.update(wanted_meta)
                    await client.update_function(keep, type_=type_, meta=merged)
            else:
                created = await client.create_function(iid, spec.type, spec.meta)
                _LOGGER.info("IoT Open: created function %s for %s", created.id, entity_id)
            exported[entity_id] = spec

        # ---- remove what is no longer labelled
        for entity_id, fns in own_fns.items():
            for fn in fns:
                await client.delete_function(iid, fn.id)
                _LOGGER.info("IoT Open: deleted function %s (%s no longer exported)", fn.id, entity_id)
        for devs in own_devs.values():
            for dev in devs:
                await client.delete_device(iid, dev.id)

        newly = set(exported) - set(self.exported)
        self.exported = exported
        self._track_states()
        self.hub.extra_topics = {
            t for t in self.hub.extra_topics if not t.startswith(f"{self.client_id}/set/obj/ha/")
        } | {
            full_topic(self.client_id, t)
            for s in exported.values()
            for t in (s.topic_write, s.meta.get("topic_write_onoff"))
            if t
        }
        self.hub.extra_topics.update(structure_topics(self.client_id))
        await self.hub.async_update_subscriptions()
        for entity_id in newly:
            self._publish(entity_id, self.hass.states.get(entity_id))

    # ------------------------------------------------------------------ values HA -> IoT Open
    @callback
    def _track_states(self) -> None:
        if self._state_unsub:
            self._state_unsub()
            self._state_unsub = None
        if self.exported:
            self._state_unsub = async_track_state_change_event(
                self.hass, list(self.exported), self._on_state_changed
            )

    @callback
    def _on_state_changed(self, event: Event[EventStateChangedData]) -> None:
        self._publish(event.data["entity_id"], event.data["new_state"])

    @callback
    def _publish_all(self) -> None:
        for entity_id in self.exported:
            self._publish(entity_id, self.hass.states.get(entity_id))

    @callback
    def _publish(self, entity_id: str, state: State | None) -> None:
        spec = self.exported.get(entity_id)
        if spec is None or state is None or not self.hub.mqtt.connected:
            return
        timestamp = state.last_updated.timestamp()
        messages: list[tuple[str, Any, str]] = []
        onoff_topic = spec.meta.get("topic_read_onoff")
        if onoff_topic:  # dimmer: level on topic_read, 1/0 on topic_read_onoff
            level = dimmer_level(state.state, dict(state.attributes))
            if level is not None:
                messages.append((spec.topic_read, level, ""))
            converted = export_value(entity_id.split(".", 1)[0], state.state)
            if converted is not None:
                messages.append((onoff_topic, converted[0], converted[1]))
        else:
            converted = export_value(entity_id.split(".", 1)[0], state.state)
            if converted is not None:
                messages.append((spec.topic_read, converted[0], converted[1]))
        for topic, value, msg in messages:
            payload = {"value": value, "timestamp": timestamp, "msg": msg}
            self.hub.entry.async_create_background_task(
                self.hass,
                self._async_publish(full_topic(self.client_id, topic), payload),
                f"iotopen publish {entity_id}",
            )

    async def _async_publish(self, topic: str, payload: dict[str, Any]) -> None:
        try:
            await self.hub.mqtt.publish(topic, payload)
        except IoTOpenError as err:
            _LOGGER.debug("Publish to %s failed: %s", topic, err)

    # ------------------------------------------------------------------ commands IoT Open -> HA
    @callback
    def _on_broker_message(self, client_id: int, event: ValueUpdate | StructureEvent) -> bool:
        if client_id != self.client_id:
            return False
        if isinstance(event, StructureEvent):
            self.schedule_reconcile(5)  # someone may have edited/deleted our objects
            return False  # the import side also wants it
        if not event.topic.startswith("set/obj/ha/"):
            return False
        entity_id = entity_for_topic(event.topic)
        spec = self.exported.get(entity_id or "")
        if entity_id is None or spec is None or spec.topic_write is None:
            return True
        if spec.meta.get("topic_read_onoff") and not is_onoff_topic(event.topic):
            call = dimmer_command(event.value)  # level 0-100
        else:
            call = command_service(entity_id.split(".", 1)[0], event.value)
        if call is None:
            _LOGGER.warning("IoT Open: unsupported command %r for %s", event.value, entity_id)
            return True
        domain, service, data = call
        _LOGGER.debug("IoT Open command: %s.%s %s %s", domain, service, entity_id, data)
        self.hub.entry.async_create_background_task(
            self.hass, self._async_call(domain, service, entity_id, data), f"iotopen command {entity_id}"
        )
        return True

    async def _async_call(self, domain: str, service: str, entity_id: str, data: dict[str, Any]) -> None:
        try:
            await self.hass.services.async_call(domain, service, {ATTR_ENTITY_ID: entity_id, **data}, blocking=True)
        except Exception as err:  # noqa: BLE001 - a bad command must never break the integration
            _LOGGER.warning("IoT Open command %s.%s for %s failed: %s", domain, service, entity_id, err)

    def diagnostics(self) -> dict[str, Any]:
        return {
            "installation_id": self.installation.id,
            "label": self.label_name,
            "allow_control": self.allow_control,
            "exported": {eid: {"type": s.type, "meta": s.meta} for eid, s in self.exported.items()},
            "last_error": self.last_error,
        }
