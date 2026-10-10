# pyiotopen

Async Python client for [IoT Open](https://iotopen.se) (Lynx). Covers the REST API and live values
pushed over MQTT 3.1.1 on WebSocket. Built for the Home Assistant integration `iotopen`, but it
works in any asyncio application.

- Pure Python. The only dependency is `aiohttp`, so nothing is compiled on ARM/Raspberry Pi.
- Auth with an API key, or username/password including 2FA. Both end up in the `X-API-Key` header.
- REST: installations, devices (`devicex`), functions (`functionx`), metadata, latest values
  (`status`) and history (`v3beta/log`, where topics are normalised to their bare form).
- Live values: `wss://<host>/mqtt`, sub-protocol `mqtt`, with the token as both username and password.
  Includes back-off reconnect, keep-alive, subscription diffing and QoS 0 publish for control.
- Structure events: `<client_id>/evt/functionx/updated` and `<client_id>/evt/devicex/updated`
  tell you when to re-read an installation, so the structure never has to be polled.

## Example

```python
import asyncio, aiohttp
from pyiotopen import (IoTOpenClient, IoTOpenMqtt, ValueUpdate, StructureEvent,
                       parse_message, full_topic, structure_topics)

async def main():
    async with aiohttp.ClientSession() as session:
        client = IoTOpenClient(session, "https://lynx.iotopen.se", api_key="...")
        inst = (await client.installations())[0]
        snap = await client.snapshot(inst)          # devices, functions, latest values

        def on_message(topic, payload):
            match parse_message(topic, payload):
                case ValueUpdate(topic=t, value=v):
                    print(t, v)
                case StructureEvent():
                    print("structure changed - re-read devicex/functionx")

        mqtt = IoTOpenMqtt(session, client.mqtt_url, client.api_key, on_message=on_message)
        await mqtt.set_subscriptions(
            [full_topic(inst.client_id, t) for t in snap.read_topics()]
            + structure_topics(inst.client_id)
        )
        await mqtt.start()
        await asyncio.sleep(60)
        await mqtt.stop()

asyncio.run(main())
```

### Login with 2FA

```python
from pyiotopen import IoTOpenTwoFactorRequired
try:
    token = await client.authenticate(user, password)
except IoTOpenTwoFactorRequired as err:
    token = (await client.login_2fa(err.token, input("Code: "))).token
```

Use an API key for long-running services. It does not expire, while a login token does.

### Controlling a function

Publish `{"value": ...}` to `<client_id>/<topic_write>`:

```python
await mqtt.publish(full_topic(inst.client_id, fn.topic_write), {"value": 1})
```

## Development

```bash
pip install -e ".[test]"
pytest
```

The tests run against an in-process fake Lynx (`tests/fake_lynx.py`) that provides the REST
endpoints and a small MQTT-over-WebSocket broker.
