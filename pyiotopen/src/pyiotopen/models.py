"""Data models for IoT Open (Lynx) objects.

All models keep the untouched API object in ``raw`` so callers can reach fields that are not
modelled explicitly. Metadata values in IoT Open are always strings.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .const import (
    META_DEVICE_ID,
    META_FORMAT,
    META_NAME,
    META_STATE_OFF,
    META_STATE_ON,
    META_TEXT_OFF,
    META_TEXT_ON,
    META_TOPIC_READ,
    META_TOPIC_SET_LEGACY,
    META_TOPIC_WRITE,
    META_UNIT,
)
from .topics import normalize_topic


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _meta(data: dict[str, Any], key: str = "meta") -> dict[str, str]:
    meta = data.get(key) or {}
    if not isinstance(meta, dict):
        return {}
    return {str(k): "" if v is None else str(v) for k, v in meta.items()}


def _str_or_none(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


@dataclass(slots=True)
class AuthResult:
    """Result of ``POST/PUT /api/v2/auth``."""

    token: str
    next_step: str | None = None
    expires_at: int | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def needs_second_factor(self) -> bool:
        """True when the token is only a temporary token waiting for a 2FA challenge."""
        return bool(self.next_step)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AuthResult:
        """Build from an API response."""
        return cls(
            token=str(data.get("token") or ""),
            next_step=data.get("next_step") or None,
            expires_at=_int_or_none(data.get("expires_at") or data.get("expires")),
            raw=data,
        )


@dataclass(slots=True)
class Installation:
    """An IoT Open installation (from ``/api/v2/installationinfo``)."""

    id: int
    client_id: int
    name: str
    organization_id: int | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Installation:
        """Build from an API response."""
        iid = int(data["id"])
        return cls(
            id=iid,
            client_id=int(data["client_id"]),
            name=(str(data.get("name") or "").strip() or f"Installation {iid}"),
            organization_id=_int_or_none(data.get("organization_id")),
            raw=data,
        )


@dataclass(slots=True)
class Device:
    """An IoT Open device (``/api/v2/devicex/{installation_id}``)."""

    id: int
    installation_id: int
    type: str
    meta: dict[str, str] = field(default_factory=dict)
    protected_meta: dict[str, str] = field(default_factory=dict)
    created: int | None = None
    updated: int | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Device:
        """Build from an API response."""
        return cls(
            id=int(data["id"]),
            installation_id=int(data.get("installation_id") or 0),
            type=str(data.get("type") or ""),
            meta=_meta(data),
            protected_meta=_meta(data, "protected_meta"),
            created=_int_or_none(data.get("created")),
            updated=_int_or_none(data.get("updated")),
            raw=data,
        )

    @property
    def name(self) -> str:
        """Human readable name (meta ``name``, falling back to ``<type> <id>``)."""
        return _str_or_none(self.meta.get(META_NAME)) or f"{self.type or 'Device'} {self.id}".strip()

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        """Return a metadata value (public meta first, then protected meta)."""
        if key in self.meta:
            return self.meta[key]
        return self.protected_meta.get(key, default)

    def signature(self) -> tuple[Any, ...]:
        """Value used to detect changes between two reads."""
        return (self.type, tuple(sorted(self.meta.items())), self.updated)


@dataclass(slots=True)
class Function:
    """An IoT Open function (``/api/v2/functionx/{installation_id}``)."""

    id: int
    installation_id: int
    type: str
    meta: dict[str, str] = field(default_factory=dict)
    protected_meta: dict[str, str] = field(default_factory=dict)
    created: int | None = None
    updated: int | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Function:
        """Build from an API response."""
        return cls(
            id=int(data["id"]),
            installation_id=int(data.get("installation_id") or 0),
            type=str(data.get("type") or ""),
            meta=_meta(data),
            protected_meta=_meta(data, "protected_meta"),
            created=_int_or_none(data.get("created")),
            updated=_int_or_none(data.get("updated")),
            raw=data,
        )

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        """Return a metadata value (public meta first, then protected meta)."""
        if key in self.meta:
            return self.meta[key]
        return self.protected_meta.get(key, default)

    @property
    def name(self) -> str:
        """Human readable name (meta ``name``, falling back to ``<type> <id>``)."""
        return _str_or_none(self.meta.get(META_NAME)) or f"{self.type or 'Function'} {self.id}".strip()

    @property
    def device_id(self) -> int | None:
        """Id of the device this function belongs to, if any."""
        return _int_or_none(self.meta.get(META_DEVICE_ID))

    @property
    def topic_read(self) -> str | None:
        """Bare topic that carries the function's value."""
        return normalize_topic(self.meta.get(META_TOPIC_READ))

    @property
    def topic_write(self) -> str | None:
        """Bare topic used to control the function (IoT Open convention: ``set/obj/...``)."""
        return normalize_topic(self.meta.get(META_TOPIC_WRITE)) or normalize_topic(
            self.meta.get(META_TOPIC_SET_LEGACY)
        )

    @property
    def unit(self) -> str | None:
        """Unit of measurement (meta ``unit``)."""
        return _str_or_none(self.meta.get(META_UNIT))

    @property
    def format(self) -> str | None:
        """printf style format, e.g. ``%.1f`` (meta ``format``)."""
        return _str_or_none(self.meta.get(META_FORMAT))

    @property
    def state_on(self) -> str | None:
        """Value that means "on" (meta ``state_on``)."""
        return _str_or_none(self.meta.get(META_STATE_ON))

    @property
    def state_off(self) -> str | None:
        """Value that means "off" (meta ``state_off``)."""
        return _str_or_none(self.meta.get(META_STATE_OFF))

    @property
    def text_on(self) -> str | None:
        """Text shown for the "on" state (meta ``text_on``)."""
        return _str_or_none(self.meta.get(META_TEXT_ON))

    @property
    def text_off(self) -> str | None:
        """Text shown for the "off" state (meta ``text_off``)."""
        return _str_or_none(self.meta.get(META_TEXT_OFF))

    @property
    def is_binary(self) -> bool:
        """True when both ``state_on`` and ``state_off`` are defined."""
        return self.state_on is not None and self.state_off is not None

    @property
    def is_controllable(self) -> bool:
        """True when the function has a ``topic_write`` (``set/obj/...``)."""
        return self.topic_write is not None

    def signature(self) -> tuple[Any, ...]:
        """Value used to detect changes between two reads."""
        return (self.type, tuple(sorted(self.meta.items())), self.updated)


@dataclass(slots=True)
class Status:
    """Latest value of one topic (``/api/v2/status/{installation_id}`` or an MQTT message)."""

    topic: str
    value: Any
    timestamp: float
    msg: str = ""
    client_id: int | None = None
    installation_id: int | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Status:
        """Build from an API response entry."""
        return cls(
            topic=normalize_topic(data.get("topic")) or "",
            value=data.get("value"),
            timestamp=_float_or_none(data.get("timestamp")) or 0.0,
            msg=str(data.get("msg") or ""),
            client_id=_int_or_none(data.get("client_id")),
            installation_id=_int_or_none(data.get("installation_id")),
        )

    @classmethod
    def from_payload(
        cls,
        topic: str,
        payload: dict[str, Any],
        *,
        client_id: int | None = None,
        installation_id: int | None = None,
    ) -> Status:
        """Build from an MQTT payload ``{"value", "timestamp", "msg"}``."""
        return cls(
            topic=normalize_topic(topic) or "",
            value=payload.get("value"),
            timestamp=_float_or_none(payload.get("timestamp")) or time.time(),
            msg=str(payload.get("msg") or ""),
            client_id=client_id,
            installation_id=installation_id,
        )


@dataclass(slots=True)
class InstallationSnapshot:
    """Everything needed to represent one installation: structure plus latest values."""

    installation: Installation
    devices: dict[int, Device] = field(default_factory=dict)
    functions: dict[int, Function] = field(default_factory=dict)
    values: dict[str, Status] = field(default_factory=dict)

    def functions_for_topic(self, topic: str) -> list[Function]:
        """All functions whose ``topic_read`` equals ``topic`` (bare form)."""
        bare = normalize_topic(topic)
        return [f for f in self.functions.values() if f.topic_read == bare]

    def read_topics(self) -> set[str]:
        """Bare ``topic_read`` of every function."""
        return {f.topic_read for f in self.functions.values() if f.topic_read}

    def merge_newer_values(self, older: InstallationSnapshot) -> None:
        """Keep values from ``older`` that are newer than what this snapshot holds."""
        for topic, old in older.values.items():
            new = self.values.get(topic)
            if new is None or new.timestamp < old.timestamp:
                self.values[topic] = old
