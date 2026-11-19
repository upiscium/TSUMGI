from __future__ import annotations

import base64

import httpx
import pytest
from fastapi import FastAPI

from astrumweaver.control import InMemoryControlRepository
from astrumweaver.control.admin import create_admin_router
from astrumweaver.control.admin_auth import (
    FAILURE_WINDOW_SECONDS,
    MAX_FAILURES,
    BasicAuth,
    hash_password,
    parse_hash,
)
from astrumweaver.control.daemon import build_app

from test_admin_console_daemon import write_config
from test_chat_gateway_daemon import base_environment

PASSWORD = "correct horse battery"
HASH = hash_password(PASSWORD, salt=b"0123456789abcdef")


def basic(user="ops", password=PASSWORD):
    return {"authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


def make(clock=None):
    clock = clock or Clock()
    app = FastAPI()
    app.include_router(create_admin_router(InMemoryControlRepository(), auth=BasicAuth("ops", HASH, clock=clock)))
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://c")
    return client, clock


ROUTES = [("GET", "/admin"), ("GET", "/admin/app.js"), ("GET", "/admin/app.css"), ("GET", "/admin/api/workers"), ("POST", "/admin/api/model-check")]


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", ROUTES)
async def test_every_route_requires_credentials(method, path):
    client, _ = make()
    async with client:
        response = await client.request(method, path)
        assert response.status_code == 401
        assert response.headers["www-authenticate"].startswith("Basic ")
        assert (await client.request(method, path, headers=basic(password="wrong"))).status_code == 401
        assert (await client.request(method, path, headers=basic(user="root"))).status_code == 401


@pytest.mark.asyncio
async def test_correct_credentials_open_the_console():
    client, _ = make()
    async with client:
        assert (await client.get("/admin", headers=basic())).status_code == 200
        assert (await client.get("/admin/api/workers", headers=basic())).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "header",
    ["Bearer abc", "Basic", "Basic !!!notbase64", "Basic " + base64.b64encode(b"no-colon").decode(), ""],
)
async def test_malformed_authorization_is_a_plain_401(header):
    client, _ = make()
    async with client:
        response = await client.get("/admin", headers={"authorization": header})
        assert response.status_code == 401


@pytest.mark.asyncio
async def test_repeated_failures_lock_the_client_out_even_for_the_right_password():
    client, clock = make()
    async with client:
        for _ in range(MAX_FAILURES):
            assert (await client.get("/admin", headers=basic(password="nope"))).status_code == 401
        locked = await client.get("/admin", headers=basic())
        assert locked.status_code == 429 and int(locked.headers["retry-after"]) >= 1
        clock.now += FAILURE_WINDOW_SECONDS + 1
        assert (await client.get("/admin", headers=basic())).status_code == 200


@pytest.mark.asyncio
async def test_a_visit_without_credentials_does_not_count_as_a_failure():
    client, _ = make()
    async with client:
        for _ in range(MAX_FAILURES * 2):
            assert (await client.get("/admin")).status_code == 401
        assert (await client.get("/admin", headers=basic())).status_code == 200


@pytest.mark.asyncio
async def test_lockout_is_per_client_and_uses_the_rightmost_forwarded_address():
    client, _ = make()
    async with client:
        # A forged left-hand entry must not dodge or move the lockout.
        for i in range(MAX_FAILURES):
            await client.get("/admin", headers={**basic(password="x"), "x-forwarded-for": f"9.9.9.{i}, 1.2.3.4"})
        assert (await client.get("/admin", headers={**basic(), "x-forwarded-for": "8.8.8.8, 1.2.3.4"})).status_code == 429
        assert (await client.get("/admin", headers={**basic(), "x-forwarded-for": "1.2.3.4, 5.6.7.8"})).status_code == 200


def test_hash_format_and_validation():
    assert HASH.startswith("scrypt$16384$8$1$")
    assert hash_password(PASSWORD) != hash_password(PASSWORD)  # random salt
    for bad in ("", "plain", "md5$1$2$3$a$b", "scrypt$16384$8$1$!!$!!", "scrypt$3$8$1$MDEyMw==$MDEyMw==", "scrypt$1073741824$8$1$MDEyMw==$MDEyMw=="):
        with pytest.raises(ValueError):
            parse_hash(bad)
    for user in ("", "a:b", "tab\tname"):
        with pytest.raises(ValueError):
            BasicAuth(user, HASH)


def _config(tmp_path, extra):
    path = write_config(tmp_path, admin="true")
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(extra)
    return path


@pytest.mark.asyncio
async def test_daemon_enforces_the_configured_credentials(tmp_path, monkeypatch):
    base_environment(monkeypatch)
    app = build_app(_config(tmp_path, f'username = "ops"\npassword_hash = "{HASH}"\n'))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://c") as client:
        assert (await client.get("/admin")).status_code == 401
        assert (await client.get("/admin", headers=basic())).status_code == 200


@pytest.mark.parametrize(
    "extra",
    ['username = "ops"\n', f'password_hash = "{HASH}"\n', 'username = "ops"\npassword_hash = "plain"\n', f'username = 3\npassword_hash = "{HASH}"\n'],
)
def test_daemon_rejects_incomplete_or_invalid_credentials(tmp_path, monkeypatch, extra):
    base_environment(monkeypatch)
    with pytest.raises((RuntimeError, ValueError)):
        build_app(_config(tmp_path, extra))
