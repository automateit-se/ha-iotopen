"""Classification of messages received from the IoT Open broker."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from .const import EVT_DEVICEX_UPDATED, EVT_FUNCTIONX_UPDATED
from .topics import split_topic


@dataclass(slots=True, frozen=True)
class ValueUpdate:
    """A new value for ``<client_id>/<topic>``."""

    client_id: int
    topic: str
    value: Any
    timestamp: float
    msg: str = ""


@dataclass(slots=True, frozen=True)
class StructureEvent:
    """Devices (``kind == "devicex"``) or functions (``"functionx"``) changed in an installation."""

    client_id: int
    kind: str


LiveEvent = ValueUpdate | StructureEvent


def decode_payload(raw: bytes | str) -> dict[str, Any]:
    """Decode a message payload to ``{"value", "timestamp", "msg"}``.

    IoT Open sends JSON objects. Anything else (plain numbers or strings from third party
    publishers) is wrapped as ``{"value": ...}``.
    """
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else raw
    text = text.strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        try:
            return {"value": float(text)}
        except ValueError:
            return {"value": text}
    if isinstance(data, dict):
        return data
    return {"value": data}


def parse_message(topic: str, payload: dict[str, Any]) -> LiveEvent | None:
    """Turn a broker message into a :class:`ValueUpdate` or :class:`StructureEvent`."""
    split = split_topic(topic)
    if split is None:
        return None
    client_id, bare = split
    if bare == EVT_FUNCTIONX_UPDATED:
        return StructureEvent(client_id, "functionx")
    if bare == EVT_DEVICEX_UPDATED:
        return StructureEvent(client_id, "devicex")
    if bare.startswith("evt/"):
        return None
    try:
        timestamp = float(payload.get("timestamp"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        timestamp = time.time()
    return ValueUpdate(
        client_id=client_id,
        topic=bare,
        value=payload.get("value"),
        timestamp=timestamp,
        msg=str(payload.get("msg") or ""),
    )
