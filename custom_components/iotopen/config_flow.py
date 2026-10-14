"""Config flow for IoT Open.

user -> (api_key | credentials [-> two_factor]) -> installations
reauth -> reauth_confirm -> (api_key | credentials [-> two_factor]) -> update entry
options -> choose installations
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import voluptuous as vol

from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigSubentryFlow,
    SubentryFlowResult,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_PASSWORD, CONF_TOKEN, CONF_URL, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers import instance_id
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import slugify
from homeassistant.helpers.selector import (
    BooleanSelector,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .pyiotopen import (
    Installation,
    IoTOpenAuthError,
    IoTOpenClient,
    IoTOpenConnectionError,
    IoTOpenError,
    normalize_base_url,
)

from .const import (
    AUTH_API_KEY,
    AUTH_CREDENTIALS,
    CONF_ADD_NEW_INSTALLATIONS,
    CONF_AUTH_METHOD,
    CONF_CODE,
    CONF_EXPORT_CATEGORIZED,
    CONF_EXPORT_CONTROL,
    CONF_IMPORT_MODE,
    CONF_INSTALLATION_ID,
    CONF_USE_SUBENTRIES,
    SUBENTRY_INSTALLATION,
    CONF_INSTANCE_NAME,
    DEFAULT_IMPORT_MODE,
    IMPORT_ALL,
    IMPORT_TAGGED,
    CONF_EXPORT_INSTALLATION,
    CONF_EXPORT_LABEL,
    DEFAULT_EXPORT_LABEL,
    EXPORT_NONE,
    CONF_INSTALLATIONS,
    CONF_KNOWN_INSTALLATIONS,
    DEFAULT_URL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

AUTH_METHOD_SELECTOR = SelectSelector(
    SelectSelectorConfig(
        options=[AUTH_API_KEY, AUTH_CREDENTIALS],
        translation_key=CONF_AUTH_METHOD,
        mode=SelectSelectorMode.LIST,
    )
)
PASSWORD_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


def _installation_selector(installations: list[Installation]) -> SelectSelector:
    options = [
        SelectOptionDict(value=str(i.id), label=f"{i.name} ({i.id})")
        for i in sorted(installations, key=lambda x: x.name.lower())
    ]
    return SelectSelector(
        SelectSelectorConfig(options=options, multiple=True, mode=SelectSelectorMode.LIST)
    )


def _installations_schema(installations: list[Installation], selected: list[int], add_new: bool) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_INSTALLATIONS, default=[str(i) for i in selected]): _installation_selector(
                installations
            ),
            vol.Required(CONF_ADD_NEW_INSTALLATIONS, default=add_new): BooleanSelector(),
        }
    )


class IoTOpenConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for IoT Open."""

    VERSION = 1
    MINOR_VERSION = 1

    def __init__(self) -> None:
        self._url: str = DEFAULT_URL
        self._auth_method: str = AUTH_API_KEY
        self._token: str | None = None
        self._temporary_token: str | None = None
        self._username: str | None = None
        self._user: dict[str, Any] = {}
        self._installations: list[Installation] = []

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: Any) -> IoTOpenOptionsFlow:
        """Options flow."""
        return IoTOpenOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """One subentry per imported IoT Open installation."""
        return {SUBENTRY_INSTALLATION: InstallationSubentryFlow}

    def _client(self, token: str | None = None) -> IoTOpenClient:
        return IoTOpenClient(async_get_clientsession(self.hass), self._url, token)

    # ------------------------------------------------------------------ user
    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Server and authentication method."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                self._url = normalize_base_url(user_input[CONF_URL])
            except ValueError:
                errors[CONF_URL] = "invalid_url"
            else:
                self._auth_method = user_input[CONF_AUTH_METHOD]
                return await self._async_step_auth()
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(
                    {
                        vol.Required(CONF_URL): TextSelector(TextSelectorConfig(type=TextSelectorType.URL)),
                        vol.Required(CONF_AUTH_METHOD): AUTH_METHOD_SELECTOR,
                    }
                ),
                {CONF_URL: self._url, CONF_AUTH_METHOD: self._auth_method, **(user_input or {})},
            ),
            errors=errors,
            description_placeholders={"default_url": DEFAULT_URL},
        )

    async def _async_step_auth(self) -> ConfigFlowResult:
        if self._auth_method == AUTH_CREDENTIALS:
            return await self.async_step_credentials()
        return await self.async_step_api_key()

    # ------------------------------------------------------------------ api key
    async def async_step_api_key(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """API key (recommended: it does not expire)."""
        errors: dict[str, str] = {}
        if user_input is not None:
            token = user_input[CONF_TOKEN].strip()
            if (error := await self._async_validate_token(token)) is None:
                self._auth_method = AUTH_API_KEY
                return await self._async_after_auth()
            errors["base"] = error
        return self.async_show_form(
            step_id="api_key",
            data_schema=vol.Schema({vol.Required(CONF_TOKEN): PASSWORD_SELECTOR}),
            errors=errors,
            description_placeholders={"url": self._url},
        )

    # ------------------------------------------------------------------ credentials
    async def async_step_credentials(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Username/password; the password is only used to obtain a token and is never stored."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._username = user_input[CONF_USERNAME].strip()
            client = self._client()
            try:
                result = await client.login(self._username, user_input[CONF_PASSWORD])
            except IoTOpenAuthError:
                errors["base"] = "invalid_auth"
            except IoTOpenConnectionError:
                errors["base"] = "cannot_connect"
            except IoTOpenError:
                _LOGGER.exception("Unexpected response during login")
                errors["base"] = "unknown"
            else:
                if result.needs_second_factor:
                    self._temporary_token = result.token
                    return await self.async_step_two_factor()
                if (error := await self._async_validate_token(result.token)) is None:
                    self._auth_method = AUTH_CREDENTIALS
                    return await self._async_after_auth()
                errors["base"] = error
        return self.async_show_form(
            step_id="credentials",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(
                    {
                        vol.Required(CONF_USERNAME): TextSelector(
                            TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
                        ),
                        vol.Required(CONF_PASSWORD): TextSelector(
                            TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
                        ),
                    }
                ),
                {CONF_USERNAME: self._username or ""},
            ),
            errors=errors,
            description_placeholders={"url": self._url},
        )

    async def async_step_two_factor(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Second factor (e.g. TOTP code)."""
        errors: dict[str, str] = {}
        if user_input is not None and self._temporary_token:
            client = self._client()
            try:
                result = await client.login_2fa(self._temporary_token, user_input[CONF_CODE])
            except IoTOpenAuthError:
                errors["base"] = "invalid_code"
            except IoTOpenConnectionError:
                errors["base"] = "cannot_connect"
            except IoTOpenError:
                _LOGGER.exception("Unexpected response during 2FA")
                errors["base"] = "unknown"
            else:
                if (error := await self._async_validate_token(result.token)) is None:
                    self._auth_method = AUTH_CREDENTIALS
                    return await self._async_after_auth()
                errors["base"] = error
        return self.async_show_form(
            step_id="two_factor",
            data_schema=vol.Schema(
                {vol.Required(CONF_CODE): TextSelector(TextSelectorConfig(autocomplete="one-time-code"))}
            ),
            errors=errors,
        )

    # ------------------------------------------------------------------ shared
    async def _async_validate_token(self, token: str) -> str | None:
        """Check a token; stores token, user and installations. Returns an error key or None."""
        client = self._client(token)
        try:
            installations = await client.installations()
        except IoTOpenAuthError:
            return "invalid_auth"
        except IoTOpenConnectionError:
            return "cannot_connect"
        except IoTOpenError:
            _LOGGER.exception("Unexpected response while validating token")
            return "unknown"
        try:
            self._user = await client.me()
        except IoTOpenError:
            self._user = {}  # some API keys may not read /user/me; not fatal
        self._token = token
        self._installations = installations
        return None

    def _unique_id(self) -> str:
        host = urlsplit(self._url).netloc
        if user_id := self._user.get("id"):
            return f"{host}_{user_id}"
        return f"{host}_{'-'.join(str(i.id) for i in sorted(self._installations, key=lambda x: x.id))}"

    async def _async_after_auth(self) -> ConfigFlowResult:
        if self.source == SOURCE_REAUTH:
            if self._user.get("id"):
                # only comparable when the account id is known (see _unique_id fallback)
                await self.async_set_unique_id(self._unique_id())
                self._abort_if_unique_id_mismatch(reason="wrong_account")
            updates: dict[str, Any] = {CONF_TOKEN: self._token, CONF_AUTH_METHOD: self._auth_method}
            if self._auth_method == AUTH_CREDENTIALS and self._username:
                updates[CONF_USERNAME] = self._username
            return self.async_update_reload_and_abort(self._get_reauth_entry(), data_updates=updates)
        await self.async_set_unique_id(self._unique_id())
        self._abort_if_unique_id_configured()
        if not self._installations:
            return self.async_abort(reason="no_installations")
        return await self.async_step_installations()

    async def async_step_installations(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Choose which installations to add."""
        errors: dict[str, str] = {}
        if user_input is not None:
            selected = sorted(int(i) for i in user_input[CONF_INSTALLATIONS])
            if not selected:
                errors["base"] = "no_selection"
            else:
                title = self._user.get("email") or urlsplit(self._url).netloc
                data = {CONF_URL: self._url, CONF_AUTH_METHOD: self._auth_method, CONF_TOKEN: self._token}
                if self._username:
                    data[CONF_USERNAME] = self._username
                by_id = {i.id: i for i in self._installations}
                return self.async_create_entry(
                    title=f"IoT Open ({title})",
                    data=data,
                    subentries=[
                        {
                            "data": {CONF_INSTALLATION_ID: iid},
                            "subentry_type": SUBENTRY_INSTALLATION,
                            "title": by_id[iid].name if iid in by_id else f"Installation {iid}",
                            "unique_id": str(iid),
                        }
                        for iid in selected
                    ],
                    options={
                        CONF_USE_SUBENTRIES: True,
                        CONF_ADD_NEW_INSTALLATIONS: user_input[CONF_ADD_NEW_INSTALLATIONS],
                        CONF_KNOWN_INSTALLATIONS: sorted(i.id for i in self._installations),
                        CONF_IMPORT_MODE: DEFAULT_IMPORT_MODE,
                        CONF_INSTANCE_NAME: slugify(self.hass.config.location_name or "home") or "home",
                    },
                )
        return self.async_show_form(
            step_id="installations",
            data_schema=_installations_schema(
                self._installations, [i.id for i in self._installations], add_new=True
            ),
            errors=errors,
        )

    # ------------------------------------------------------------------ reauth
    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Token or API key rejected."""
        self._url = entry_data[CONF_URL]
        self._auth_method = entry_data.get(CONF_AUTH_METHOD, AUTH_API_KEY)
        self._username = entry_data.get(CONF_USERNAME)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Choose how to sign in again."""
        if user_input is not None:
            self._auth_method = user_input[CONF_AUTH_METHOD]
            return await self._async_step_auth()
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema({vol.Required(CONF_AUTH_METHOD): AUTH_METHOD_SELECTOR}),
                {CONF_AUTH_METHOD: self._auth_method},
            ),
            description_placeholders={"url": self._url},
        )


class IoTOpenOptionsFlow(OptionsFlow):
    """Change which installations are included."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Installation selection."""
        errors: dict[str, str] = {}
        entry = self.config_entry
        client = IoTOpenClient(
            async_get_clientsession(self.hass), entry.data[CONF_URL], entry.data[CONF_TOKEN]
        )
        try:
            installations = await client.installations()
        except IoTOpenAuthError:
            return self.async_abort(reason="invalid_auth")
        except IoTOpenError:
            return self.async_abort(reason="cannot_connect")

        use_subentries = bool(entry.options.get(CONF_USE_SUBENTRIES))
        if user_input is not None:
            selected = (
                [] if use_subentries else sorted(int(i) for i in user_input[CONF_INSTALLATIONS])
            )
            if not use_subentries and not selected:
                errors["base"] = "no_selection"
            else:
                data = dict(entry.options)
                if not use_subentries:
                    data[CONF_INSTALLATIONS] = selected
                return self.async_create_entry(
                    data=data
                    | {
                        CONF_ADD_NEW_INSTALLATIONS: user_input[CONF_ADD_NEW_INSTALLATIONS],
                        CONF_KNOWN_INSTALLATIONS: sorted(i.id for i in installations),
                        CONF_EXPORT_INSTALLATION: user_input[CONF_EXPORT_INSTALLATION],
                        CONF_EXPORT_LABEL: user_input[CONF_EXPORT_LABEL].strip() or DEFAULT_EXPORT_LABEL,
                        CONF_EXPORT_CONTROL: user_input[CONF_EXPORT_CONTROL],
                        CONF_EXPORT_CATEGORIZED: user_input[CONF_EXPORT_CATEGORIZED],
                        CONF_INSTANCE_NAME: slugify(user_input[CONF_INSTANCE_NAME]) or "home",
                        CONF_IMPORT_MODE: user_input[CONF_IMPORT_MODE],
                    }
                )
        if use_subentries:
            selected_now = [
                int(s.data[CONF_INSTALLATION_ID])
                for s in entry.subentries.values()
                if s.subentry_type == SUBENTRY_INSTALLATION
            ]
            base = vol.Schema(
                {
                    vol.Required(
                        CONF_ADD_NEW_INSTALLATIONS,
                        default=entry.options.get(CONF_ADD_NEW_INSTALLATIONS, True),
                    ): BooleanSelector()
                }
            )
        else:
            selected_now = [int(i) for i in entry.options.get(CONF_INSTALLATIONS, [])]
            base = _installations_schema(
                installations, selected_now, entry.options.get(CONF_ADD_NEW_INSTALLATIONS, True)
            )
        export_default = entry.options.get(CONF_EXPORT_INSTALLATION)
        if export_default is None:
            export_default = str(selected_now[0]) if len(selected_now) == 1 else EXPORT_NONE
        schema = base.extend(
            {
                vol.Required(CONF_EXPORT_INSTALLATION, default=str(export_default)): SelectSelector(
                    SelectSelectorConfig(
                        options=[SelectOptionDict(value=EXPORT_NONE, label="-")]
                        + [
                            SelectOptionDict(value=str(i.id), label=f"{i.name} ({i.id})")
                            for i in sorted(installations, key=lambda x: x.name.lower())
                        ],
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required(
                    CONF_EXPORT_LABEL, default=entry.options.get(CONF_EXPORT_LABEL, DEFAULT_EXPORT_LABEL)
                ): TextSelector(),
                vol.Required(
                    CONF_EXPORT_CONTROL, default=entry.options.get(CONF_EXPORT_CONTROL, True)
                ): BooleanSelector(),
                vol.Required(
                    CONF_EXPORT_CATEGORIZED, default=entry.options.get(CONF_EXPORT_CATEGORIZED, False)
                ): BooleanSelector(),
                vol.Required(
                    CONF_INSTANCE_NAME,
                    default=entry.options.get(CONF_INSTANCE_NAME)
                    or slugify(self.hass.config.location_name or "home")
                    or "home",
                ): TextSelector(),
                vol.Required(
                    CONF_IMPORT_MODE, default=entry.options.get(CONF_IMPORT_MODE, DEFAULT_IMPORT_MODE)
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=[IMPORT_TAGGED, IMPORT_ALL],
                        translation_key=CONF_IMPORT_MODE,
                        mode=SelectSelectorMode.LIST,
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            errors=errors,
            description_placeholders={"instance_id": await instance_id.async_get(self.hass)},
        )


class InstallationSubentryFlow(ConfigSubentryFlow):
    """Add another IoT Open installation to import from."""

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Pick an installation that is not added yet."""
        entry = self._get_entry()
        client = IoTOpenClient(
            async_get_clientsession(self.hass), entry.data[CONF_URL], entry.data[CONF_TOKEN]
        )
        try:
            installations = await client.installations()
        except IoTOpenAuthError:
            return self.async_abort(reason="invalid_auth")
        except IoTOpenError:
            return self.async_abort(reason="cannot_connect")
        added = {
            str(s.data.get(CONF_INSTALLATION_ID))
            for s in entry.subentries.values()
            if s.subentry_type == SUBENTRY_INSTALLATION
        }
        available = [i for i in installations if str(i.id) not in added]
        if not available:
            return self.async_abort(reason="no_more_installations")
        if user_input is not None:
            iid = int(user_input[CONF_INSTALLATION_ID])
            inst = next((i for i in available if i.id == iid), None)
            if inst is not None:
                return self.async_create_entry(
                    title=inst.name, data={CONF_INSTALLATION_ID: iid}, unique_id=str(iid)
                )
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_INSTALLATION_ID): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(value=str(i.id), label=f"{i.name} ({i.id})")
                                for i in sorted(available, key=lambda x: x.name.lower())
                            ],
                            mode=SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
        )
