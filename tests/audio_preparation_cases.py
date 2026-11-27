"""Shared HTTP oracle for the opt-in audio preparation admission boundary.

All qualification material is synthetic test data. This suite neither grants
real rights nor starts or attests an audio engine.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib

from audio_epoch_acceptance import CLIENT, E1, E2, OPERATOR, WORKER_ID, envelope
from worker_identity_cases import OTHER, OTHER_ID, bearer

REQUEST = "32345678-1234-4234-9234-123456789abc"


def digest(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode()).hexdigest()


def qualification_for(harness, repository, *, worker_id=WORKER_ID,
                     reviewed=True, issued_at=None, expires_at=None,
                     **overrides):
    from tsumgi.control.audio_prepare import (
        AudioPreparationQualification, worker_spec_revision,
    )

    record = next(w for w in repository.list_workers() if w.worker_id == worker_id)
    profile = harness.profile.resolved
    fields = dict(
        worker_id=worker_id,
        deployment_revision=profile.deployment.revision,
        serving_contract_revision=profile.contract.revision,
        worker_spec_revision=worker_spec_revision(record),
        voice_semantic_revision=digest("synthetic voice semantics"),
        namespace_revision=digest("synthetic namespace"),
        trust_evidence_revision=digest("synthetic reviewed test data"),
        rights_evidence_revision=digest("synthetic reviewed test data"),
        privacy_evidence_revision=digest("synthetic reviewed test data"),
        review_authority_revision=digest("synthetic test reviewer"),
        reviewed=reviewed,
        issued_at=issued_at or datetime.now(UTC) - timedelta(seconds=1),
        expires_at=expires_at or datetime.now(UTC) + timedelta(seconds=120),
    )
    fields.update(overrides)
    qualification = AudioPreparationQualification(**fields)
    repository.store_audio_preparation_qualification(qualification)
    return qualification


def prepare(h, revision, *, epoch=E2, request_id=REQUEST,
            expected_intent_revision=0, worker_id=WORKER_ID, headers=None):
    return h.client.post(
        f"/v1/workers/{worker_id}/audio-epochs/{epoch}/prepare",
        headers=h.worker_headers if headers is None else headers,
        json={
            "protocol_version": "v1",
            "qualification_revision": revision,
            "expected_intent_revision": expected_intent_revision,
            "prepare_request_id": request_id,
        },
    )


PREPARATION_TABLES = (
    "audio_preparation_qualifications",
    "audio_preparation_qualification_revocations",
    "audio_epoch_preparations",
    "audio_epoch_preparation_releases",
)


def core_effects(h):
    """Effects on Workers, Jobs, events, fences and spool files.

    A successful preparation legitimately adds a preparation row (and a stored
    qualification is setup data), so the PostgreSQL observer's raw preparation
    tables are left out here; the preparation tables themselves are compared
    exactly wherever a request must have no effect at all.
    """
    state, files = h.effects()
    rows = state.get("database_rows") if isinstance(state, dict) else None
    if rows is not None:
        state = {**state, "database_rows": {k: v for k, v in rows.items() if k not in PREPARATION_TABLES}}
    return state, files


RELEASE = "52345678-1234-4234-9234-123456789abc"
RELEASE_2 = "62345678-1234-4234-9234-123456789abc"
EPOCH_3 = "32345678-1234-4234-9234-123456789def"


def release(h, epoch=E2, request_id=RELEASE, *, worker_id=WORKER_ID, headers=None):
    return h.client.post(
        f"/v1/admin/audio-epochs/{worker_id}/{epoch}/release",
        headers=OPERATOR if headers is None else headers,
        json={"protocol_version": "v1", "release_request_id": request_id},
    )


def read_release(h, epoch=E2, *, worker_id=WORKER_ID):
    return h.client.get(
        f"/v1/admin/audio-epochs/{worker_id}/{epoch}/release", headers=OPERATOR,
    )


def read_preparation(h, epoch=E2):
    return h.client.get(
        f"/v1/workers/{WORKER_ID}/audio-epochs/{epoch}", headers=h.worker_headers,
    )


def offline_worker(h):
    assert h.register(E1).status_code == 201
    assert h.state(E1, "offline").status_code == 200


def prepare_expired(h, repo):
    """A preparation recorded in the past, so it has already expired now."""
    now = datetime.now(UTC)
    q = qualification_for(
        h, repo, issued_at=now - timedelta(seconds=300), expires_at=now + timedelta(seconds=300),
    )
    record = repo.prepare_audio_epoch(
        WORKER_ID, E2, qualification_revision=q.qualification_revision,
        expected_intent_revision=0, prepare_request_id=REQUEST, ttl_seconds=60,
        now=now - timedelta(seconds=120),
    )
    assert record.expires_at < now
    return q, record


def force_online(repo, worker_id=WORKER_ID):
    """Put a prepared Worker online behind the API's back, to test the selection guard."""
    if hasattr(repo, "database_url"):
        import psycopg

        with psycopg.connect(repo.database_url) as connection:
            connection.execute(
                "UPDATE workers SET state = 'online', last_seen_at = clock_timestamp() "
                "WHERE id = %s", (worker_id,),
            )
        return
    from dataclasses import replace
    from tsumgi.control.models import WorkerState

    with repo._lock:
        repo._workers[worker_id] = replace(
            repo._workers[worker_id], state=WorkerState.ONLINE, last_seen_at=datetime.now(UTC),
        )


def submit_status(h):
    return h.client.post("/v1/jobs", headers=CLIENT, json=envelope(
        capability="audio.synthesize", serving=h.profile.binding.to_dict(),
        deadline_at=(datetime.now(UTC) + timedelta(seconds=300)).isoformat(),
        max_attempts=3, payload={"input": "synthetic epoch acceptance"},
    )).status_code


class AudioPreparationCases:
    def test_invalid_qualification_and_forged_body_have_no_effect(self, preparation_harness):
        h = preparation_harness
        repo = h.qualification_repository
        assert h.register(E1).status_code == 201
        assert h.state(E1, "offline").status_code == 200
        missing = digest("not stored")
        unreviewed = qualification_for(h, repo, reviewed=False)
        revoked = qualification_for(h, repo)
        repo.revoke_audio_preparation_qualification(revoked.qualification_revision)
        expired = qualification_for(
            h, repo, issued_at=datetime.now(UTC) - timedelta(seconds=120),
            expires_at=datetime.now(UTC) - timedelta(seconds=60),
        )
        before = h.effects()  # after the fixtures: only the requests below are measured
        assert prepare(h, missing).status_code == 409
        assert prepare(h, unreviewed.qualification_revision).status_code == 409
        assert prepare(h, revoked.qualification_revision).status_code == 409
        assert prepare(h, expired.qualification_revision).status_code == 409
        forged = {
            "protocol_version": "v1", "qualification_revision": missing,
            "expected_intent_revision": 0, "prepare_request_id": REQUEST,
            "reviewed": True, "rights_evidence_revision": digest("forged"),
        }
        response = h.client.post(
            f"/v1/workers/{WORKER_ID}/audio-epochs/{E2}/prepare",
            headers=h.worker_headers, json=forged,
        )
        assert response.status_code == 422
        assert h.effects() == before

    def test_owner_prepare_replay_revoke_and_historical_readback(self, preparation_harness):
        h = preparation_harness
        repo = h.qualification_repository
        assert h.register(E1).status_code == 201
        assert h.state(E1, "offline").status_code == 200
        q = qualification_for(h, repo)
        before = core_effects(h)
        response = prepare(h, q.qualification_revision)
        assert response.status_code == 200, response.text
        receipt = response.json()
        assert receipt["schema_version"] == "audio-epoch-preparation-v1"
        assert receipt["state"] == "prepared" and receipt["record_revision"] == 1
        assert receipt["can_claim"] is False and receipt["can_activate"] is False
        assert receipt["worker_id"] == WORKER_ID and receipt["runtime_instance_epoch"] == E2
        assert receipt["qualification_revision"] == q.qualification_revision
        assert receipt["expected_intent_revision"] == 0 and receipt["intent_revision"] == 1
        assert h.client.get(
            f"/v1/workers/{WORKER_ID}/audio-epochs/{E2}",
            headers=h.worker_headers,
        ).json() == receipt
        assert core_effects(h)[0] == before[0]
        assert prepare(h, q.qualification_revision).json() == receipt
        assert h.client.get(
            f"/v1/workers/{WORKER_ID}/audio-epochs/{E2}",
            headers=bearer(OTHER),
        ).status_code == 403
        repo.revoke_audio_preparation_qualification(q.qualification_revision)
        assert prepare(h, q.qualification_revision).status_code == 409
        historical = h.client.get(
            f"/v1/workers/{WORKER_ID}/audio-epochs/{E2}", headers=h.worker_headers,
        )
        assert historical.status_code == 200
        assert historical.json() == receipt
        assert historical.json()["can_claim"] is False
        assert historical.json()["can_activate"] is False
        # Revoking the qualification releases the Worker (it no longer holds it out);
        # the history above stays readable and unchanged.
        assert h.register(E2).status_code == 201
        assert h.client.get(
            f"/v1/workers/{WORKER_ID}/audio-epochs/{E2}", headers=h.worker_headers,
        ).json() == receipt

    def test_foreign_worker_qualification_and_credentials_are_rejected(self, preparation_harness):
        h = preparation_harness
        repo = h.qualification_repository
        assert h.register(E1).status_code == 201
        assert h.state(E1, "offline").status_code == 200
        # The other worker has a separately registered identity; its approved
        # synthetic qualification cannot authorize this worker's route.
        from worker_identity_cases import register
        assert register(h, OTHER_ID).status_code == 201
        q = qualification_for(h, repo, worker_id=OTHER_ID)
        before = h.effects()
        assert prepare(h, q.qualification_revision).status_code == 409
        for token in (OTHER, "unknown-synthetic-token", None):
            headers = {} if token is None else bearer(token)
            result = prepare(h, q.qualification_revision, headers=headers)
            assert result.status_code == (403 if token == OTHER else 401)
        assert h.effects() == before

    def test_prepare_does_not_admit_worker_lifecycle_or_job_claims(self, preparation_harness):
        h = preparation_harness
        repo = h.qualification_repository
        assert h.register(E1).status_code == 201
        assert h.state(E1, "offline").status_code == 200
        q = qualification_for(h, repo)
        receipt = prepare(h, q.qualification_revision).json()
        assert receipt["state"] == "prepared"
        assert h.register(E2).status_code == 409
        assert h.register("42345678-1234-4234-9234-123456789abc").status_code == 409
        assert h.claim(E1).status_code == 409
        assert h.claim(E2).status_code == 409
        assert h.state(E1, "online").status_code == 409

    def test_bad_request_shape_and_intent_conflict(self, preparation_harness):
        h = preparation_harness
        repo = h.qualification_repository
        assert h.register(E1).status_code == 201
        assert h.state(E1, "offline").status_code == 200
        q = qualification_for(h, repo)
        body = {
            "protocol_version": "v1", "qualification_revision": q.qualification_revision,
            "expected_intent_revision": 0, "prepare_request_id": REQUEST,
        }
        path = f"/v1/workers/{WORKER_ID}/audio-epochs/{E2}/prepare"
        assert h.client.post(path, headers=h.worker_headers,
                             content='{"protocol_version":"v1","protocol_version":"v1"}'
                             ).status_code == 422
        assert h.client.post(path, headers=h.worker_headers,
                             json={**body, "prepare_request_id": "not-a-uuid"}).status_code == 422
        assert prepare(h, q.qualification_revision, expected_intent_revision=1).status_code == 409

    def test_a_fenced_epoch_can_never_be_prepared(self, preparation_harness):
        h = preparation_harness
        repo = h.qualification_repository
        assert h.register(E1).status_code == 201
        assert h.state(E1, "offline").status_code == 200
        assert h.fence(E1).status_code == 200
        assert h.register(E2).status_code == 201
        assert h.state(E2, "offline").status_code == 200
        q = qualification_for(h, repo)
        before = h.effects()
        assert prepare(h, q.qualification_revision, epoch=E1).status_code == 409
        assert h.effects() == before

    def test_expiry_replay_and_request_id_reuse_are_conflicts_not_server_errors(self, preparation_harness):
        import pytest
        from tsumgi.control.repository import ConflictError

        h = preparation_harness
        repo = h.qualification_repository
        assert h.register(E1).status_code == 201
        assert h.state(E1, "offline").status_code == 200
        q = qualification_for(h, repo)
        t0 = datetime.now(UTC)
        first = repo.prepare_audio_epoch(
            WORKER_ID, E2, qualification_revision=q.qualification_revision,
            expected_intent_revision=0, prepare_request_id=REQUEST, ttl_seconds=60, now=t0,
        )
        assert first.expires_at == t0 + timedelta(seconds=60)
        later = t0 + timedelta(seconds=61)  # the first record has expired, the qualification has not
        with pytest.raises(ConflictError):  # an expired record is not replayed
            repo.prepare_audio_epoch(
                WORKER_ID, E2, qualification_revision=q.qualification_revision,
                expected_intent_revision=0, prepare_request_id=REQUEST, ttl_seconds=60, now=later,
            )
        epoch3 = "32345678-1234-4234-9234-123456789def"
        with pytest.raises(ConflictError):  # the request id is used up, across epochs
            repo.prepare_audio_epoch(
                WORKER_ID, epoch3, qualification_revision=q.qualification_revision,
                expected_intent_revision=1, prepare_request_id=REQUEST, ttl_seconds=60, now=later,
            )
        second = repo.prepare_audio_epoch(
            WORKER_ID, epoch3, qualification_revision=q.qualification_revision,
            expected_intent_revision=1, prepare_request_id="42345678-1234-4234-9234-123456789def",
            ttl_seconds=60, now=later,
        )
        assert second.intent_revision == 2
        assert second.expires_at == q.expires_at  # a qualification that ends sooner shortens the record

    def test_expiry_releases_the_guard(self, preparation_harness):
        h = preparation_harness
        repo = h.qualification_repository
        offline_worker(h)
        _, record = prepare_expired(h, repo)
        before = read_preparation(h).json()
        assert h.register(E2).status_code == 201
        assert h.state(E2, "online").status_code == 200
        queued = h.submit()
        claimed = h.claim(E2)
        assert claimed.status_code == 200, claimed.text
        assert claimed.json()["job_id"] == queued["job_id"]
        # History is not rewritten: the preparation reads back exactly as recorded.
        assert read_preparation(h).json() == before
        assert before["prepare_request_id"] == record.prepare_request_id

    def test_qualification_revocation_releases_the_guard(self, preparation_harness):
        h = preparation_harness
        repo = h.qualification_repository
        offline_worker(h)
        q = qualification_for(h, repo)
        receipt = prepare(h, q.qualification_revision).json()
        assert h.register(E2).status_code == 409
        repo.revoke_audio_preparation_qualification(q.qualification_revision)
        assert h.register(E2).status_code == 201
        assert h.state(E2, "online").status_code == 200
        h.submit()
        assert h.claim(E2).status_code == 200
        assert read_preparation(h).json() == receipt

    def test_explicit_release_releases_the_guard_and_keeps_history(self, preparation_harness):
        h = preparation_harness
        repo = h.qualification_repository
        offline_worker(h)
        q = qualification_for(h, repo)
        receipt = prepare(h, q.qualification_revision).json()
        assert h.register(E2).status_code == 409
        assert read_release(h).status_code == 404
        before = core_effects(h)
        response = release(h)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        released = response.json()
        assert released["protocol_version"] == "v1"
        assert released["schema_version"] == "audio-epoch-preparation-release-v1"
        assert released["state"] == "released"
        assert released["worker_id"] == WORKER_ID and released["runtime_instance_epoch"] == E2
        assert released["release_request_id"] == RELEASE
        assert released["released_at"].endswith("Z")
        assert set(released) == {
            "protocol_version", "schema_version", "state", "worker_id",
            "runtime_instance_epoch", "release_request_id", "released_at",
        }
        assert read_release(h).json() == released
        assert core_effects(h)[0] == before[0]  # a release does not touch Workers or Jobs
        assert read_preparation(h).json() == receipt
        assert h.register(E2).status_code == 201
        assert h.state(E2, "online").status_code == 200
        queued = h.submit()
        claimed = h.claim(E2)
        assert claimed.status_code == 200 and claimed.json()["job_id"] == queued["job_id"]

    def test_an_active_preparation_still_holds_the_worker_out(self, preparation_harness):
        h = preparation_harness
        repo = h.qualification_repository
        offline_worker(h)
        q = qualification_for(h, repo)
        prepare(h, q.qualification_revision)
        assert h.register(E2).status_code == 409
        assert h.register(E1).status_code == 409
        assert h.state(E1, "online").status_code == 409
        assert h.claim(E1).status_code == 409
        # Selection is guarded on its own: even an online Worker is not a candidate.
        force_online(repo)
        assert submit_status(h) == 503
        assert release(h).status_code == 200
        assert submit_status(h) == 201

    def test_release_is_idempotent_and_conflicts_are_409(self, preparation_harness):
        h = preparation_harness
        repo = h.qualification_repository
        offline_worker(h)
        q = qualification_for(h, repo)
        prepare(h, q.qualification_revision)
        first = release(h)
        assert first.status_code == 200
        before = h.effects()
        assert release(h).json() == first.json()  # same request id: unchanged receipt
        assert h.effects() == before
        assert release(h, request_id=RELEASE_2).status_code == 409
        assert read_release(h).json() == first.json()
        # Unknown preparation: 404, and nothing is recorded.
        assert release(h, epoch=EPOCH_3).status_code == 404
        assert read_release(h, epoch=EPOCH_3).status_code == 404
        # A request id already used by another preparation is a conflict.
        second = prepare(h, q.qualification_revision, epoch=EPOCH_3,
                         request_id="72345678-1234-4234-9234-123456789abc",
                         expected_intent_revision=1)
        assert second.status_code == 200, second.text
        assert release(h, epoch=EPOCH_3, request_id=RELEASE).status_code == 409
        assert read_release(h, epoch=EPOCH_3).status_code == 404
        assert release(h, epoch=EPOCH_3, request_id=RELEASE_2).status_code == 200

    def test_expired_or_revoked_preparations_can_still_be_released(self, preparation_harness):
        h = preparation_harness
        repo = h.qualification_repository
        offline_worker(h)
        q, _ = prepare_expired(h, repo)
        assert release(h).status_code == 200
        assert read_release(h).status_code == 200
        revoked = qualification_for(h, repo)
        assert prepare(h, revoked.qualification_revision, epoch=EPOCH_3,
                       request_id="72345678-1234-4234-9234-123456789abc",
                       expected_intent_revision=1).status_code == 200
        repo.revoke_audio_preparation_qualification(revoked.qualification_revision)
        assert release(h, epoch=EPOCH_3, request_id=RELEASE_2).status_code == 200

    def test_new_preparation_after_release_needs_the_right_intent_revision(self, preparation_harness):
        h = preparation_harness
        repo = h.qualification_repository
        offline_worker(h)
        q = qualification_for(h, repo)
        first = prepare(h, q.qualification_revision)
        assert first.status_code == 200
        # While the first is active a second intent is refused ...
        assert prepare(h, q.qualification_revision, epoch=EPOCH_3,
                       request_id="72345678-1234-4234-9234-123456789abc",
                       expected_intent_revision=1).status_code == 409
        assert release(h).status_code == 200
        # ... a released preparation is not replayed as active ...
        assert prepare(h, q.qualification_revision).status_code == 409
        # ... and after release the next intent is recorded with the right revision.
        assert prepare(h, q.qualification_revision, epoch=EPOCH_3,
                       request_id="72345678-1234-4234-9234-123456789abc",
                       expected_intent_revision=0).status_code == 409
        third = prepare(h, q.qualification_revision, epoch=EPOCH_3,
                        request_id="72345678-1234-4234-9234-123456789abc",
                        expected_intent_revision=1)
        assert third.status_code == 200, third.text
        assert third.json()["intent_revision"] == 2
        assert h.register(E2).status_code == 409  # the new preparation is active again
        assert read_preparation(h).json() == first.json()
