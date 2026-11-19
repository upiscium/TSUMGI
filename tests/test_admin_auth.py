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
    StaticCredentials,
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


def make(clock=None, trusted=()):
    clock = clock or Clock()
    app = FastAPI()
    auth = BasicAuth(StaticCredentials("ops", HASH), clock=clock, trusted_proxies=trusted)
    app.include_router(create_admin_router(InMemoryControlRepository(), auth=auth))
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
    client, _ = make(trusted=("127.0.0.1",))
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
            StaticCredentials(user, HASH)


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


@pytest.mark.asyncio
async def test_forwarded_for_is_ignored_unless_the_peer_is_a_trusted_proxy():
    client, _ = make()  # no trusted proxies: every request is the same peer
    async with client:
        for i in range(MAX_FAILURES):
            await client.get("/admin", headers={**basic(password="x"), "x-forwarded-for": f"7.7.7.{i}"})
        assert (await client.get("/admin", headers={**basic(), "x-forwarded-for": "6.6.6.6"})).status_code == 429


@pytest.mark.asyncio
async def test_ipv6_clients_share_a_bucket_per_64():
    client, _ = make(trusted=("127.0.0.1",))
    async with client:
        for i in range(MAX_FAILURES):
            await client.get("/admin", headers={**basic(password="x"), "x-forwarded-for": f"2001:db8:1:2::{i + 1}"})
        assert (await client.get("/admin", headers={**basic(), "x-forwarded-for": "2001:db8:1:2:ffff::9"})).status_code == 429
        assert (await client.get("/admin", headers={**basic(), "x-forwarded-for": "2001:db8:1:3::1"})).status_code == 200


@pytest.mark.asyncio
async def test_visits_without_credentials_leave_no_state_behind():
    app = FastAPI()
    auth = BasicAuth(StaticCredentials("ops", HASH), trusted_proxies=("127.0.0.1",))
    app.include_router(create_admin_router(InMemoryControlRepository(), auth=auth))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://c") as client:
        for i in range(50):
            await client.get("/admin", headers={"x-forwarded-for": f"10.0.0.{i}"})
    assert auth._failures == {}


def test_tracking_is_bounded_and_keeps_locked_out_clients():
    import astrumweaver.control.admin_auth as mod

    clock = Clock()
    auth = BasicAuth(StaticCredentials("ops", HASH), clock=clock)
    for _ in range(MAX_FAILURES):
        auth._record("attacker", clock.now, False)
    for i in range(mod._MAX_TRACKED_CLIENTS + 10):
        clock.now += 0.001
        auth._record(f"c{i}", clock.now, False)
    assert len(auth._failures) <= mod._MAX_TRACKED_CLIENTS
    assert "attacker" in auth._failures and len(auth._failures["attacker"]) == MAX_FAILURES


@pytest.mark.asyncio
async def test_busy_verifier_refuses_quickly_instead_of_queueing():
    import astrumweaver.control.admin_auth as mod

    app = FastAPI()
    auth = BasicAuth(StaticCredentials("ops", HASH))
    app.include_router(create_admin_router(InMemoryControlRepository(), auth=auth))
    for _ in range(mod._MAX_CONCURRENT_VERIFICATIONS):
        auth._slots.acquire()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://c") as client:
        busy = await client.get("/admin", headers=basic())
        assert busy.status_code == 429 and busy.headers["retry-after"] == "1"
        assert (await client.get("/admin")).status_code == 401  # no scrypt needed
    for _ in range(mod._MAX_CONCURRENT_VERIFICATIONS):
        auth._slots.release()


def test_hashes_that_cannot_run_are_refused_at_start_up():
    import base64 as b64

    salt = b64.b64encode(b"0123456789abcdef").decode()
    key = b64.b64encode(b"k" * 32).decode()
    for n, r in ((2**17, 8), (2**15, 32), (2**20, 1)):  # above the memory cap
        with pytest.raises(ValueError):
            StaticCredentials("ops", f"scrypt${n}${r}$1${salt}${key}")
    with pytest.raises(ValueError):  # key length out of range
        StaticCredentials("ops", f"scrypt$16384$8$1${salt}${b64.b64encode(b'k').decode()}")


@pytest.mark.asyncio
async def test_non_ascii_credentials_and_exact_retry_after():
    unicode_hash = hash_password("パスワードは長いほうがいい", salt=b"0123456789abcdef")
    app = FastAPI()
    clock = Clock()
    app.include_router(create_admin_router(InMemoryControlRepository(), auth=BasicAuth(StaticCredentials("運用", unicode_hash), clock=clock)))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://c") as client:
        assert (await client.get("/admin", headers=basic("運用", "パスワードは長いほうがいい"))).status_code == 200
        for _ in range(MAX_FAILURES):
            await client.get("/admin", headers=basic("運用", "x"))
        clock.now += 100
        locked = await client.get("/admin", headers=basic("運用", "パスワードは長いほうがいい"))
        assert locked.status_code == 429 and locked.headers["retry-after"] == str(int(FAILURE_WINDOW_SECONDS - 100) + 1)


@pytest.mark.asyncio
async def test_host_check_runs_before_authentication():
    app = FastAPI()
    app.include_router(
        create_admin_router(InMemoryControlRepository(), allowed_hosts=["control.lan"], auth=BasicAuth(StaticCredentials("ops", HASH)))
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://x") as client:
        assert (await client.get("/admin", headers={**basic(), "host": "evil.example"})).status_code == 403
        assert (await client.get("/admin", headers={"host": "control.lan"})).status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["HEAD", "OPTIONS", "PUT", "DELETE"])
async def test_other_methods_never_reach_a_handler_without_credentials(method):
    client, _ = make()
    async with client:
        for path in ("/admin", "/admin/", "/admin/api/workers"):
            response = await client.request(method, path)
            assert response.status_code in (401, 405) and not response.content.startswith(b"<!"), (method, path)


@pytest.mark.parametrize("extra", ['trusted_proxies = "10.0.0.1"\n', "trusted_proxies = [1]\n"])
def test_daemon_validates_trusted_proxies(tmp_path, monkeypatch, extra):
    base_environment(monkeypatch)
    with pytest.raises(RuntimeError, match="trusted_proxies"):
        build_app(_config(tmp_path, f'username = "ops"\npassword_hash = "{HASH}"\n' + extra))


def test_password_prompt_rejects_short_and_mismatched_passwords(monkeypatch, capsys):
    from astrumweaver.control import admin_auth

    answers = iter(["short", "x"])
    monkeypatch.setattr(admin_auth.getpass, "getpass", lambda _p: next(answers))
    assert admin_auth.main() == 1
    answers = iter(["long enough password", "different password!"])
    assert admin_auth.main() == 1
    answers = iter(["long enough password"] * 2)
    assert admin_auth.main() == 0 and capsys.readouterr().out.strip().startswith("scrypt$")


# ---- first-run setup ----------------------------------------------------------

from astrumweaver.control.admin_auth import CredentialsFile  # noqa: E402

SETUP = {"username": "ops", "password": "a long enough password"}


def make_setup(tmp_path, clock=None, trusted=()):
    store = CredentialsFile(tmp_path / "admin-credentials.json")
    auth = BasicAuth(store, clock=clock or Clock(), trusted_proxies=trusted)
    app = FastAPI()
    app.include_router(create_admin_router(InMemoryControlRepository(), auth=auth))
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://c")
    return client, store, auth


def code_of(store):
    return store.code_path.read_text().strip()


def post_setup(client, **body):
    return client.post("/admin/api/setup", json=body)


@pytest.mark.asyncio
async def test_setup_mode_serves_only_the_setup_page(tmp_path):
    client, store, auth = make_setup(tmp_path)
    assert auth.needs_setup and store.code_path.stat().st_mode & 0o777 == 0o600
    async with client:
        page = await client.get("/admin")
        assert page.status_code == 200 and "セットアップコード" in page.text
        assert (await client.get("/admin/setup.js")).status_code == 200
        assert (await client.get("/admin/app.css")).status_code == 200
        for path in ("/admin/api/workers", "/admin/app.js"):
            assert (await client.get(path)).status_code == 403, path
        assert (await client.post("/admin/api/model-check", json={"url": "x"})).status_code == 403


@pytest.mark.asyncio
async def test_setup_with_the_code_sets_the_password_once(tmp_path):
    client, store, _ = make_setup(tmp_path)
    code = code_of(store)
    async with client:
        done = await post_setup(client, code=code, **SETUP)
        assert done.status_code == 200
        assert not store.code_path.exists() and store.path.stat().st_mode & 0o777 == 0o600
        assert SETUP["password"] not in store.path.read_text()
        # Now the normal login applies.
        assert (await client.get("/admin")).status_code == 401
        assert (await client.get("/admin", headers=basic("ops", SETUP["password"]))).status_code == 200
        # Setup is closed, with or without the old code.
        again = await post_setup(client, code=code, username="evil", password="another long password")
        assert again.status_code == 401  # needs the login first
        assert (await client.post("/admin/api/setup", json={"code": code, **SETUP}, headers=basic("ops", SETUP["password"]))).status_code == 409


@pytest.mark.asyncio
async def test_a_wrong_setup_code_is_refused_and_throttled(tmp_path):
    client, store, _ = make_setup(tmp_path)
    async with client:
        for _ in range(MAX_FAILURES):
            assert (await post_setup(client, code="0000-0000-0000-0000-0000", **SETUP)).status_code == 403
        locked = await post_setup(client, code=code_of(store), **SETUP)
        assert locked.status_code == 429 and not store.path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"username": "", "password": "a long enough password"},
        {"username": "a:b", "password": "a long enough password"},
        {"username": "ops", "password": "short"},
        {"username": "ops", "password": "x" * 300},
        {"username": 5, "password": "a long enough password"},
    ],
)
async def test_bad_username_or_password_is_rejected_without_burning_the_code(tmp_path, body):
    client, store, _ = make_setup(tmp_path)
    async with client:
        response = await post_setup(client, code=code_of(store), **body)
        assert response.status_code == 400 and not store.path.exists() and store.code_path.exists()
        assert (await post_setup(client, code=code_of(store), **SETUP)).status_code == 200


@pytest.mark.asyncio
async def test_setup_needs_json_and_an_object_body(tmp_path):
    client, store, _ = make_setup(tmp_path)
    async with client:
        form = await client.post("/admin/api/setup", data={"code": code_of(store), **SETUP})
        assert form.status_code == 415
        assert (await client.post("/admin/api/setup", json=["x"])).status_code == 400
        assert (await post_setup(client, username="ops", password="a long enough password")).status_code == 400  # no code


def test_the_code_survives_a_restart_and_is_not_reissued_once_set_up(tmp_path):
    first = CredentialsFile(tmp_path / "c.json")
    code = code_of(first)
    assert code_of(CredentialsFile(tmp_path / "c.json")) == code
    assert first.set_up(code, "ops", "a long enough password") is True
    assert CredentialsFile(tmp_path / "c.json").needs_setup is False
    assert not (tmp_path / "c.json.setup-code").exists()


def test_a_broken_credentials_file_stops_the_start_up(tmp_path):
    (tmp_path / "c.json").write_text("{not json")
    with pytest.raises(ValueError):
        CredentialsFile(tmp_path / "c.json")
    (tmp_path / "c.json").write_text('{"username": "ops", "password_hash": "plain"}')
    with pytest.raises(ValueError):
        CredentialsFile(tmp_path / "c.json")
    with pytest.raises(ValueError):
        CredentialsFile(tmp_path / "missing-dir" / "c.json")


@pytest.mark.asyncio
async def test_daemon_credentials_file_mode(tmp_path, monkeypatch):
    base_environment(monkeypatch)
    path = tmp_path / "admin-credentials.json"
    app = build_app(_config(tmp_path, f'credentials_file = "{path}"\n'))
    code = (tmp_path / "admin-credentials.json.setup-code").read_text().strip()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://c") as client:
        assert (await client.get("/admin/api/workers")).status_code == 403
        assert (await client.post("/admin/api/setup", json={"code": code, **SETUP})).status_code == 200
        assert (await client.get("/admin", headers=basic("ops", SETUP["password"]))).status_code == 200


def test_daemon_refuses_both_credential_sources_and_a_bad_path(tmp_path, monkeypatch):
    base_environment(monkeypatch)
    both = f'username = "ops"\npassword_hash = "{HASH}"\ncredentials_file = "{tmp_path}/c.json"\n'
    with pytest.raises(RuntimeError, match="not both"):
        build_app(_config(tmp_path, both))
    with pytest.raises(RuntimeError):
        build_app(_config(tmp_path, 'credentials_file = ""\n'))
    with pytest.raises(RuntimeError):
        build_app(_config(tmp_path, f'credentials_file = "{tmp_path}/nodir/c.json"\n'))
