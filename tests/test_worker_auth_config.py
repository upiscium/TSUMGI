"""Strict Worker identity configuration and pre-effect authorization."""
from __future__ import annotations

import json
import os
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from astrumweaver.control import InMemoryControlRepository, daemon
from astrumweaver.control.api import create_app
from astrumweaver.control.worker_auth import WorkerCredentials
from audio_epoch_acceptance import E1, WORKER_ID, registration
from test_audio_voice_api import make_profile

OWN = "configuration-owner-credential-" + "a" * 32
OTHER = "configuration-other-credential-" + "b" * 32
SHARED = "configuration-shared-credential-" + "s" * 32
CLIENT = "configuration-client-credential-" + "c" * 32
OPERATOR = "configuration-operator-credential-" + "o" * 32
CREDS = {WORKER_ID: OWN, "other": OTHER}


def private_credentials(tmp_path, workers):
    path = tmp_path / "worker-secrets.json"
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as handle:
        json.dump({"schema_version": "worker-credentials-v1", "workers": workers}, handle)
    return path


def configure(tmp_path, monkeypatch, text):
    monkeypatch.setenv("ASTRUMWEAVER_DATABASE_URL", "postgresql://unused.invalid/auth-config")
    monkeypatch.setenv("ASTRUMWEAVER_CLIENT_TOKEN", CLIENT)
    monkeypatch.setenv("ASTRUMWEAVER_WORKER_TOKEN", SHARED)
    monkeypatch.setenv("ASTRUMWEAVER_AUDIO_EPOCH_OPERATOR_TOKEN", OPERATOR)
    path = tmp_path / "control.toml"
    path.write_text(text)
    return path


def test_strict_daemon_needs_no_shared_token_and_rejects_old_shared_token(tmp_path, monkeypatch):
    credentials = private_credentials(tmp_path, CREDS)
    config = configure(tmp_path, monkeypatch, '[worker_auth]\nmode = "per-worker"\n'
                       f"credentials_file = {json.dumps(str(credentials))}\n")
    monkeypatch.delenv("ASTRUMWEAVER_WORKER_TOKEN")
    repo = InMemoryControlRepository()
    monkeypatch.setattr(daemon, "PostgresControlRepository", lambda *a, **k: repo)
    with TestClient(daemon.build_app(str(config))) as client:
        payload = registration(make_profile("model-tts"), E1)
        for token in (SHARED, CLIENT, OPERATOR):
            response = client.post("/v1/workers/register",
                                   headers={"authorization": "Bearer " + token}, json=payload)
            assert response.status_code == 401
            assert repo.list_workers() == []
        response = client.post("/v1/workers/register",
                               headers={"authorization": "Bearer " + OWN}, json=payload)
        assert response.status_code == 201
        assert repo.get_worker(WORKER_ID).worker_id == WORKER_ID


@pytest.mark.parametrize("text", [
    'worker_auth = true\n',
    '[worker_auth]\nmode = "unknown"\n',
    '[worker_auth]\nmode = false\n',
    '[worker_auth]\nmode = "per-worker"\n',
    '[worker_auth]\nmode = "per-worker"\ncredentials_file = 42\n',
    '[worker_auth]\ncredentials_file = "/does/not/exist"\n',
    '[worker_auth]\nmode = "per-worker"\ntokens = "inline-not-allowed"\n',
    '[control]\nclient_auth = "none"\n[worker_auth]\nmode = "per-worker"\n',
])
def test_invalid_daemon_config_fails_before_repository_construction(tmp_path, monkeypatch, text):
    config = configure(tmp_path, monkeypatch, text)
    def unexpected(*args, **kwargs):
        raise AssertionError("invalid credentials must fail before repository construction")
    monkeypatch.setattr(daemon, "PostgresControlRepository", unexpected)
    with pytest.raises(RuntimeError) as error:
        daemon.build_app(str(config))
    assert str(tmp_path) not in str(error.value)
    for token in (OWN, OTHER, SHARED, CLIENT, OPERATOR):
        assert token not in str(error.value)


@pytest.mark.parametrize("collision", ["client", "shared", "operator", "audio"])
def test_daemon_rejects_worker_role_token_collisions(tmp_path, monkeypatch, collision):
    token = {"client": CLIENT, "shared": SHARED, "operator": OPERATOR, "audio": OWN}[collision]
    credentials = private_credentials(tmp_path, {WORKER_ID: token})
    config = configure(tmp_path, monkeypatch, '[audio_epoch_fencing]\nenabled = true\n'
                       '[worker_auth]\nmode = "per-worker"\n'
                       f"credentials_file = {json.dumps(str(credentials))}\n")
    if collision == "audio":
        # No spool must be initialized before the cross-role check.
        monkeypatch.setattr(daemon, "load_audio_gateway_config", lambda _: SimpleNamespace(token=OWN))
    def unexpected(*args, **kwargs):
        raise AssertionError("collision must fail before repository construction")
    monkeypatch.setattr(daemon, "PostgresControlRepository", unexpected)
    with pytest.raises(RuntimeError, match="^invalid worker credentials configuration$"):
        daemon.build_app(str(config))


def test_strict_app_has_no_shared_fallback_even_when_shared_token_is_set():
    app = create_app(InMemoryControlRepository(), client_token=CLIENT,
                     worker_token=SHARED, worker_credentials=WorkerCredentials(CREDS))
    with TestClient(app) as client:
        response = client.post("/v1/workers/register", content=b"not JSON",
                               headers={"authorization": "Bearer " + SHARED})
        assert response.status_code == 401


def test_shared_default_remains_compatible():
    app = create_app(InMemoryControlRepository(), client_token=CLIENT, worker_token=SHARED)
    with TestClient(app) as client:
        response = client.post("/v1/workers/register",
                               headers={"authorization": "Bearer " + SHARED},
                               json=registration(make_profile("model-tts"), E1))
        assert response.status_code == 201


@pytest.mark.parametrize("headers,expected", [
    ([], 401),
    ([("authorization", "Bearer " + SHARED)], 401),
    ([("authorization", "Bearer " + OTHER)], 403),
    ([("authorization", "Bearer " + OWN), ("authorization", "Bearer " + OTHER)], 401),
    ([("authorization", "Bearer " + OWN), ("Authorization", "Bearer " + OWN)], 401),
    ([(b"authorization", b"Bearer \xff")], 401),
])
@pytest.mark.asyncio
async def test_identity_is_checked_before_body_or_storage(headers, expected):
    # No repository methods exist: any accidental access is a test failure.
    app = create_app(SimpleNamespace(), client_token=CLIENT, worker_token=SHARED,
                     worker_credentials=WorkerCredentials(CREDS))
    async def unread_body():
        raise AssertionError("unauthorized request body was consumed")
        yield b""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/v1/workers/{WORKER_ID}/heartbeat",
                                     headers=headers, content=unread_body())
    assert response.status_code == expected
    for token in (OWN, OTHER, SHARED):
        assert token not in response.text


@pytest.mark.parametrize("role", ["client", "shared", "operator"])
def test_direct_app_cannot_share_worker_credentials_with_other_roles(role):
    kwargs = {"client_token": CLIENT, "worker_token": SHARED, "audio_epoch_operator_token": OPERATOR}
    kwargs[{"client": "client_token", "shared": "worker_token", "operator": "audio_epoch_operator_token"}[role]] = OWN
    with pytest.raises(ValueError, match="invalid worker credentials configuration"):
        create_app(InMemoryControlRepository(), worker_credentials=WorkerCredentials(CREDS), **kwargs)


def test_direct_app_strict_mode_requires_client_authentication():
    with pytest.raises(ValueError, match="per-worker authentication requires"):
        create_app(InMemoryControlRepository(), client_token=None, worker_token=None,
                   client_auth="none", worker_credentials=WorkerCredentials(CREDS))


def test_credential_rotation_requires_new_application_snapshot(tmp_path, monkeypatch):
    credentials = private_credentials(tmp_path, CREDS)
    config = configure(tmp_path, monkeypatch, '[worker_auth]\nmode = "per-worker"\n'
                       f"credentials_file = {json.dumps(str(credentials))}\n")
    repo = InMemoryControlRepository()
    monkeypatch.setattr(daemon, "PostgresControlRepository", lambda *a, **k: repo)
    payload = registration(make_profile("model-tts"), E1)
    replacement = "replacement-worker-credential-" + "r" * 32
    with TestClient(daemon.build_app(str(config))) as old:
        credentials.write_text(json.dumps({
            "schema_version": "worker-credentials-v1", "workers": {WORKER_ID: replacement},
        }))
        assert old.post("/v1/workers/register", headers={"authorization": "Bearer " + OWN},
                        json=payload).status_code == 201
        assert old.post("/v1/workers/register", headers={"authorization": "Bearer " + replacement},
                        json=payload).status_code == 401
    with TestClient(daemon.build_app(str(config))) as new:
        assert new.post("/v1/workers/register", headers={"authorization": "Bearer " + OWN},
                        json=payload).status_code == 401
        assert new.post("/v1/workers/register", headers={"authorization": "Bearer " + replacement},
                        json=payload).status_code == 201
