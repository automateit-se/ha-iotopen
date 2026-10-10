"""Constants for pyiotopen."""

from __future__ import annotations

DEFAULT_BASE_URL = "https://lynx.iotopen.se"
DEFAULT_TIMEOUT = 30

API_KEY_HEADER = "X-API-Key"

# IoT Open publishes these (payload {}) on "<client_id>/<event>" whenever a function or device in the
# installation is created, changed (including metadata) or deleted. The payload does not say WHAT
# changed, so the receiver re-reads devicex/functionx for that installation.
EVT_FUNCTIONX_UPDATED = "evt/functionx/updated"
EVT_DEVICEX_UPDATED = "evt/devicex/updated"
STRUCTURE_EVENTS: tuple[str, ...] = (EVT_FUNCTIONX_UPDATED, EVT_DEVICEX_UPDATED)

# Function metadata keys used by IoT Open conventions.
META_NAME = "name"
META_DEVICE_ID = "device_id"
META_TOPIC_READ = "topic_read"
# IoT Open convention: read status from "obj/...", control via "set/obj/..."
META_TOPIC_WRITE = "topic_write"
META_TOPIC_SET_LEGACY = "topic_set"  # older/non-standard spelling, only used as fallback
META_UNIT = "unit"
META_FORMAT = "format"
META_STATE_ON = "state_on"
META_STATE_OFF = "state_off"
META_TEXT_ON = "text_on"
META_TEXT_OFF = "text_off"
