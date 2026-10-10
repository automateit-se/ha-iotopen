"""Async REST client for the IoT Open (Lynx) API."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import aiohttp

from .const import API_KEY_HEADER, DEFAULT_BASE_URL, DEFAULT_TIMEOUT
from .exceptions import (
    IoTOpenAuthError,
    IoTOpenConnectionError,
    IoTOpenForbiddenError,
    IoTOpenNotFoundError,
    IoTOpenResponseError,
    IoTOpenTwoFactorRequired,
)
from .models import AuthResult, Device, Function, Installation, InstallationSnapshot, Status
from .topics import normalize_topic

_LOGGER = logging.getLogger(__name__)

ParamValue = str | int | float | bool | None | Iterable[str | int | float]


def normalize_base_url(url: str | None) -> str:
    """Return ``scheme://host[:port][/path]`` without trailing slash; ``https`` is assumed."""
    url = (url or DEFAULT_BASE_URL).strip()
    if "://" not in url:
        url = f"https://{url}"
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"Invalid IoT Open URL: {url!r}")
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def mqtt_url_for(base_url: str) -> str:
    """WebSocket MQTT endpoint for a Lynx base URL, e.g. ``wss://lynx.iotopen.se/mqtt``."""
    parts = urlsplit(normalize_base_url(base_url))
    scheme = "wss" if parts.scheme == "https" else "ws"
    return urlunsplit((scheme, parts.netloc, f"{parts.path}/mqtt", "", ""))


def _params(params: Mapping[str, ParamValue] | None) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for key, value in (params or {}).items():
        if value is None:
            continue
        if isinstance(value, bool):
            out.append((key, "true" if value else "false"))
        elif isinstance(value, (str, int, float)):
            out.append((key, str(value)))
        else:
            out.extend((key, str(v)) for v in value)
    return out


class IoTOpenClient:
    """Thin async wrapper around the Lynx REST API.

    The client never owns the :class:`aiohttp.ClientSession`; pass the one your application uses
    (in Home Assistant: ``async_get_clientsession(hass)``).
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str = DEFAULT_BASE_URL,
        api_key: str | None = None,
        *,
        request_timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._session = session
        self._base_url = normalize_base_url(base_url)
        self._api_key = api_key
        self._timeout = request_timeout

    # ------------------------------------------------------------------ properties
    @property
    def session(self) -> aiohttp.ClientSession:
        """The aiohttp session used for all requests."""
        return self._session

    @property
    def base_url(self) -> str:
        """Normalised base URL, e.g. ``https://lynx.iotopen.se``."""
        return self._base_url

    @property
    def mqtt_url(self) -> str:
        """WebSocket MQTT URL for this server."""
        return mqtt_url_for(self._base_url)

    @property
    def api_key(self) -> str | None:
        """The token or API key sent as ``X-API-Key``."""
        return self._api_key

    @api_key.setter
    def api_key(self, value: str | None) -> None:
        self._api_key = value

    # ------------------------------------------------------------------ transport
    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, ParamValue] | None = None,
        json_body: Any = None,
        headers: Mapping[str, str] | None = None,
        auth: aiohttp.BasicAuth | None = None,
        use_api_key: bool = True,
    ) -> Any:
        """Perform a request and return the decoded JSON body (``None`` for empty bodies)."""
        hdrs: dict[str, str] = {"Accept": "application/json"}
        if use_api_key and self._api_key:
            hdrs[API_KEY_HEADER] = self._api_key
        if headers:
            hdrs.update(headers)
        url = f"{self._base_url}{path}"
        try:
            async with asyncio.timeout(self._timeout):
                async with self._session.request(
                    method,
                    url,
                    params=_params(params),
                    json=json_body,
                    headers=hdrs,
                    auth=auth,
                ) as resp:
                    text = await resp.text()
                    status = resp.status
        except TimeoutError as err:
            raise IoTOpenConnectionError(f"Timeout talking to {self._base_url}") from err
        except aiohttp.ClientError as err:
            raise IoTOpenConnectionError(f"Error talking to {self._base_url}: {err}") from err

        body: Any = None
        if text:
            try:
                body = json.loads(text)
            except ValueError:
                body = text

        if status >= 400:
            message = ""
            if isinstance(body, dict):
                message = str(body.get("message") or body.get("error") or "")
            elif isinstance(body, str):
                message = body[:200]
            message = message or f"{method} {path} failed"
            if status == 401:
                raise IoTOpenAuthError(message)
            if status == 403:
                raise IoTOpenForbiddenError(status, message, body)
            if status == 404:
                raise IoTOpenNotFoundError(status, message, body)
            raise IoTOpenResponseError(status, message, body)
        return body

    # ------------------------------------------------------------------ auth
    async def login(self, username: str, password: str) -> AuthResult:
        """Log in with username/password.

        Returns an :class:`AuthResult`. When ``result.needs_second_factor`` is true the token is
        temporary and must be exchanged with :meth:`login_2fa`. Use :meth:`authenticate` if you
        prefer an exception for that case.
        """
        data = await self.request(
            "POST",
            "/api/v2/auth",
            auth=aiohttp.BasicAuth(username, password),
            use_api_key=False,
        )
        if not isinstance(data, dict) or not data.get("token"):
            raise IoTOpenAuthError("Login response did not contain a token")
        return AuthResult.from_dict(data)

    async def login_2fa(self, temporary_token: str, challenge: str) -> AuthResult:
        """Complete a two-factor login with the challenge (e.g. TOTP code)."""
        data = await self.request(
            "PUT",
            "/api/v2/auth",
            json_body={"challenge": str(challenge).strip()},
            headers={API_KEY_HEADER: temporary_token},
            use_api_key=False,
        )
        if not isinstance(data, dict) or not data.get("token"):
            raise IoTOpenAuthError("2FA response did not contain a token")
        result = AuthResult.from_dict(data)
        if result.needs_second_factor:
            raise IoTOpenTwoFactorRequired(result.token, result.next_step)
        return result

    async def authenticate(self, username: str, password: str) -> str:
        """Log in and store the token; raises :class:`IoTOpenTwoFactorRequired` when 2FA is needed."""
        result = await self.login(username, password)
        if result.needs_second_factor:
            raise IoTOpenTwoFactorRequired(result.token, result.next_step)
        self._api_key = result.token
        return result.token

    async def logout(self) -> None:
        """Invalidate the current session token (do not call this for API keys)."""
        await self.request("DELETE", "/api/v2/auth")

    # ------------------------------------------------------------------ users
    async def me(self) -> dict[str, Any]:
        """The user the token belongs to."""
        data = await self.request("GET", "/api/v2/user/me")
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------------ installations
    async def installations(self, *, assigned: bool = False) -> list[Installation]:
        """Installations the token can access (``/api/v2/installationinfo``)."""
        data = await self.request("GET", "/api/v2/installationinfo", params={"assigned": assigned})
        return [Installation.from_dict(i) for i in (data or []) if isinstance(i, dict) and "id" in i]

    # ------------------------------------------------------------------ devices & functions
    async def devices(
        self, installation_id: int, *, filters: Mapping[str, ParamValue] | None = None
    ) -> list[Device]:
        """All devices in an installation."""
        data = await self.request("GET", f"/api/v2/devicex/{installation_id}", params=filters)
        return [Device.from_dict(d) for d in (data or []) if isinstance(d, dict)]

    async def functions(
        self, installation_id: int, *, filters: Mapping[str, ParamValue] | None = None
    ) -> list[Function]:
        """All functions in an installation."""
        data = await self.request("GET", f"/api/v2/functionx/{installation_id}", params=filters)
        return [Function.from_dict(f) for f in (data or []) if isinstance(f, dict)]

    async def create_device(
        self, installation_id: int, type_: str, meta: Mapping[str, str], protected_meta: Mapping[str, str] | None = None
    ) -> Device:
        """Create a device (``POST /api/v2/devicex/{installation_id}``)."""
        data = await self.request(
            "POST",
            f"/api/v2/devicex/{installation_id}",
            json_body={
                "installation_id": installation_id,
                "type": type_,
                "meta": dict(meta),
                "protected_meta": dict(protected_meta or {}),
            },
        )
        return Device.from_dict(data)

    async def update_device(self, device: Device, *, type_: str | None = None, meta: Mapping[str, str] | None = None) -> Device:
        """Replace type and/or meta of a device (``PUT``); other fields are kept."""
        body = {
            "id": device.id,
            "installation_id": device.installation_id,
            "type": type_ if type_ is not None else device.type,
            "meta": dict(meta if meta is not None else device.meta),
            "protected_meta": dict(device.protected_meta),
        }
        data = await self.request("PUT", f"/api/v2/devicex/{device.installation_id}/{device.id}", json_body=body)
        return Device.from_dict(data) if isinstance(data, dict) and "id" in data else device

    async def delete_device(self, installation_id: int, device_id: int) -> None:
        """Delete a device."""
        await self.request("DELETE", f"/api/v2/devicex/{installation_id}/{device_id}")

    async def create_function(
        self, installation_id: int, type_: str, meta: Mapping[str, str], protected_meta: Mapping[str, str] | None = None
    ) -> Function:
        """Create a function (``POST /api/v2/functionx/{installation_id}``)."""
        data = await self.request(
            "POST",
            f"/api/v2/functionx/{installation_id}",
            json_body={
                "installation_id": installation_id,
                "type": type_,
                "meta": dict(meta),
                "protected_meta": dict(protected_meta or {}),
            },
        )
        return Function.from_dict(data)

    async def update_function(
        self, function: Function, *, type_: str | None = None, meta: Mapping[str, str] | None = None
    ) -> Function:
        """Replace type and/or meta of a function (``PUT``); other fields are kept."""
        body = {
            "id": function.id,
            "installation_id": function.installation_id,
            "type": type_ if type_ is not None else function.type,
            "meta": dict(meta if meta is not None else function.meta),
            "protected_meta": dict(function.protected_meta),
        }
        data = await self.request(
            "PUT", f"/api/v2/functionx/{function.installation_id}/{function.id}", json_body=body
        )
        return Function.from_dict(data) if isinstance(data, dict) and "id" in data else function

    async def delete_function(self, installation_id: int, function_id: int) -> None:
        """Delete a function."""
        await self.request("DELETE", f"/api/v2/functionx/{installation_id}/{function_id}")

    async def set_function_meta(
        self,
        installation_id: int,
        function_id: int,
        key: str,
        value: str,
        *,
        protected: bool = False,
    ) -> Any:
        """Create or update one metadata key on a function."""
        return await self.request(
            "PUT",
            f"/api/v2/functionx/{installation_id}/{function_id}/meta/{quote(key, safe='')}",
            params={"create_missing": True},
            json_body={"value": value, "protected": protected},
        )

    async def set_device_meta(
        self,
        installation_id: int,
        device_id: int,
        key: str,
        value: str,
        *,
        protected: bool = False,
    ) -> Any:
        """Create or update one metadata key on a device."""
        return await self.request(
            "PUT",
            f"/api/v2/devicex/{installation_id}/{device_id}/meta/{quote(key, safe='')}",
            params={"create_missing": True},
            json_body={"value": value, "protected": protected},
        )

    # ------------------------------------------------------------------ values
    async def status(
        self, installation_id: int, topics: Iterable[str] | None = None
    ) -> list[Status]:
        """Latest value per topic (topics returned in bare form)."""
        params: dict[str, ParamValue] = {}
        if topics is not None:
            params["topics"] = [t for t in (normalize_topic(x) for x in topics) if t]
        data = await self.request("GET", f"/api/v2/status/{installation_id}", params=params)
        return [Status.from_dict(s) for s in (data or []) if isinstance(s, dict)]

    async def log(
        self,
        installation_id: int,
        *,
        start: float | None = None,
        end: float | None = None,
        limit: int = 500,
        offset: int = 0,
        order: str = "desc",
        topics: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """History (``/api/v3beta/log``). Topics in ``data`` are normalised to the bare form."""
        now = time.time()
        params: dict[str, ParamValue] = {
            "from": start if start is not None else now - 86400,
            "to": end if end is not None else now,
            "limit": limit,
            "offset": offset,
            "order": order,
        }
        if topics is not None:
            # the log API stores topics with the client_id prefix but accepts bare topics as filter
            params["topics"] = [t for t in (normalize_topic(x) for x in topics) if t]
        data = await self.request("GET", f"/api/v3beta/log/{installation_id}", params=params)
        if not isinstance(data, dict):
            return {"data": []}
        entries = data.get("data") or []
        data["data"] = [
            {**e, "topic": normalize_topic(e.get("topic"))} for e in entries if isinstance(e, dict)
        ]
        return data

    # ------------------------------------------------------------------ convenience
    async def snapshot(self, installation: Installation) -> InstallationSnapshot:
        """Read devices, functions and latest values of one installation concurrently.

        A failing ``/status`` call is tolerated (values then arrive over MQTT); failing structure
        calls raise.
        """
        devices, functions, statuses = await asyncio.gather(
            self.devices(installation.id),
            self.functions(installation.id),
            self._status_or_empty(installation.id),
        )
        return InstallationSnapshot(
            installation=installation,
            devices={d.id: d for d in devices},
            functions={f.id: f for f in functions},
            values={s.topic: s for s in statuses if s.topic},
        )

    async def _status_or_empty(self, installation_id: int) -> list[Status]:
        try:
            return await self.status(installation_id)
        except (IoTOpenAuthError, IoTOpenConnectionError):
            raise
        except IoTOpenResponseError as err:
            _LOGGER.debug("Could not read status for installation %s: %s", installation_id, err)
            return []
