"""Shared HTTP oracle for the opt-in audio preparation admission boundary.

All qualification material is synthetic test data. This suite neither grants
real rights nor starts or attests an audio engine.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib

from audio_epoch_acceptance import E1, E2, WORKER_ID
from worker_identity_cases import OTHER, OTHER_ID, bearer

REQUEST = "32345678-1234-4234-9234-123456789abc"


def digest(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode()).hexdigest()


def qualification_for(harness, repository, *, worker_id=WORKER_ID,
                     reviewed=True, issued_at=None, expires_at=None,
                     **overrides):
    from astrumweaver.control.audio_prepare import (
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
        assert h.register(E2).status_code == 409

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
        assert h.claim(E1).status_code in (204, 409)
        assert h.claim(E2).status_code in (204, 409)
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
