"""Configuration, strict parsing and default-off behaviour of audio preparation."""
from __future__ import annotations

import json

import pytest

from astrumweaver.control import InMemoryControlRepository, daemon
from astrumweaver.control.api import create_app
from audio_epoch_acceptance import CLIENT_TOKEN, E1, E2, WORKER_ID, WORKER_TOKEN
from audio_preparation_cases import REQUEST, digest, prepare, qualification_for
from test_audio_preparation import preparation_harness  # noqa: F401  (fixture)
from worker_identity_cases import WORKERS


def _configure(tmp_path, monkeypatch, text):
    monkeypatch.setenv("ASTRUMWEAVER_DATABASE_URL", "postgresql://unused.invalid/prep-config")
    monkeypatch.setenv("ASTRUMWEAVER_CLIENT_TOKEN", CLIENT_TOKEN)
    monkeypatch.setenv("ASTRUMWEAVER_WORKER_TOKEN", WORKER_TOKEN)
    path = tmp_path / "control.toml"
    path.write_text(text)
    return str(path)


@pytest.mark.parametrize("section", [
    'enabled = "yes"', "enabled = 1", "enabled = true\nttl_seconds = 0",
    "enabled = true\nttl_seconds = 301", "enabled = true\nttl_seconds = true",
    'enabled = true\nttl_seconds = "60"', 'enabled = true\ntoken = "no"',
])
def test_untyped_or_out_of_range_preparation_configuration_is_rejected(tmp_path, monkeypatch, section):
    path = _configure(tmp_path, monkeypatch, "[audio_epoch_preparation]\n" + section + "\n")
    with pytest.raises(RuntimeError, match="audio_epoch_preparation configuration is invalid"):
        daemon.build_app(path)


def test_preparation_needs_per_worker_credentials_in_the_daemon(tmp_path, monkeypatch):
    path = _configure(tmp_path, monkeypatch, "[audio_epoch_preparation]\nenabled = true\n")
    with pytest.raises(RuntimeError, match="per-worker"):
        daemon.build_app(path)


@pytest.mark.parametrize("ttl", [0, 301, True, "60", 1.5, -1])
def test_create_app_rejects_a_bad_ttl_or_shared_worker_token(ttl):
    with pytest.raises(ValueError, match="per-worker credentials"):
        create_app(InMemoryControlRepository(), client_token=CLIENT_TOKEN, worker_token=WORKER_TOKEN,
                   audio_preparation_ttl_seconds=ttl)


def test_a_shared_worker_token_never_enables_the_routes():
    with pytest.raises(ValueError, match="per-worker credentials"):
        create_app(InMemoryControlRepository(), client_token=CLIENT_TOKEN, worker_token=WORKER_TOKEN,
                   audio_preparation_ttl_seconds=60)


def test_requests_are_strict_bounded_and_uncacheable(preparation_harness):  # noqa: F811
    h = preparation_harness
    repo = h.qualification_repository
    assert h.register(E1).status_code == 201
    assert h.state(E1, "offline").status_code == 200
    q = qualification_for(h, repo)
    path = f"/v1/workers/{WORKER_ID}/audio-epochs/{E2}/prepare"
    good = {"protocol_version": "v1", "qualification_revision": q.qualification_revision,
            "expected_intent_revision": 0, "prepare_request_id": REQUEST}
    before = h.effects()
    bad_bodies = [
        {**good, "protocol_version": "v2"}, {**good, "extra": 1},
        {k: v for k, v in good.items() if k != "prepare_request_id"},
        {**good, "expected_intent_revision": True}, {**good, "expected_intent_revision": -1},
        {**good, "expected_intent_revision": "0"}, {**good, "qualification_revision": "0" * 64},
        {**good, "qualification_revision": digest("x").upper()},
        {**good, "prepare_request_id": REQUEST.upper()}, [], "text", None,
    ]
    for body in bad_bodies:
        response = h.client.post(path, headers=h.worker_headers, json=body)
        assert response.status_code == 422, body
        assert response.headers["cache-control"] == "no-store"
    assert h.client.post(path, headers=h.worker_headers, content=b"{not json").status_code == 422
    assert h.client.post(path, headers=h.worker_headers, content=b"").status_code == 422
    oversized = json.dumps({**good, "pad": "x" * 4096})
    assert h.client.post(path, headers=h.worker_headers, content=oversized).status_code in (413, 422)
    for bad_epoch in ("not-a-uuid", E2.upper(), "00000000-0000-0000-0000-000000000000"):
        assert prepare(h, q.qualification_revision, epoch=bad_epoch).status_code in (404, 422)
    assert h.effects() == before
    ok = prepare(h, q.qualification_revision)
    assert ok.status_code == 200 and ok.headers["cache-control"] == "no-store"
    assert ok.json()["protocol_version"] == "v1"


def test_readback_of_an_unknown_epoch_is_404_and_a_bad_epoch_is_422(preparation_harness):  # noqa: F811
    h = preparation_harness
    assert h.register(E1).status_code == 201
    unknown = h.client.get(f"/v1/workers/{WORKER_ID}/audio-epochs/{E2}", headers=h.worker_headers)
    assert unknown.status_code == 404 and unknown.headers["cache-control"] == "no-store"
    bad = h.client.get(f"/v1/workers/{WORKER_ID}/audio-epochs/not-a-uuid", headers=h.worker_headers)
    assert bad.status_code == 422
