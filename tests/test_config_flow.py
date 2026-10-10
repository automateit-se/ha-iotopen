"""Config, reauth and options flow tests."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.iotopen.const import (
    AUTH_API_KEY,
    AUTH_CREDENTIALS,
    CONF_ADD_NEW_INSTALLATIONS,
    CONF_AUTH_METHOD,
    CONF_CODE,
    CONF_INSTALLATIONS,
    CONF_KNOWN_INSTALLATIONS,
    DOMAIN,
)
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_PASSWORD, CONF_TOKEN, CONF_URL, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from custom_components.iotopen.pyiotopen import AuthResult, IoTOpenAuthError, IoTOpenConnectionError

from .conftest import API_KEY, URL, FakeMqtt


@pytest.fixture(autouse=True)
def no_setup():
    """Do not set up the entry after the flow finishes."""
    with (
        patch("custom_components.iotopen.async_setup_entry", return_value=True),
        patch("custom_components.iotopen.async_unload_entry", return_value=True),
    ):
        yield


async def _start(hass: HomeAssistant, method: str) -> dict:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: "lynx.iotopen.se/", CONF_AUTH_METHOD: method}
    )


async def test_api_key_flow(hass: HomeAssistant, mock_client: MagicMock) -> None:
    result = await _start(hass, AUTH_API_KEY)
    assert result["step_id"] == "api_key"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TOKEN: f" {API_KEY} "})
    assert result["step_id"] == "installations"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_INSTALLATIONS: ["1001"], CONF_ADD_NEW_INSTALLATIONS: False}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "IoT Open (user@example.com)"
    assert result["data"] == {CONF_URL: URL, CONF_AUTH_METHOD: AUTH_API_KEY, CONF_TOKEN: API_KEY}
    options = result["options"]
    assert CONF_INSTALLATIONS not in options  # the selection lives in subentries
    assert options["use_subentries"] is True
    assert options[CONF_ADD_NEW_INSTALLATIONS] is False
    assert options[CONF_KNOWN_INSTALLATIONS] == [1001, 1002]
    entry = result["result"]
    assert [(s.subentry_type, s.data["installation_id"], s.title) for s in entry.subentries.values()] == [
        ("installation", 1001, "Test Group A")
    ]
    assert result["result"].unique_id == "lynx.iotopen.se_42"


@pytest.mark.parametrize(
    ("error", "key"),
    [(IoTOpenAuthError("x"), "invalid_auth"), (IoTOpenConnectionError("x"), "cannot_connect")],
)
async def test_api_key_errors(hass: HomeAssistant, mock_client: MagicMock, error: Exception, key: str) -> None:
    result = await _start(hass, AUTH_API_KEY)
    installations = mock_client.installations.side_effect
    mock_client.installations.side_effect = error
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TOKEN: "bad"})
    assert result["errors"] == {"base": key}
    mock_client.installations.side_effect = installations
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TOKEN: API_KEY})
    assert result["step_id"] == "installations"


async def test_invalid_url(hass: HomeAssistant, mock_client: MagicMock) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: "ftp://x", CONF_AUTH_METHOD: AUTH_API_KEY}
    )
    assert result["errors"] == {CONF_URL: "invalid_url"}


async def test_credentials_with_2fa(hass: HomeAssistant, mock_client: MagicMock) -> None:
    mock_client.login.return_value = AuthResult(token="tmp", next_step="totp")
    result = await _start(hass, AUTH_CREDENTIALS)
    assert result["step_id"] == "credentials"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: "user@example.com", CONF_PASSWORD: "secret"}
    )
    assert result["step_id"] == "two_factor"

    mock_client.login_2fa.side_effect = IoTOpenAuthError("bad code")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_CODE: "000000"})
    assert result["errors"] == {"base": "invalid_code"}

    mock_client.login_2fa.side_effect = None
    mock_client.login_2fa.return_value = AuthResult(token="session-token")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_CODE: "123456"})
    assert result["step_id"] == "installations"
    mock_client.login_2fa.assert_awaited_with("tmp", "123456")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_INSTALLATIONS: ["1001", "1002"], CONF_ADD_NEW_INSTALLATIONS: True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_TOKEN] == "session-token"
    assert result["data"][CONF_AUTH_METHOD] == AUTH_CREDENTIALS
    assert result["data"][CONF_USERNAME] == "user@example.com"
    assert CONF_PASSWORD not in result["data"]


async def test_credentials_wrong_password(hass: HomeAssistant, mock_client: MagicMock) -> None:
    mock_client.login.side_effect = IoTOpenAuthError("no")
    result = await _start(hass, AUTH_CREDENTIALS)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: "user@example.com", CONF_PASSWORD: "wrong"}
    )
    assert result["errors"] == {"base": "invalid_auth"}


async def test_no_selection(hass: HomeAssistant, mock_client: MagicMock) -> None:
    result = await _start(hass, AUTH_API_KEY)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TOKEN: API_KEY})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_INSTALLATIONS: [], CONF_ADD_NEW_INSTALLATIONS: True}
    )
    assert result["errors"] == {"base": "no_selection"}


async def test_already_configured(
    hass: HomeAssistant, mock_client: MagicMock, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    result = await _start(hass, AUTH_API_KEY)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TOKEN: API_KEY})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_no_installations(hass: HomeAssistant, mock_client: MagicMock) -> None:
    mock_client.installations.side_effect = lambda **_: []
    result = await _start(hass, AUTH_API_KEY)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TOKEN: API_KEY})
    assert result["reason"] == "no_installations"


async def test_reauth(hass: HomeAssistant, mock_client: MagicMock, config_entry: MockConfigEntry) -> None:
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_AUTH_METHOD: AUTH_API_KEY})
    assert result["step_id"] == "api_key"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TOKEN: "new-key"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_TOKEN] == "new-key"


async def test_reauth_wrong_account(
    hass: HomeAssistant, mock_client: MagicMock, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    mock_client.me.return_value = {"id": 99}
    result = await config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_AUTH_METHOD: AUTH_API_KEY})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TOKEN: "other"})
    assert result["reason"] == "wrong_account"


async def test_options_flow(
    hass: HomeAssistant, mock_client: MagicMock, mock_mqtt: type[FakeMqtt], config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_INSTALLATIONS: ["1002"], CONF_ADD_NEW_INSTALLATIONS: False}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options[CONF_INSTALLATIONS] == [1002]
    assert config_entry.options[CONF_ADD_NEW_INSTALLATIONS] is False
    assert config_entry.options[CONF_KNOWN_INSTALLATIONS] == [1001, 1002]
