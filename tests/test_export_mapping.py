"""Tests for HA -> IoT Open mirroring rules (no Home Assistant needed)."""

from __future__ import annotations

from custom_components.iotopen.export_mapping import (
    command_service,
    entity_for_topic,
    export_value,
    function_spec,
    managed_keys_removed,
    meta_differs,
    topic_for,
)


def test_topics_roundtrip():
    assert topic_for("sensor.Living-Room") == "obj/ha/sensor/living_room"
    assert entity_for_topic("obj/ha/switch/kitchen") == "switch.kitchen"
    assert entity_for_topic("set/obj/ha/switch/kitchen") == "switch.kitchen"
    assert entity_for_topic("obj/demo/1/level") is None


def test_switch_spec():
    spec = function_spec("switch.kitchen", "Kitchen", {}, "src", iot_device_id=7)
    assert spec.type == "switch"
    assert spec.meta["topic_read"] == "obj/ha/switch/kitchen"
    assert spec.meta["topic_write"] == "set/obj/ha/switch/kitchen"
    assert spec.meta["state_on"] == "1" and spec.meta["state_off"] == "0"
    assert spec.meta["device_id"] == "7" and spec.meta["ha.source"] == "src"
    ro = function_spec("switch.kitchen", "Kitchen", {}, "src", allow_control=False)
    assert "topic_write" not in ro.meta


def test_sensor_and_binary_spec():
    s = function_spec("sensor.t", "T", {"device_class": "temperature", "unit_of_measurement": "°C"}, "x")
    assert s.type == "temperature" and s.meta["unit"] == "°C" and "topic_write" not in s.meta
    b = function_spec("binary_sensor.door", "Door", {"device_class": "door"}, "x")
    assert b.type == "door" and b.meta["state_open"] == "1" and b.meta["state_closed"] == "0"
    assert "state_on" not in b.meta and "text_off" not in b.meta and "topic_write" not in b.meta
    c = function_spec("cover.blind", "Blind", {}, "x")
    assert c.type == "cover" and c.meta["topic_write"] == "set/obj/ha/cover/blind"


def test_export_value():
    assert export_value("switch", "on") == (1, "")
    assert export_value("binary_sensor", "off") == (0, "")
    assert export_value("sensor", "21.5") == (21.5, "")
    assert export_value("sensor", "3") == (3, "")
    assert export_value("sensor", "unavailable") is None
    assert export_value("lock", "locked") == (1, "locked")
    assert export_value("select", "Eco") == ("Eco", "")
    assert export_value("sensor", "nan") is None


def test_command_service():
    assert command_service("switch", 1) == ("switch", "turn_on", {})
    assert command_service("light", "0") == ("light", "turn_off", {})
    assert command_service("input_number", "12.5") == ("input_number", "set_value", {"value": 12.5})
    assert command_service("cover", 1) == ("cover", "open_cover", {})
    assert command_service("cover", 40) == ("cover", "set_cover_position", {"position": 40})
    assert command_service("lock", 0) == ("lock", "unlock", {})
    assert command_service("button", 1) == ("button", "press", {})
    assert command_service("sensor", 1) is None
    assert command_service("switch", "banana") is None


def test_meta_diff_helpers():
    assert not meta_differs({"a": "1", "user": "x"}, {"a": "1"})
    assert meta_differs({"a": "1"}, {"a": "2"})
    assert managed_keys_removed({"topic_write": "x", "user": "y"}, {"topic_read": "z"}) == ["topic_write"]


def test_excluded_domains():
    from custom_components.iotopen.export_mapping import is_exportable

    assert not is_exportable("event.scene_id")
    assert not is_exportable("update.firmware")
    assert is_exportable("light.facade")


def test_source_name_and_id():
    from custom_components.iotopen.export_mapping import is_own

    spec = function_spec("switch.a", "A", {}, "home", source_id="abc123")
    assert spec.meta["ha.source"] == "home" and spec.meta["ha.source_id"] == "abc123"
    assert is_own(spec.meta.get, "abc123")
    assert is_own({"ha.source": "abc123"}.get, "abc123")  # objects created before ha.source_id
    assert not is_own({"ha.source": "home"}.get, "abc123")  # same name, other instance
    assert not is_own(spec.meta.get, None)



def test_standard_types():
    from custom_components.iotopen.export_mapping import (
        dimmer_command,
        dimmer_level,
        is_onoff_topic,
    )

    m = function_spec("binary_sensor.pir", "PIR", {"device_class": "motion"}, "x")
    assert m.type == "movement" and m.meta["state_movement"] == "1" and m.meta["state_nomovement"] == "0"
    g = function_spec("binary_sensor.leak", "Leak", {"device_class": "moisture"}, "x")
    assert g.type == "boolean_text" and g.meta["state_true"] == "1" and g.meta["ha.device_class"] == "moisture"
    sw = function_spec("switch.plug", "Plug", {}, "x")
    assert sw.type == "switch" and sw.meta["topic_write"] == "set/obj/ha/switch/plug"
    onoff_light = function_spec("light.hall", "Hall", {"supported_color_modes": ["onoff"]}, "x")
    assert onoff_light.type == "switch"
    d = function_spec("light.facade", "Facade", {"supported_color_modes": ["brightness"]}, "x")
    assert d.type == "dimmer" and d.meta["min"] == "0" and d.meta["max"] == "100"
    assert d.meta["topic_read_onoff"] == "obj/ha/light/facade/onoff"
    assert d.meta["topic_write_onoff"] == "set/obj/ha/light/facade/onoff"
    assert d.meta["topic_write"] == "set/obj/ha/light/facade"
    assert entity_for_topic("set/obj/ha/light/facade/onoff") == "light.facade"
    assert is_onoff_topic("set/obj/ha/light/facade/onoff") and not is_onoff_topic("set/obj/ha/light/facade")
    assert dimmer_level("on", {"brightness": 255}) == 100 and dimmer_level("off", {}) == 0
    assert dimmer_level("on", {}) == 100 and dimmer_level("unavailable", {}) is None
    assert dimmer_command(40) == ("light", "turn_on", {"brightness_pct": 40})
    assert dimmer_command("0") == ("light", "turn_off", {})


def test_manual_edits_in_iot_open_are_kept():
    """IoT Open is master for name/visible/unit/text_*: HA fills them in only when missing."""
    from custom_components.iotopen.export_mapping import desired_device_meta, desired_update

    spec = function_spec("binary_sensor.entre", "Front door", {}, "x")
    assert spec.type == "boolean_text" and spec.meta["text_false"] == "Off"
    edited = {**spec.meta, "text_false": "Closed", "text_true": "Open", "name": "Front", "visible": "false"}
    type_, wanted, removed = desired_update(edited, "boolean_text", spec)
    assert removed == [] and type_ == "boolean_text"
    assert "text_false" not in wanted and "text_true" not in wanted
    assert "name" not in wanted and "visible" not in wanted
    assert not meta_differs(edited, wanted)  # nothing to write -> no update, no evt loop
    # structural keys still follow HA
    broken = {**edited, "topic_read": "obj/other", "state_false": "9"}
    _, wanted, _ = desired_update(broken, "boolean_text", spec)
    assert wanted["topic_read"] == "obj/ha/binary_sensor/entre" and wanted["state_false"] == "0"
    # a deleted text key is filled in again from HA
    missing = {k: v for k, v in edited.items() if k != "text_false"}
    _, wanted, _ = desired_update(missing, "boolean_text", spec)
    assert wanted["text_false"] == "Off" and "text_true" not in wanted
    # devices: a name set in IoT Open stays
    assert "name" not in desired_device_meta({"name": "Mitt namn"}, {"name": "HA", "ha.device_id": "d"})
    assert desired_device_meta({}, {"name": "HA"})["name"] == "HA"


def test_type_change_replaces_texts():
    """HA suggests a new type (e.g. new device_class): HA's type and texts are written."""
    from custom_components.iotopen.export_mapping import desired_update

    old = {"state_true": "1", "state_false": "0", "text_true": "Open", "text_false": "Closed", "name": "X", "ha.type": "boolean_text"}
    spec = function_spec("binary_sensor.door", "Door", {"device_class": "door"}, "x")
    type_, wanted, removed = desired_update(old, "boolean_text", spec)
    assert type_ == "door" and wanted["ha.type"] == "door"
    assert set(removed) == {"state_true", "state_false", "text_true", "text_false"}
    assert wanted["text_closed"] == "Closed" and wanted["state_open"] == "1"
    assert wanted["name"] == "Door"  # type changed: everything follows HA again


def test_type_changed_in_iot_open_is_kept():
    """A person changed boolean_text -> door in IoT Open: type, state_* and text_* stay."""
    from custom_components.iotopen.export_mapping import desired_update

    spec = function_spec("binary_sensor.entre", "Front door", {}, "x")
    assert spec.type == "boolean_text" and spec.meta["ha.type"] == "boolean_text"
    edited = {**spec.meta, "text_true": "Open", "text_false": "Closed"}
    type_, wanted, removed = desired_update(edited, "door", spec)
    assert type_ == "door" and removed == []
    assert not meta_differs(edited, wanted)  # nothing to write -> no update loop
    # the person also switched to door's own keys: HA neither re-adds nor removes state/text keys
    door = {k: v for k, v in edited.items() if not k.startswith(("state_", "text_"))}
    door.update(state_open="1", state_closed="0", text_open="Öppen", text_closed="Stängd")
    type_, wanted, removed = desired_update(door, "door", spec)
    assert type_ == "door" and removed == [] and not any(k.startswith(("state_", "text_")) for k in wanted)
    # structural keys still follow HA
    _, wanted, _ = desired_update({**door, "topic_read": "obj/x"}, "door", spec)
    assert wanted["topic_read"] == "obj/ha/binary_sensor/entre"
    # objects from before ha.type existed: a different type counts as a manual change
    legacy = {k: v for k, v in edited.items() if k != "ha.type"}
    type_, wanted, _ = desired_update(legacy, "door", spec)
    assert type_ == "door" and wanted["ha.type"] == "boolean_text"

def test_old_keys_removed_when_type_changes():
    old = {"state_on": "1", "state_off": "0", "text_on": "On", "text_off": "Off", "user": "x"}
    new = function_spec("binary_sensor.door", "Door", {"device_class": "door"}, "x").meta
    assert set(managed_keys_removed(old, new)) == {"state_on", "state_off", "text_on", "text_off"}
