"""Fixtures for the IoT Open integration tests.

Lynx is mocked at the pyiotopen boundary: ``IoTOpenClient`` is a mock backed by the mutable
``LynxData`` below and ``IoTOpenMqtt`` is replaced by ``FakeMqtt`` so tests can push values,
structure events and connection state changes.
"""

from __future__ import annotations

import copy
from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.iotopen.const import (
    AUTH_API_KEY,
    CONF_ADD_NEW_INSTALLATIONS,
    CONF_AUTH_METHOD,
    CONF_INSTALLATIONS,
    CONF_KNOWN_INSTALLATIONS,
    DOMAIN,
)
from homeassistant.const import CONF_TOKEN, CONF_URL
from homeassistant.core import HomeAssistant
from custom_components.iotopen.pyiotopen import (
    AuthResult,
    ConnectionState,
    Device,
    Function,
    Installation,
    InstallationSnapshot,
    Status,
)

pytest_plugins = "pytest_homeassistant_custom_component"

URL = "https://lynx.iotopen.se"
API_KEY = "test-api-key"

INSTALLATIONS = [
    {"id": 1001, "client_id": 2001, "name": "Test Group A", "organization_id": 1},
    {"id": 1002, "client_id": 2002, "name": "Test Group B", "organization_id": 1},
]
DEVICES = {
    1001: [{"id": 101, "installation_id": 1001, "type": "Gateway", "meta": {"name": "Demo Device 1"}, "updated": 1}],
    1002: [],
}
FUNCTIONS = {
    1001: [
        {"id": 3001, "installation_id": 1001, "type": "device_status", "updated": 1,
         "meta": {"name": "Status", "device_id": "101", "topic_read": "obj/demo/101/device_status", "format": "%d"}},
        {"id": 3002, "installation_id": 1001, "type": "level", "updated": 1,
         "meta": {"name": "Level", "device_id": "101", "topic_read": "obj/demo/101/level"}},
        {"id": 3003, "installation_id": 1001, "type": "velocity", "updated": 1,
         "meta": {"name": "Velocity", "device_id": "101", "topic_read": "obj/demo/101/velocity", "unit": "m/s", "format": "%.2f"}},
        {"id": 3004, "installation_id": 1001, "type": "fault", "updated": 1,
         "meta": {"name": "Fault", "device_id": "101", "topic_read": "obj/demo/101/fault", "state_on": "1", "state_off": "0"}},
        {"id": 3005, "installation_id": 1001, "type": "switch", "updated": 1,
         "meta": {"name": "Light", "device_id": "101", "topic_read": "obj/demo/101/light",
                  "topic_write": "set/obj/demo/101/light", "state_on": "1", "state_off": "0"}},
        {"id": 3010, "installation_id": 1001, "type": "temperature", "updated": 1,
         "meta": {"name": "Utility room", "topic_read": "obj/room/temp", "unit": "C", "format": "%.1f"}},
    ],
    1002: [],
}
STATUS = {
    1001: [
        {"topic": "obj/demo/101/device_status", "value": 0, "timestamp": 1700000000, "msg": "OK"},
        {"topic": "obj/demo/101/level", "value": 3, "timestamp": 1700000000},
        {"topic": "obj/demo/101/velocity", "value": 0.5, "timestamp": 1700000000},
        {"topic": "obj/demo/101/fault", "value": 0, "timestamp": 1700000000},
        {"topic": "obj/demo/101/light", "value": 0, "timestamp": 1700000000},
        {"topic": "obj/room/temp", "value": 21.5, "timestamp": 1700000000},
    ],
    1002: [],
}


class LynxData:
    """Mutable fake server state."""

    def __init__(self) -> None:
        self.installations = copy.deepcopy(INSTALLATIONS)
        self.devices = copy.deepcopy(DEVICES)
        self.functions = copy.deepcopy(FUNCTIONS)
        self.status = copy.deepcopy(STATUS)

    def installation_objects(self) -> list[Installation]:
        return [Installation.from_dict(i) for i in self.installations]

    def snapshot(self, inst: Installation) -> InstallationSnapshot:
        return InstallationSnapshot(
            installation=inst,
            devices={d["id"]: Device.from_dict(d) for d in self.devices.get(inst.id, [])},
            functions={f["id"]: Function.from_dict(f) for f in self.functions.get(inst.id, [])},
            values={s["topic"]: Status.from_dict(s) for s in self.status.get(inst.id, [])},
        )


class FakeMqtt:
    """Stand-in for pyiotopen.IoTOpenMqtt."""

    instances: list[FakeMqtt] = []

    def __init__(self, session: Any, url: str, token: str, *, on_message=None, on_state=None, **_: Any) -> None:
        self.url = url
        self.token = token
        self.on_message = on_message
        self.on_state = on_state
        self.subscriptions: frozenset[str] = frozenset()
        self.published: list[tuple[str, Any]] = []
        self.state = ConnectionState.DISCONNECTED
        self.connect_count = 0
        FakeMqtt.instances.append(self)

    @property
    def connected(self) -> bool:
        return self.state is ConnectionState.CONNECTED

    async def start(self) -> None:
        self.set_state(ConnectionState.CONNECTING)
        self.connect_count += 1
        self.set_state(ConnectionState.CONNECTED)

    async def stop(self) -> None:
        self.state = ConnectionState.STOPPED

    async def set_subscriptions(self, topics) -> None:
        self.subscriptions = frozenset(topics)

    async def publish(self, topic: str, payload: Any, *, retain: bool = False) -> None:
        self.published.append((topic, payload))

    # test helpers
    def set_state(self, state: ConnectionState) -> None:
        self.state = state
        if self.on_state:
            self.on_state(state)

    def reconnect(self) -> None:
        self.set_state(ConnectionState.DISCONNECTED)
        self.connect_count += 1
        self.set_state(ConnectionState.CONNECTED)

    def push(self, topic: str, payload: dict[str, Any]) -> None:
        assert self.on_message is not None
        self.on_message(topic, payload)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Enable loading custom_components/iotopen."""


@pytest.fixture
def lynx() -> LynxData:
    return LynxData()


@pytest.fixture
def mock_client(lynx: LynxData) -> Generator[MagicMock]:
    """Patch IoTOpenClient everywhere it is used."""
    client = MagicMock()
    client.base_url = URL
    client.mqtt_url = "wss://lynx.iotopen.se/mqtt"
    client.api_key = API_KEY
    client.installations = AsyncMock(side_effect=lambda **_: lynx.installation_objects())
    client.snapshot = AsyncMock(side_effect=lynx.snapshot)
    client.me = AsyncMock(return_value={"id": 42, "email": "user@example.com"})
    client.login = AsyncMock(return_value=AuthResult(token="session-token"))
    client.login_2fa = AsyncMock(return_value=AuthResult(token="session-token"))
    with (
        patch("custom_components.iotopen.integration.IoTOpenClient", return_value=client),
        patch("custom_components.iotopen.config_flow.IoTOpenClient", return_value=client),
    ):
        yield client


@pytest.fixture
def mock_mqtt() -> Generator[type[FakeMqtt]]:
    FakeMqtt.instances = []
    with patch("custom_components.iotopen.hub.IoTOpenMqtt", FakeMqtt):
        yield FakeMqtt


@pytest.fixture
def config_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="IoT Open (user@example.com)",
        unique_id="lynx.iotopen.se_42",
        data={CONF_URL: URL, CONF_AUTH_METHOD: AUTH_API_KEY, CONF_TOKEN: API_KEY},
        options={
            CONF_INSTALLATIONS: [1001, 1002],
            CONF_ADD_NEW_INSTALLATIONS: True,
            CONF_KNOWN_INSTALLATIONS: [1001, 1002],
            "import_mode": "all",
            "export_installation": "none",
        },
    )


@pytest.fixture
async def setup_integration(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, mock_mqtt: type[FakeMqtt]
) -> FakeMqtt:
    """Set up the integration and return the fake MQTT connection."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return mock_mqtt.instances[-1]
