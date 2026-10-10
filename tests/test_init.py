"""Setup, entities, live values and structure changes."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.iotopen.const import DOMAIN, device_identifier, hub_identifier
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util
from custom_components.iotopen.pyiotopen import ConnectionState, IoTOpenAuthError, IoTOpenConnectionError

from .conftest import FakeMqtt, LynxData


def entity_id(hass: HomeAssistant, platform: str, iid: int, fid: int) -> str | None:
    return er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"iotopen_{iid}_{fid}")


async def advance(hass: HomeAssistant, seconds: float) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_setup_creates_entities_and_devices(hass: HomeAssistant, setup_integration: FakeMqtt) -> None:
    level = entity_id(hass, "sensor", 1001, 3002)
    velocity = entity_id(hass, "sensor", 1001, 3003)
    fault = entity_id(hass, "binary_sensor", 1001, 3004)
    light = entity_id(hass, "switch", 1001, 3005)
    temp = entity_id(hass, "sensor", 1001, 3010)
    assert all((level, velocity, fault, light, temp))

    assert hass.states.get(level).state == "3"
    velocity_state = hass.states.get(velocity)
    assert float(velocity_state.state) == 0.5
    assert velocity_state.attributes["unit_of_measurement"] == "m/s"
    assert hass.states.get(fault).state == STATE_OFF
    assert hass.states.get(light).state == STATE_OFF
    temp_state = hass.states.get(temp)
    assert temp_state.attributes["device_class"] == "temperature"
    assert temp_state.attributes["state_class"] == "measurement"

    devices = dr.async_get(hass)
    hub = devices.async_get_device(identifiers={(DOMAIN, hub_identifier(1001))})
    demo = devices.async_get_device(identifiers={(DOMAIN, device_identifier(1001, 101))})
    assert hub is not None and hub.name == "Test Group A"
    assert demo is not None and demo.model == "Gateway"
    # devices are grouped per installation through the installation's config subentry
    assert demo.config_entries_subentries == hub.config_entries_subentries
    # function without device lands on the installation hub device
    assert er.async_get(hass).async_get(temp).device_id == hub.id
    assert er.async_get(hass).async_get(level).device_id == demo.id

    # only function topics + structure events are subscribed
    assert setup_integration.subscriptions == {
        "2001/obj/demo/101/device_status",
        "2001/obj/demo/101/level",
        "2001/obj/demo/101/velocity",
        "2001/obj/demo/101/fault",
        "2001/obj/demo/101/light",
        "2001/obj/room/temp",
        "2001/evt/functionx/updated",
        "2001/evt/devicex/updated",
        "2002/evt/functionx/updated",
        "2002/evt/devicex/updated",
    }


async def test_live_values(hass: HomeAssistant, setup_integration: FakeMqtt) -> None:
    level = entity_id(hass, "sensor", 1001, 3002)
    fault = entity_id(hass, "binary_sensor", 1001, 3004)
    setup_integration.push("2001/obj/demo/101/level", {"value": 5, "timestamp": 1700000100, "msg": ""})
    setup_integration.push("2001/obj/demo/101/fault", {"value": 1, "timestamp": 1700000100, "msg": "Door"})
    await hass.async_block_till_done()
    assert hass.states.get(level).state == "5"
    assert hass.states.get(fault).state == STATE_ON
    assert hass.states.get(fault).attributes["message"] == "Door"

    # an older message does not overwrite a newer value
    setup_integration.push("2001/obj/demo/101/level", {"value": 1, "timestamp": 1600000000})
    await hass.async_block_till_done()
    assert hass.states.get(level).state == "5"


async def test_switch_publishes(hass: HomeAssistant, setup_integration: FakeMqtt) -> None:
    light = entity_id(hass, "switch", 1001, 3005)
    await hass.services.async_call("switch", "turn_on", {"entity_id": light}, blocking=True)
    assert setup_integration.published[-1] == ("2001/set/obj/demo/101/light", {"value": 1})
    await hass.services.async_call("switch", "turn_off", {"entity_id": light}, blocking=True)
    assert setup_integration.published[-1] == ("2001/set/obj/demo/101/light", {"value": 0})
    # state follows topic_read, not the command
    assert hass.states.get(light).state == STATE_OFF
    setup_integration.push("2001/obj/demo/101/light", {"value": 1, "timestamp": 1700000200})
    await hass.async_block_till_done()
    assert hass.states.get(light).state == STATE_ON


async def test_structure_event_adds_updates_removes(
    hass: HomeAssistant, setup_integration: FakeMqtt, lynx: LynxData, mock_client: MagicMock
) -> None:
    functions = lynx.functions[1001]
    # remove "Fault", rename "Level", add a new energy meter
    functions[:] = [f for f in functions if f["id"] != 3004]
    next(f for f in functions if f["id"] == 3002)["meta"]["name"] = "Current level"
    functions.append(
        {"id": 3020, "installation_id": 1001, "type": "energy", "updated": 2,
         "meta": {"name": "Energy", "device_id": "101", "topic_read": "obj/demo/101/energy", "unit": "kWh"}}
    )
    lynx.status[1001].append({"topic": "obj/demo/101/energy", "value": 12.5, "timestamp": 1700000300})

    calls_before = mock_client.snapshot.call_count
    # a burst of events is debounced into one reload
    setup_integration.push("2001/evt/functionx/updated", {})
    setup_integration.push("2001/evt/functionx/updated", {})
    setup_integration.push("2001/evt/devicex/updated", {})
    await advance(hass, 2)
    assert mock_client.snapshot.call_count == calls_before + 1

    assert entity_id(hass, "binary_sensor", 1001, 3004) is None
    level = entity_id(hass, "sensor", 1001, 3002)
    assert hass.states.get(level).name.endswith("Current level")
    energy = entity_id(hass, "sensor", 1001, 3020)
    assert energy is not None
    energy_state = hass.states.get(energy)
    assert float(energy_state.state) == 12.5
    assert energy_state.attributes["state_class"] == "total_increasing"
    assert "2001/obj/demo/101/energy" in setup_integration.subscriptions
    assert "2001/obj/demo/101/fault" not in setup_integration.subscriptions


async def test_platform_change_recreates_entity(
    hass: HomeAssistant, setup_integration: FakeMqtt, lynx: LynxData
) -> None:
    fn = next(f for f in lynx.functions[1001] if f["id"] == 3002)
    fn["meta"].update({"state_on": "1", "state_off": "0"})
    setup_integration.push("2001/evt/functionx/updated", {})
    await advance(hass, 2)
    assert entity_id(hass, "sensor", 1001, 3002) is None
    assert entity_id(hass, "binary_sensor", 1001, 3002) is not None


async def test_device_removed_moves_functions_to_hub(
    hass: HomeAssistant, setup_integration: FakeMqtt, lynx: LynxData
) -> None:
    lynx.devices[1001] = []
    setup_integration.push("2001/evt/devicex/updated", {})
    await advance(hass, 2)
    devices = dr.async_get(hass)
    assert devices.async_get_device(identifiers={(DOMAIN, device_identifier(1001, 101))}) is None
    hub = devices.async_get_device(identifiers={(DOMAIN, hub_identifier(1001))})
    level = entity_id(hass, "sensor", 1001, 3002)
    assert er.async_get(hass).async_get(level).device_id == hub.id


async def test_hidden_meta(hass: HomeAssistant, setup_integration: FakeMqtt, lynx: LynxData) -> None:
    fn = next(f for f in lynx.functions[1001] if f["id"] == 3001)
    fn["meta"]["ha.hidden"] = "true"
    setup_integration.push("2001/evt/functionx/updated", {})
    await advance(hass, 2)
    entry = er.async_get(hass).async_get(entity_id(hass, "sensor", 1001, 3001))
    assert entry.hidden_by is er.RegistryEntryHider.INTEGRATION


async def test_disconnect_grace_and_reconnect(
    hass: HomeAssistant, setup_integration: FakeMqtt, mock_client: MagicMock, lynx: LynxData
) -> None:
    level = entity_id(hass, "sensor", 1001, 3002)
    setup_integration.set_state(ConnectionState.DISCONNECTED)
    await advance(hass, 10)
    assert hass.states.get(level).state == "3"  # still within grace period
    await advance(hass, 60)
    assert hass.states.get(level).state == STATE_UNAVAILABLE

    # value changed while offline; reconnect triggers a fresh snapshot
    lynx.status[1001][1] = {"topic": "obj/demo/101/level", "value": 7, "timestamp": 1700000500}
    calls_before = mock_client.snapshot.call_count
    setup_integration.reconnect()
    await advance(hass, 1)
    assert mock_client.snapshot.call_count == calls_before + 2  # both installations
    assert hass.states.get(level).state == "7"


async def test_mqtt_auth_failure_starts_reauth(
    hass: HomeAssistant, setup_integration: FakeMqtt, config_entry: MockConfigEntry
) -> None:
    setup_integration.set_state(ConnectionState.AUTH_FAILED)
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"]["source"] == "reauth" for f in flows)


@pytest.mark.parametrize(
    ("error", "state"),
    [
        (IoTOpenAuthError("nope"), ConfigEntryState.SETUP_ERROR),
        (IoTOpenConnectionError("down"), ConfigEntryState.SETUP_RETRY),
    ],
)
async def test_setup_errors(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_client: MagicMock,
    mock_mqtt: type[FakeMqtt],
    error: Exception,
    state: ConfigEntryState,
) -> None:
    mock_client.installations.side_effect = error
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is state


async def test_new_installation_found_hourly(
    hass: HomeAssistant, setup_integration: FakeMqtt, lynx: LynxData, config_entry: MockConfigEntry
) -> None:
    lynx.installations.append({"id": 1003, "client_id": 2003, "name": "New group"})
    lynx.devices[1003] = []
    lynx.functions[1003] = []
    lynx.status[1003] = []
    await advance(hass, 3601)
    installations = {s.data["installation_id"] for s in config_entry.subentries.values()}
    assert 1003 in installations
    assert 1003 in config_entry.options["known_installations"]


async def test_unload(hass: HomeAssistant, setup_integration: FakeMqtt, config_entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.NOT_LOADED
    assert setup_integration.state is ConnectionState.STOPPED


async def test_removed_function_cleaned_up_on_restart(
    hass: HomeAssistant, setup_integration: FakeMqtt, lynx: LynxData, config_entry: MockConfigEntry
) -> None:
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    lynx.functions[1001] = [f for f in lynx.functions[1001] if f["id"] != 3003]
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert entity_id(hass, "sensor", 1001, 3003) is None


async def test_failed_installation_keeps_entities(
    hass: HomeAssistant,
    setup_integration: FakeMqtt,
    mock_client: MagicMock,
    lynx: LynxData,
    config_entry: MockConfigEntry,
) -> None:
    """A temporary 403 on restart must not wipe the user's entities and devices."""
    from custom_components.iotopen.pyiotopen import IoTOpenForbiddenError

    assert await hass.config_entries.async_unload(config_entry.entry_id)

    def snapshot(inst):
        if inst.id == 1001:
            raise IoTOpenForbiddenError(403, "forbidden")
        return lynx.snapshot(inst)

    mock_client.snapshot.side_effect = snapshot
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert entity_id(hass, "sensor", 1001, 3002) is not None
    assert dr.async_get(hass).async_get_device(identifiers={(DOMAIN, device_identifier(1001, 101))})
