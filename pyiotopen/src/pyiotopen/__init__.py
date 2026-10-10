"""Async Python client for IoT Open (Lynx): REST API plus live values over MQTT/WebSocket."""

from __future__ import annotations

from .client import IoTOpenClient, mqtt_url_for, normalize_base_url
from .const import DEFAULT_BASE_URL, EVT_DEVICEX_UPDATED, EVT_FUNCTIONX_UPDATED, STRUCTURE_EVENTS
from .events import LiveEvent, StructureEvent, ValueUpdate, decode_payload, parse_message
from .exceptions import (
    IoTOpenAuthError,
    IoTOpenConnectionError,
    IoTOpenError,
    IoTOpenForbiddenError,
    IoTOpenNotFoundError,
    IoTOpenResponseError,
    IoTOpenTwoFactorRequired,
)
from .models import AuthResult, Device, Function, Installation, InstallationSnapshot, Status
from .mqtt import ConnectionState, IoTOpenMqtt
from .topics import full_topic, normalize_topic, split_topic, structure_topics

__version__ = "0.1.0"

__all__ = [
    "DEFAULT_BASE_URL",
    "EVT_DEVICEX_UPDATED",
    "EVT_FUNCTIONX_UPDATED",
    "STRUCTURE_EVENTS",
    "AuthResult",
    "ConnectionState",
    "Device",
    "Function",
    "Installation",
    "InstallationSnapshot",
    "IoTOpenAuthError",
    "IoTOpenClient",
    "IoTOpenConnectionError",
    "IoTOpenError",
    "IoTOpenForbiddenError",
    "IoTOpenMqtt",
    "IoTOpenNotFoundError",
    "IoTOpenResponseError",
    "IoTOpenTwoFactorRequired",
    "LiveEvent",
    "Status",
    "StructureEvent",
    "ValueUpdate",
    "__version__",
    "decode_payload",
    "full_topic",
    "mqtt_url_for",
    "normalize_base_url",
    "normalize_topic",
    "parse_message",
    "split_topic",
    "structure_topics",
]
