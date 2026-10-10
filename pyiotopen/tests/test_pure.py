"""Tests for pure helpers: topics, models, events and the MQTT codec (no network)."""

from __future__ import annotations

import pytest

from pyiotopen import (
    Function,
    Installation,
    InstallationSnapshot,
    Status,
    StructureEvent,
    ValueUpdate,
    decode_payload,
    full_topic,
    mqtt_url_for,
    normalize_base_url,
    normalize_topic,
    parse_message,
    split_topic,
    structure_topics,
)
from pyiotopen import mqtt_codec as codec


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2001/obj/demo/1/level", "obj/demo/1/level"),
        ("obj/demo/1/level", "obj/demo/1/level"),
        ("/obj/x/", "obj/x"),
        ("", None),
        (None, None),
        ("  2001/obj/x ", "obj/x"),
    ],
)
def test_normalize_topic(raw, expected):
    assert normalize_topic(raw) == expected


def test_topic_helpers():
    assert full_topic(2001, "2001/obj/a") == "2001/obj/a"
    assert full_topic("2001", "obj/a") == "2001/obj/a"
    assert split_topic("2001/obj/a/b") == (2001, "obj/a/b")
    assert split_topic("obj/a") is None
    assert structure_topics(2001) == ["2001/evt/functionx/updated", "2001/evt/devicex/updated"]
    with pytest.raises(ValueError):
        full_topic(1, "")


def test_urls():
    assert normalize_base_url("lynx.iotopen.se/") == "https://lynx.iotopen.se"
    assert normalize_base_url(None) == "https://lynx.iotopen.se"
    assert mqtt_url_for("https://lynx.iotopen.se") == "wss://lynx.iotopen.se/mqtt"
    assert mqtt_url_for("http://localhost:8080/lynx/") == "ws://localhost:8080/lynx/mqtt"
    with pytest.raises(ValueError):
        normalize_base_url("ftp://x")


def test_function_model():
    fn = Function.from_dict(
        {
            "id": 5,
            "installation_id": 1001,
            "type": "switch",
            "meta": {
                "name": " Light ",
                "device_id": "101",
                "topic_read": "2001/obj/light",
                "topic_write": "set/obj/light",
                "state_on": "1",
                "state_off": "0",
                "unit": "",
                "number": 3,
            },
        }
    )
    assert fn.name == "Light"
    assert fn.device_id == 101
    assert fn.topic_read == "obj/light"
    assert fn.topic_write == "set/obj/light"
    assert fn.unit is None
    assert fn.is_binary and fn.is_controllable
    assert fn.meta["number"] == "3"
    no_name = Function.from_dict({"id": 6, "type": "temperature", "meta": {"device_id": "abc"}})
    assert no_name.name == "temperature 6"
    assert no_name.device_id is None


def test_installation_and_status():
    inst = Installation.from_dict({"id": 1001, "client_id": 2001, "name": "  "})
    assert inst.name == "Installation 1001"
    st = Status.from_dict({"topic": "2001/obj/x", "value": 1, "timestamp": "12.5"})
    assert st.topic == "obj/x" and st.timestamp == 12.5


def test_snapshot_merge():
    inst = Installation(1, 2, "x")
    old = InstallationSnapshot(inst, values={"a": Status("a", 1, 100), "b": Status("b", 1, 100)})
    new = InstallationSnapshot(inst, values={"a": Status("a", 2, 50), "b": Status("b", 3, 200)})
    new.merge_newer_values(old)
    assert new.values["a"].value == 1
    assert new.values["b"].value == 3


def test_parse_message():
    assert parse_message("2001/evt/functionx/updated", {}) == StructureEvent(2001, "functionx")
    assert parse_message("2001/evt/devicex/updated", {}) == StructureEvent(2001, "devicex")
    assert parse_message("2001/evt/other", {}) is None
    assert parse_message("nope", {}) is None
    upd = parse_message("2001/obj/a", {"value": 3, "timestamp": 10, "msg": "hi"})
    assert upd == ValueUpdate(2001, "obj/a", 3, 10.0, "hi")


def test_decode_payload():
    assert decode_payload(b'{"value": 1}') == {"value": 1}
    assert decode_payload(b"12.5") == {"value": 12.5}
    assert decode_payload(b"on") == {"value": "on"}
    assert decode_payload(b"") == {}
    assert decode_payload("[1,2]") == {"value": [1, 2]}


@pytest.mark.parametrize("length", [0, 1, 127, 128, 16383, 16384, 2097151, 2097152])
def test_remaining_length_roundtrip(length):
    encoded = codec.encode_remaining_length(length)
    reader = codec.PacketReader()
    packets = list(reader.feed(bytes([0x30]) + encoded + b"\0" * length))
    assert len(packets) == 1 and len(packets[0].body) == length


def test_connect_packet():
    pkt = codec.encode_connect("cid", "tok", "tok", keepalive=60)
    assert pkt[0] == 0x10
    # protocol name "MQTT", level 4, flags user+pass+clean
    assert pkt[2:8] == b"\x00\x04MQTT"
    assert pkt[8] == 4
    assert pkt[9] == 0xC2


def test_reader_handles_split_and_merged_frames():
    a = codec.encode_publish("2001/obj/a", b'{"value":1}')
    b = codec.encode_publish("2001/obj/b", b'{"value":2}', qos=1, packet_id=7)
    stream = a + b
    reader = codec.PacketReader()
    got = []
    for i in range(0, len(stream), 3):
        got.extend(reader.feed(stream[i : i + 3]))
    assert [p.type for p in got] == [codec.PUBLISH, codec.PUBLISH]
    first, second = (codec.decode_publish(p) for p in got)
    assert first.topic == "2001/obj/a" and first.payload == b'{"value":1}' and first.packet_id is None
    assert second.qos == 1 and second.packet_id == 7


def test_subscribe_requires_topics():
    with pytest.raises(ValueError):
        codec.encode_subscribe(1, [])
    pkt = codec.encode_subscribe(1, ["a", "b"])
    assert pkt[0] == 0x82


def test_topic_write_convention_and_legacy_fallback():
    std = Function.from_dict({"id": 1, "type": "switch", "meta": {"topic_read": "obj/a", "topic_write": "set/obj/a"}})
    assert std.topic_write == "set/obj/a" and std.is_controllable
    legacy = Function.from_dict({"id": 2, "type": "switch", "meta": {"topic_read": "obj/a", "topic_set": "set/obj/a"}})
    assert legacy.topic_write == "set/obj/a"
    ro = Function.from_dict({"id": 3, "type": "temp", "meta": {"topic_read": "obj/a"}})
    assert not ro.is_controllable
