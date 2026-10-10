"""Helpers for IoT Open MQTT topics.

IoT Open uses two spellings of the same topic:

* bare topic, as stored in function metadata and returned by ``/api/v2/status``: ``obj/demo/12/level``
* broker topic, prefixed with the installation's ``client_id``: ``2001/obj/demo/12/level``
  (also used by the log API ``/api/v3beta/log``).
"""

from __future__ import annotations

import re

from .const import STRUCTURE_EVENTS

_PREFIX_RE = re.compile(r"^(\d+)/(.*)$", re.DOTALL)


def normalize_topic(topic: str | None) -> str | None:
    """Return the bare topic, stripping a leading ``<client_id>/`` prefix and surrounding slashes."""
    if topic is None:
        return None
    topic = str(topic).strip()
    if not topic:
        return None
    match = _PREFIX_RE.match(topic)
    if match:
        topic = match.group(2)
    return topic.strip("/") or None


def full_topic(client_id: int | str, topic: str) -> str:
    """Return the broker topic ``<client_id>/<bare topic>``."""
    bare = normalize_topic(topic)
    if bare is None:
        raise ValueError("empty topic")
    return f"{client_id}/{bare}"


def split_topic(topic: str) -> tuple[int, str] | None:
    """Split a broker topic into ``(client_id, bare_topic)``; ``None`` if it has no numeric prefix."""
    match = _PREFIX_RE.match(topic)
    if not match or not match.group(2):
        return None
    return int(match.group(1)), match.group(2)


def structure_topics(client_id: int | str) -> list[str]:
    """Broker topics that announce structure changes for one installation."""
    return [f"{client_id}/{event}" for event in STRUCTURE_EVENTS]
