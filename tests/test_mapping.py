"""Tests for the function -> entity mapping rules (no Home Assistant needed)."""

from __future__ import annotations

import pytest

from custom_components.iotopen.mapping import (
    EntityPlan,
    binary_state,
    command_value,
    describe,
    normalize_unit,
    precision_from_format,
    sensor_value,
)
from custom_components.iotopen.pyiotopen import Function


def fn(type_: str = "x", **meta: str) -> Function:
    meta.setdefault("topic_read", "obj/x")
    return Function.from_dict({"id": 1, "installation_id": 1, "type": type_, "meta": meta})


def test_plain_sensor_is_text():
    plan = describe(fn("level"))
    assert plan == EntityPlan(platform="sensor")
    assert not plan.numeric
    assert sensor_value(fn("level"), plan, 3.0) == "3"


def test_temperature_sensor():
    plan = describe(fn("temperature", unit="C", format="%.1f"))
    assert plan.platform == "sensor"
    assert plan.device_class == "temperature"
    assert plan.state_class == "measurement"
    assert plan.unit == "°C"
    assert plan.precision == 1
    assert sensor_value(fn("temperature"), plan, "21.44") == 21.44


def test_energy_is_total_increasing():
    plan = describe(fn("energy", unit="kWh"))
    assert (plan.device_class, plan.state_class) == ("energy", "total_increasing")


def test_type_unit_mismatch_drops_device_class():
    plan = describe(fn("temperature", unit="%"))
    assert plan.device_class is None
    assert plan.state_class == "measurement"


def test_unit_implies_device_class():
    assert describe(fn("whatever", unit="W")).device_class == "power"
    assert describe(fn("humidity", unit="%")).device_class == "humidity"
    assert describe(fn("velocity", unit="m/s")).device_class == "speed"


def test_water_without_meter_word_is_plain_measurement():
    plan = describe(fn("water", unit="m3"))
    assert plan.device_class is None and plan.state_class == "measurement"
    meter = describe(fn("water_meter", unit="m3"))
    assert (meter.device_class, meter.state_class) == ("water", "total_increasing")


def test_binary_sensor():
    f = fn("fault", state_on="1", state_off="0")
    plan = describe(f)
    assert plan.platform == "binary_sensor"
    assert plan.device_class == "problem"
    assert binary_state(f, 1) is True
    assert binary_state(f, "0") is False
    assert binary_state(f, 1.0) is True
    assert binary_state(f, 7) is None
    assert binary_state(f, None) is None


def test_switch():
    f = fn("switch", topic_write="set/obj/x", state_on="1", state_off="0")
    assert describe(f).platform == "switch"
    assert describe(fn("smart_plug", topic_write="s", state_on="on", state_off="off")).device_class == "outlet"


def test_controllable_without_states_is_sensor_for_now():
    assert describe(fn("setpoint", topic_write="set/x")).platform == "sensor"


def test_overrides():
    f = fn("level", **{"ha.platform": "sensor", "ha.state_class": "measurement", "ha.hidden": "true"})
    plan = describe(f)
    assert plan.state_class == "measurement" and plan.hidden and plan.numeric
    assert describe(fn("x", **{"ha.platform": "none"})) is None
    assert describe(fn("x", unit="C", **{"ha.device_class": "none"})).device_class is None
    assert describe(fn("x", **{"ha.unit": "kWh"})).device_class == "energy"
    # switch override without topic_write cannot control anything -> binary_sensor
    assert describe(fn("x", state_on="1", state_off="0", **{"ha.platform": "switch"})).platform == "binary_sensor"
    # binary_sensor override without state_on -> sensor
    assert describe(fn("x", **{"ha.platform": "binary_sensor"})).platform == "sensor"


def test_no_topics_no_entity():
    f = Function.from_dict({"id": 1, "type": "x", "meta": {"name": "n"}})
    assert describe(f) is None


def test_text_on_off_for_sensor():
    f = fn("mode", state_on="1", state_off="0", text_on="Running", text_off="Stopped", **{"ha.platform": "sensor"})
    plan = describe(f)
    assert plan.unit is None
    assert sensor_value(f, plan, 1) == "Running"
    assert sensor_value(f, plan, "0") == "Stopped"


def test_numeric_sensor_with_garbage_value():
    f = fn("temperature", unit="C")
    assert sensor_value(f, describe(f), "n/a") is None


@pytest.mark.parametrize(
    ("fmt", "expected"),
    [("%.2f", 2), ("%d", 0), ("%i kWh", 0), ("%s", None), (None, None), ("%5.1f", 1)],
)
def test_precision_from_format(fmt, expected):
    assert precision_from_format(fmt) == expected


def test_helpers():
    assert normalize_unit(" kwh ") == "kWh"
    assert normalize_unit("") is None
    assert normalize_unit("widgets") == "widgets"
    assert command_value("1") == 1
    assert command_value("0.5") == 0.5
    assert command_value("on") == "on"


def _dev(**meta):
    from custom_components.iotopen.pyiotopen import Device

    return Device.from_dict({"id": 9, "installation_id": 1, "type": "x", "meta": meta})


def test_destination_rules():
    from custom_components.iotopen.mapping import destination_matches, wanted_for_import

    names = {"home", "0123456789abcdef0123456789abcdef"}
    assert destination_matches(None, names) is None
    assert destination_matches(" ", names) is None
    assert destination_matches("Home", names) is True
    assert destination_matches("cabin, home", names) is True
    assert destination_matches("0123456789abcdef0123456789abcdef", names) is True
    assert destination_matches("*", names) is True
    assert destination_matches("cabin", names) is False
    assert destination_matches("none", names) is False

    plain = fn("temp")
    assert not wanted_for_import(plain, None, names, import_all=False)
    assert wanted_for_import(plain, None, names, import_all=True)
    # inherited from device
    assert wanted_for_import(plain, _dev(**{"ha.destination": "home"}), names, import_all=False)
    # function overrides device
    excluded = fn("temp", **{"ha.destination": "none"})
    assert not wanted_for_import(excluded, _dev(**{"ha.destination": "*"}), names, import_all=True)
    own = fn("temp", **{"ha.destination": "home"})
    assert wanted_for_import(own, _dev(**{"ha.destination": "cabin"}), names, import_all=False)
    # device addressed to another instance is not imported even in "all" mode
    assert not wanted_for_import(plain, _dev(**{"ha.destination": "cabin"}), names, import_all=True)



def test_standard_two_state_types_import():
    from custom_components.iotopen.mapping import binary_state, sensor_value

    door = fn("door", state_open="1", state_closed="0", text_open="Öppen", text_closed="Stängd")
    plan = describe(door)
    assert plan.platform == "binary_sensor" and plan.device_class == "door"
    assert binary_state(door, 1) is True and binary_state(door, "0") is False
    mv = fn("movement", state_movement="1", state_nomovement="0")
    assert describe(mv).device_class == "motion" and binary_state(mv, 1) is True
    bt = fn("boolean_text", state_true="1", state_false="0", **{"ha.device_class": "moisture"})
    assert describe(bt).platform == "binary_sensor" and describe(bt).device_class == "moisture"
    as_sensor = fn("door", state_open="1", state_closed="0", text_open="Öppen", **{"ha.platform": "sensor"})
    assert sensor_value(as_sensor, describe(as_sensor), 1) == "Öppen"
    dim = fn("dimmer", topic_write="set/obj/x", min="0", max="100")
    assert describe(dim).platform == "light"
    assert describe(fn("dimmer", min="0", max="100")).platform == "sensor"  # not controllable
