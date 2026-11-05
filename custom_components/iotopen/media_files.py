"""Sound files for media players (pure Python, no HA imports).

* ``scan_media`` lists the audio files in Home Assistant's media folder, for ``ha.media_list`` on a media
  player's play function.
* ``plan_sync`` decides what to download and delete when the audio files stored on an IoT Open installation
  are copied into the ``iotopen`` sub-folder of the media folder.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

AUDIO_EXTENSIONS = {".mp3", ".wav", ".ogg", ".oga", ".opus", ".flac", ".m4a", ".aac", ".wma"}
SYNC_FOLDER = "iotopen"  # sub-folder of the media folder that the sync owns
MEDIA_LIST_MAX = 2000  # characters of JSON in ha.media_list
MAX_FILE_BYTES = 50 * 1024 * 1024  # a sound file larger than this is not downloaded
_NAME_MAX = 100
_UNSAFE = re.compile(r"[^\w.\- ]+")


def scan_media(root: str) -> list[str]:
    """Audio files under ``root`` as sorted relative paths (``larm/brand.mp3``); hidden entries are skipped."""
    found: list[str] = []
    if not root or not os.path.isdir(root):
        return found
    for path, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in files:
            if name.startswith(".") or os.path.splitext(name)[1].lower() not in AUDIO_EXTENSIONS:
                continue
            found.append(os.path.relpath(os.path.join(path, name), root).replace(os.sep, "/"))
    return sorted(found, key=str.casefold)


def media_list_meta(files: Iterable[str]) -> str | None:
    """``ha.media_list``: a JSON list of file paths, cut to ``MEDIA_LIST_MAX`` characters."""
    listed = list(files)
    text = json.dumps(listed, ensure_ascii=False)
    while listed and len(text) > MEDIA_LIST_MAX:
        listed.pop()
        text = json.dumps(listed, ensure_ascii=False)
    return text if listed else None


def safe_file_name(name: str, file_id: int) -> str:
    """A file name that is safe on disk: no folders, no ``..``, no hidden files, at most 100 characters."""
    base = _UNSAFE.sub("_", os.path.basename(str(name).replace("\\", "/"))).strip(" .")
    if not base:
        return f"file-{file_id}"
    stem, ext = os.path.splitext(base)
    if len(base) > _NAME_MAX:
        base = stem[: _NAME_MAX - len(ext)] + ext
    return base


@dataclass(slots=True, frozen=True)
class RemoteFile:
    """An audio file stored on the IoT Open installation."""

    id: int
    name: str
    hash: str


def local_names(files: Iterable[RemoteFile]) -> dict[str, RemoteFile]:
    """Local file name -> remote file. Names that clash (case-insensitively) get ``-<id>`` added."""
    result: dict[str, RemoteFile] = {}
    taken: set[str] = set()
    for remote in sorted(files, key=lambda f: f.id):
        name = safe_file_name(remote.name, remote.id)
        if name.casefold() in taken:
            stem, ext = os.path.splitext(name)
            name = f"{stem}-{remote.id}{ext}"
        taken.add(name.casefold())
        result[name] = remote
    return result


@dataclass(slots=True)
class SyncPlan:
    """What the sync has to do."""

    download: dict[str, RemoteFile]  # local name -> file to fetch
    delete: list[str]  # local names we wrote earlier that are gone in IoT Open
    keep: dict[str, str]  # local name -> hash, the manifest after the sync


def plan_sync(
    remote: Iterable[RemoteFile], manifest: dict[str, str], present: Iterable[str]
) -> SyncPlan:
    """Compare IoT Open's files with what we wrote earlier (``manifest``: name -> hash) and what is on disk.

    Only files in the manifest are ever deleted, so files someone else put in the folder are left alone.
    """
    on_disk = set(present)
    wanted = local_names(remote)
    download = {
        name: f for name, f in wanted.items() if manifest.get(name) != f.hash or name not in on_disk
    }
    delete = sorted(name for name in manifest if name not in wanted and name in on_disk)
    keep = {name: f.hash for name, f in wanted.items()}
    return SyncPlan(download=download, delete=delete, keep=keep)


def remote_files(entries: Iterable[Any]) -> list[RemoteFile]:
    """Audio files from ``IoTOpenClient.files`` (objects with ``id``, ``name``, ``hash``, ``mime``)."""
    return [
        RemoteFile(id=int(e.id), name=str(e.name), hash=str(e.hash).lower())
        for e in entries
        if e.hash and str(e.mime).lower().startswith("audio/")
    ]
