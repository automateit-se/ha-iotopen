"""Minimal MQTT 3.1.1 packet encoder/decoder (pure Python, no I/O).

Only what an IoT Open client needs: CONNECT/CONNACK, SUBSCRIBE/SUBACK, UNSUBSCRIBE/UNSUBACK,
PUBLISH (QoS 0 out, QoS 0/1 in), PUBACK, PINGREQ/PINGRESP and DISCONNECT.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass

CONNECT = 1
CONNACK = 2
PUBLISH = 3
PUBACK = 4
SUBSCRIBE = 8
SUBACK = 9
UNSUBSCRIBE = 10
UNSUBACK = 11
PINGREQ = 12
PINGRESP = 13
DISCONNECT = 14

CONNACK_ACCEPTED = 0
CONNACK_BAD_PROTOCOL = 1
CONNACK_ID_REJECTED = 2
CONNACK_SERVER_UNAVAILABLE = 3
CONNACK_BAD_CREDENTIALS = 4
CONNACK_NOT_AUTHORIZED = 5

MAX_REMAINING_LENGTH = 268_435_455


class MqttProtocolError(Exception):
    """Malformed MQTT data."""


def _utf8(value: str) -> bytes:
    data = value.encode("utf-8")
    if len(data) > 0xFFFF:
        raise ValueError("MQTT string too long")
    return len(data).to_bytes(2, "big") + data


def encode_remaining_length(length: int) -> bytes:
    """Variable-length "remaining length" field."""
    if length < 0 or length > MAX_REMAINING_LENGTH:
        raise ValueError("remaining length out of range")
    out = bytearray()
    while True:
        digit = length % 128
        length //= 128
        if length:
            digit |= 0x80
        out.append(digit)
        if not length:
            return bytes(out)


def _packet(first_byte: int, body: bytes) -> bytes:
    return bytes([first_byte]) + encode_remaining_length(len(body)) + body


def encode_connect(
    client_id: str,
    username: str | None = None,
    password: str | None = None,
    keepalive: int = 60,
    clean_session: bool = True,
) -> bytes:
    """CONNECT packet (protocol level 4 = MQTT 3.1.1)."""
    flags = 0x02 if clean_session else 0
    payload = _utf8(client_id)
    if username is not None:
        flags |= 0x80
        payload += _utf8(username)
    if password is not None:
        flags |= 0x40
        payload += _utf8(password)
    variable = _utf8("MQTT") + bytes([4, flags]) + int(keepalive).to_bytes(2, "big")
    return _packet(CONNECT << 4, variable + payload)


def encode_subscribe(packet_id: int, topics: Iterable[str], qos: int = 0) -> bytes:
    """SUBSCRIBE packet for one or more topic filters."""
    body = packet_id.to_bytes(2, "big")
    count = 0
    for topic in topics:
        body += _utf8(topic) + bytes([qos & 3])
        count += 1
    if not count:
        raise ValueError("SUBSCRIBE needs at least one topic")
    return _packet((SUBSCRIBE << 4) | 0x02, body)


def encode_unsubscribe(packet_id: int, topics: Iterable[str]) -> bytes:
    """UNSUBSCRIBE packet."""
    body = packet_id.to_bytes(2, "big")
    count = 0
    for topic in topics:
        body += _utf8(topic)
        count += 1
    if not count:
        raise ValueError("UNSUBSCRIBE needs at least one topic")
    return _packet((UNSUBSCRIBE << 4) | 0x02, body)


def encode_publish(
    topic: str, payload: bytes, *, qos: int = 0, retain: bool = False, packet_id: int = 0
) -> bytes:
    """PUBLISH packet (QoS 0 or 1)."""
    if qos not in (0, 1):
        raise ValueError("only QoS 0 and 1 are supported")
    first = (PUBLISH << 4) | (qos << 1) | (1 if retain else 0)
    body = _utf8(topic)
    if qos:
        body += packet_id.to_bytes(2, "big")
    return _packet(first, body + payload)


def encode_puback(packet_id: int) -> bytes:
    """PUBACK packet."""
    return _packet(PUBACK << 4, packet_id.to_bytes(2, "big"))


def encode_pingreq() -> bytes:
    """PINGREQ packet."""
    return bytes([PINGREQ << 4, 0])


def encode_disconnect() -> bytes:
    """DISCONNECT packet."""
    return bytes([DISCONNECT << 4, 0])


@dataclass(slots=True, frozen=True)
class Packet:
    """A decoded fixed header plus raw body."""

    type: int
    flags: int
    body: bytes


@dataclass(slots=True, frozen=True)
class PublishPacket:
    """Decoded PUBLISH."""

    topic: str
    payload: bytes
    qos: int
    retain: bool
    packet_id: int | None


def decode_publish(packet: Packet) -> PublishPacket:
    """Decode the body of a PUBLISH packet."""
    body = packet.body
    if len(body) < 2:
        raise MqttProtocolError("PUBLISH too short")
    topic_len = int.from_bytes(body[0:2], "big")
    offset = 2 + topic_len
    if len(body) < offset:
        raise MqttProtocolError("PUBLISH topic truncated")
    topic = body[2:offset].decode("utf-8", errors="replace")
    qos = (packet.flags >> 1) & 3
    packet_id = None
    if qos:
        if len(body) < offset + 2:
            raise MqttProtocolError("PUBLISH packet id truncated")
        packet_id = int.from_bytes(body[offset : offset + 2], "big")
        offset += 2
    return PublishPacket(
        topic=topic,
        payload=bytes(body[offset:]),
        qos=qos,
        retain=bool(packet.flags & 1),
        packet_id=packet_id,
    )


def decode_connack(packet: Packet) -> int:
    """Return the CONNACK return code."""
    if len(packet.body) < 2:
        raise MqttProtocolError("CONNACK too short")
    return packet.body[1]


class PacketReader:
    """Reassembles MQTT packets from an arbitrary chunked byte stream (e.g. WebSocket frames)."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> Iterator[Packet]:
        """Add bytes and yield every complete packet."""
        self._buffer.extend(data)
        while True:
            if len(self._buffer) < 2:
                return
            multiplier = 1
            length = 0
            index = 1
            while True:
                if index >= len(self._buffer):
                    return  # remaining length not complete yet
                byte = self._buffer[index]
                index += 1
                length += (byte & 0x7F) * multiplier
                if not byte & 0x80:
                    break
                multiplier *= 128
                if multiplier > 128**3:
                    raise MqttProtocolError("malformed remaining length")
            end = index + length
            if len(self._buffer) < end:
                return
            first = self._buffer[0]
            body = bytes(self._buffer[index:end])
            del self._buffer[:end]
            yield Packet(type=first >> 4, flags=first & 0x0F, body=body)

    def reset(self) -> None:
        """Drop any buffered partial packet."""
        self._buffer.clear()
