"""Constants for the IoT Open integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "iotopen"
MANUFACTURER: Final = "IoT Open"
DEFAULT_URL: Final = "https://lynx.iotopen.se"

CONF_AUTH_METHOD: Final = "auth_method"
CONF_INSTALLATIONS: Final = "installations"
CONF_KNOWN_INSTALLATIONS: Final = "known_installations"
CONF_ADD_NEW_INSTALLATIONS: Final = "add_new_installations"
CONF_CODE: Final = "code"
CONF_EXPORT_INSTALLATION: Final = "export_installation"
CONF_EXPORT_LABEL: Final = "export_label"
CONF_EXPORT_CONTROL: Final = "export_control"
# also mirror diagnostic/config entities of labelled devices (off: e.g. a phone's battery sensors stay out)
CONF_EXPORT_CATEGORIZED: Final = "export_categorized"
EXPORT_NONE: Final = "none"
# how mirrored values and commands travel (structure always uses the REST API)
CONF_EXPORT_TRANSPORT: Final = "export_transport"
EXPORT_TRANSPORT_CLOUD: Final = "cloud"  # IoT Open's MQTT over WebSocket
EXPORT_TRANSPORT_LOCAL: Final = "local"  # HA's MQTT integration -> the edge client's broker and bridge
DEFAULT_EXPORT_TRANSPORT: Final = EXPORT_TRANSPORT_CLOUD
DEFAULT_EXPORT_LABEL: Final = "IoT Open"
CONF_INSTANCE_NAME: Final = "instance_name"
CONF_IMPORT_MODE: Final = "import_mode"
IMPORT_TAGGED: Final = "tagged"  # only functions/devices with ha.destination for this instance
IMPORT_ALL: Final = "all"
DEFAULT_IMPORT_MODE: Final = IMPORT_TAGGED
# one config subentry per imported installation (HA groups devices/entities under it)
SUBENTRY_INSTALLATION: Final = "installation"
CONF_INSTALLATION_ID: Final = "installation_id"
CONF_USE_SUBENTRIES: Final = "use_subentries"

AUTH_API_KEY: Final = "api_key"
AUTH_CREDENTIALS: Final = "credentials"

# evt/*/updated arrive in bursts for bulk edits -> reload each installation once
STRUCTURE_DEBOUNCE: Final = 1.0
# how long a lost MQTT connection may last before entities become unavailable
UNAVAILABLE_GRACE: Final = 60.0
# new installations do not announce themselves on an existing client_id -> rare check
INSTALLATION_CHECK_INTERVAL: Final = timedelta(hours=1)


def signal_value(entry_id: str, installation_id: int, topic: str) -> str:
    """Dispatcher signal for a new value on one topic."""
    return f"{DOMAIN}_{entry_id}_value_{installation_id}_{topic}"


def signal_function(entry_id: str, installation_id: int, function_id: int) -> str:
    """Dispatcher signal for changed function metadata."""
    return f"{DOMAIN}_{entry_id}_function_{installation_id}_{function_id}"


def signal_availability(entry_id: str) -> str:
    """Dispatcher signal for connection availability changes."""
    return f"{DOMAIN}_{entry_id}_availability"


def entity_unique_id(installation_id: int, function_id: int) -> str:
    """Stable unique id of a function entity."""
    return f"iotopen_{installation_id}_{function_id}"


def hub_identifier(installation_id: int) -> str:
    """Device registry identifier of an installation hub device."""
    return f"installation_{installation_id}"


def device_identifier(installation_id: int, device_id: int) -> str:
    """Device registry identifier of an IoT Open device."""
    return f"device_{installation_id}_{device_id}"
