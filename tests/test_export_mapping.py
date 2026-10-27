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


# ---------------------------------------------------------------- media_player

SONOS = {
    "supported_features": 16384 | 1 | 4 | 8 | 128 | 256,  # play, pause, volume set/mute, on/off
    "device_class": "speaker",
    "volume_level": 0.27,
    "is_volume_muted": False,
    "media_artist": "Some Artist",
    "media_title": "Some Title",
}


def _specs(attrs, **kwargs):
    from custom_components.iotopen.export_mapping import function_specs

    return {s.part: s for s in function_specs("media_player.living_room", "Living Room", attrs, "src", **kwargs)}


def test_media_player_is_exportable():
    from custom_components.iotopen.export_mapping import is_exportable, is_media_active

    assert is_exportable("media_player.living_room")
    assert is_media_active("playing") and is_media_active("idle")
    assert not is_media_active("off") and not is_media_active("unavailable") and not is_media_active(None)


def test_media_player_specs_follow_supported_features():
    specs = _specs(SONOS, source_id="abc", iot_device_id=7, language="sv")
    assert list(specs) == ["playing", "volume", "mute", "state", "power"]
    play = specs["playing"]
    assert play.type == "switch" and play.key == "media_player.living_room/playing"
    assert play.meta["name"] == "Living Room Uppspelning"
    assert play.meta["topic_read"] == "obj/ha/media_player/living_room/playing"
    assert play.meta["topic_write"] == "set/obj/ha/media_player/living_room/playing"
    assert play.meta["ha.part"] == "playing" and play.meta["ha.entity_id"] == "media_player.living_room"
    assert play.meta["device_id"] == "7" and play.meta["ha.source_id"] == "abc"
    assert play.meta["ha.device_class"] == "speaker" and play.meta["ha.type"] == "switch"
    volume = specs["volume"]
    assert volume.type == "dimmer" and volume.meta["unit"] == "%" and volume.meta["max"] == "100"
    assert volume.meta["name"] == "Living Room Volym"
    assert "topic_read_onoff" not in volume.meta  # no on/off sub-topic for a volume
    status = specs["state"]
    assert status.type == "text" and "topic_write" not in status.meta
    assert specs["power"].meta["name"] == "Living Room På/av"
    assert _specs(SONOS)["playing"].meta["name"] == "Living Room Playback"


def test_media_player_specs_minimal_and_read_only():
    assert list(_specs({"supported_features": 0})) == ["state"]
    assert list(_specs({})) == ["state"]
    assert list(_specs({"supported_features": 1 | 8})) == ["playing", "mute", "state"]  # pause only counts
    assert list(_specs({"supported_features": 128}, keep_parts={"volume", "mute"})) == [
        "volume", "mute", "state", "power"
    ]
    read_only = _specs(SONOS, allow_control=False)
    assert all("topic_write" not in s.meta for s in read_only.values())


def test_media_track():
    from custom_components.iotopen.export_mapping import media_track

    assert media_track({"media_artist": "A", "media_title": "T"}) == "A - T"
    assert media_track({"media_title": "T"}) == "T"
    assert media_track({"media_channel": "Radio X"}) == "Radio X"
    assert media_track({"media_title": "Song", "media_channel": "Radio X"}) == "Song"
    assert media_track({"media_album_artist": "B", "media_title": "T"}) == "B - T"
    assert media_track({}) == ""
    long = media_track({"media_artist": "A" * 150, "media_title": "T" * 150})
    assert len(long) == 200 and long.endswith("…")


def test_media_value():
    from custom_components.iotopen.export_mapping import media_value

    assert media_value("playing", "playing", SONOS) == (1, "Some Artist - Some Title")
    assert media_value("playing", "paused", SONOS) == (0, "Some Artist - Some Title")  # text kept on pause
    assert media_value("playing", "idle", SONOS) == (0, "")
    assert media_value("playing", "off", {}) == (0, "")
    assert media_value("playing", "unavailable", SONOS) is None
    assert media_value("volume", "playing", SONOS) == (27, "")
    assert media_value("volume", "off", {}) is None  # no volume_level while off
    assert media_value("mute", "playing", SONOS) == (0, "")
    assert media_value("mute", "playing", {**SONOS, "is_volume_muted": True}) == (1, "")
    assert media_value("mute", "off", {}) is None
    assert media_value("power", "idle", SONOS) == (1, "")
    assert media_value("power", "off", {}) == (0, "")
    assert media_value("power", "standby", {}) == (0, "")
    assert media_value("state", "paused", SONOS) == ("paused", "")
    assert media_value("state", "unavailable", {}) == ("unavailable", "")  # status shows everything
    assert media_value("state", None, {}) is None


def test_media_command():
    from custom_components.iotopen.export_mapping import media_command

    assert media_command("playing", 1) == ("media_player", "media_play", {})
    assert media_command("playing", "0") == ("media_player", "media_pause", {})
    assert media_command("volume", 35) == ("media_player", "volume_set", {"volume_level": 0.35})
    assert media_command("volume", 250) == ("media_player", "volume_set", {"volume_level": 1.0})
    assert media_command("volume", "loud") is None
    assert media_command("mute", "on") == ("media_player", "volume_mute", {"is_volume_muted": True})
    assert media_command("power", 0) == ("media_player", "turn_off", {})
    assert media_command("state", 1) is None


def test_suffixed_topics():
    from custom_components.iotopen.export_mapping import is_onoff_topic, topic_suffix

    assert entity_for_topic("set/obj/ha/media_player/living_room/volume") == "media_player.living_room"
    assert topic_suffix("set/obj/ha/media_player/living_room/volume") == "volume"
    assert topic_suffix("obj/ha/media_player/living_room/state") == "state"
    assert entity_for_topic("set/obj/ha/light/hall/onoff") == "light.hall"
    assert is_onoff_topic("set/obj/ha/light/hall/onoff")
    assert not is_onoff_topic("set/obj/ha/media_player/x/power")
    assert topic_suffix("set/obj/ha/switch/kitchen") == ""
    assert entity_for_topic("set/obj/ha/media_player/x/unknown") is None


THERMOSTAT = {
    "supported_features": 1 | 128 | 256,  # target temperature, turn off/on
    "hvac_modes": ["off", "heat", "auto"],
    "min_temp": 5,
    "max_temp": 30,
    "target_temp_step": 0.5,
    "current_temperature": 21.3,
    "temperature": 21.5,
    "hvac_action": "heating",
    "temperature_unit": "°C",
}


def _climate_specs(attrs, **kwargs):
    from custom_components.iotopen.export_mapping import function_specs

    return {s.part: s for s in function_specs("climate.hallway", "Hallway", attrs, "src", **kwargs)}


def test_climate_specs():
    specs = _climate_specs(THERMOSTAT, iot_device_id=7, language="sv")
    assert list(specs) == ["current", "target", "mode", "action"]
    current = specs["current"]
    assert current.type == "temperature" and current.meta["unit"] == "°C"
    assert current.meta["topic_read"] == "obj/ha/climate/hallway/current" and "topic_write" not in current.meta
    assert current.meta["name"] == "Hallway Temperatur" and current.meta["device_id"] == "7"
    target = specs["target"]
    assert target.type == "setpoint" and target.meta["name"] == "Hallway Börvärde"
    assert (target.meta["min"], target.meta["max"], target.meta["step"]) == ("5", "30", "0.5")
    assert target.meta["topic_write"] == "set/obj/ha/climate/hallway/target"
    mode = specs["mode"]
    assert mode.type == "text" and mode.meta["ha.hvac_modes"] == "off,heat,auto"
    assert mode.meta["topic_write"] == "set/obj/ha/climate/hallway/mode"
    assert specs["action"].type == "text" and "topic_write" not in specs["action"].meta
    assert _climate_specs(THERMOSTAT)["target"].meta["name"] == "Hallway Target temperature"


def test_climate_specs_minimal_and_read_only():
    assert list(_climate_specs({})) == ["mode"]
    assert list(_climate_specs({"current_temperature": 20})) == ["current", "mode"]
    assert list(_climate_specs({}, keep_parts={"target", "current"})) == ["current", "target", "mode"]
    assert _climate_specs({"current_temperature": 68, "temperature_unit": "°F"})["current"].meta["unit"] == "°F"
    read_only = _climate_specs(THERMOSTAT, allow_control=False)
    assert all("topic_write" not in s.meta for s in read_only.values())


def test_climate_value_and_command():
    from custom_components.iotopen.export_mapping import climate_command, part_command, part_value

    assert part_value("climate.hallway", "current", "heat", THERMOSTAT) == (21.3, "")
    assert part_value("climate.hallway", "target", "heat", THERMOSTAT) == (21.5, "")
    assert part_value("climate.hallway", "target", "heat", {**THERMOSTAT, "temperature": 21.0}) == (21, "")
    assert part_value("climate.hallway", "target", "off", {**THERMOSTAT, "temperature": None}) is None
    assert part_value("climate.hallway", "mode", "heat", THERMOSTAT) == ("heat", "heat")
    assert part_value("climate.hallway", "action", "heat", THERMOSTAT) == ("heating", "heating")
    assert part_value("climate.hallway", "mode", "unavailable", THERMOSTAT) is None
    assert part_command("climate.hallway", "target", "22.5") == ("climate", "set_temperature", {"temperature": 22.5})
    assert climate_command("target", "warm") is None
    assert climate_command("mode", "Heat") == ("climate", "set_hvac_mode", {"hvac_mode": "heat"})
    assert climate_command("current", 20) is None and climate_command("action", "idle") is None
    assert entity_for_topic("set/obj/ha/climate/hallway/target") == "climate.hallway"
