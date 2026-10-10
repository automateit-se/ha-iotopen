"""REST client tests against the fake Lynx server."""

from __future__ import annotations

import aiohttp
import pytest

from pyiotopen import (
    IoTOpenAuthError,
    IoTOpenClient,
    IoTOpenConnectionError,
    IoTOpenTwoFactorRequired,
)

from .fake_lynx import PASSWORD, SESSION_TOKEN, TMP_TOKEN, TOTP, USERNAME, USERNAME_2FA


async def test_installations_and_structure(client: IoTOpenClient):
    installations = await client.installations()
    assert [(i.id, i.client_id) for i in installations] == [(1001, 2001), (1002, 2002)]
    devices = await client.devices(1001)
    assert devices[0].type == "Gateway"
    assert devices[0].name == "Demo Device 1"
    functions = await client.functions(1001)
    assert {f.id for f in functions} == {3001, 3002, 3003, 3004, 3005}


async def test_snapshot(client: IoTOpenClient):
    inst = (await client.installations())[0]
    snap = await client.snapshot(inst)
    assert snap.values["obj/demo/101/level"].value == 3
    assert snap.functions_for_topic("2001/obj/demo/101/level")[0].id == 3002
    assert "obj/demo/101/velocity" in snap.read_topics()


async def test_status_filter_and_log(client: IoTOpenClient, lynx):
    status = await client.status(1001, ["2001/obj/demo/101/level"])
    assert [s.topic for s in status] == ["obj/demo/101/level"]
    assert "topics=obj%2Fdemo%2F101%2Flevel" in lynx.requests[-1][1] or "topics=obj/demo/101/level" in lynx.requests[-1][1]
    log = await client.log(1001)
    assert all(not e["topic"].startswith("2001/") for e in log["data"])


async def test_set_function_meta(client: IoTOpenClient, lynx):
    await client.set_function_meta(1001, 3002, "ha.hidden", "true")
    fn = next(f for f in lynx.data["functions"][1001] if f["id"] == 3002)
    assert fn["meta"]["ha.hidden"] == "true"


async def test_bad_api_key(session, base_url):
    client = IoTOpenClient(session, base_url, "wrong")
    with pytest.raises(IoTOpenAuthError):
        await client.installations()


async def test_login(session, base_url):
    client = IoTOpenClient(session, base_url)
    token = await client.authenticate(USERNAME, PASSWORD)
    assert token == SESSION_TOKEN
    assert client.api_key == SESSION_TOKEN
    assert await client.installations()


async def test_login_wrong_password(session, base_url):
    client = IoTOpenClient(session, base_url)
    with pytest.raises(IoTOpenAuthError):
        await client.login(USERNAME, "nope")


async def test_login_2fa(session, base_url):
    client = IoTOpenClient(session, base_url)
    with pytest.raises(IoTOpenTwoFactorRequired) as exc:
        await client.authenticate(USERNAME_2FA, PASSWORD)
    assert exc.value.token == TMP_TOKEN
    with pytest.raises(IoTOpenAuthError):
        await client.login_2fa(TMP_TOKEN, "000000")
    result = await client.login_2fa(TMP_TOKEN, TOTP)
    assert result.token == SESSION_TOKEN


async def test_connection_error():
    async with aiohttp.ClientSession() as session:
        client = IoTOpenClient(session, "http://127.0.0.1:9", "x", request_timeout=2)
        with pytest.raises(IoTOpenConnectionError):
            await client.installations()
