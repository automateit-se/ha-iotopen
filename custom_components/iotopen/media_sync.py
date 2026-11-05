"""Copy the audio files stored on the mirror installation into Home Assistant's media folder.

IoT Open cannot write to the Raspberry Pi, so Home Assistant fetches: it lists the installation's
``audio/*`` files and downloads new or changed ones into ``<media folder>/iotopen/``. That happens at
start-up, when IoT Open announces a change (``<client_id>/evt/file/updated``, sent when a file is added,
changed or deleted), when the broker connection comes back after an outage, and every ``SAFETY_INTERVAL``
in case an event was missed. There is no polling in between. A media player's play function can then play ``iotopen/<name>``, also
when IoT Open cannot be reached. Files deleted in IoT Open are deleted here; only files this sync
wrote (kept in a manifest) are ever deleted.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import tempfile
from collections.abc import Callable
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .media_files import MAX_FILE_BYTES, SYNC_FOLDER, plan_sync, remote_files
from .pyiotopen import (
    EVT_FILE_UPDATED,
    IoTOpenAuthError,
    IoTOpenConnectionError,
    IoTOpenError,
    StructureEvent,
    ValueUpdate,
    full_topic,
)

if TYPE_CHECKING:
    from .hub import IoTOpenHub

_LOGGER = logging.getLogger(__name__)

SAFETY_INTERVAL = timedelta(hours=6)  # in case a file event was missed
EVENT_DEBOUNCE = 3.0  # several uploads in a row give one sync
RETRY_DELAY = 300.0  # after a failed round (e.g. no internet)
STORE_VERSION = 1


def media_root(hass: HomeAssistant) -> str | None:
    """Home Assistant's local media folder (``/media`` in a container), if there is one."""
    return hass.config.media_dirs.get("local")


def _present(folder: str) -> list[str]:
    try:
        return [n for n in os.listdir(folder) if os.path.isfile(os.path.join(folder, n))]
    except FileNotFoundError:
        return []


def _write(folder: str, name: str, data: bytes) -> None:
    """Write atomically, so a player never reads half a file."""
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".iotopen-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(tmp, 0o644)
        os.replace(tmp, os.path.join(folder, name))
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _delete(folder: str, name: str) -> None:
    try:
        os.unlink(os.path.join(folder, name))
    except FileNotFoundError:
        pass


class IoTOpenMediaSync:
    """Keeps ``<media folder>/iotopen/`` equal to the installation's audio files."""

    def __init__(
        self,
        hass: HomeAssistant,
        hub: IoTOpenHub,
        installation_id: int,
        client_id: int,
        on_change: Callable[[], None],
    ) -> None:
        self.hass = hass
        self.hub = hub
        self.installation_id = installation_id
        self.client_id = client_id
        self.event_topic = full_topic(client_id, EVT_FILE_UPDATED)
        self._on_change = on_change
        self._store: Store[dict[str, Any]] = Store(
            hass, STORE_VERSION, f"{DOMAIN}.media_sync_{hub.entry.entry_id}"
        )
        self._manifest: dict[str, str] = {}  # local name -> sha256 of what we wrote
        self._lock = asyncio.Lock()
        self._unsubs: list[Callable[[], None]] = []
        self._timer: CALLBACK_TYPE | None = None
        self.last_error: str | None = None
        self.last_sync: float | None = None
        self.rounds = 0

    async def async_start(self) -> None:
        stored = await self._store.async_load() or {}
        self._manifest = {str(k): str(v) for k, v in (stored.get("files") or {}).items()}
        self._unsubs.append(self.hub.add_message_hook(self._on_message))
        self._unsubs.append(self.hub.add_connect_listener(self.schedule_sync))
        self._unsubs.append(async_track_time_interval(self.hass, self._on_interval, SAFETY_INTERVAL))
        self.hub.extra_topics.add(self.event_topic)
        await self.hub.async_update_subscriptions()
        self.schedule_sync(0)

    async def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._timer:
            self._timer()
            self._timer = None
        self.hub.extra_topics.discard(self.event_topic)

    @callback
    def schedule_sync(self, delay: float = EVENT_DEBOUNCE) -> None:
        """Sync after ``delay`` seconds; a new call before then restarts the wait."""
        if self._timer:
            self._timer()

        @callback
        def _fire(_now: Any) -> None:
            self._timer = None
            self.hub.entry.async_create_background_task(self.hass, self.async_sync(), "iotopen media sync")

        self._timer = async_call_later(self.hass, delay, _fire)

    @callback
    def _on_message(self, client_id: int, event: ValueUpdate | StructureEvent) -> bool:
        if client_id != self.client_id or not isinstance(event, StructureEvent) or event.kind != "file":
            return False
        self.schedule_sync()
        return True

    @callback
    def _on_interval(self, _now: Any) -> None:
        self.schedule_sync(0)

    async def async_sync(self) -> None:
        """One sync round. Never raises: a failed round is retried at the next interval."""
        async with self._lock:
            self.rounds += 1
            try:
                changed = await self._sync()
                self.last_error = None
            except IoTOpenAuthError as err:
                self.last_error = "auth"
                _LOGGER.warning("IoT Open media sync: not authorized: %s", err)
                return
            except IoTOpenError as err:
                self.last_error = str(err)
                _LOGGER.debug("IoT Open media sync failed, retrying later: %s", err)
                if isinstance(err, IoTOpenConnectionError) and self._unsubs:
                    self.schedule_sync(RETRY_DELAY)
                return
            except OSError as err:
                self.last_error = str(err)
                _LOGGER.warning("IoT Open media sync could not write to the media folder: %s", err)
                return
            self.last_sync = self.hass.loop.time()
            if changed:
                self._on_change()

    async def _sync(self) -> bool:
        root = media_root(self.hass)
        if not root:
            self.last_error = "no local media folder"
            return False
        folder = os.path.join(root, SYNC_FOLDER)
        remote = remote_files(await self.hub.client.files(self.installation_id, mime="audio/*"))
        present = await self.hass.async_add_executor_job(_present, folder)
        plan = plan_sync(remote, self._manifest, present)
        keep = dict(plan.keep)
        for name, remote_file in plan.download.items():
            try:
                data = await self.hub.client.download_file(remote_file.hash, max_bytes=MAX_FILE_BYTES)
            except IoTOpenAuthError:
                raise
            except IoTOpenError as err:
                _LOGGER.warning("IoT Open media sync: could not download %s: %s", remote_file.name, err)
                self._keep_old(keep, name, present)
                continue
            if hashlib.sha256(data).hexdigest() != remote_file.hash:
                _LOGGER.warning("IoT Open media sync: %s does not match its checksum, skipped", remote_file.name)
                self._keep_old(keep, name, present)
                continue
            await self.hass.async_add_executor_job(_write, folder, name, data)
            _LOGGER.info("IoT Open media sync: saved %s/%s", SYNC_FOLDER, name)
        for name in plan.delete:
            await self.hass.async_add_executor_job(_delete, folder, name)
            _LOGGER.info("IoT Open media sync: deleted %s/%s", SYNC_FOLDER, name)
        changed = bool(plan.download or plan.delete) or keep != self._manifest
        if keep != self._manifest:
            self._manifest = keep
            await self._store.async_save({"files": keep})
        return changed

    def _keep_old(self, keep: dict[str, str], name: str, present: list[str]) -> None:
        """A failed download keeps the copy we already have (if any) and is retried next round."""
        if name in self._manifest and name in present:
            keep[name] = self._manifest[name]
        else:
            keep.pop(name, None)

    def diagnostics(self) -> dict[str, Any]:
        return {
            "installation_id": self.installation_id,
            "rounds": self.rounds,
            "files": sorted(self._manifest),
            "last_error": self.last_error,
            "synced": self.last_sync is not None,
        }
