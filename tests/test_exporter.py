"""Mirroring HA -> IoT Open: media players and diagnostic entities of labelled devices."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)

from custom_components.iotopen.pyiotopen import Device, Function
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er, label_registry as lr
from homeassistant.util import dt as dt_util

from .conftest import FakeMqtt, LynxData

IID = 1001
CLIENT_ID = 2001
PLAYER = "media_player.living_room"
FEATURES = 16384 | 1 | 4 | 8 | 128 | 256  # play, pause, volume set, mute, turn on/off
PLAYING = {
    "friendly_name": "Living Room",
    "supported_features": FEATURES,
    "volume_level": 0.3,
    "is_volume_muted": False,
    "media_artist": "Some Artist",
    "media_title": "First Song",
}


@pytest.fixture
def export_client(mock_client: MagicMock, lynx: LynxData) -> MagicMock:
    """CRUD for functions and devices on top of the fake Lynx data."""
    ids = iter(range(9000, 10000))

    def find(items: list[dict[str, Any]], item_id: int) -> dict[str, Any]:
        return next(i for i in items if i["id"] == item_id)

    def create(store: dict[int, list[dict[str, Any]]], cls: Any):
        def _create(iid: int, type_: str, meta: dict[str, str], protected_meta: Any = None) -> Any:
            item = {"id": next(ids), "installation_id": iid, "type": type_, "meta": dict(meta)}
            store.setdefault(iid, []).append(item)
            return cls.from_dict(item)

        return _create

    def update(store: dict[int, list[dict[str, Any]]], cls: Any):
        def _update(obj: Any, *, type_: str | None = None, meta: dict[str, str] | None = None) -> Any:
            item = find(store[obj.installation_id], obj.id)
            if type_ is not None:
                item["type"] = type_
            if meta is not None:
                item["meta"] = dict(meta)
            return cls.from_dict(item)

        return _update

    def delete(store: dict[int, list[dict[str, Any]]]):
        def _delete(iid: int, item_id: int) -> None:
            store[iid] = [i for i in store[iid] if i["id"] != item_id]

        return _delete

    mock_client.functions = AsyncMock(
        side_effect=lambda iid, **_: [Function.from_dict(f) for f in lynx.functions.get(iid, [])]
    )
    mock_client.devices = AsyncMock(
        side_effect=lambda iid, **_: [Device.from_dict(d) for d in lynx.devices.get(iid, [])]
    )
    mock_client.create_function = AsyncMock(side_effect=create(lynx.functions, Function))
    mock_client.update_function = AsyncMock(side_effect=update(lynx.functions, Function))
    mock_client.delete_function = AsyncMock(side_effect=delete(lynx.functions))
    mock_client.create_device = AsyncMock(side_effect=create(lynx.devices, Device))
    mock_client.update_device = AsyncMock(side_effect=update(lynx.devices, Device))
    mock_client.delete_device = AsyncMock(side_effect=delete(lynx.devices))
    return mock_client


async def _setup(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_mqtt: type[FakeMqtt], **options: Any
) -> FakeMqtt:
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry, options={**config_entry.options, "export_installation": str(IID), **options}
    )
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    return mock_mqtt.instances[-1]


def _label(hass: HomeAssistant) -> str:
    return lr.async_get(hass).async_create("IoT Open").label_id


def _player(hass: HomeAssistant, label_id: str, features: int = FEATURES) -> er.RegistryEntry:
    ent_reg = er.async_get(hass)
    entry = ent_reg.async_get_or_create(
        "media_player", "sonos", "player-1", suggested_object_id="living_room", supported_features=features
    )
    return ent_reg.async_update_entity(entry.entity_id, labels={label_id})


def _own_functions(lynx: LynxData) -> dict[str, dict[str, Any]]:
    """part -> function for the mirrored player."""
    return {
        f["meta"]["ha.part"]: f
        for f in lynx.functions[IID]
        if f["meta"].get("ha.entity_id") == PLAYER
    }


def _published(mqtt: FakeMqtt, part: str) -> list[dict[str, Any]]:
    topic = f"{CLIENT_ID}/obj/ha/media_player/living_room/{part}"
    return [payload for t, payload in mqtt.published if t == topic]


async def _later(hass: HomeAssistant, seconds: float) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_media_player_becomes_several_functions(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    export_client: MagicMock,
    mock_mqtt: type[FakeMqtt],
    lynx: LynxData,
) -> None:
    _player(hass, _label(hass))
    hass.states.async_set(PLAYER, "playing", PLAYING)
    mqtt = await _setup(hass, config_entry, mock_mqtt)

    functions = _own_functions(lynx)
    assert set(functions) == {"playing", "volume", "mute", "state", "power"}
    assert functions["volume"]["type"] == "dimmer"
    assert functions["playing"]["meta"]["topic_write"] == "set/obj/ha/media_player/living_room/playing"
    assert "topic_write" not in functions["state"]["meta"]
    assert f"{CLIENT_ID}/set/obj/ha/media_player/living_room/volume" in mqtt.subscriptions

    # first publish of a new function is immediate
    assert _published(mqtt, "playing")[-1]["value"] == 1
    assert _published(mqtt, "playing")[-1]["msg"] == "Some Artist - First Song"
    assert _published(mqtt, "volume")[-1]["value"] == 30
    assert (_published(mqtt, "state")[-1]["value"], _published(mqtt, "state")[-1]["msg"]) == ("playing", "")

    # an attribute-only update (position, artwork) publishes nothing
    count = len(mqtt.published)
    hass.states.async_set(PLAYER, "playing", {**PLAYING, "media_position": 42})
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(mqtt.published) == count

    # a new track: same value, new msg
    hass.states.async_set(PLAYER, "playing", {**PLAYING, "media_title": "Second Song"})
    await hass.async_block_till_done(wait_background_tasks=True)
    assert _published(mqtt, "playing")[-1]["value"] == 1
    assert _published(mqtt, "playing")[-1]["msg"] == "Some Artist - Second Song"
    assert len(_published(mqtt, "state")) == 1

    # pause keeps the text, idle clears it
    hass.states.async_set(PLAYER, "paused", {**PLAYING, "media_title": "Second Song"})
    await hass.async_block_till_done(wait_background_tasks=True)
    assert (_published(mqtt, "playing")[-1]["value"], _published(mqtt, "playing")[-1]["msg"]) == (
        0,
        "Some Artist - Second Song",
    )
    hass.states.async_set(PLAYER, "idle", {"friendly_name": "Living Room", "supported_features": FEATURES})
    await hass.async_block_till_done(wait_background_tasks=True)
    assert _published(mqtt, "playing")[-1]["msg"] == ""
    assert _published(mqtt, "state")[-1]["value"] == "idle"


async def test_volume_is_debounced(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    export_client: MagicMock,
    mock_mqtt: type[FakeMqtt],
) -> None:
    _player(hass, _label(hass))
    hass.states.async_set(PLAYER, "playing", PLAYING)
    mqtt = await _setup(hass, config_entry, mock_mqtt)
    assert [p["value"] for p in _published(mqtt, "volume")] == [30]

    for level in (0.35, 0.4, 0.45):
        hass.states.async_set(PLAYER, "playing", {**PLAYING, "volume_level": level})
        await hass.async_block_till_done()
    assert [p["value"] for p in _published(mqtt, "volume")] == [30]
    await _later(hass, 2)
    assert [p["value"] for p in _published(mqtt, "volume")] == [30, 45]

    # back and forth within the debounce ends at the last value, even if it equals the published one
    hass.states.async_set(PLAYER, "playing", {**PLAYING, "volume_level": 0.5})
    await hass.async_block_till_done()
    hass.states.async_set(PLAYER, "playing", {**PLAYING, "volume_level": 0.45})
    await hass.async_block_till_done()
    await _later(hass, 4)
    assert [p["value"] for p in _published(mqtt, "volume")] == [30, 45]


async def test_media_player_commands(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    export_client: MagicMock,
    mock_mqtt: type[FakeMqtt],
) -> None:
    _player(hass, _label(hass))
    hass.states.async_set(PLAYER, "playing", PLAYING)
    mqtt = await _setup(hass, config_entry, mock_mqtt)
    calls = {
        service: async_mock_service(hass, "media_player", service)
        for service in ("media_play", "media_pause", "volume_set", "volume_mute", "turn_on", "turn_off")
    }

    base = f"{CLIENT_ID}/set/obj/ha/media_player/living_room"
    mqtt.push(f"{base}/playing", {"value": 0})
    mqtt.push(f"{base}/volume", {"value": 25})
    mqtt.push(f"{base}/mute", {"value": 1})
    mqtt.push(f"{base}/power", {"value": 0})
    mqtt.push(f"{base}/state", {"value": 1})  # read only: ignored
    await hass.async_block_till_done(wait_background_tasks=True)

    assert len(calls["media_pause"]) == 1 and calls["media_pause"][0].data["entity_id"] == PLAYER
    assert calls["volume_set"][0].data["volume_level"] == 0.25
    assert calls["volume_mute"][0].data["is_volume_muted"] is True
    assert len(calls["turn_off"]) == 1
    assert not calls["media_play"] and not calls["turn_on"]


async def test_read_only_player_has_no_topic_write(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    export_client: MagicMock,
    mock_mqtt: type[FakeMqtt],
    lynx: LynxData,
) -> None:
    _player(hass, _label(hass))
    hass.states.async_set(PLAYER, "playing", PLAYING)
    await _setup(hass, config_entry, mock_mqtt, export_control=False)
    assert all("topic_write" not in f["meta"] for f in _own_functions(lynx).values())


async def test_unsupported_functions_are_removed(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    export_client: MagicMock,
    mock_mqtt: type[FakeMqtt],
    lynx: LynxData,
) -> None:
    _player(hass, _label(hass))
    hass.states.async_set(PLAYER, "playing", PLAYING)
    await _setup(hass, config_entry, mock_mqtt)
    ids = {part: f["id"] for part, f in _own_functions(lynx).items()}

    # the player loses mute and on/off: those functions go, the others stay as they are
    features = 16384 | 1 | 4
    hass.states.async_set(PLAYER, "playing", {**PLAYING, "supported_features": features})
    await hass.async_block_till_done()
    await _later(hass, 5)
    functions = _own_functions(lynx)
    assert set(functions) == {"playing", "volume", "state"}
    assert {part: f["id"] for part, f in functions.items()} == {p: ids[p] for p in functions}

    # off or unavailable players may report fewer features: their functions stay
    for state in ("off", "unavailable"):
        hass.states.async_set(PLAYER, state, {"friendly_name": "Living Room", "supported_features": 128})
        await hass.async_block_till_done()
        hass.config_entries.async_schedule_reload(config_entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert {part: f["id"] for part, f in _own_functions(lynx).items()} == {
            p: ids[p] for p in ("playing", "volume", "state")
        } | {"power": _own_functions(lynx)["power"]["id"]}


async def test_manual_edits_in_iot_open_are_kept(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    export_client: MagicMock,
    mock_mqtt: type[FakeMqtt],
    lynx: LynxData,
) -> None:
    _player(hass, _label(hass))
    hass.states.async_set(PLAYER, "playing", PLAYING)
    await _setup(hass, config_entry, mock_mqtt)
    volume = _own_functions(lynx)["volume"]
    volume["meta"]["name"] = "Sonos volume"
    hass.config_entries.async_schedule_reload(config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert _own_functions(lynx)["volume"]["meta"]["name"] == "Sonos volume"
    assert len([f for f in lynx.functions[IID] if f["meta"].get("ha.entity_id") == PLAYER]) == 5


@pytest.mark.parametrize(
    ("include", "expected"),
    [(False, {"sensor.phone_steps"}), (True, {"sensor.phone_steps", "sensor.phone_battery"})],
)
async def test_diagnostic_entities_of_labelled_devices(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    export_client: MagicMock,
    mock_mqtt: type[FakeMqtt],
    lynx: LynxData,
    include: bool,
    expected: set[str],
) -> None:
    label_id = _label(hass)
    other = MockConfigEntry(domain="mobile_app")
    other.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=other.entry_id, identifiers={("mobile_app", "phone")}, name="Phone"
    )
    dr.async_get(hass).async_update_device(device.id, labels={label_id})
    ent_reg = er.async_get(hass)
    ent_reg.async_get_or_create(
        "sensor", "mobile_app", "steps", suggested_object_id="phone_steps", device_id=device.id
    )
    ent_reg.async_get_or_create(
        "sensor",
        "mobile_app",
        "battery",
        suggested_object_id="phone_battery",
        device_id=device.id,
        entity_category=EntityCategory.DIAGNOSTIC,
    )
    hass.states.async_set("sensor.phone_steps", "1200")
    hass.states.async_set("sensor.phone_battery", "80")

    await _setup(hass, config_entry, mock_mqtt, export_categorized=include)
    mirrored = {f["meta"].get("ha.entity_id") for f in lynx.functions[IID]} - {None}
    assert mirrored == expected


async def test_media_player_source(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    export_client: MagicMock,
    mock_mqtt: type[FakeMqtt],
    lynx: LynxData,
) -> None:
    _player(hass, _label(hass), FEATURES | 2048)
    hass.states.async_set(
        PLAYER,
        "on",
        {**PLAYING, "supported_features": FEATURES | 2048, "source": "TV", "source_list": ["TV", "CD"]},
    )
    mqtt = await _setup(hass, config_entry, mock_mqtt)
    source = _own_functions(lynx)["source"]
    assert source["meta"]["ha.source_list"] == '["TV", "CD"]'
    assert _published(mqtt, "source")[-1]["value"] == "TV"

    calls = async_mock_service(hass, "media_player", "select_source")
    mqtt.push(f"{CLIENT_ID}/set/obj/ha/media_player/living_room/source", {"value": "CD"})
    await hass.async_block_till_done(wait_background_tasks=True)
    assert calls[0].data == {"entity_id": PLAYER, "source": "CD"}


async def test_button_press_is_sent_once(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    export_client: MagicMock,
    mock_mqtt: type[FakeMqtt],
) -> None:
    label_id = _label(hass)
    ent_reg = er.async_get(hass)
    entry = ent_reg.async_get_or_create("button", "yamaha", "scene-tv", suggested_object_id="tv_viewing")
    ent_reg.async_update_entity(entry.entity_id, labels={label_id})
    # restored state after a restart: the time of an old press
    hass.states.async_set("button.tv_viewing", (dt_util.utcnow() - timedelta(hours=2)).isoformat())
    mqtt = await _setup(hass, config_entry, mock_mqtt)
    topic = f"{CLIENT_ID}/obj/ha/button/tv_viewing"
    assert not [p for t, p in mqtt.published if t == topic]

    pressed = dt_util.utcnow().isoformat()
    hass.states.async_set("button.tv_viewing", pressed)
    await hass.async_block_till_done(wait_background_tasks=True)
    sent = [p for t, p in mqtt.published if t == topic]
    assert [(p["value"], p["msg"]) for p in sent] == [(1, pressed)]

    mqtt.reconnect()  # a reconnect republishes states, but not presses
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len([p for t, p in mqtt.published if t == topic]) == 1
