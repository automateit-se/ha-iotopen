"""MQTT-over-WebSocket tests against the fake broker."""

from __future__ import annotations

import asyncio

from pyiotopen import (
    ConnectionState,
    IoTOpenClient,
    IoTOpenMqtt,
    StructureEvent,
    ValueUpdate,
    parse_message,
    structure_topics,
)


async def _wait_for(predicate, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.02)


async def test_live_values_and_structure_events(client: IoTOpenClient, lynx):
    events = []
    states = []
    mqtt = IoTOpenMqtt(
        client.session,
        client.mqtt_url,
        client.api_key,
        on_message=lambda t, p: events.append(parse_message(t, p)),
        on_state=states.append,
    )
    await mqtt.set_subscriptions(["2001/obj/demo/101/level", *structure_topics(2001)])
    await mqtt.start()
    assert await mqtt.wait_connected(5)
    await _wait_for(lambda: lynx.clients and len(lynx.clients[0].subscriptions) == 3)

    assert await lynx.publish("2001/obj/demo/101/level", {"value": 5, "timestamp": 1700000100, "msg": ""}) == 1
    assert await lynx.publish("2001/evt/functionx/updated", {}) == 1
    await _wait_for(lambda: len(events) == 2)
    assert events[0] == ValueUpdate(2001, "obj/demo/101/level", 5, 1700000100.0, "")
    assert events[1] == StructureEvent(2001, "functionx")

    # subscription changes are applied on the live connection
    await mqtt.set_subscriptions(["2001/obj/demo/101/velocity"])
    await _wait_for(lambda: lynx.clients[0].subscriptions == {"2001/obj/demo/101/velocity"})

    await mqtt.stop()
    assert states[0] is ConnectionState.CONNECTING
    assert ConnectionState.CONNECTED in states
    assert states[-1] is ConnectionState.STOPPED


async def test_publish(client: IoTOpenClient, lynx):
    mqtt = IoTOpenMqtt(client.session, client.mqtt_url, client.api_key)
    await mqtt.start()
    assert await mqtt.wait_connected(5)
    await mqtt.publish("2001/set/obj/demo/101/light", {"value": 1})
    await _wait_for(lambda: lynx.published)
    assert lynx.published[0] == ("2001/set/obj/demo/101/light", {"value": 1})
    await mqtt.stop()


async def test_reconnect_resubscribes(client: IoTOpenClient, lynx):
    mqtt = IoTOpenMqtt(client.session, client.mqtt_url, client.api_key, reconnect_min=0.05)
    await mqtt.set_subscriptions(["2001/obj/a"])
    await mqtt.start()
    assert await mqtt.wait_connected(5)
    await _wait_for(lambda: lynx.clients)
    await lynx.drop_connections()
    await _wait_for(lambda: mqtt.connect_count == 2 and mqtt.connected)
    await _wait_for(lambda: lynx.clients and lynx.clients[0].subscriptions == {"2001/obj/a"})
    await mqtt.stop()


async def test_auth_failure_stops(client: IoTOpenClient):
    states = []
    mqtt = IoTOpenMqtt(client.session, client.mqtt_url, "wrong", on_state=states.append)
    await mqtt.start()
    await _wait_for(lambda: mqtt.state is ConnectionState.AUTH_FAILED)
    assert not mqtt.connected
    await mqtt.stop()
