"""Operator authority, parsing and default-off regressions of the release route."""
from __future__ import annotations

import json

import pytest

from tsumgi.control import InMemoryControlRepository
from audio_epoch_acceptance import (
    CLIENT, E1, E2, OPERATOR, OPERATOR_TOKEN, WORKER, WORKER_ID, WORKER_TOKEN,
    make_harness, snapshot_repository,
)
from audio_preparation_cases import (
    RELEASE, offline_worker, qualification_for, prepare, read_release, release,
)
from test_audio_preparation import preparation_harness  # noqa: F401  (fixture)
from worker_identity_cases import WORKERS

PATH = f"/v1/admin/audio-epochs/{WORKER_ID}/{E2}/release"


def _prepared(h):
    offline_worker(h)
    q = qualification_for(h, h.qualification_repository)
    assert prepare(h, q.qualification_revision).status_code == 200


def test_release_routes_are_absent_while_the_operator_routes_are_off(tmp_path, monkeypatch):
    repo = InMemoryControlRepository(worker_ttl_seconds=300, lease_seconds=60)
    with make_harness(
        tmp_path, monkeypatch, database_url="postgresql://unused.invalid/release-off",
        observer=lambda: snapshot_repository(repo, []), memory_repository=repo,
        fencing_enabled=False, preparation_enabled=True, worker_credentials=WORKERS,
        worker_token=WORKERS[next(iter(WORKERS))],
    ) as h:
        assert release(h).status_code == 404
        assert read_release(h).status_code == 404


def test_release_authority_is_the_operator_credential_only(preparation_harness):
    h = preparation_harness
    _prepared(h)
    for headers, status in (
        ({}, 401), (WORKER, 401), (CLIENT, 401), (h.worker_headers, 401),
        ({"authorization": "Bearer " + "x" * 20}, 401),
    ):
        response = release(h, headers=headers)
        assert response.status_code == status, (headers, response.text)
        assert OPERATOR_TOKEN not in response.text
        response = h.client.get(PATH, headers=headers)
        assert response.status_code == status
    assert read_release(h).status_code == 404  # none of them recorded anything


@pytest.mark.parametrize("change, status", [
    ("empty", 422), ("list", 422), ("malformed", 422), ("unknown", 422),
    ("missing", 422), ("duplicate", 422), ("request-id", 422), ("nil-id", 422),
    ("protocol", 422), ("oversize", 413), ("content-type", 415),
])
def test_release_requests_are_strict_and_bounded(preparation_harness, change, status):
    h = preparation_harness
    _prepared(h)
    body = {"protocol_version": "v1", "release_request_id": RELEASE}
    headers = {**OPERATOR, "content-type": "application/json"}
    if change == "empty":
        raw = b""
    elif change == "list":
        raw = b"[]"
    elif change == "malformed":
        raw = b"{"
    elif change == "duplicate":
        raw = json.dumps(body).replace("{", '{"release_request_id":"duplicate",', 1).encode()
    elif change == "oversize":
        raw = b" " * 2049
    else:
        if change == "unknown":
            body["released"] = True
        elif change == "missing":
            del body["release_request_id"]
        elif change == "request-id":
            body["release_request_id"] = "not-a-uuid"
        elif change == "nil-id":
            body["release_request_id"] = "00000000-0000-0000-0000-000000000000"
        elif change == "protocol":
            body["protocol_version"] = "v0"
        elif change == "content-type":
            headers["content-type"] = "text/plain"
        raw = json.dumps(body).encode()
    before = h.effects()
    response = h.client.post(PATH, headers=headers, content=raw)
    assert response.status_code == status, response.text
    assert response.headers["cache-control"] == "no-store"
    assert h.effects() == before
    assert read_release(h).status_code == 404


def test_release_route_arguments_are_validated(preparation_harness):
    h = preparation_harness
    assert read_release(h, epoch="not-a-uuid").status_code == 422
    assert release(h, epoch="not-a-uuid").status_code == 422


def test_release_of_an_unknown_preparation_is_404_and_leaves_no_record(preparation_harness):
    h = preparation_harness
    offline_worker(h)
    response = release(h)
    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"
    assert read_release(h).status_code == 404
