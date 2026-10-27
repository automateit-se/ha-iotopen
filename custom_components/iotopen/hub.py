"""Runtime state of one IoT Open config entry.

Pattern (same as the reference dashboard):

* start / reload: REST snapshot per installation (devicex, functionx, status)
* afterwards: values arrive only over MQTT (``<client_id>/<topic_read>``)
* ``<client_id>/evt/{functionx,devicex}/updated`` -> debounced re-read of that installation, then
  entities are added, updated or removed
* MQTT reconnect -> fresh snapshot of everything (values or changes may have been missed)
* new installations never announce themselves on an existing client_id -> hourly check
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from types import MappingProxyType

from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er, instance_id
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import slugify
from homeassistant.helpers.event import async_call_later, async_track_time_interval

from .pyiotopen import (
    ConnectionState,
    Device,
    Function,
    Installation,
    InstallationSnapshot,
    IoTOpenAuthError,
    IoTOpenClient,
    IoTOpenConnectionError,
    IoTOpenError,
    IoTOpenForbiddenError,
    IoTOpenMqtt,
    IoTOpenNotFoundError,
    Status,
    StructureEvent,
    ValueUpdate,
    full_topic,
    normalize_topic,
    parse_message,
    structure_topics,
)

from .const import (
    CONF_ADD_NEW_INSTALLATIONS,
    CONF_EXPORT_CATEGORIZED,
    CONF_EXPORT_TRANSPORT,
    CONF_EXPORT_CONTROL,
    CONF_IMPORT_MODE,
    CONF_INSTALLATION_ID,
    CONF_USE_SUBENTRIES,
    SUBENTRY_INSTALLATION,
    CONF_INSTANCE_NAME,
    DEFAULT_IMPORT_MODE,
    IMPORT_ALL,
    CONF_EXPORT_INSTALLATION,
    CONF_EXPORT_LABEL,
    CONF_INSTALLATIONS,
    DEFAULT_EXPORT_LABEL,
    DEFAULT_EXPORT_TRANSPORT,
    EXPORT_NONE,
    CONF_KNOWN_INSTALLATIONS,
    DOMAIN,
    INSTALLATION_CHECK_INTERVAL,
    MANUFACTURER,
    STRUCTURE_DEBOUNCE,
    UNAVAILABLE_GRACE,
    device_identifier,
    entity_unique_id,
    hub_identifier,
    signal_availability,
    signal_function,
    signal_value,
)
from .export_mapping import is_own
from .mapping import EntityPlan, command_value, describe, wanted_for_import

if TYPE_CHECKING:
    from . import IoTOpenConfigEntry

_LOGGER = logging.getLogger(__name__)

type FunctionKey = tuple[int, int]  # (installation_id, function_id)
type PlatformAdder = Callable[[list[FunctionKey]], None]
# (client_id, event) -> True when handled; lets the exporter see commands for its installation
type MessageHook = Callable[[int, ValueUpdate | StructureEvent], bool]


class IoTOpenHub:
    """Holds snapshots, the MQTT connection and entity bookkeeping for one config entry."""

    def __init__(self, hass: HomeAssistant, entry: IoTOpenConfigEntry, client: IoTOpenClient) -> None:
        self.hass = hass
        self.entry = entry
        self.client = client
        self.snapshots: dict[int, InstallationSnapshot] = {}
        self.plans: dict[FunctionKey, EntityPlan] = {}
        self.mqtt = IoTOpenMqtt(
            async_get_clientsession(hass),
            client.mqtt_url,
            client.api_key or "",
            on_message=self._on_message,
            on_state=self._on_state,
            client_id=f"ha-iotopen-{entry.entry_id[:8]}-{secrets.token_hex(3)}",
        )
        self.available = True
        self._client_to_installation: dict[int, int] = {}
        self._adders: dict[str, PlatformAdder] = {}
        self._reload_timers: dict[int, CALLBACK_TYPE] = {}
        self._reload_tasks: set[asyncio.Task[Any]] = set()
        self._unavailable_timer: CALLBACK_TYPE | None = None
        self._unsubs: list[CALLBACK_TYPE] = []
        self._reauth_started = False
        self._reload_locks: dict[int, asyncio.Lock] = {}
        self._stop_unsub: CALLBACK_TYPE | None = None
        self.loaded_options_key = self.options_key
        self.source_id: str | None = None  # HA instance id; marks objects we export
        self.available_installations: dict[int, Installation] = {}
        self._message_hooks: list[MessageHook] = []
        self._connect_listeners: list[Callable[[], None]] = []
        self.extra_topics: set[str] = set()
        self.exporter: Any = None  # IoTOpenExporter when mirroring is enabled

    # ------------------------------------------------------------------ lifecycle
    @property
    def options_key(self) -> tuple[Any, ...]:
        """Options that require a reload when they change."""
        return (
            tuple(sorted(self.selected_installations)),
            bool(self.entry.options.get(CONF_ADD_NEW_INSTALLATIONS, True)),
            self.export_installation_id,
            self.entry.options.get(CONF_EXPORT_LABEL, DEFAULT_EXPORT_LABEL),
            bool(self.entry.options.get(CONF_EXPORT_CONTROL, True)),
            bool(self.entry.options.get(CONF_EXPORT_CATEGORIZED, False)),
            self.export_transport,
            self.instance_name,
            self.import_mode,
        )

    @property
    def instance_name(self) -> str:
        """Short name used in ``ha.destination`` (defaults to the slugified HA location name)."""
        name = str(self.entry.options.get(CONF_INSTANCE_NAME) or "").strip()
        return name or slugify(self.hass.config.location_name or "home") or "home"

    @property
    def export_transport(self) -> str:
        """``cloud`` (IoT Open's MQTT) or ``local`` (HA's MQTT integration, the edge client's broker)."""
        return str(self.entry.options.get(CONF_EXPORT_TRANSPORT, DEFAULT_EXPORT_TRANSPORT))

    @property
    def import_mode(self) -> str:
        """``tagged`` (only ha.destination for this instance) or ``all``."""
        return str(self.entry.options.get(CONF_IMPORT_MODE, DEFAULT_IMPORT_MODE))

    @property
    def destination_names(self) -> set[str]:
        """Values of ``ha.destination`` that address this instance."""
        names = {self.instance_name.lower()}
        if self.source_id:
            names.add(self.source_id.lower())
        return names

    @property
    def export_installation_id(self) -> int | None:
        """Installation HA is mirrored into (default: the only selected installation)."""
        value = self.entry.options.get(CONF_EXPORT_INSTALLATION)
        if value is None:
            selected = self.selected_installations
            return selected[0] if len(selected) == 1 else None
        if str(value) == EXPORT_NONE:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @property
    def uses_subentries(self) -> bool:
        """True once the selection lives in config subentries (one per installation)."""
        return bool(self.entry.options.get(CONF_USE_SUBENTRIES))

    @property
    def selected_installations(self) -> list[int]:
        """Installation ids chosen by the user."""
        if self.uses_subentries:
            return sorted(self.installation_subentries())
        return [int(i) for i in self.entry.options.get(CONF_INSTALLATIONS, [])]

    def installation_subentries(self) -> dict[int, str]:
        """installation id -> subentry id."""
        result: dict[int, str] = {}
        for sub in self.entry.subentries.values():
            if sub.subentry_type != SUBENTRY_INSTALLATION:
                continue
            try:
                result[int(sub.data[CONF_INSTALLATION_ID])] = sub.subentry_id
            except (KeyError, TypeError, ValueError):
                continue
        return result

    def subentry_for(self, installation_id: int) -> str | None:
        """Subentry id that owns an installation's devices and entities."""
        return self.installation_subentries().get(installation_id)

    def group_by_subentry(self, keys: Iterable[FunctionKey]) -> dict[str | None, list[FunctionKey]]:
        """Group function keys by the subentry of their installation (for async_add_entities)."""
        groups: dict[str | None, list[FunctionKey]] = {}
        for key in keys:
            groups.setdefault(self.subentry_for(key[0]), []).append(key)
        return groups

    @callback
    def _add_installation_subentry(self, installation: Installation) -> None:
        self.hass.config_entries.async_add_subentry(
            self.entry,
            ConfigSubentry(
                data=MappingProxyType({CONF_INSTALLATION_ID: installation.id}),
                subentry_type=SUBENTRY_INSTALLATION,
                title=installation.name,
                unique_id=str(installation.id),
            ),
        )

    @callback
    def _migrate_to_subentries(self, available: dict[int, Installation]) -> None:
        """Move the installation selection from options into one subentry per installation."""
        if self.uses_subentries:
            return
        existing = self.installation_subentries()
        for iid in [int(i) for i in self.entry.options.get(CONF_INSTALLATIONS, [])]:
            if iid in existing:
                continue
            inst = available.get(iid) or Installation(id=iid, client_id=0, name=f"Installation {iid}")
            self._add_installation_subentry(inst)
        options = dict(self.entry.options)
        options[CONF_USE_SUBENTRIES] = True
        options.pop(CONF_INSTALLATIONS, None)
        self.hass.config_entries.async_update_entry(self.entry, options=options)

    async def async_setup(self) -> None:
        """Initial REST load, device/entity cleanup and MQTT start."""
        self.source_id = await instance_id.async_get(self.hass)
        await async_register_frontend(self.hass)
        try:
            available = {i.id: i for i in await self.client.installations()}
        except IoTOpenAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except IoTOpenError as err:
            raise ConfigEntryNotReady(f"Cannot reach IoT Open: {err}") from err

        self.available_installations = available
        self._migrate_to_subentries(available)
        self._pin_export_installation()
        self._pick_up_new_installations(set(available))
        self.loaded_options_key = self.options_key
        installations = [available[i] for i in self.selected_installations if i in available]
        for missing in set(self.selected_installations) - set(available):
            _LOGGER.warning("Installation %s is no longer accessible with this token", missing)

        results = await asyncio.gather(
            *(self.client.snapshot(inst) for inst in installations), return_exceptions=True
        )
        for inst, result in zip(installations, results, strict=True):
            if isinstance(result, IoTOpenAuthError):
                raise ConfigEntryAuthFailed(str(result)) from result
            if isinstance(result, (IoTOpenForbiddenError, IoTOpenNotFoundError)):
                _LOGGER.warning("Skipping installation %s (%s): %s", inst.id, inst.name, result)
                continue
            if isinstance(result, BaseException):
                raise ConfigEntryNotReady(f"Cannot load installation {inst.name}: {result}") from result
            self._store_snapshot(result)

        self._sync_devices()
        self._remove_stale_entities()
        self._remove_stale_devices()

        await self.mqtt.set_subscriptions(self._wanted_topics())
        await self.mqtt.start()
        # HA does not unload config entries on shutdown: close the broker session ourselves
        self._stop_unsub = self.hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP, self._async_on_stop
        )
        self._unsubs.append(
            async_track_time_interval(
                self.hass, self._async_check_installations, INSTALLATION_CHECK_INTERVAL
            )
        )
        await self._async_start_exporter()

    async def _async_start_exporter(self) -> None:
        """Mirror labelled HA entities into the chosen installation (if any).

        Never raises: a broken exporter must not take the import side (or the MQTT connection)
        down with it.
        """
        try:
            from .exporter import IoTOpenExporter  # noqa: PLC0415 - avoid import cycle
        except Exception:
            _LOGGER.exception("Could not load the IoT Open exporter; mirroring is disabled")
            return

        iid = self.export_installation_id
        if iid is None:
            return
        installation = self.available_installations.get(iid)
        if installation is None:
            _LOGGER.warning("Export installation %s is not accessible with this token", iid)
            return
        self.exporter = IoTOpenExporter(
            self.hass,
            self,
            installation,
            self.entry.options.get(CONF_EXPORT_LABEL, DEFAULT_EXPORT_LABEL),
            bool(self.entry.options.get(CONF_EXPORT_CONTROL, True)),
            bool(self.entry.options.get(CONF_EXPORT_CATEGORIZED, False)),
            self.export_transport,
        )
        try:
            await self.exporter.async_start()
        except Exception:  # never let mirroring problems break the import side
            _LOGGER.exception("Could not start mirroring to IoT Open installation %s", iid)

    async def _async_on_stop(self, _event: Event) -> None:
        self._stop_unsub = None  # already fired, must not be removed again
        await self.mqtt.stop()

    async def async_unload(self) -> None:
        """Stop everything."""
        if self.exporter is not None:
            await self.exporter.async_stop()
            self.exporter = None
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._stop_unsub is not None:
            self._stop_unsub()
            self._stop_unsub = None
        for cancel in self._reload_timers.values():
            cancel()
        self._reload_timers.clear()
        if self._unavailable_timer:
            self._unavailable_timer()
            self._unavailable_timer = None
        for task in list(self._reload_tasks):
            task.cancel()
        await self.mqtt.stop()

    # ------------------------------------------------------------------ data access (entities)
    def function(self, key: FunctionKey) -> Function | None:
        """Current function object."""
        snap = self.snapshots.get(key[0])
        return snap.functions.get(key[1]) if snap else None

    def installation(self, installation_id: int) -> Installation | None:
        """Installation object."""
        snap = self.snapshots.get(installation_id)
        return snap.installation if snap else None

    def value(self, installation_id: int, topic: str | None) -> Status | None:
        """Latest value of a topic."""
        snap = self.snapshots.get(installation_id)
        if snap is None or topic is None:
            return None
        return snap.values.get(topic)

    def keys_for_platform(self, platform: str) -> list[FunctionKey]:
        """All function keys that belong to a platform."""
        return [key for key, plan in self.plans.items() if plan.platform == platform]

    @callback
    def register_platform(self, platform: str, adder: PlatformAdder) -> None:
        """Called by each platform; receives the keys of entities to create now and later."""
        self._adders[platform] = adder
        adder(self.keys_for_platform(platform))

    def device_identifier_for(self, key: FunctionKey) -> tuple[str, str]:
        """Device registry identifier an entity belongs to."""
        installation_id, _ = key
        fn = self.function(key)
        snap = self.snapshots.get(installation_id)
        if fn is not None and snap is not None and fn.device_id in snap.devices:
            return (DOMAIN, device_identifier(installation_id, fn.device_id))  # type: ignore[arg-type]
        return (DOMAIN, hub_identifier(installation_id))

    async def async_publish(self, key: FunctionKey, topic: str | None, value: Any) -> None:
        """Publish ``{"value": value}`` to a bare topic of the function's installation."""
        inst = self.installation(key[0])
        if inst is None or not topic:
            raise HomeAssistantError(f"Function {key[1]} cannot be controlled")
        if not self.mqtt.connected:
            raise HomeAssistantError("Not connected to IoT Open")
        try:
            await self.mqtt.publish(full_topic(inst.client_id, topic), {"value": value})
        except IoTOpenError as err:
            raise HomeAssistantError(f"Could not send to IoT Open: {err}") from err

    async def async_send(self, key: FunctionKey, raw_value: str) -> None:
        """Publish a control value to the function's ``topic_write``."""
        fn = self.function(key)
        inst = self.installation(key[0])
        if fn is None or inst is None or fn.topic_write is None:
            raise HomeAssistantError(f"Function {key[1]} cannot be controlled")
        if not self.mqtt.connected:
            raise HomeAssistantError("Not connected to IoT Open")
        try:
            await self.mqtt.publish(full_topic(inst.client_id, fn.topic_write), {"value": command_value(raw_value)})
        except IoTOpenError as err:
            raise HomeAssistantError(f"Could not send to IoT Open: {err}") from err

    # ------------------------------------------------------------------ snapshots & plans
    def _store_snapshot(self, snap: InstallationSnapshot) -> None:
        iid = snap.installation.id
        self.snapshots[iid] = snap
        self._client_to_installation[snap.installation.client_id] = iid
        for key in [k for k in self.plans if k[0] == iid]:
            del self.plans[key]
        names = self.destination_names
        import_all = self.import_mode == IMPORT_ALL
        for fn in snap.functions.values():
            if is_own(fn.get_meta, self.source_id):
                continue  # mirrored from this HA instance: never import it back
            device = snap.devices.get(fn.device_id) if fn.device_id is not None else None
            if not wanted_for_import(fn, device, names, import_all):
                continue
            plan = describe(fn)
            if plan is not None:
                self.plans[(iid, fn.id)] = plan

    # ------------------------------------------------------------------ hooks for the exporter
    @callback
    def add_message_hook(self, hook: MessageHook) -> CALLBACK_TYPE:
        """Receive parsed broker messages before the import side handles them."""
        self._message_hooks.append(hook)
        return lambda: self._message_hooks.remove(hook)

    @callback
    def add_connect_listener(self, listener: Callable[[], None]) -> CALLBACK_TYPE:
        """Called every time the broker session is (re-)established."""
        self._connect_listeners.append(listener)
        return lambda: self._connect_listeners.remove(listener)

    async def async_update_subscriptions(self) -> None:
        """Re-apply the subscription set (after extra_topics changed)."""
        await self.mqtt.set_subscriptions(self._wanted_topics())

    def _wanted_topics(self) -> list[str]:
        topics: set[str] = set(self.extra_topics)
        for iid, snap in self.snapshots.items():
            client_id = snap.installation.client_id
            topics.update(structure_topics(client_id))
            for (plan_iid, fid) in self.plans:
                if plan_iid != iid:
                    continue
                fn = snap.functions.get(fid)
                if fn is None:
                    continue
                for topic in (fn.topic_read, normalize_topic(fn.get_meta("topic_read_onoff"))):
                    if topic:
                        topics.add(full_topic(client_id, topic))
        return sorted(topics)

    # ------------------------------------------------------------------ registries
    def _wanted_device_identifiers(self) -> set[str]:
        wanted: set[str] = set()
        for iid, snap in self.snapshots.items():
            for key in self.plans:
                if key[0] != iid:
                    continue
                fn = snap.functions.get(key[1])
                if fn is not None and fn.device_id in snap.devices:
                    wanted.add(device_identifier(iid, fn.device_id))  # type: ignore[arg-type]
                else:
                    # functions without an IoT Open device live on an installation device;
                    # installations without such functions are not shown as devices at all
                    wanted.add(hub_identifier(iid))
        return wanted

    @callback
    def _sync_devices(self) -> None:
        """Create/update hub devices first, then IoT Open devices that have entities."""
        registry = dr.async_get(self.hass)
        wanted = self._wanted_device_identifiers()
        for iid, snap in self.snapshots.items():
            inst = snap.installation
            if hub_identifier(iid) in wanted:
                hub_dev = registry.async_get_or_create(
                    config_entry_id=self.entry.entry_id,
                    config_subentry_id=self.subentry_for(iid),
                    identifiers={(DOMAIN, hub_identifier(iid))},
                    name=inst.name,
                    manufacturer=MANUFACTURER,
                    model="Installation",
                    serial_number=str(iid),
                    configuration_url=f"{self.client.base_url}/installations/{iid}",
                )
                self._fix_device_subentry(registry, hub_dev, iid)
            for device in snap.devices.values():
                if device_identifier(iid, device.id) in wanted:
                    self._create_device(registry, iid, device)

    def _fix_device_subentry(self, registry: dr.DeviceRegistry, device: dr.DeviceEntry, iid: int) -> None:
        """Devices created before subentries existed: drop the plain (no subentry) link."""
        sub = self.subentry_for(iid)
        if hasattr(device, "config_subentry_id"):
            # HA 2026.10+: a device belongs to exactly one (entry, subentry); async_get_or_create
            # with config_subentry_id already put it in the right place
            return
        links = device.config_entries_subentries.get(self.entry.entry_id, set())
        if sub is not None and None in links and sub in links:
            registry.async_update_device(
                device.id, remove_config_entry_id=self.entry.entry_id, remove_config_subentry_id=None
            )

    def _create_device(self, registry: dr.DeviceRegistry, iid: int, device: Device) -> None:
        created = registry.async_get_or_create(
            config_entry_id=self.entry.entry_id,
            config_subentry_id=self.subentry_for(iid),
            identifiers={(DOMAIN, device_identifier(iid, device.id))},
            name=device.name,
            manufacturer=_first_meta(device, "manufacturer", "brand") or MANUFACTURER,
            model=_first_meta(device, "model") or device.type or None,
            model_id=device.type or None,
            serial_number=str(device.id),
            configuration_url=f"{self.client.base_url}/installations/{iid}/devices",
        )
        self._fix_device_subentry(registry, created, iid)

    def _may_prune(self, installation_id: int | None) -> bool:
        """Registry entries may be removed for loaded or deselected installations only.

        An installation that failed to load (API glitch, temporary 403/404) keeps its entities and
        devices so user customisations survive.
        """
        if installation_id is None:
            return True
        return installation_id in self.snapshots or installation_id not in self.selected_installations

    @callback
    def _remove_stale_devices(self) -> None:
        registry = dr.async_get(self.hass)
        wanted = self._wanted_device_identifiers()
        for device in dr.async_entries_for_config_entry(registry, self.entry.entry_id):
            ours = {ident for domain, ident in device.identifiers if domain == DOMAIN}
            if not ours or ours & wanted:
                continue
            if all(self._may_prune(_installation_from_identifier(i)) for i in ours):
                registry.async_update_device(device.id, remove_config_entry_id=self.entry.entry_id)

    @callback
    def _remove_stale_entities(self) -> None:
        registry = er.async_get(self.hass)
        wanted = {
            (plan.platform, entity_unique_id(*key)) for key, plan in self.plans.items()
        }
        for entity in er.async_entries_for_config_entry(registry, self.entry.entry_id):
            iid = _installation_from_unique_id(entity.unique_id)
            if (entity.domain, entity.unique_id) not in wanted and self._may_prune(iid):
                registry.async_remove(entity.entity_id)
                continue
            sub = self.subentry_for(iid) if iid is not None else None
            if sub is not None and entity.config_subentry_id != sub:
                registry.async_update_entity(entity.entity_id, config_subentry_id=sub)

    @callback
    def _entity_id(self, platform: str, key: FunctionKey) -> str | None:
        return er.async_get(self.hass).async_get_entity_id(platform, DOMAIN, entity_unique_id(*key))

    # ------------------------------------------------------------------ MQTT callbacks
    @callback
    def _on_message(self, topic: str, payload: dict[str, Any]) -> None:
        event = parse_message(topic, payload)
        if event is None:
            return
        for hook in list(self._message_hooks):
            if hook(event.client_id, event):
                return
        iid = self._client_to_installation.get(event.client_id)
        if iid is None:
            return
        if isinstance(event, StructureEvent):
            self._schedule_reload(iid)
            return
        assert isinstance(event, ValueUpdate)
        snap = self.snapshots[iid]
        current = snap.values.get(event.topic)
        if current is not None and current.timestamp > event.timestamp:
            return  # older than what we have (e.g. retained or replayed message)
        snap.values[event.topic] = Status(
            topic=event.topic,
            value=event.value,
            timestamp=event.timestamp,
            msg=event.msg,
            client_id=event.client_id,
            installation_id=iid,
        )
        async_dispatcher_send(self.hass, signal_value(self.entry.entry_id, iid, event.topic))

    @callback
    def _on_state(self, state: ConnectionState) -> None:
        _LOGGER.debug("IoT Open MQTT state: %s", state)
        if state is ConnectionState.CONNECTED:
            if self._unavailable_timer:
                self._unavailable_timer()
                self._unavailable_timer = None
            self._set_available(True)
            for listener in list(self._connect_listeners):
                listener()
            if self.mqtt.connect_count > 1:
                # things may have changed while we were offline
                for iid in self.snapshots:
                    self._schedule_reload(iid, delay=0.1)
        elif state is ConnectionState.DISCONNECTED:
            if self._unavailable_timer is None and self.available:
                self._unavailable_timer = async_call_later(
                    self.hass, UNAVAILABLE_GRACE, self._grace_expired
                )
        elif state is ConnectionState.AUTH_FAILED:
            self._set_available(False)
            self._start_reauth()

    @callback
    def _grace_expired(self, _now: datetime) -> None:
        self._unavailable_timer = None
        if not self.mqtt.connected:
            self._set_available(False)

    @callback
    def _set_available(self, available: bool) -> None:
        if available != self.available:
            self.available = available
            async_dispatcher_send(self.hass, signal_availability(self.entry.entry_id))

    @callback
    def _start_reauth(self) -> None:
        if not self._reauth_started:
            self._reauth_started = True
            self.entry.async_start_reauth(self.hass)

    # ------------------------------------------------------------------ structure reload
    @callback
    def _schedule_reload(self, iid: int, delay: float = STRUCTURE_DEBOUNCE) -> None:
        if cancel := self._reload_timers.pop(iid, None):
            cancel()

        @callback
        def _fire(_now: datetime) -> None:
            self._reload_timers.pop(iid, None)
            task = self.entry.async_create_background_task(
                self.hass, self.async_reload_installation(iid), f"iotopen reload {iid}"
            )
            self._reload_tasks.add(task)
            task.add_done_callback(self._reload_tasks.discard)

        self._reload_timers[iid] = async_call_later(self.hass, delay, _fire)

    async def async_reload_installation(self, iid: int) -> None:
        """Re-read one installation and apply the differences to entities and devices.

        Reloads of the same installation run one at a time so two overlapping reloads never diff
        against the same old snapshot (which would add entities twice).
        """
        lock = self._reload_locks.setdefault(iid, asyncio.Lock())
        async with lock:
            await self._async_reload_installation(iid)

    async def _async_reload_installation(self, iid: int) -> None:
        old = self.snapshots.get(iid)
        if old is None:
            return
        try:
            new = await self.client.snapshot(old.installation)
        except IoTOpenAuthError:
            self._start_reauth()
            return
        except IoTOpenConnectionError as err:
            _LOGGER.debug("Reload of installation %s failed: %s", iid, err)
            return  # next event or reconnect retries
        except IoTOpenError as err:
            _LOGGER.warning("Reload of installation %s failed: %s", iid, err)
            return
        new.merge_newer_values(old)
        old_plans = {k: v for k, v in self.plans.items() if k[0] == iid}
        self._store_snapshot(new)
        new_plans = {k: v for k, v in self.plans.items() if k[0] == iid}
        self._apply_changes(iid, old, new, old_plans, new_plans)

    @callback
    def _apply_changes(
        self,
        iid: int,
        old: InstallationSnapshot,
        new: InstallationSnapshot,
        old_plans: dict[FunctionKey, EntityPlan],
        new_plans: dict[FunctionKey, EntityPlan],
    ) -> None:
        ent_reg = er.async_get(self.hass)
        dev_reg = dr.async_get(self.hass)
        self._sync_devices()

        added: dict[str, list[FunctionKey]] = {}
        for key in old_plans.keys() - new_plans.keys():
            if entity_id := self._entity_id(old_plans[key].platform, key):
                ent_reg.async_remove(entity_id)
        for key, plan in new_plans.items():
            before = old_plans.get(key)
            if before is None or before.platform != plan.platform:
                if before is not None and (entity_id := self._entity_id(before.platform, key)):
                    ent_reg.async_remove(entity_id)
                added.setdefault(plan.platform, []).append(key)
                continue
            old_fn, new_fn = old.functions[key[1]], new.functions[key[1]]
            entity_id = self._entity_id(plan.platform, key)
            if entity_id is not None:
                self._update_registry_entry(ent_reg, dev_reg, entity_id, key, old_fn, before, plan)
            if old_fn.signature() != new_fn.signature() or before != plan:
                async_dispatcher_send(self.hass, signal_function(self.entry.entry_id, *key))

        # values re-read over REST (e.g. after a reconnect) that differ from what entities show
        for topic, status in new.values.items():
            if old.values.get(topic) != status:
                async_dispatcher_send(self.hass, signal_value(self.entry.entry_id, iid, topic))

        # device renames etc. are handled by _sync_devices
        for platform, keys in added.items():
            if adder := self._adders.get(platform):
                adder(keys)
        self._remove_stale_devices()
        self.entry.async_create_background_task(
            self.hass, self.mqtt.set_subscriptions(self._wanted_topics()), "iotopen subscriptions"
        )

    def _update_registry_entry(
        self,
        ent_reg: er.EntityRegistry,
        dev_reg: dr.DeviceRegistry,
        entity_id: str,
        key: FunctionKey,
        old_fn: Function,
        before: EntityPlan,
        plan: EntityPlan,
    ) -> None:
        """Move entity to another device and apply ha.hidden without touching user choices."""
        entry = ent_reg.async_get(entity_id)
        if entry is None:
            return
        changes: dict[str, Any] = {}
        device = _get_device(dev_reg, self.device_identifier_for(key))
        if device is not None and entry.device_id != device.id:
            changes["device_id"] = device.id
        if plan.hidden != before.hidden:
            if plan.hidden and entry.hidden_by is None:
                changes["hidden_by"] = er.RegistryEntryHider.INTEGRATION
            elif not plan.hidden and entry.hidden_by is er.RegistryEntryHider.INTEGRATION:
                changes["hidden_by"] = None
        if changes:
            ent_reg.async_update_entity(entity_id, **changes)

    @callback
    def _pin_export_installation(self) -> None:
        """Store the implicit export choice so a later new installation cannot switch it off.

        Without this, the default "the only selected installation" disappears as soon as a second
        installation becomes accessible and is auto-selected.
        """
        options = dict(self.entry.options)
        if CONF_EXPORT_INSTALLATION not in options:
            iid = self.export_installation_id
            options[CONF_EXPORT_INSTALLATION] = str(iid) if iid is not None else EXPORT_NONE
        if not options.get(CONF_INSTANCE_NAME):
            options[CONF_INSTANCE_NAME] = self.instance_name
        options.setdefault(CONF_IMPORT_MODE, DEFAULT_IMPORT_MODE)
        if options == dict(self.entry.options):
            return
        self.hass.config_entries.async_update_entry(self.entry, options=options)
        self.loaded_options_key = self.options_key

    # ------------------------------------------------------------------ new installations
    @callback
    def _pick_up_new_installations(self, current: set[int]) -> bool:
        """Record installations not seen before; select them if the user wants that.

        Returns True when the selection changed.
        """
        known = {int(i) for i in self.entry.options.get(CONF_KNOWN_INSTALLATIONS, [])}
        new = current - known
        if not new:
            return False
        options = dict(self.entry.options)
        options[CONF_KNOWN_INSTALLATIONS] = sorted(known | current)
        changed = False
        add = bool(options.get(CONF_ADD_NEW_INSTALLATIONS, True))
        if add:
            _LOGGER.info("New IoT Open installations found: %s", sorted(new))
            if not self.uses_subentries:
                options[CONF_INSTALLATIONS] = sorted(set(self.selected_installations) | new)
            changed = True
        self.hass.config_entries.async_update_entry(self.entry, options=options)
        if add and self.uses_subentries:
            for iid in sorted(new):
                if (inst := self.available_installations.get(iid)) is not None:
                    self._add_installation_subentry(inst)
        return changed

    async def _async_check_installations(self, _now: datetime | None = None) -> None:
        """Hourly: find installations created after setup."""
        try:
            available = {i.id: i for i in await self.client.installations()}
        except IoTOpenAuthError:
            self._start_reauth()
            return
        except IoTOpenError as err:
            _LOGGER.debug("Installation check failed: %s", err)
            return
        # new installations need their details to get a subentry
        self.available_installations = available
        current = set(available)
        # the update listener reloads the entry when the selection changed
        self._pick_up_new_installations(current)

    def diagnostics(self) -> dict[str, Any]:
        """Data for diagnostics (no secrets)."""
        return {
            "instance": {
                "name": self.instance_name,
                "id": self.source_id,
                "import_mode": self.import_mode,
            },
            "export": self.exporter.diagnostics() if self.exporter else None,
            "mqtt": {
                "state": self.mqtt.state,
                "connect_count": self.mqtt.connect_count,
                "subscriptions": len(self.mqtt.subscriptions),
            },
            "available": self.available,
            "installations": {
                str(iid): {
                    "client_id": snap.installation.client_id,
                    "name": snap.installation.name,
                    "devices": {str(d.id): {"type": d.type, "meta": d.meta} for d in snap.devices.values()},
                    "functions": {
                        str(f.id): {
                            "type": f.type,
                            "meta": f.meta,
                            "plan": _plan_dict(self.plans.get((iid, f.id))),
                            "value": _status_dict(snap.values.get(f.topic_read or "")),
                        }
                        for f in snap.functions.values()
                    },
                }
                for iid, snap in self.snapshots.items()
            },
        }


FRONTEND_URL = "/iotopen_static"
ICONS_JS = "iotopen-icons.js"


async def async_register_frontend(hass: HomeAssistant) -> None:
    """Serve the icon set ("iotopen:logo") once per HA run."""
    key = f"{DOMAIN}_frontend_registered"
    if hass.data.get(key):
        return
    hass.data[key] = True
    try:
        from pathlib import Path  # noqa: PLC0415

        from homeassistant.components.frontend import add_extra_js_url  # noqa: PLC0415
        from homeassistant.components.http import StaticPathConfig  # noqa: PLC0415

        folder = Path(__file__).parent / "frontend"
        await hass.http.async_register_static_paths(
            [StaticPathConfig(FRONTEND_URL, str(folder), cache_headers=False)]
        )
        add_extra_js_url(hass, f"{FRONTEND_URL}/{ICONS_JS}?v=1")
    except Exception:  # an icon must never break the integration
        _LOGGER.exception("Could not register the IoT Open icon set")


def _get_device(registry: dr.DeviceRegistry, identifier: tuple[str, str]) -> dr.DeviceEntry | None:
    """Look a device up by identifier on both old and new device registry APIs."""
    by_identifier = getattr(registry, "async_get_device_by_identifier", None)
    if by_identifier is not None:
        try:
            return by_identifier(identifier)
        except TypeError:
            pass
    return registry.async_get_device(identifiers={identifier})


def _first_meta(device: Device, *keys: str) -> str | None:
    """First non-empty metadata value among ``keys``."""
    for key in keys:
        value = (device.get_meta(key) or "").strip()
        if value:
            return value
    return None


def _installation_from_unique_id(unique_id: str) -> int | None:
    """``iotopen_<iid>_<fid>`` -> iid."""
    parts = unique_id.split("_")
    if len(parts) == 3 and parts[0] == "iotopen" and parts[1].isdigit():
        return int(parts[1])
    return None


def _installation_from_identifier(identifier: str) -> int | None:
    """``installation_<iid>`` / ``device_<iid>_<id>`` -> iid."""
    parts = identifier.split("_")
    if len(parts) >= 2 and parts[0] in ("installation", "device") and parts[1].isdigit():
        return int(parts[1])
    return None


def _plan_dict(plan: EntityPlan | None) -> dict[str, Any] | None:
    if plan is None:
        return None
    return {
        "platform": plan.platform,
        "device_class": plan.device_class,
        "state_class": plan.state_class,
        "unit": plan.unit,
        "precision": plan.precision,
        "hidden": plan.hidden,
    }


def _status_dict(status: Status | None) -> dict[str, Any] | None:
    if status is None:
        return None
    return {"value": status.value, "timestamp": status.timestamp, "msg": status.msg}


def installation_options(installations: Iterable[Installation]) -> list[dict[str, str]]:
    """Select options for installations (label includes the id for duplicates)."""
    return [
        {"value": str(i.id), "label": f"{i.name} ({i.id})"}
        for i in sorted(installations, key=lambda x: x.name.lower())
    ]
