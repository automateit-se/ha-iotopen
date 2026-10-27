"""Mirroring HA -> IoT Open: media players and diagnostic entities of labelled devices."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
    async_fire_time_changed,
    async_mock_service,
)
from pytest_homeassistant_custom_component.typing import MqttMockHAClient

from custom_components.iotopen.const import DOMAIN
from custom_components.iotopen.pyiotopen import Device, Function, IoTOpenForbiddenError
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
    label_registry as lr,
)
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


async def test_forbidden_export_raises_repair_issue(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    export_client: MagicMock,
    mock_mqtt: type[FakeMqtt],
    lynx: LynxData,
) -> None:
    """A 403 from IoT Open shows up in Repairs and disappears once access is granted."""
    _player(hass, _label(hass))
    hass.states.async_set(PLAYER, "playing", PLAYING)
    create_function = export_client.create_function.side_effect
    export_client.create_function.side_effect = IoTOpenForbiddenError(
        403, "Permission required to access this resource."
    )
    await _setup(hass, config_entry, mock_mqtt)

    issue_id = f"export_forbidden_{config_entry.entry_id}"
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.translation_key == "export_forbidden"
    assert issue.severity == ir.IssueSeverity.ERROR
    assert issue.translation_placeholders["installation_id"] == str(IID)
    assert issue.translation_placeholders["url"].endswith(f"/installations/{IID}")
    assert config_entry.runtime_data.exporter.last_error.startswith("HTTP 403")

    # still forbidden: the issue stays and the exporter keeps retrying
    await _later(hass, 61)
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None

    # the user got write access: the next retry mirrors the player and clears the issue
    export_client.create_function.side_effect = create_function
    await _later(hass, 61)
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
    assert set(_own_functions(lynx)) == {"playing", "volume", "mute", "state", "power"}


async def test_forbidden_issue_removed_on_unload(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    export_client: MagicMock,
    mock_mqtt: type[FakeMqtt],
) -> None:
    """Stopping mirroring (unload or another installation) removes a stale issue."""
    _player(hass, _label(hass))
    hass.states.async_set(PLAYER, "playing", PLAYING)
    export_client.create_device.side_effect = IoTOpenForbiddenError(403, "Permission required")
    export_client.create_function.side_effect = IoTOpenForbiddenError(403, "Permission required")
    await _setup(hass, config_entry, mock_mqtt)
    issue_id = f"export_forbidden_{config_entry.entry_id}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_local_transport_uses_the_edge_broker(
    hass: HomeAssistant,
    mqtt_mock: MqttMockHAClient,
    config_entry: MockConfigEntry,
    export_client: MagicMock,
    mock_mqtt: type[FakeMqtt],
    lynx: LynxData,
) -> None:
    """Local mode: values and commands over HA's MQTT integration, with bare topics."""
    _player(hass, _label(hass))
    hass.states.async_set(PLAYER, "playing", PLAYING)
    cloud = await _setup(hass, config_entry, mock_mqtt, export_transport="local")
    calls = {service: async_mock_service(hass, "media_player", service) for service in ("media_play", "media_pause")}

    # structure still goes over REST
    assert set(_own_functions(lynx)) == {"playing", "volume", "mute", "state", "power"}

    # values: bare topic on the local broker, QoS 1, nothing on IoT Open's cloud MQTT
    local = {c.args[0]: c.args[1:] for c in mqtt_mock.async_publish.call_args_list}
    payload, qos, retain = local["obj/ha/media_player/living_room/playing"]
    assert json.loads(payload)["value"] == 1
    assert (qos, retain) == (1, False)
    assert not [t for t, _ in cloud.published if "/obj/ha/" in t]
    assert not [t for t in cloud.subscriptions if "/set/obj/ha/" in t]

    # a state change is published locally
    mqtt_mock.async_publish.reset_mock()
    hass.states.async_set(PLAYER, "paused", PLAYING)
    await hass.async_block_till_done(wait_background_tasks=True)
    topics = [c.args[0] for c in mqtt_mock.async_publish.call_args_list]
    assert "obj/ha/media_player/living_room/playing" in topics

    # a command on the edge broker (local dashboard, or bridged down from IoT Open) calls HA once
    async_fire_mqtt_message(hass, "set/obj/ha/media_player/living_room/playing", '{"value": 1}')
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(calls["media_play"]) == 1

    # the same command arriving over the cloud connection is not executed a second time
    cloud.push(f"{CLIENT_ID}/set/obj/ha/media_player/living_room/playing", {"value": 0})
    await hass.async_block_till_done(wait_background_tasks=True)
    assert not calls["media_pause"]


async def test_cloud_transport_ignores_the_local_broker(
    hass: HomeAssistant,
    mqtt_mock: MqttMockHAClient,
    config_entry: MockConfigEntry,
    export_client: MagicMock,
    mock_mqtt: type[FakeMqtt],
) -> None:
    """Default mode: HA's MQTT integration is neither used for values nor for commands."""
    _player(hass, _label(hass))
    hass.states.async_set(PLAYER, "playing", PLAYING)
    cloud = await _setup(hass, config_entry, mock_mqtt)
    calls = async_mock_service(hass, "media_player", "media_play")

    assert not [c for c in mqtt_mock.async_publish.call_args_list if c.args[0].startswith("obj/ha/")]
    assert _published(cloud, "playing")
    async_fire_mqtt_message(hass, "set/obj/ha/media_player/living_room/playing", '{"value": 1}')
    await hass.async_block_till_done(wait_background_tasks=True)
    assert not calls


THERMOSTAT = "climate.hallway"
HEATING = {
    "friendly_name": "Hallway",
    "supported_features": 1,
    "hvac_modes": ["off", "heat"],
    "min_temp": 5,
    "max_temp": 30,
    "target_temp_step": 0.5,
    "current_temperature": 20.5,
    "temperature": 21,
    "hvac_action": "heating",
}


async def test_thermostat_becomes_several_functions(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    export_client: MagicMock,
    mock_mqtt: type[FakeMqtt],
    lynx: LynxData,
) -> None:
    label_id = _label(hass)
    ent_reg = er.async_get(hass)
    entry = ent_reg.async_get_or_create("climate", "demo", "thermostat-1", suggested_object_id="hallway")
    ent_reg.async_update_entity(entry.entity_id, labels={label_id})
    hass.states.async_set(THERMOSTAT, "heat", HEATING)
    mqtt = await _setup(hass, config_entry, mock_mqtt)

    functions = {f["meta"]["ha.part"]: f for f in lynx.functions[IID] if f["meta"].get("ha.entity_id") == THERMOSTAT}
    assert set(functions) == {"current", "target", "mode", "action"}
    assert functions["target"]["type"] == "setpoint" and functions["target"]["meta"]["unit"] == "°C"
    assert functions["mode"]["meta"]["ha.hvac_modes"] == "off,heat"

    def published(part: str) -> list[Any]:
        topic = f"{CLIENT_ID}/obj/ha/climate/hallway/{part}"
        return [p["value"] for t, p in mqtt.published if t == topic]

    assert published("current") == [20.5] and published("target") == [21]
    assert published("mode") == ["heat"] and published("action") == ["heating"]

    hass.states.async_set(THERMOSTAT, "heat", {**HEATING, "current_temperature": 20.7, "hvac_action": "idle"})
    await hass.async_block_till_done(wait_background_tasks=True)
    assert published("current") == [20.5, 20.7] and published("action") == ["heating", "idle"]
    assert published("target") == [21]  # unchanged values are not repeated

    calls = {s: async_mock_service(hass, "climate", s) for s in ("set_temperature", "set_hvac_mode")}
    base = f"{CLIENT_ID}/set/obj/ha/climate/hallway"
    mqtt.push(f"{base}/target", {"value": 22.5})
    mqtt.push(f"{base}/mode", {"value": "off"})
    mqtt.push(f"{base}/current", {"value": 30})  # read only: ignored
    await hass.async_block_till_done(wait_background_tasks=True)
    assert calls["set_temperature"][0].data == {"entity_id": THERMOSTAT, "temperature": 22.5}
    assert calls["set_hvac_mode"][0].data == {"entity_id": THERMOSTAT, "hvac_mode": "off"}
