"""Exceptions raised by pyiotopen."""

from __future__ import annotations


class IoTOpenError(Exception):
    """Base class for all pyiotopen errors."""


class IoTOpenConnectionError(IoTOpenError):
    """The IoT Open server could not be reached (network error, timeout, DNS, TLS ...)."""


class IoTOpenAuthError(IoTOpenError):
    """The credentials, token or API key were rejected."""


class IoTOpenTwoFactorRequired(IoTOpenError):
    """Login succeeded with username/password but a second factor is required.

    Pass ``token`` together with the challenge (e.g. a TOTP code) to
    :meth:`pyiotopen.IoTOpenClient.login_2fa`.
    """

    def __init__(self, token: str, next_step: str | None = None) -> None:
        super().__init__("Two-factor authentication required")
        self.token = token
        self.next_step = next_step


class IoTOpenResponseError(IoTOpenError):
    """The server answered with an unexpected HTTP status."""

    def __init__(self, status: int, message: str, body: object = None) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message
        self.body = body


class IoTOpenForbiddenError(IoTOpenResponseError):
    """The token is valid but lacks permission for the requested resource (HTTP 403)."""


class IoTOpenNotFoundError(IoTOpenResponseError):
    """The requested resource does not exist (HTTP 404)."""
