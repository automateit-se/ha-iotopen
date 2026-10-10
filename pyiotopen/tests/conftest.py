"""Shared fixtures."""

from __future__ import annotations

from collections.abc import AsyncIterator

import aiohttp
import pytest

from pyiotopen import IoTOpenClient

from .fake_lynx import API_KEY, FakeLynx


@pytest.fixture
def lynx() -> FakeLynx:
    return FakeLynx()


@pytest.fixture
async def server(aiohttp_server, lynx: FakeLynx):
    return await aiohttp_server(lynx.app)


@pytest.fixture
async def session() -> AsyncIterator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession() as s:
        yield s


@pytest.fixture
def base_url(server) -> str:
    return str(server.make_url("")).rstrip("/")


@pytest.fixture
def client(session, base_url) -> IoTOpenClient:
    return IoTOpenClient(session, base_url, API_KEY)
