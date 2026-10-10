"""A small in-process fake of IoT Open Lynx: REST endpoints plus an MQTT-over-WebSocket broker.

Synthetic test data: installation 1001 (client_id 2001) with an
Gateway device and functions under ``obj/demo/<deviceId>/<x>``.
"""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass, field
from typing import Any

from aiohttp import WSMsgType, web

from pyiotopen import mqtt_codec as codec

API_KEY = "test-api-key"
USERNAME = "user@example.com"
PASSWORD = "secret"
USERNAME_2FA = "twofactor@example.com"
TMP_TOKEN = "tmp-2fa-token"
TOTP = "123456"
SESSION_TOKEN = "session-token"


def default_data() -> dict[str, Any]:
    """Two installations, one device each."""
    return {
        "installations": [
            {"id": 1001, "client_id": 2001, "name": "Test Group A", "organization_id": 1},
            {"id": 1002, "client_id": 2002, "name": "Test Group B", "organization_id": 1},
        ],
        "devices": {
            1001: [
                {
                    "id": 101,
                    "installation_id": 1001,
                    "type": "Gateway",
                    "meta": {"name": "Demo Device 1"},
                    "protected_meta": {},
                    "created": 1,
                    "updated": 1,
                }
            ],
            1002: [],
        },
        "functions": {
            1001: [
                {
                    "id": 3001,
                    "installation_id": 1001,
                    "type": "device_status",
                    "meta": {"name": "Status", "device_id": "101", "topic_read": "obj/demo/101/device_status", "format": "%d"},
                    "protected_meta": {},
                    "updated": 1,
                },
                {
                    "id": 3002,
                    "installation_id": 1001,
                    "type": "level",
                    "meta": {"name": "Level", "device_id": "101", "topic_read": "obj/demo/101/level"},
                    "protected_meta": {},
                    "updated": 1,
                },
                {
                    "id": 3003,
                    "installation_id": 1001,
                    "type": "velocity",
                    "meta": {"name": "Velocity", "device_id": "101", "topic_read": "obj/demo/101/velocity", "unit": "m/s", "format": "%.2f"},
                    "protected_meta": {},
                    "updated": 1,
                },
                {
                    "id": 3004,
                    "installation_id": 1001,
                    "type": "fault",
                    "meta": {"name": "Fault", "device_id": "101", "topic_read": "obj/demo/101/fault", "state_on": "1", "state_off": "0"},
                    "protected_meta": {},
                    "updated": 1,
                },
                {
                    "id": 3005,
                    "installation_id": 1001,
                    "type": "switch",
                    "meta": {
                        "name": "Light",
                        "device_id": "101",
                        "topic_read": "obj/demo/101/light",
                        "topic_write": "set/obj/demo/101/light",
                        "state_on": "1",
                        "state_off": "0",
                    },
                    "protected_meta": {},
                    "updated": 1,
                },
            ],
            1002: [],
        },
        "status": {
            1001: [
                {"client_id": 2001, "installation_id": 1001, "topic": "obj/demo/101/device_status", "value": 0, "timestamp": 1700000000, "msg": "OK"},
                {"client_id": 2001, "installation_id": 1001, "topic": "obj/demo/101/level", "value": 3, "timestamp": 1700000000, "msg": ""},
                {"client_id": 2001, "installation_id": 1001, "topic": "obj/demo/101/velocity", "value": 0.5, "timestamp": 1700000000, "msg": ""},
                {"client_id": 2001, "installation_id": 1001, "topic": "obj/demo/101/fault", "value": 0, "timestamp": 1700000000, "msg": ""},
                {"client_id": 2001, "installation_id": 1001, "topic": "obj/demo/101/light", "value": 0, "timestamp": 1700000000, "msg": ""},
            ],
            1002: [],
        },
    }


@dataclass
class BrokerClient:
    ws: web.WebSocketResponse
    client_id: str = ""
    username: str | None = None
    subscriptions: set[str] = field(default_factory=set)


class FakeLynx:
    """State + aiohttp application."""

    def __init__(self) -> None:
        self.data = default_data()
        self.valid_tokens = {API_KEY, SESSION_TOKEN}
        self.clients: list[BrokerClient] = []
        self.published: list[tuple[str, dict[str, Any]]] = []
        self.requests: list[tuple[str, str]] = []
        self.subscribed = asyncio.Event()
        self.app = web.Application()
        r = self.app.router
        r.add_post("/api/v2/auth", self.auth_post)
        r.add_put("/api/v2/auth", self.auth_put)
        r.add_get("/api/v2/user/me", self.me)
        r.add_get("/api/v2/installationinfo", self.installations)
        r.add_get("/api/v2/devicex/{iid}", self.devices)
        r.add_get("/api/v2/functionx/{iid}", self.functions)
        r.add_put("/api/v2/functionx/{iid}/{fid}/meta/{key}", self.function_meta)
        r.add_get("/api/v2/status/{iid}", self.status)
        r.add_get("/api/v3beta/log/{iid}", self.log)
        r.add_get("/mqtt", self.mqtt)

    # ------------------------------------------------------------ REST
    def _check(self, request: web.Request) -> None:
        self.requests.append((request.method, request.path_qs))
        if request.headers.get("X-API-Key") not in self.valid_tokens:
            raise web.HTTPUnauthorized(text=json.dumps({"message": "unauthorized"}), content_type="application/json")

    async def auth_post(self, request: web.Request) -> web.Response:
        header = request.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            raise web.HTTPUnauthorized()
        user, _, pwd = base64.b64decode(header[6:]).decode().partition(":")
        if pwd != PASSWORD:
            raise web.HTTPUnauthorized(text='{"message":"bad credentials"}', content_type="application/json")
        if user == USERNAME_2FA:
            return web.json_response({"token": TMP_TOKEN, "next_step": "totp"})
        if user == USERNAME:
            return web.json_response({"token": SESSION_TOKEN, "expires_at": 1999999999})
        raise web.HTTPUnauthorized()

    async def auth_put(self, request: web.Request) -> web.Response:
        if request.headers.get("X-API-Key") != TMP_TOKEN:
            raise web.HTTPUnauthorized()
        body = await request.json()
        if body.get("challenge") != TOTP:
            raise web.HTTPUnauthorized(text='{"message":"bad challenge"}', content_type="application/json")
        return web.json_response({"token": SESSION_TOKEN, "expires_at": 1999999999})

    async def me(self, request: web.Request) -> web.Response:
        self._check(request)
        return web.json_response({"id": 42, "email": USERNAME, "organisations": [1]})

    async def installations(self, request: web.Request) -> web.Response:
        self._check(request)
        return web.json_response(self.data["installations"])

    async def devices(self, request: web.Request) -> web.Response:
        self._check(request)
        return web.json_response(self.data["devices"].get(int(request.match_info["iid"]), []))

    async def functions(self, request: web.Request) -> web.Response:
        self._check(request)
        return web.json_response(self.data["functions"].get(int(request.match_info["iid"]), []))

    async def function_meta(self, request: web.Request) -> web.Response:
        self._check(request)
        iid, fid, key = int(request.match_info["iid"]), int(request.match_info["fid"]), request.match_info["key"]
        body = await request.json()
        for fn in self.data["functions"].get(iid, []):
            if fn["id"] == fid:
                fn["meta"][key] = body["value"]
                return web.json_response(fn)
        raise web.HTTPNotFound()

    async def status(self, request: web.Request) -> web.Response:
        self._check(request)
        entries = self.data["status"].get(int(request.match_info["iid"]), [])
        topics = request.query.getall("topics", [])
        if topics:
            entries = [e for e in entries if e["topic"] in topics]
        return web.json_response(entries)

    async def log(self, request: web.Request) -> web.Response:
        self._check(request)
        iid = int(request.match_info["iid"])
        inst = next(i for i in self.data["installations"] if i["id"] == iid)
        data = [{**e, "topic": f"{inst['client_id']}/{e['topic']}"} for e in self.data["status"].get(iid, [])]
        return web.json_response({"total": len(data), "last": 0, "count": len(data), "data": data})

    # ------------------------------------------------------------ MQTT broker
    async def mqtt(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(protocols=("mqtt",))
        await ws.prepare(request)
        client = BrokerClient(ws)
        reader = codec.PacketReader()
        try:
            async for msg in ws:
                if msg.type is not WSMsgType.BINARY:
                    continue
                for pkt in reader.feed(msg.data):
                    await self._handle(client, pkt)
        finally:
            if client in self.clients:
                self.clients.remove(client)
        return ws

    async def _handle(self, client: BrokerClient, pkt: codec.Packet) -> None:
        ws = client.ws
        if pkt.type == codec.CONNECT:
            body = pkt.body
            pos = 2 + int.from_bytes(body[0:2], "big")  # protocol name
            flags = body[pos + 1]
            pos += 4
            fields = []
            while pos < len(body):
                ln = int.from_bytes(body[pos : pos + 2], "big")
                fields.append(body[pos + 2 : pos + 2 + ln].decode())
                pos += 2 + ln
            client.client_id = fields[0]
            username = fields[1] if flags & 0x80 else None
            password = fields[2] if flags & 0x40 else None
            if password not in self.valid_tokens or username != password:
                await ws.send_bytes(bytes([0x20, 2, 0, codec.CONNACK_NOT_AUTHORIZED]))
                await ws.close()
                return
            client.username = username
            self.clients.append(client)
            await ws.send_bytes(bytes([0x20, 2, 0, 0]))
        elif pkt.type == codec.SUBSCRIBE:
            pid = pkt.body[0:2]
            pos, codes = 2, []
            while pos < len(pkt.body):
                ln = int.from_bytes(pkt.body[pos : pos + 2], "big")
                client.subscriptions.add(pkt.body[pos + 2 : pos + 2 + ln].decode())
                pos += 3 + ln
                codes.append(0)
            await ws.send_bytes(bytes([0x90, 2 + len(codes)]) + pid + bytes(codes))
            self.subscribed.set()
        elif pkt.type == codec.UNSUBSCRIBE:
            pid = pkt.body[0:2]
            pos = 2
            while pos < len(pkt.body):
                ln = int.from_bytes(pkt.body[pos : pos + 2], "big")
                client.subscriptions.discard(pkt.body[pos + 2 : pos + 2 + ln].decode())
                pos += 2 + ln
            await ws.send_bytes(bytes([0xB0, 2]) + pid)
        elif pkt.type == codec.PINGREQ:
            await ws.send_bytes(bytes([0xD0, 0]))
        elif pkt.type == codec.PUBLISH:
            pub = codec.decode_publish(pkt)
            self.published.append((pub.topic, json.loads(pub.payload or b"{}")))
        elif pkt.type == codec.DISCONNECT:
            await ws.close()

    async def publish(self, topic: str, payload: dict[str, Any] | bytes) -> int:
        """Send a message to every client subscribed to ``topic``; returns the number of receivers."""
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        count = 0
        for client in list(self.clients):
            if topic in client.subscriptions:
                await client.ws.send_bytes(codec.encode_publish(topic, data))
                count += 1
        return count

    async def drop_connections(self) -> None:
        """Simulate a broker restart."""
        for client in list(self.clients):
            await client.ws.close()
        self.clients.clear()
