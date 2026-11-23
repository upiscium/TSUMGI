"""Negative-only operator authority, parsing and default-off regressions."""
from __future__ import annotations

import json

import pytest

from astrumweaver.control import InMemoryControlRepository, daemon
from astrumweaver.control.api import create_app
from audio_epoch_acceptance import (
    CLIENT, CLIENT_TOKEN, E1, E2, OPERATOR, OPERATOR_TOKEN, WORKER_ID,
    WORKER_TOKEN, make_harness, snapshot_repository,
)
from test_audio_epoch_acceptance import epoch_harness


def test_operator_environment_alone_does_not_enable_management_routes(tmp_path, monkeypatch):
    repo = InMemoryControlRepository()
    with make_harness(
        tmp_path, monkeypatch, database_url="postgresql://unused.invalid/default-off",
        observer=lambda: snapshot_repository(repo, []), memory_repository=repo,
        fencing_enabled=False,
    ) as h:
        assert h.register(E1).status_code == 201
        assert h.fence().status_code == 404
        assert h.read_fence().status_code == 404
        assert not repo.get_worker(WORKER_ID).state.value == "offline"


@pytest.mark.parametrize("token", ["", "short", "bad\noperator-token", "é" * 20,
                                  WORKER_TOKEN, CLIENT_TOKEN])
def test_operator_token_is_distinct_bounded_and_header_safe(token):
    with pytest.raises(ValueError, match="distinct operator bearer"):
        create_app(InMemoryControlRepository(), client_token=CLIENT_TOKEN,
                   worker_token=WORKER_TOKEN, audio_epoch_operator_token=token)


@pytest.mark.parametrize("section", [
    'enabled = "yes"', "enabled = 1", 'enabled = true\noperator_token = "not-allowed"',
])
def test_unknown_or_untyped_fence_configuration_is_rejected(tmp_path, monkeypatch, section):
    monkeypatch.setenv("ASTRUMWEAVER_DATABASE_URL", "postgresql://unused.invalid/config")
    monkeypatch.setenv("ASTRUMWEAVER_CLIENT_TOKEN", CLIENT_TOKEN)
    monkeypatch.setenv("ASTRUMWEAVER_WORKER_TOKEN", WORKER_TOKEN)
    path = tmp_path / "control.toml"
    path.write_text("[audio_epoch_fencing]\n" + section + "\n")
    with pytest.raises(RuntimeError, match="configuration is invalid"):
        daemon.build_app(str(path))


@pytest.mark.parametrize("token", ["", CLIENT_TOKEN, WORKER_TOKEN])
def test_enabled_management_requires_separate_operator_environment(tmp_path, monkeypatch, token):
    monkeypatch.setenv("ASTRUMWEAVER_DATABASE_URL", "postgresql://unused.invalid/config")
    monkeypatch.setenv("ASTRUMWEAVER_CLIENT_TOKEN", CLIENT_TOKEN)
    monkeypatch.setenv("ASTRUMWEAVER_WORKER_TOKEN", WORKER_TOKEN)
    monkeypatch.setenv("ASTRUMWEAVER_AUDIO_EPOCH_OPERATOR_TOKEN", token)
    path = tmp_path / "control.toml"
    path.write_text("[audio_epoch_fencing]\nenabled = true\n")
    with pytest.raises(RuntimeError, match="distinct operator bearer"):
        daemon.build_app(str(path))


@pytest.mark.parametrize("change, status", [
    ("empty", 422), ("list", 422), ("malformed", 422), ("unknown", 422),
    ("missing", 422), ("duplicate", 422), ("request-id", 422),
    ("revision", 422), ("protocol", 422), ("oversize", 413), ("content-type", 415),
])
def test_operator_requests_are_strict_and_bounded(epoch_harness, change, status):
    h = epoch_harness
    h.seed_claim()
    body = h.fence_body()
    headers = {**OPERATOR, "content-type": "application/json"}
    if change == "empty":
        raw = b""
    elif change == "list":
        raw = b"[]"
    elif change == "malformed":
        raw = b"{"
    elif change == "duplicate":
        raw = json.dumps(body).replace("{", '{"fence_request_id":"duplicate",', 1).encode()
    elif change == "oversize":
        raw = b" " * 2049
    else:
        if change == "unknown":
            body["ready"] = True
        elif change == "missing":
            del body["fence_request_id"]
        elif change == "request-id":
            body["fence_request_id"] = "not-a-uuid"
        elif change == "revision":
            body["expected_deployment_revision"] = "unapproved"
        elif change == "protocol":
            body["protocol_version"] = "v0"
        elif change == "content-type":
            headers["content-type"] = "text/plain"
        raw = json.dumps(body).encode()
    response = h.denied(lambda: h.client.post(
        f"/v1/admin/audio-epochs/{WORKER_ID}/{E1}/fence", headers=headers, content=raw,
    ), status=status)
    assert response.headers["cache-control"] == "no-store"
    assert h.read_fence().status_code == 404


def test_request_id_cannot_be_rebound_to_e2(epoch_harness):
    h = epoch_harness
    first = h.seed_claim()
    receipt = h.assert_fenced()
    assert h.client.post(f"/v1/jobs/{first['job_id']}/cancel", headers=CLIENT).status_code == 200
    assert h.register(E2).status_code == 201
    h.denied(lambda: h.fence(E2))
    response = h.fence(E2, request_id="42345678-1234-4234-9234-123456789abc")
    assert response.status_code == 200
    assert h.read_fence(E1).json() == receipt
    assert h.read_fence(E2).json() == response.json()


def test_unknown_epoch_readback_is_not_an_admission_receipt(epoch_harness):
    h = epoch_harness
    h.seed_claim()
    response = h.read_fence()
    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"
    assert OPERATOR_TOKEN not in response.text
