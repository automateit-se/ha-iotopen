"""Maps IoT Open functions to Home Assistant entity descriptions.

Pure Python (no Home Assistant imports) so the rules are easy to test and to reuse.

Rules, in priority order:

1. Overrides in function metadata:
   ``ha.platform`` (sensor | binary_sensor | switch | none), ``ha.device_class``, ``ha.state_class``,
   ``ha.unit``, ``ha.hidden`` (true/false).
2. ``topic_write`` with ``state_on``/``state_off`` -> switch.
   ``topic_write`` without them -> number (not in the MVP, so falls back to a read-only sensor).
3. ``state_on``/``state_off`` and no ``topic_write`` -> binary_sensor.
4. Everything else -> sensor, with device_class/state_class derived from ``type`` and ``unit``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .pyiotopen import Device, Function

PLATFORM_SENSOR = "sensor"
PLATFORM_BINARY_SENSOR = "binary_sensor"
PLATFORM_SWITCH = "switch"
PLATFORM_LIGHT = "light"
SUPPORTED_PLATFORMS = (PLATFORM_SENSOR, PLATFORM_BINARY_SENSOR, PLATFORM_SWITCH, PLATFORM_LIGHT)

# IoT Open standard function types with their own two-state keys (IoT Open Academy, "Function
# metadata"): (on key, off key, on text key, off text key, HA binary_sensor device class)
TWO_STATE_TYPES: dict[str, tuple[str, str, str, str, str | None]] = {
    "door": ("state_open", "state_closed", "text_open", "text_closed", "door"),
    "movement": ("state_movement", "state_nomovement", "text_movement", "text_nomovement", "motion"),
    "boolean_text": ("state_true", "state_false", "text_true", "text_false", None),
}
_DEFAULT_TWO_STATE = ("state_on", "state_off", "text_on", "text_off", None)


@dataclass(slots=True, frozen=True)
class TwoState:
    """The two states of a function, whatever keys its type uses."""

    on: str | None
    off: str | None
    text_on: str | None
    text_off: str | None
    device_class: str | None


def two_state(fn: Function) -> TwoState:
    """``state_open``/``state_closed`` for door, ``state_movement``/... for movement, etc."""
    keys = TWO_STATE_TYPES.get(fn.type.lower(), _DEFAULT_TWO_STATE)

    def get(key: str) -> str | None:
        value = fn.get_meta(key)
        return value.strip() or None if value is not None else None

    return TwoState(get(keys[0]), get(keys[1]), get(keys[2]), get(keys[3]), keys[4])


def is_binary(fn: Function) -> bool:
    """True when both states are defined for the function's type."""
    ts = two_state(fn)
    return ts.on is not None and ts.off is not None


def is_dimmer(fn: Function) -> bool:
    """IoT Open ``dimmer``: level on topic_read/topic_write between min and max."""
    return fn.type.lower() == "dimmer" and fn.topic_write is not None

META_PLATFORM = "ha.platform"
META_DEVICE_CLASS = "ha.device_class"
META_STATE_CLASS = "ha.state_class"
META_UNIT = "ha.unit"
META_HIDDEN = "ha.hidden"

STATE_CLASS_MEASUREMENT = "measurement"
STATE_CLASS_TOTAL_INCREASING = "total_increasing"
STATE_CLASS_TOTAL = "total"
STATE_CLASSES = (STATE_CLASS_MEASUREMENT, STATE_CLASS_TOTAL_INCREASING, STATE_CLASS_TOTAL)

_TRUE = {"1", "true", "yes", "on"}
_DISABLED_PLATFORMS = {"none", "ignore", "off", "disabled"}

# spelling in IoT Open -> Home Assistant unit
_UNIT_ALIASES: dict[str, str] = {
    "c": "°C",
    "°c": "°C",
    "celsius": "°C",
    "degc": "°C",
    "f": "°F",
    "°f": "°F",
    "k": "K",
    "wh": "Wh",
    "kwh": "kWh",
    "mwh": "MWh",
    "w": "W",
    "kw": "kW",
    "v": "V",
    "mv": "mV",
    "a": "A",
    "ma": "mA",
    "%": "%",
    "ppm": "ppm",
    "ppb": "ppb",
    "lux": "lx",
    "lx": "lx",
    "pa": "Pa",
    "hpa": "hPa",
    "kpa": "kPa",
    "mbar": "mbar",
    "bar": "bar",
    "m3": "m³",
    "m³": "m³",
    "l": "L",
    "liter": "L",
    "db": "dB",
    "dbm": "dBm",
    "hz": "Hz",
    "khz": "kHz",
    "s": "s",
    "ms": "ms",
    "min": "min",
    "h": "h",
    "d": "d",
    "m/s": "m/s",
    "km/h": "km/h",
    "mm": "mm",
    "cm": "cm",
    "m": "m",
    "km": "km",
    "ug/m3": "µg/m³",
    "µg/m3": "µg/m³",
    "µg/m³": "µg/m³",
}

# device class -> units Home Assistant accepts for it (subset that matters here)
_DEVICE_CLASS_UNITS: dict[str, set[str]] = {
    "temperature": {"°C", "°F", "K"},
    "humidity": {"%"},
    "moisture": {"%"},
    "battery": {"%"},
    "carbon_dioxide": {"ppm"},
    "energy": {"Wh", "kWh", "MWh"},
    "power": {"W", "kW"},
    "voltage": {"V", "mV"},
    "current": {"A", "mA"},
    "pressure": {"Pa", "hPa", "kPa", "mbar", "bar"},
    "illuminance": {"lx"},
    "pm25": {"µg/m³"},
    "pm10": {"µg/m³"},
    "signal_strength": {"dB", "dBm"},
    "frequency": {"Hz", "kHz"},
    "speed": {"m/s", "km/h"},
    "duration": {"s", "ms", "min", "h", "d"},
    "volume": {"L", "m³"},
    "water": {"L", "m³"},
    "gas": {"m³"},
    "distance": {"mm", "cm", "m", "km"},
}

# device class implied by the unit alone
_UNIT_DEVICE_CLASS: dict[str, str] = {
    "°C": "temperature",
    "°F": "temperature",
    "K": "temperature",
    "Wh": "energy",
    "kWh": "energy",
    "MWh": "energy",
    "W": "power",
    "kW": "power",
    "V": "voltage",
    "mV": "voltage",
    "A": "current",
    "mA": "current",
    "hPa": "pressure",
    "kPa": "pressure",
    "mbar": "pressure",
    "bar": "pressure",
    "lx": "illuminance",
    "dBm": "signal_strength",
    "Hz": "frequency",
    "kHz": "frequency",
    "km/h": "speed",
}

# keyword in the function type -> sensor device class (checked in order)
_TYPE_SENSOR_CLASS: tuple[tuple[str, str], ...] = (
    ("temperature", "temperature"),
    ("temp", "temperature"),
    ("humidity", "humidity"),
    ("co2", "carbon_dioxide"),
    ("battery", "battery"),
    ("energy", "energy"),
    ("power", "power"),
    ("voltage", "voltage"),
    ("current", "current"),
    ("pressure", "pressure"),
    ("illuminance", "illuminance"),
    ("luminance", "illuminance"),
    ("lux", "illuminance"),
    ("pm25", "pm25"),
    ("pm2.5", "pm25"),
    ("pm10", "pm10"),
    ("rssi", "signal_strength"),
    ("signal", "signal_strength"),
    ("velocity", "speed"),
    ("speed", "speed"),
    ("water", "water"),
    ("gas", "gas"),
    ("volume", "volume"),
    ("distance", "distance"),
    ("moisture", "moisture"),
)

# device classes whose value is a meter reading
_METER_CLASSES = {"energy", "water", "gas", "volume"}
_METER_TYPE_WORDS = ("meter", "total", "consumption", "counter", "accumulated", "energy")

# keyword in the function type -> binary_sensor device class (checked in order)
_TYPE_BINARY_CLASS: tuple[tuple[str, str], ...] = (
    ("door", "door"),
    ("window", "window"),
    ("garage", "garage_door"),
    ("motion", "motion"),
    ("pir", "motion"),
    ("occupancy", "occupancy"),
    ("presence", "presence"),
    ("smoke", "smoke"),
    ("leak", "moisture"),
    ("moisture", "moisture"),
    ("water", "moisture"),
    ("fault", "problem"),
    ("alarm", "problem"),
    ("error", "problem"),
    ("problem", "problem"),
    ("connect", "connectivity"),
    ("online", "connectivity"),
    ("vibration", "vibration"),
    ("lock", "lock"),
    ("opening", "opening"),
    ("running", "running"),
    ("moving", "moving"),
    ("battery", "battery"),
    ("tamper", "tamper"),
)

_FORMAT_PRECISION = re.compile(r"%[-+ 0#]*\d*\.(\d+)[fFeEgG]")
_FORMAT_INT = re.compile(r"%[-+ 0#]*\d*[di]")


@dataclass(slots=True, frozen=True)
class EntityPlan:
    """How one function is represented in Home Assistant."""

    platform: str
    device_class: str | None = None
    state_class: str | None = None
    unit: str | None = None
    precision: int | None = None
    hidden: bool = False

    @property
    def numeric(self) -> bool:
        """True when Home Assistant expects a numeric state."""
        return self.state_class is not None or self.unit is not None or (
            self.device_class is not None and self.device_class in _DEVICE_CLASS_UNITS
        )


def normalize_unit(unit: str | None) -> str | None:
    """Map IoT Open unit spellings to Home Assistant units."""
    if unit is None:
        return None
    unit = unit.strip()
    if not unit:
        return None
    return _UNIT_ALIASES.get(unit.lower(), unit)


def _meta_bool(value: str | None) -> bool:
    return value is not None and value.strip().lower() in _TRUE


def _meta_str(fn: Function, key: str) -> str | None:
    value = fn.get_meta(key)
    if value is None:
        return None
    value = value.strip()
    return value or None


def precision_from_format(fmt: str | None) -> int | None:
    """Decimals from a printf format: ``%.1f`` -> 1, ``%d`` -> 0."""
    if not fmt:
        return None
    match = _FORMAT_PRECISION.search(fmt)
    if match:
        return int(match.group(1))
    if _FORMAT_INT.search(fmt):
        return 0
    return None


def _default_platform(fn: Function) -> str:
    if is_dimmer(fn):
        return PLATFORM_LIGHT
    if fn.is_controllable:
        # number/select come later; without on/off states it stays a read-only sensor
        return PLATFORM_SWITCH if is_binary(fn) else PLATFORM_SENSOR
    if is_binary(fn):
        return PLATFORM_BINARY_SENSOR
    return PLATFORM_SENSOR


def _keyword_class(type_: str, table: tuple[tuple[str, str], ...]) -> str | None:
    lowered = type_.lower()
    for keyword, device_class in table:
        if keyword in lowered:
            return device_class
    return None


def _sensor_classes(fn: Function, unit: str | None) -> tuple[str | None, str | None]:
    """Derive (device_class, state_class) for a sensor."""
    device_class = _keyword_class(fn.type, _TYPE_SENSOR_CLASS)
    if device_class is not None and (unit is None or unit not in _DEVICE_CLASS_UNITS[device_class]):
        device_class = None  # type says e.g. "temperature" but the unit does not fit -> no class
    if device_class is None and unit is not None:
        device_class = _UNIT_DEVICE_CLASS.get(unit)
        if unit == "%" and "humid" in fn.type.lower():
            device_class = "humidity"
        if unit == "ppm" and "co2" in fn.type.lower():
            device_class = "carbon_dioxide"
    state_class: str | None = None
    if device_class in _METER_CLASSES and (
        device_class == "energy" or any(w in fn.type.lower() for w in _METER_TYPE_WORDS)
    ):
        state_class = STATE_CLASS_TOTAL_INCREASING
    elif device_class is not None or unit is not None:
        state_class = STATE_CLASS_MEASUREMENT
    if device_class in _METER_CLASSES and state_class == STATE_CLASS_MEASUREMENT:
        # HA does not allow "measurement" for energy/water/gas; a non-meter reading gets no class
        device_class = None
    return device_class, state_class


def describe(fn: Function) -> EntityPlan | None:
    """Return the entity plan for a function, or ``None`` if it should not become an entity."""
    if fn.topic_read is None and fn.topic_write is None:
        return None
    override = (_meta_str(fn, META_PLATFORM) or "").lower()
    if override in _DISABLED_PLATFORMS:
        return None
    platform = override if override in SUPPORTED_PLATFORMS else _default_platform(fn)
    if platform in (PLATFORM_SWITCH, PLATFORM_LIGHT) and not fn.is_controllable:
        platform = PLATFORM_BINARY_SENSOR if is_binary(fn) else PLATFORM_SENSOR
    if platform == PLATFORM_BINARY_SENSOR and two_state(fn).on is None:
        platform = PLATFORM_SENSOR
    if platform == PLATFORM_LIGHT:
        return EntityPlan(platform=platform, hidden=_meta_bool(_meta_str(fn, META_HIDDEN)))

    hidden = _meta_bool(_meta_str(fn, META_HIDDEN))
    dc_override = _meta_str(fn, META_DEVICE_CLASS)
    sc_override = _meta_str(fn, META_STATE_CLASS)

    if platform == PLATFORM_BINARY_SENSOR:
        return EntityPlan(
            platform=platform,
            device_class=dc_override
            or two_state(fn).device_class
            or _keyword_class(fn.type, _TYPE_BINARY_CLASS),
            hidden=hidden,
        )
    if platform == PLATFORM_SWITCH:
        device_class = dc_override
        if device_class is None and any(w in fn.type.lower() for w in ("outlet", "plug", "socket")):
            device_class = "outlet"
        return EntityPlan(platform=platform, device_class=device_class, hidden=hidden)

    unit = normalize_unit(_meta_str(fn, META_UNIT) or fn.unit)
    if is_binary(fn) and _meta_str(fn, META_UNIT) is None:
        unit = None  # an on/off function shown as a sensor has a text state
    device_class, state_class = _sensor_classes(fn, unit)
    if dc_override:
        device_class = None if dc_override.lower() in _DISABLED_PLATFORMS else dc_override
    if sc_override:
        state_class = None if sc_override.lower() in _DISABLED_PLATFORMS else sc_override
        if state_class is not None and state_class not in STATE_CLASSES:
            state_class = None
    precision = precision_from_format(fn.format)
    if not (device_class or state_class or unit):
        precision = None
    return EntityPlan(
        platform=PLATFORM_SENSOR,
        device_class=device_class,
        state_class=state_class,
        unit=unit,
        precision=precision,
        hidden=hidden,
    )


META_DESTINATION = "ha.destination"
_DEST_ALL = {"*", "all", "alla"}
_DEST_NONE = {"none", "-", "ingen"}


def destination_matches(value: str | None, names: set[str]) -> bool | None:
    """Evaluate one ``ha.destination`` value; ``None`` when the value is not set.

    ``value`` is a comma separated list of instance names or ids, ``*`` for every instance or
    ``none`` to exclude explicitly. ``names`` are this instance's name and id (lower case).
    """
    if value is None or not value.strip():
        return None
    tokens = {t.strip().lower() for t in value.split(",") if t.strip()}
    if tokens & _DEST_NONE and not tokens - _DEST_NONE:
        return False
    if tokens & _DEST_ALL:
        return True
    return bool(tokens & names)


def wanted_for_import(
    fn: Function, device: Device | None, names: set[str], import_all: bool
) -> bool:
    """Should this function become an entity in this HA instance?

    Function ``ha.destination`` wins over the device's; without either, ``import_all`` decides.
    """
    own = destination_matches(fn.get_meta(META_DESTINATION), names)
    if own is not None:
        return own
    if device is not None:
        inherited = destination_matches(device.get_meta(META_DESTINATION), names)
        if inherited is not None:
            return inherited
    return import_all


def _equal(value: Any, reference: str) -> bool:
    if str(value).strip() == reference:
        return True
    try:
        return float(value) == float(reference)
    except (TypeError, ValueError):
        return False


def binary_state(fn: Function, value: Any) -> bool | None:
    """Interpret a value using the type's on/off (open/closed, movement/nomovement ...) states."""
    if value is None:
        return None
    ts = two_state(fn)
    if ts.on is not None and _equal(value, ts.on):
        return True
    if ts.off is not None and _equal(value, ts.off):
        return False
    if ts.off is None and ts.on is not None:
        return False  # only "on" defined: anything else is off
    return None


def sensor_value(fn: Function, plan: EntityPlan, value: Any) -> Any:
    """Convert a raw value to a sensor state (number for numeric sensors, else text)."""
    if value is None:
        return None
    if plan.numeric:
        if isinstance(value, bool):
            return int(value)
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if plan.precision == 0 and number.is_integer():
            return int(number)
        return number
    ts = two_state(fn)
    if ts.on is not None and _equal(value, ts.on):
        return ts.text_on or ts.on
    if ts.off is not None and _equal(value, ts.off):
        return ts.text_off or ts.off
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, (dict, list)):
        return None
    text = str(value)
    return text[:255]


def command_value(raw: str) -> Any:
    """Value to publish for ``state_on``/``state_off``: numbers stay numbers."""
    try:
        number = float(raw)
    except ValueError:
        return raw
    return int(number) if number.is_integer() else number
