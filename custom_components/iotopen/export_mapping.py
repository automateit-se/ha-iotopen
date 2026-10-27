"""Rules for mirroring Home Assistant entities into IoT Open (pure Python, no HA imports).

IoT Open conventions:
* ``topic_read``  = ``obj/ha/<domain>/<object_id>``      (status, logged by IoT Open)
* ``topic_write`` = ``set/obj/ha/<domain>/<object_id>``  (control, only for controllable entities)
* functions/devices created by this integration carry ``ha.source`` = the instance name and
  ``ha.source_id`` = the stable HA instance id, so the
  import side can ignore them and the export side knows which objects it owns.
* most entities become one function; a ``media_player`` becomes several (playback, volume, mute, status,
  power) on topics ``obj/ha/media_player/<object_id>/<part>``, told apart by ``ha.part``; a ``climate``
  entity likewise (current temperature, target temperature, HVAC mode, HVAC action) on
  ``obj/ha/climate/<object_id>/<part>``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

META_SOURCE = "ha.source"  # readable instance name, e.g. "home" (follows renames)
META_SOURCE_ID = "ha.source_id"  # stable HA instance id; decides ownership
META_ENTITY_ID = "ha.entity_id"
META_HA_DEVICE_ID = "ha.device_id"
META_DEVICE_CLASS = "ha.device_class"  # keeps HA's class for types like boolean_text
META_HA_TYPE = "ha.type"  # the type HA suggested; a different function type means it was changed in IoT Open
META_PART = "ha.part"  # which function of a multi-function entity (media_player, climate); absent otherwise
META_HVAC_MODES = "ha.hvac_modes"  # the HVAC modes a thermostat offers, comma separated (on its mode function)

DOOR_CLASSES = {"door", "window", "garage_door", "opening"}
MOVEMENT_CLASSES = {"motion", "occupancy", "presence", "moving"}
ONOFF_SUFFIX = "onoff"


def is_dimmable(attributes: dict[str, Any]) -> bool:
    """A light that supports more than on/off."""
    modes = attributes.get("supported_color_modes") or []
    return any(str(m) != "onoff" for m in modes) or attributes.get("brightness") is not None

TOPIC_PREFIX = "obj/ha"
WRITE_PREFIX = "set/"

UNAVAILABLE_STATES = {"unavailable", "unknown", "none", ""}

# domains whose state is not a meaningful value (timestamps, firmware info ...)
EXCLUDED_DOMAINS = {
    "event", "update", "image", "camera", "tts", "stt", "conversation", "notify",
    "wake_word", "assist_satellite", "ai_task", "todo", "calendar", "remote",
}

MEDIA_PLAYER = "media_player"
PART_PLAYING = "playing"
PART_VOLUME = "volume"
PART_MUTE = "mute"
PART_STATE = "state"
PART_POWER = "power"
MEDIA_PARTS = (PART_PLAYING, PART_VOLUME, PART_MUTE, PART_STATE, PART_POWER)
CLIMATE = "climate"
PART_CURRENT = "current"
PART_TARGET = "target"
PART_MODE = "mode"
PART_ACTION = "action"
CLIMATE_PARTS = (PART_CURRENT, PART_TARGET, PART_MODE, PART_ACTION)
CLIMATE_TARGET_TEMPERATURE = 1  # ClimateEntityFeature.TARGET_TEMPERATURE
TOPIC_SUFFIXES = {ONOFF_SUFFIX, *MEDIA_PARTS, *CLIMATE_PARTS}

# MediaPlayerEntityFeature bits (homeassistant.components.media_player)
MP_PAUSE = 1
MP_VOLUME_SET = 4
MP_VOLUME_MUTE = 8
MP_TURN_ON = 128
MP_TURN_OFF = 256
MP_PLAY = 16384
_PART_FEATURES = {
    PART_PLAYING: MP_PLAY | MP_PAUSE,
    PART_VOLUME: MP_VOLUME_SET,
    PART_MUTE: MP_VOLUME_MUTE,
    PART_POWER: MP_TURN_ON | MP_TURN_OFF,
}
MEDIA_OFF_STATES = {"off", "standby"}
MSG_MAX = 200  # track text in msg

# function name = "<entity name> <part name>"; IoT Open is master for the name once it is created
_PART_NAMES = {
    "en": {
        PART_PLAYING: "Playback", PART_VOLUME: "Volume", PART_MUTE: "Mute", PART_STATE: "Status", PART_POWER: "Power"
    },
    "sv": {
        PART_PLAYING: "Uppspelning", PART_VOLUME: "Volym", PART_MUTE: "Mute", PART_STATE: "Status", PART_POWER: "På/av"
    },
}
_CLIMATE_NAMES = {
    "en": {PART_CURRENT: "Temperature", PART_TARGET: "Target temperature", PART_MODE: "Mode", PART_ACTION: "Action"},
    "sv": {PART_CURRENT: "Temperatur", PART_TARGET: "Börvärde", PART_MODE: "Läge", PART_ACTION: "Drift"},
}


def is_exportable(entity_id: str) -> bool:
    """Whether an entity's domain can be mirrored as an IoT Open function."""
    return entity_id.split(".", 1)[0] not in EXCLUDED_DOMAINS

# on/off style domains: value 1/0, controllable with turn_on/turn_off
ON_OFF_CONTROL = {"switch", "light", "input_boolean", "fan", "automation", "siren", "humidifier"}
ON_OFF_READ_ONLY = {"binary_sensor"}
NUMBER_CONTROL = {"number", "input_number"}

# state -> value for other two-state domains
_TWO_STATE: dict[str, tuple[dict[str, int], str, str]] = {
    # domain: (state->value, text_on, text_off)
    "lock": ({"locked": 1, "unlocked": 0, "locking": 1, "unlocking": 0, "jammed": 0, "open": 0}, "Locked", "Unlocked"),
    "cover": ({"open": 1, "opening": 1, "closed": 0, "closing": 0}, "Open", "Closed"),
    "valve": ({"open": 1, "opening": 1, "closed": 0, "closing": 0}, "Open", "Closed"),
    "person": ({"home": 1, "not_home": 0}, "Home", "Away"),
}
TWO_STATE_CONTROL = {"lock", "cover", "valve"}

_SAFE = re.compile(r"[^a-z0-9_]+")


@dataclass(slots=True, frozen=True)
class FunctionSpec:
    """Desired IoT Open function for one HA entity."""

    entity_id: str
    type: str
    meta: dict[str, str] = field(default_factory=dict)

    @property
    def topic_read(self) -> str:
        return self.meta["topic_read"]

    @property
    def topic_write(self) -> str | None:
        return self.meta.get("topic_write")

    @property
    def part(self) -> str:
        """``""`` for one-function entities, else e.g. ``volume``."""
        return self.meta.get(META_PART, "")

    @property
    def key(self) -> str:
        """Unique per function: ``media_player.x/volume`` or just the entity id."""
        return f"{self.entity_id}/{self.part}" if self.part else self.entity_id


def topic_for(entity_id: str) -> str:
    """``sensor.living_room`` -> ``obj/ha/sensor/living_room``."""
    domain, _, object_id = entity_id.partition(".")
    return f"{TOPIC_PREFIX}/{_SAFE.sub('_', domain.lower())}/{_SAFE.sub('_', object_id.lower())}"


def _split_topic(topic: str) -> tuple[str, str] | None:
    """``set/obj/ha/media_player/x/volume`` -> (``media_player.x``, ``volume``); suffix ``""`` if none."""
    if topic.startswith(WRITE_PREFIX):
        topic = topic[len(WRITE_PREFIX) :]
    parts = topic.rstrip("/").split("/")
    suffix = ""
    if len(parts) == 5 and parts[4] in TOPIC_SUFFIXES:
        suffix = parts.pop()
    if len(parts) != 4 or f"{parts[0]}/{parts[1]}" != TOPIC_PREFIX:
        return None
    return f"{parts[2]}.{parts[3]}", suffix


def entity_for_topic(topic: str) -> str | None:
    """Inverse of :func:`topic_for` (``obj/ha/sensor/x`` or ``set/obj/ha/sensor/x`` -> ``sensor.x``).

    Suffixed topics (``.../onoff`` of a dimmer, ``.../volume`` etc. of a media player) map to the
    same entity; :func:`topic_suffix` tells them apart.
    """
    split = _split_topic(topic)
    return split[0] if split else None


def topic_suffix(topic: str) -> str:
    """``set/obj/ha/media_player/x/volume`` -> ``volume``; ``""`` for unsuffixed topics."""
    split = _split_topic(topic)
    return split[1] if split else ""


def is_onoff_topic(topic: str) -> bool:
    """``set/obj/ha/light/x/onoff``."""
    return topic_suffix(topic) == ONOFF_SUFFIX


def dimmer_level(state: str | None, attributes: dict[str, Any]) -> int | None:
    """Brightness 0-100 % for a dimmer (0 when off)."""
    if state is None or str(state).lower() in UNAVAILABLE_STATES:
        return None
    if state == "off":
        return 0
    brightness = attributes.get("brightness")
    if brightness is None:
        return 100
    return max(0, min(100, round(float(brightness) / 255 * 100)))


def is_controllable(domain: str) -> bool:
    """Whether IoT Open may control entities of this domain."""
    return domain in ON_OFF_CONTROL or domain in NUMBER_CONTROL or domain in TWO_STATE_CONTROL or domain in (
        "button",
        "input_button",
        "scene",
        "script",
    )


def function_spec(
    entity_id: str,
    name: str,
    attributes: dict[str, Any],
    source: str,
    *,
    source_id: str | None = None,
    allow_control: bool = True,
    iot_device_id: int | None = None,
) -> FunctionSpec:
    """Build the IoT Open function for an entity."""
    domain = entity_id.split(".", 1)[0]
    device_class = attributes.get("device_class")
    topic = topic_for(entity_id)
    meta: dict[str, str] = {
        "name": name,
        "topic_read": topic,
        META_SOURCE: source,
        META_ENTITY_ID: entity_id,
    }
    if source_id:
        meta[META_SOURCE_ID] = source_id
    if iot_device_id is not None:
        meta["device_id"] = str(iot_device_id)

    type_ = str(device_class or domain)
    meta["visible"] = "true"
    if device_class:
        meta[META_DEVICE_CLASS] = str(device_class)
    if domain == "binary_sensor":
        # IoT Open standard types: door, movement, boolean_text (IoT Open Academy, function metadata)
        if device_class in DOOR_CLASSES:
            type_ = "door"
            meta.update(state_open="1", state_closed="0", text_open="Open", text_closed="Closed")
        elif device_class in MOVEMENT_CLASSES:
            type_ = "movement"
            meta.update(
                state_movement="1", state_nomovement="0", text_movement="Movement", text_nomovement="No movement"
            )
        else:
            type_ = "boolean_text"
            meta.update(state_true="1", state_false="0", text_true="On", text_false="Off")
    elif domain == "light" and is_dimmable(attributes):
        type_ = "dimmer"
        meta.update(
            min="0",
            max="100",
            unit="%",
            state_on="1",
            state_off="0",
            topic_read_onoff=f"{topic}/{ONOFF_SUFFIX}",
        )
        if allow_control:
            meta["topic_write_onoff"] = f"{WRITE_PREFIX}{topic}/{ONOFF_SUFFIX}"
    elif domain in ON_OFF_CONTROL:
        type_ = "switch"
        meta.update(state_on="1", state_off="0", text_on="On", text_off="Off")
    elif domain in _TWO_STATE:
        _, text_on, text_off = _TWO_STATE[domain]
        meta.update(state_on="1", state_off="0", text_on=text_on, text_off=text_off)
        type_ = domain if domain != "cover" or not device_class else str(device_class)
    elif domain in NUMBER_CONTROL or domain == "sensor":
        unit = attributes.get("unit_of_measurement")
        if unit:
            meta["unit"] = str(unit)
        if domain in NUMBER_CONTROL:
            type_ = domain
    if allow_control and is_controllable(domain):
        meta["topic_write"] = f"{WRITE_PREFIX}{topic}"
    meta[META_HA_TYPE] = type_
    return FunctionSpec(entity_id=entity_id, type=type_, meta=meta)


def media_parts(attributes: dict[str, Any], keep: Iterable[str] = ()) -> list[str]:
    """Functions a media player gets: the ones its supported_features allow, plus status (always).

    ``keep`` adds parts that must stay although the features do not list them (a player that is off or
    unavailable may report fewer features; its functions must not be deleted and recreated).
    """
    try:
        features = int(attributes.get("supported_features") or 0)
    except (TypeError, ValueError):
        features = 0
    kept = set(keep)
    return [p for p in MEDIA_PARTS if p == PART_STATE or p in kept or features & _PART_FEATURES[p]]


def is_media_active(state: str | None) -> bool:
    """Whether a media player's features can be trusted (not off, standby or unavailable)."""
    return state is not None and str(state).lower() not in UNAVAILABLE_STATES | MEDIA_OFF_STATES


def part_name(part: str, language: str | None = None, domain: str = MEDIA_PLAYER) -> str:
    """Name suffix of a media player or thermostat function, in Swedish or English."""
    table = _CLIMATE_NAMES if domain == CLIMATE else _PART_NAMES
    names = table["sv"] if (language or "").lower().startswith("sv") else table["en"]
    return names[part]


def has_parts(entity_id: str) -> bool:
    """Whether an entity becomes several functions (media player, thermostat)."""
    return entity_id.split(".", 1)[0] in (MEDIA_PLAYER, CLIMATE)


def climate_parts(attributes: dict[str, Any], keep: Iterable[str] = ()) -> list[str]:
    """Functions a thermostat gets: mode (always), the current temperature, the target temperature and the
    HVAC action when it reports them. ``keep`` as for :func:`media_parts` (an unavailable thermostat has no
    attributes)."""
    try:
        features = int(attributes.get("supported_features") or 0)
    except (TypeError, ValueError):
        features = 0
    has = {
        PART_CURRENT: "current_temperature" in attributes,
        PART_TARGET: bool(features & CLIMATE_TARGET_TEMPERATURE) or "temperature" in attributes,
        PART_MODE: True,
        PART_ACTION: "hvac_action" in attributes,
    }
    kept = set(keep)
    return [p for p in CLIMATE_PARTS if has[p] or p in kept]


def entity_parts(entity_id: str, attributes: dict[str, Any], keep: Iterable[str] = ()) -> list[str]:
    """The parts of a multi-function entity (empty for one-function entities)."""
    domain = entity_id.split(".", 1)[0]
    if domain == MEDIA_PLAYER:
        return media_parts(attributes, keep)
    if domain == CLIMATE:
        return climate_parts(attributes, keep)
    return []


def is_parts_active(entity_id: str, state: str | None) -> bool:
    """Whether a multi-function entity's attributes can be trusted to decide its functions."""
    if entity_id.split(".", 1)[0] == MEDIA_PLAYER:
        return is_media_active(state)
    return state is not None and str(state).lower() not in UNAVAILABLE_STATES


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number or number in (float("inf"), float("-inf")) else number


def _fmt(number: float) -> str:
    return str(int(number)) if float(number).is_integer() else str(number)


def _climate_spec(
    entity_id: str,
    name: str,
    part: str,
    attributes: dict[str, Any],
    base: dict[str, str],
    allow_control: bool,
    language: str | None,
) -> FunctionSpec:
    topic = f"{topic_for(entity_id)}/{part}"
    meta = {**base, "name": f"{name} {part_name(part, language, CLIMATE)}", "topic_read": topic, META_PART: part}
    unit = str(attributes.get("temperature_unit") or attributes.get("unit_of_measurement") or "°C")
    if part in (PART_CURRENT, PART_TARGET):
        meta["unit"] = unit
        meta["format"] = f"%.1f {unit}"
    if part == PART_CURRENT:
        type_ = "temperature"
    elif part == PART_TARGET:
        type_ = "setpoint"
        for key, attr in (("min", "min_temp"), ("max", "max_temp"), ("step", "target_temp_step")):
            number = _number(attributes.get(attr))
            if number is not None:
                meta[key] = _fmt(number)
    elif part == PART_MODE:
        type_ = "text"
        modes = [str(m) for m in attributes.get("hvac_modes") or [] if str(m)]
        if modes:
            meta[META_HVAC_MODES] = ",".join(modes)
    else:
        type_ = "text"
    if allow_control and part in (PART_TARGET, PART_MODE):
        meta["topic_write"] = f"{WRITE_PREFIX}{topic}"
    meta[META_HA_TYPE] = type_
    return FunctionSpec(entity_id=entity_id, type=type_, meta=meta)


def climate_value(part: str, state: str | None, attributes: dict[str, Any]) -> tuple[Any, str] | None:
    """Value for one thermostat function. ``None`` when nothing should be published.

    Mode and action also go in ``msg``: IoT Open's status API can return 0 for a text value.
    """
    if state is None or str(state).lower() in UNAVAILABLE_STATES:
        return None
    if part == PART_MODE:
        return str(state)[:255], str(state)[:255]
    if part == PART_ACTION:
        action = attributes.get("hvac_action")
        return None if action is None else (str(action)[:255], str(action)[:255])
    number = _number(attributes.get("current_temperature" if part == PART_CURRENT else "temperature"))
    if number is None:
        return None
    return (int(number) if number.is_integer() else round(number, 2)), ""


def climate_command(part: str, value: Any) -> tuple[str, str, dict[str, Any]] | None:
    """Command on a thermostat function -> (domain, service, data)."""
    if part == PART_TARGET:
        number = _number(value)
        return None if number is None else (CLIMATE, "set_temperature", {"temperature": number})
    if part == PART_MODE:
        mode = str(value).strip().lower()
        return (CLIMATE, "set_hvac_mode", {"hvac_mode": mode}) if mode else None
    return None


def part_value(entity_id: str, part: str, state: str | None, attributes: dict[str, Any]) -> tuple[Any, str] | None:
    """Value of one function of a multi-function entity."""
    if entity_id.split(".", 1)[0] == CLIMATE:
        return climate_value(part, state, attributes)
    return media_value(part, state, attributes)


def part_command(entity_id: str, part: str, value: Any) -> tuple[str, str, dict[str, Any]] | None:
    """Command on one function of a multi-function entity."""
    if entity_id.split(".", 1)[0] == CLIMATE:
        return climate_command(part, value)
    return media_command(part, value)


def _media_spec(
    entity_id: str,
    name: str,
    part: str,
    attributes: dict[str, Any],
    base: dict[str, str],
    allow_control: bool,
    language: str | None,
) -> FunctionSpec:
    topic = f"{topic_for(entity_id)}/{part}"
    meta = {**base, "name": f"{name} {part_name(part, language)}", "topic_read": topic, META_PART: part}
    if part == PART_VOLUME:
        type_ = "dimmer"
        meta.update(min="0", max="100", unit="%")
    elif part == PART_STATE:
        type_ = "text"
    else:
        type_ = "switch"
        text_on, text_off = {
            PART_PLAYING: ("Playing", "Not playing"),
            PART_MUTE: ("Muted", "Unmuted"),
            PART_POWER: ("On", "Off"),
        }[part]
        meta.update(state_on="1", state_off="0", text_on=text_on, text_off=text_off)
    if allow_control and part != PART_STATE:
        meta["topic_write"] = f"{WRITE_PREFIX}{topic}"
    meta[META_HA_TYPE] = type_
    return FunctionSpec(entity_id=entity_id, type=type_, meta=meta)


def function_specs(
    entity_id: str,
    name: str,
    attributes: dict[str, Any],
    source: str,
    *,
    source_id: str | None = None,
    allow_control: bool = True,
    iot_device_id: int | None = None,
    language: str | None = None,
    keep_parts: Iterable[str] = (),
) -> list[FunctionSpec]:
    """All IoT Open functions for an entity: one, or several for a media player or a thermostat (see
    :func:`media_parts`, :func:`climate_parts`)."""
    if not has_parts(entity_id):
        return [
            function_spec(
                entity_id,
                name,
                attributes,
                source,
                source_id=source_id,
                allow_control=allow_control,
                iot_device_id=iot_device_id,
            )
        ]
    base: dict[str, str] = {META_SOURCE: source, META_ENTITY_ID: entity_id, "visible": "true"}
    if source_id:
        base[META_SOURCE_ID] = source_id
    if iot_device_id is not None:
        base["device_id"] = str(iot_device_id)
    if attributes.get("device_class"):
        base[META_DEVICE_CLASS] = str(attributes["device_class"])
    build = _climate_spec if entity_id.split(".", 1)[0] == CLIMATE else _media_spec
    return [
        build(entity_id, name, part, attributes, base, allow_control, language)
        for part in entity_parts(entity_id, attributes, keep_parts)
    ]


def _shorten(text: str, limit: int = MSG_MAX) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def media_track(attributes: dict[str, Any]) -> str:
    """``Artist - Title`` (only the title when there is no artist; the channel for radio without a title)."""

    def attr(key: str) -> str:
        return str(attributes.get(key) or "").strip()

    artist = attr("media_artist") or attr("media_album_artist")
    title = attr("media_title")
    if artist and title:
        text = f"{artist} - {title}"
    else:
        text = title or artist or attr("media_channel")
    return _shorten(text)


def media_value(part: str, state: str | None, attributes: dict[str, Any]) -> tuple[Any, str] | None:
    """Value and msg for one media player function. ``None`` when nothing should be published.

    Only playback carries a msg: the track while playing or paused, empty otherwise.
    """
    if state is None:
        return None
    raw = str(state)
    if part == PART_STATE:
        return _shorten(raw, 255), ""
    if raw.lower() in UNAVAILABLE_STATES:
        return None
    if part == PART_PLAYING:
        return (1 if raw == "playing" else 0), (media_track(attributes) if raw in ("playing", "paused") else "")
    if part == PART_POWER:
        return (0 if raw in MEDIA_OFF_STATES else 1), ""
    if part == PART_MUTE:
        muted = attributes.get("is_volume_muted")
        return None if muted is None else (1 if muted else 0, "")
    if part == PART_VOLUME:
        level = attributes.get("volume_level")
        try:
            return max(0, min(100, round(float(level) * 100))), ""
        except (TypeError, ValueError):
            return None
    return None


def media_command(part: str, value: Any) -> tuple[str, str, dict[str, Any]] | None:
    """Command on a media player function -> (domain, service, data)."""
    if part == PART_VOLUME:
        try:
            level = float(value)
        except (TypeError, ValueError):
            return None
        return MEDIA_PLAYER, "volume_set", {"volume_level": max(0.0, min(100.0, level)) / 100}
    on = _truthy(value)
    if on is None:
        return None
    if part == PART_PLAYING:
        return MEDIA_PLAYER, "media_play" if on else "media_pause", {}
    if part == PART_MUTE:
        return MEDIA_PLAYER, "volume_mute", {"is_volume_muted": on}
    if part == PART_POWER:
        return MEDIA_PLAYER, "turn_on" if on else "turn_off", {}
    return None


def export_value(domain: str, state: str | None) -> tuple[Any, str] | None:
    """HA state -> (IoT Open value, msg). ``None`` when the state should not be published."""
    if state is None:
        return None
    raw = str(state)
    if raw.lower() in UNAVAILABLE_STATES:
        return None
    if domain in ON_OFF_CONTROL or domain in ON_OFF_READ_ONLY:
        if raw == "on":
            return 1, ""
        if raw == "off":
            return 0, ""
        return None
    if domain in _TWO_STATE:
        mapping = _TWO_STATE[domain][0]
        if raw in mapping:
            return mapping[raw], raw
        return None
    try:
        number = float(raw)
    except ValueError:
        return raw[:255], ""  # text state (e.g. enum sensor, select, person zone)
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return (int(number) if number.is_integer() and "." not in raw else number), ""


def _truthy(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if text in ("1", "on", "true", "open", "locked", "yes"):
        return True
    if text in ("0", "off", "false", "closed", "close", "unlocked", "no"):
        return False
    try:
        return float(text) != 0
    except ValueError:
        return None


def dimmer_command(value: Any) -> tuple[str, str, dict[str, Any]] | None:
    """Level 0-100 on a dimmer's topic_write -> light service."""
    try:
        level = float(value)
    except (TypeError, ValueError):
        return None
    if level <= 0:
        return "light", "turn_off", {}
    return "light", "turn_on", {"brightness_pct": max(1, min(100, round(level)))}


def command_service(domain: str, value: Any) -> tuple[str, str, dict[str, Any]] | None:
    """IoT Open command value -> (domain, service, data) for ``hass.services.async_call``."""
    if domain in ON_OFF_CONTROL:
        on = _truthy(value)
        if on is None:
            return None
        return domain, "turn_on" if on else "turn_off", {}
    if domain in NUMBER_CONTROL:
        try:
            return domain, "set_value", {"value": float(value)}
        except (TypeError, ValueError):
            return None
    if domain == "lock":
        on = _truthy(value)
        return None if on is None else ("lock", "lock" if on else "unlock", {})
    if domain in ("cover", "valve"):
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = None
        if number is not None and number not in (0, 1) and 0 <= number <= 100:
            return domain, f"set_{domain}_position", {"position": int(number)}
        on = _truthy(value)
        return None if on is None else (domain, f"open_{domain}" if on else f"close_{domain}", {})
    if domain in ("button", "input_button"):
        return (domain, "press", {}) if _truthy(value) else None
    if domain in ("scene", "script"):
        return (domain, "turn_on", {}) if _truthy(value) else None
    return None


def device_meta(
    name: str,
    manufacturer: str | None,
    model: str | None,
    ha_device_id: str,
    source: str,
    source_id: str | None = None,
) -> dict[str, str]:
    """Meta for an IoT Open device that mirrors a HA device."""
    meta = {"name": name, META_SOURCE: source, META_HA_DEVICE_ID: ha_device_id}
    if source_id:
        meta[META_SOURCE_ID] = source_id
    if manufacturer:
        meta["manufacturer"] = manufacturer
    if model:
        meta["model"] = model
    return meta


def meta_differs(current: dict[str, str], wanted: dict[str, str]) -> bool:
    """True when any key we own has a different value (other keys are left alone)."""
    return any(current.get(k) != v for k, v in wanted.items())


def managed_keys_removed(current: dict[str, str], wanted: dict[str, str]) -> list[str]:
    """Keys we used to set but no longer want (e.g. topic_write after control was disabled)."""
    ours = {
        "topic_write", "unit", "device_id", META_DEVICE_CLASS,
        "state_on", "state_off", "text_on", "text_off",
        "state_open", "state_closed", "text_open", "text_closed",
        "state_movement", "state_nomovement", "text_movement", "text_nomovement",
        "state_true", "state_false", "text_true", "text_false",
        "min", "max", "topic_read_onoff", "topic_write_onoff",
    }
    return [k for k in ours if k in current and k not in wanted]


# Presentation keys a person may change in IoT Open (IoT Open is master for these). HA only fills them in
# when they are missing – or when the function's type changes – so a manual edit such as
# text_false = "Closed", a new name or visible = "false" is not reverted on the next reconcile.
# Delete the key in IoT Open to get HA's value back.
SEED_ONLY_KEYS = {"name", "visible", "unit"}


def is_seed_only(key: str) -> bool:
    """Keys HA sets once (create) and then leaves to IoT Open."""
    return key in SEED_ONLY_KEYS or key.startswith("text_")


def desired_update(
    current_meta: dict[str, str], current_type: str, spec: FunctionSpec
) -> tuple[str, dict[str, str], list[str]]:
    """What to write to an existing function: (type, meta to set, keys to remove).

    IoT Open is master for the type too. ``ha.type`` remembers the type HA suggested:
    * same type as HA wants: structural keys (topics, ha.*, device_id, state_*, min/max …) follow HA,
      seed-only keys (name, visible, unit, text_*) are only filled in when missing;
    * type changed in IoT Open (e.g. boolean_text -> door) while HA still suggests the same type:
      the type is kept, and so are all state_*/text_* keys (they belong to that type now);
    * HA suggests a new type (e.g. the entity got another device_class): HA's type and keys are written
      and the old texts are replaced, since they no longer fit.
    Objects from before ``ha.type`` existed count as changed in IoT Open when the types differ.
    """
    if current_type == spec.type:
        ha_changed = overridden = False
    else:
        previous = current_meta.get(META_HA_TYPE)
        ha_changed = previous is not None and previous != spec.type
        overridden = not ha_changed
    type_ = current_type if overridden else spec.type

    def users(k: str) -> bool:
        """Keys IoT Open decides (HA does not touch them)."""
        return overridden and (k.startswith("state_") or k.startswith("text_"))

    wanted = {
        k: v
        for k, v in spec.meta.items()
        if not users(k) and (ha_changed or not is_seed_only(k) or k not in current_meta)
    }
    removed = [
        k
        for k in managed_keys_removed(current_meta, spec.meta)
        if not users(k) and (ha_changed or not is_seed_only(k))
    ]
    return type_, wanted, removed


def desired_device_meta(current_meta: dict[str, str], meta: dict[str, str]) -> dict[str, str]:
    """Device meta to enforce: everything except a name that has been set (renames in IoT Open stay)."""
    return {k: v for k, v in meta.items() if not (k == "name" and current_meta.get("name"))}


def is_own(get_meta: Any, instance_id: str | None) -> bool:
    """Was this object created by the HA instance ``instance_id``?

    ``ha.source_id`` decides; objects from before it existed carry the id in ``ha.source``.
    """
    if not instance_id:
        return False
    return get_meta(META_SOURCE_ID) == instance_id or get_meta(META_SOURCE) == instance_id
