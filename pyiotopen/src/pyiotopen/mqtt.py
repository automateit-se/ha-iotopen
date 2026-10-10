"""Async MQTT 3.1.1 over WebSocket client for IoT Open live values.

Connects to ``wss://<host>/mqtt`` (sub-protocol ``mqtt``) with the IoT Open token or API key as
both username and password, keeps the subscription set in sync, re-connects with exponential
back-off and reports connection state changes.

Messages are delivered to ``on_message(topic, payload_dict)`` in the event loop. Use
:func:`pyiotopen.events.parse_message` to classify them.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import secrets
import time
from collections.abc import Callable, Iterable
from enum import StrEnum
from typing import Any

import aiohttp

from . import mqtt_codec as codec
from .events import decode_payload
from .exceptions import IoTOpenAuthError, IoTOpenConnectionError

_LOGGER = logging.getLogger(__name__)

SUBSCRIBE_CHUNK = 50
CONNACK_TIMEOUT = 15.0
CONNECT_TIMEOUT = 20.0


class ConnectionState(StrEnum):
    """State of the live connection."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    AUTH_FAILED = "auth_failed"
    STOPPED = "stopped"


MessageCallback = Callable[[str, dict[str, Any]], None]
StateCallback = Callable[[ConnectionState], None]


class IoTOpenMqtt:
    """Push client for IoT Open (MQTT 3.1.1 over WebSocket, QoS 0)."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        url: str,
        token: str,
        *,
        on_message: MessageCallback | None = None,
        on_state: StateCallback | None = None,
        keepalive: int = 60,
        client_id: str | None = None,
        reconnect_min: float = 1.0,
        reconnect_max: float = 60.0,
    ) -> None:
        self._session = session
        self._url = url
        self._token = token
        self._on_message = on_message
        self._on_state = on_state
        self._keepalive = max(10, int(keepalive))
        self._client_id = client_id or f"pyiotopen-{secrets.token_hex(6)}"
        self._reconnect_min = reconnect_min
        self._reconnect_max = reconnect_max

        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._task: asyncio.Task[None] | None = None
        self._state = ConnectionState.DISCONNECTED
        self._stopping = False
        self._wanted: set[str] = set()
        self._subscribed: set[str] = set()
        self._packet_id = 0
        self._send_lock = asyncio.Lock()
        self._sync_lock = asyncio.Lock()
        self._connected_event = asyncio.Event()
        self._last_rx = 0.0
        self._connect_count = 0

    # ------------------------------------------------------------------ public API
    @property
    def state(self) -> ConnectionState:
        """Current connection state."""
        return self._state

    @property
    def connected(self) -> bool:
        """True while the broker session is established."""
        return self._state is ConnectionState.CONNECTED

    @property
    def connect_count(self) -> int:
        """How many times a broker session has been established (1 = first connect)."""
        return self._connect_count

    @property
    def subscriptions(self) -> frozenset[str]:
        """Topics the caller wants to be subscribed to."""
        return frozenset(self._wanted)

    def set_token(self, token: str) -> None:
        """Use a new token on the next (re)connect."""
        self._token = token

    async def start(self) -> None:
        """Start connecting in the background. Returns immediately."""
        if self._task and not self._task.done():
            return
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="pyiotopen-mqtt")

    async def wait_connected(self, timeout: float | None = None) -> bool:
        """Wait until connected; returns False on timeout."""
        try:
            async with asyncio.timeout(timeout):
                await self._connected_event.wait()
        except TimeoutError:
            return False
        return True

    async def stop(self) -> None:
        """Disconnect and stop reconnecting."""
        self._stopping = True
        ws = self._ws
        if ws is not None and not ws.closed:
            with contextlib.suppress(Exception):
                await ws.send_bytes(codec.encode_disconnect())
            with contextlib.suppress(Exception):
                await ws.close()
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._set_state(ConnectionState.STOPPED)

    async def set_subscriptions(self, topics: Iterable[str]) -> None:
        """Replace the wanted subscription set (broker topics, e.g. ``2001/obj/x``)."""
        self._wanted = {t for t in topics if t}
        if self.connected:
            try:
                await self._sync_subscriptions()
            except IoTOpenConnectionError as err:
                # the connection is dropping; the reconnect subscribes to everything wanted
                _LOGGER.debug("Subscription update deferred to reconnect: %s", err)

    async def publish(
        self, topic: str, payload: dict[str, Any] | str | bytes | float | int, *, retain: bool = False
    ) -> None:
        """Publish (QoS 0). Dicts are JSON encoded. Raises if not connected."""
        if not self.connected or self._ws is None:
            raise IoTOpenConnectionError("MQTT is not connected")
        if isinstance(payload, bytes):
            data = payload
        elif isinstance(payload, str):
            data = payload.encode()
        else:
            data = json.dumps(payload, separators=(",", ":")).encode()
        await self._send(codec.encode_publish(topic, data, retain=retain))

    # ------------------------------------------------------------------ internals
    def _set_state(self, state: ConnectionState) -> None:
        if state is self._state:
            return
        self._state = state
        if state is ConnectionState.CONNECTED:
            self._connected_event.set()
        else:
            self._connected_event.clear()
        if self._on_state:
            try:
                self._on_state(state)
            except Exception:  # pragma: no cover - callback errors must not kill the loop
                _LOGGER.exception("Error in MQTT state callback")

    def _next_packet_id(self) -> int:
        self._packet_id = (self._packet_id % 0xFFFF) + 1
        return self._packet_id

    async def _send(self, data: bytes) -> None:
        ws = self._ws
        if ws is None or ws.closed:
            raise IoTOpenConnectionError("MQTT WebSocket is closed")
        async with self._send_lock:
            try:
                await ws.send_bytes(data)
            except (aiohttp.ClientError, ConnectionError, RuntimeError) as err:
                raise IoTOpenConnectionError(f"MQTT send failed: {err}") from err

    async def _sync_subscriptions(self) -> None:
        async with self._sync_lock:
            if not self.connected:
                return
            wanted = set(self._wanted)
            remove = sorted(self._subscribed - wanted)
            add = sorted(wanted - self._subscribed)
            for i in range(0, len(remove), SUBSCRIBE_CHUNK):
                chunk = remove[i : i + SUBSCRIBE_CHUNK]
                await self._send(codec.encode_unsubscribe(self._next_packet_id(), chunk))
                self._subscribed.difference_update(chunk)
            for i in range(0, len(add), SUBSCRIBE_CHUNK):
                chunk = add[i : i + SUBSCRIBE_CHUNK]
                await self._send(codec.encode_subscribe(self._next_packet_id(), chunk))
                self._subscribed.update(chunk)
            if add or remove:
                _LOGGER.debug("MQTT subscriptions: +%d -%d (total %d)", len(add), len(remove), len(self._subscribed))

    async def _run(self) -> None:
        attempt = 0
        while not self._stopping:
            self._set_state(ConnectionState.CONNECTING)
            sessions_before = self._connect_count
            try:
                await self._connect_and_read()
            except IoTOpenAuthError as err:
                _LOGGER.warning("IoT Open MQTT rejected the credentials: %s", err)
                self._set_state(ConnectionState.AUTH_FAILED)
                return
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - any failure leads to a reconnect
                _LOGGER.debug("IoT Open MQTT connection error: %r", err)
            finally:
                if self._connect_count > sessions_before:
                    attempt = 0  # we had a working session: first retry is fast again
                self._subscribed.clear()
                ws, self._ws = self._ws, None
                if ws is not None and not ws.closed:
                    with contextlib.suppress(Exception):
                        await ws.close()
            if self._stopping:
                break
            self._set_state(ConnectionState.DISCONNECTED)
            delay = min(self._reconnect_max, self._reconnect_min * (2**attempt))
            delay *= random.uniform(0.8, 1.2)
            attempt = min(attempt + 1, 16)
            _LOGGER.debug("IoT Open MQTT reconnecting in %.1fs", delay)
            await asyncio.sleep(delay)

    async def _connect_and_read(self) -> None:
        self._last_rx = 0.0
        try:
            async with asyncio.timeout(CONNECT_TIMEOUT):
                self._ws = await self._session.ws_connect(
                    self._url,
                    protocols=("mqtt",),
                    heartbeat=None,
                    autoping=True,
                    max_msg_size=0,
                )
        except aiohttp.WSServerHandshakeError as err:
            if err.status in (401, 403):
                raise IoTOpenAuthError(f"WebSocket handshake rejected ({err.status})") from err
            raise IoTOpenConnectionError(f"WebSocket handshake failed: {err}") from err
        except (aiohttp.ClientError, TimeoutError) as err:
            raise IoTOpenConnectionError(f"Cannot connect to {self._url}: {err}") from err

        await self._send(
            codec.encode_connect(self._client_id, self._token, self._token, self._keepalive)
        )
        reader = codec.PacketReader()
        ping_task: asyncio.Task[None] | None = None
        connack_deadline = time.monotonic() + CONNACK_TIMEOUT
        try:
            while True:
                timeout = None
                if not self.connected:
                    timeout = max(0.1, connack_deadline - time.monotonic())
                try:
                    msg = await self._ws.receive(timeout=timeout)
                except TimeoutError as err:
                    raise IoTOpenConnectionError("No CONNACK from broker") from err
                if msg.type is aiohttp.WSMsgType.BINARY:
                    data: bytes = msg.data
                elif msg.type is aiohttp.WSMsgType.TEXT:
                    data = msg.data.encode()
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.ERROR,
                ):
                    _LOGGER.debug("IoT Open MQTT WebSocket closed: %s", msg.type)
                    return
                else:
                    continue
                self._last_rx = time.monotonic()
                for packet in reader.feed(data):
                    if packet.type == codec.CONNACK:
                        await self._handle_connack(packet)
                        if ping_task is None:
                            ping_task = asyncio.create_task(self._pinger())
                    elif packet.type == codec.PUBLISH:
                        await self._handle_publish(packet)
                    # SUBACK / UNSUBACK / PINGRESP need no action
        finally:
            if ping_task is not None:
                ping_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await ping_task

    async def _handle_connack(self, packet: codec.Packet) -> None:
        code = codec.decode_connack(packet)
        if code in (codec.CONNACK_BAD_CREDENTIALS, codec.CONNACK_NOT_AUTHORIZED):
            raise IoTOpenAuthError(f"CONNACK return code {code}")
        if code != codec.CONNACK_ACCEPTED:
            raise IoTOpenConnectionError(f"CONNACK return code {code}")
        self._connect_count += 1
        self._subscribed.clear()
        # subscribe BEFORE announcing the state so listeners that re-read over REST don't miss values
        await self._sync_subscriptions_force()
        self._set_state(ConnectionState.CONNECTED)
        # catch set_subscriptions() calls that raced with the initial subscribe
        await self._sync_subscriptions()
        _LOGGER.debug("IoT Open MQTT connected (%s)", self._url)

    async def _sync_subscriptions_force(self) -> None:
        """Subscribe to everything wanted right after CONNACK (state is not CONNECTED yet)."""
        async with self._sync_lock:
            add = sorted(self._wanted)
            for i in range(0, len(add), SUBSCRIBE_CHUNK):
                chunk = add[i : i + SUBSCRIBE_CHUNK]
                await self._send(codec.encode_subscribe(self._next_packet_id(), chunk))
                self._subscribed.update(chunk)

    async def _handle_publish(self, packet: codec.Packet) -> None:
        try:
            pub = codec.decode_publish(packet)
        except codec.MqttProtocolError as err:
            _LOGGER.debug("Ignoring malformed PUBLISH: %s", err)
            return
        if pub.qos == 1 and pub.packet_id is not None:
            await self._send(codec.encode_puback(pub.packet_id))
        if self._on_message is None:
            return
        try:
            self._on_message(pub.topic, decode_payload(pub.payload))
        except Exception:  # pragma: no cover - callback errors must not kill the loop
            _LOGGER.exception("Error handling MQTT message on %s", pub.topic)

    async def _pinger(self) -> None:
        interval = self._keepalive / 2
        while True:
            await asyncio.sleep(interval)
            if self._last_rx and time.monotonic() - self._last_rx > self._keepalive * 1.5:
                _LOGGER.debug("IoT Open MQTT keep-alive timeout, reconnecting")
                if self._ws is not None:
                    with contextlib.suppress(Exception):
                        await self._ws.close()
                return
            with contextlib.suppress(IoTOpenConnectionError):
                await self._send(codec.encode_pingreq())
