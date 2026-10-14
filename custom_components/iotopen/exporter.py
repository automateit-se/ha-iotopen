"""Mirror Home Assistant into an IoT Open installation.

* Which entities: every entity carrying the HA label (default "IoT Open"), plus all entities of
  devices carrying that label. Entities from this integration are never exported (no loops).
* Structure: HA device -> IoT Open device, HA entity -> IoT Open function(s) (see export_mapping;
  a media player becomes several functions on one device).
  Objects we own are marked with ``ha.source`` = HA instance id and reconciled (create / update /
  delete) whenever labels, entities or devices change, on start and on IoT Open structure events.
* Values: every state change is published to ``<client_id>/obj/ha/<domain>/<object_id>``. Media player
  functions only publish when their value or msg changed, and volume is debounced.
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
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .export_mapping import (
    MEDIA_PLAYER,
    META_ENTITY_ID,
    META_HA_DEVICE_ID,
    META_PART,
    ONOFF_SUFFIX,
    PART_VOLUME,
    PRESS_DOMAINS,
    FunctionSpec,
    command_service,
    desired_device_meta,
    desired_update,
    device_meta,
    entity_for_topic,
    export_value,
    dimmer_command,
    dimmer_level,
    function_specs,
    is_own,
    is_exportable,
    is_media_active,
    media_command,
    media_parts,
    media_value,
    meta_differs,
    press_value,
    topic_suffix,
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
VOLUME_DEBOUNCE = 1.0  # a dragged volume slider publishes only its last value
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
        include_categorized: bool = False,
    ) -> None:
        self.hass = hass
        self.hub = hub
        self.installation = installation
        self.label_name = label_name
        self.allow_control = allow_control
        # also mirror diagnostic/config entities of labelled devices (a label on the entity always counts)
        self.include_categorized = include_categorized
        self.exported: dict[str, list[FunctionSpec]] = {}
        self._last_sent: dict[str, tuple[Any, str]] = {}  # topic -> (value, msg) for deduplicated parts
        self._volume_timers: dict[str, CALLBACK_TYPE] = {}
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
        for cancel in self._volume_timers.values():
            cancel()
        self._volume_timers.clear()
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
                if entry.hidden_by is None and (entry.entity_category is None or self.include_categorized):
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
        own_fns: dict[tuple[str, str], list[Function]] = {}  # (entity_id, part) -> functions
        for fn in functions:
            if is_own(fn.get_meta, source_id) and fn.get_meta(META_ENTITY_ID):
                key = (fn.get_meta(META_ENTITY_ID) or "", fn.get_meta(META_PART) or "")
                own_fns.setdefault(key, []).append(fn)
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
        exported: dict[str, list[FunctionSpec]] = {}
        for entity_id, entry in wanted.items():
            state = self.hass.states.get(entity_id)
            name = (state.name if state else None) or (entry.name or entry.original_name if entry else None) or entity_id
            attrs = dict(state.attributes) if state else {}
            if entry is not None and "device_class" not in attrs and (entry.device_class or entry.original_device_class):
                attrs["device_class"] = entry.device_class or entry.original_device_class
            keep_parts: set[str] = set()
            if entity_id.split(".", 1)[0] == MEDIA_PLAYER and not is_media_active(state.state if state else None):
                # off or unavailable players may report fewer features: only add functions, never delete
                keep_parts = {part for eid, part in own_fns if eid == entity_id and part}
                if entry is not None and "supported_features" not in attrs:
                    attrs["supported_features"] = entry.supported_features
            specs = function_specs(
                entity_id,
                str(name),
                attrs,
                source,
                source_id=source_id,
                allow_control=self.allow_control,
                iot_device_id=device_map.get(entry.device_id) if entry and entry.device_id else None,
                language=self.hass.config.language,
                keep_parts=keep_parts,
            )
            for spec in specs:
                await self._sync_function(own_fns.pop((entity_id, spec.part), []), spec)
            exported[entity_id] = specs

        # ---- remove what is no longer labelled (or no longer supported by a media player)
        for (entity_id, part), fns in own_fns.items():
            for fn in fns:
                await client.delete_function(iid, fn.id)
                _LOGGER.info(
                    "IoT Open: deleted function %s (%s%s no longer exported)",
                    fn.id,
                    entity_id,
                    f" {part}" if part else "",
                )
        for devs in own_devs.values():
            for dev in devs:
                await client.delete_device(iid, dev.id)

        before = {s.key for specs in self.exported.values() for s in specs}
        newly = {s.entity_id for specs in exported.values() for s in specs if s.key not in before}
        self.exported = exported
        self._track_states()
        self.hub.extra_topics = {
            t for t in self.hub.extra_topics if not t.startswith(f"{self.client_id}/set/obj/ha/")
        } | {
            full_topic(self.client_id, t)
            for specs in exported.values()
            for s in specs
            for t in (s.topic_write, s.meta.get("topic_write_onoff"))
            if t
        }
        self.hub.extra_topics.update(structure_topics(self.client_id))
        await self.hub.async_update_subscriptions()
        for entity_id in newly:
            self._publish(entity_id, self.hass.states.get(entity_id), force=True)

    async def _sync_function(self, existing: list[Function], spec: FunctionSpec) -> None:
        """Create the function, or update the one we own (and drop duplicates)."""
        client = self.hub.client
        iid = self.installation.id
        if not existing:
            created = await client.create_function(iid, spec.type, spec.meta)
            _LOGGER.info("IoT Open: created function %s for %s", created.id, spec.key)
            return
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
        entity_id = event.data["entity_id"]
        new_state = event.data["new_state"]
        if (
            entity_id.split(".", 1)[0] == MEDIA_PLAYER
            and new_state is not None
            and is_media_active(new_state.state)
            and "supported_features" in new_state.attributes
            and set(media_parts(dict(new_state.attributes))) != {s.part for s in self.exported.get(entity_id, [])}
        ):
            self.schedule_reconcile()  # the player gained or lost features
        self._publish(entity_id, new_state)

    @callback
    def _publish_all(self) -> None:
        for entity_id in self.exported:
            self._publish(entity_id, self.hass.states.get(entity_id), force=True)

    @callback
    def _publish(self, entity_id: str, state: State | None, *, force: bool = False) -> None:
        """Publish the state of every function of ``entity_id``.

        ``force`` (after a (re)connect or for new functions) skips deduplication and debouncing.
        """
        specs = self.exported.get(entity_id)
        if not specs or state is None or not self.hub.mqtt.connected:
            return
        timestamp = state.last_updated.timestamp()
        domain = entity_id.split(".", 1)[0]
        attrs = dict(state.attributes)
        for spec in specs:
            if spec.part:
                converted = media_value(spec.part, state.state, attrs)
                if converted is None:
                    continue
                topic = spec.topic_read
                if spec.part == PART_VOLUME and not force:
                    self._debounce_volume(topic, converted, timestamp)
                    continue
                if not force and self._last_sent.get(topic) == converted:
                    continue  # attribute updates (position, artwork ...) must not fill the history
                if cancel := self._volume_timers.pop(topic, None):
                    cancel()
                self._send(topic, converted, timestamp)
                continue
            if domain in PRESS_DOMAINS:
                # a press is an event: never re-sent on (re)connect, and a restored press time is skipped
                pressed = None if force else press_value(state.state, dt_util.utcnow())
                if pressed is not None:
                    self._send(spec.topic_read, pressed, timestamp)
                continue
            onoff_topic = spec.meta.get("topic_read_onoff")
            if onoff_topic:  # dimmer: level on topic_read, 1/0 on topic_read_onoff
                level = dimmer_level(state.state, attrs)
                if level is not None:
                    self._send(spec.topic_read, (level, ""), timestamp)
                converted = export_value(domain, state.state)
                if converted is not None:
                    self._send(onoff_topic, converted, timestamp)
            else:
                converted = export_value(domain, state.state)
                if converted is not None:
                    self._send(spec.topic_read, converted, timestamp)

    @callback
    def _debounce_volume(self, topic: str, converted: tuple[Any, str], timestamp: float) -> None:
        """Publish a volume ``VOLUME_DEBOUNCE`` s after the last change (the last value wins)."""
        cancel = self._volume_timers.pop(topic, None)
        if cancel is None and self._last_sent.get(topic) == converted:
            return  # unchanged and nothing pending
        if cancel:
            cancel()

        @callback
        def _fire(_now: datetime) -> None:
            self._volume_timers.pop(topic, None)
            if self.hub.mqtt.connected and self._last_sent.get(topic) != converted:
                self._send(topic, converted, timestamp)

        self._volume_timers[topic] = async_call_later(self.hass, VOLUME_DEBOUNCE, _fire)

    @callback
    def _send(self, topic: str, converted: tuple[Any, str], timestamp: float) -> None:
        self._last_sent[topic] = converted
        value, msg = converted
        payload = {"value": value, "timestamp": timestamp, "msg": msg}
        self.hub.entry.async_create_background_task(
            self.hass,
            self._async_publish(full_topic(self.client_id, topic), payload),
            f"iotopen publish {topic}",
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
        suffix = topic_suffix(event.topic)
        part = "" if suffix == ONOFF_SUFFIX else suffix
        spec = next((s for s in self.exported.get(entity_id or "", []) if s.part == part), None)
        if entity_id is None or spec is None or spec.topic_write is None:
            return True
        if spec.part:
            call = media_command(spec.part, event.value)
        elif spec.meta.get("topic_read_onoff") and suffix != ONOFF_SUFFIX:
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
            "include_categorized": self.include_categorized,
            "exported": {
                s.key: {"type": s.type, "meta": s.meta} for specs in self.exported.values() for s in specs
            },
            "last_error": self.last_error,
        }
