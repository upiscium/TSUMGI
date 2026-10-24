"""Issue #174: call-time authority, not a grant for future publication.

The same independent oracle is inherited by the real PostgreSQL suite. Shared
Worker Bearer authentication remains ROLE-only; this predicate fences claims.
"""
from __future__ import annotations

from copy import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from astrumweaver import ResourceShape, WorkerSpec
from astrumweaver.control import (
    ConflictError, InMemoryControlRepository, JobStatus, JobSubmission,
    WorkerRegistration, WorkerState,
)
from astrumweaver.execution import JobResult
from astrumweaver.serving import (
    DeploymentIdentity, LogicalServingProfile, RuntimeInstance, ServingContract,
    ServingJobBinding, WorkerServingAdvertisement, resolve_profile,
)
from astrumweaver.transport import job_record_to_dict

EPOCH = "12345678-1234-4234-9234-123456789abc"
NEXT_EPOCH = "22345678-1234-4234-9234-123456789abc"
REJECTION = "audio artifact authority rejected"


def serving_values(capability, epoch=EPOCH):
    digest = lambda c: "sha256:" + c * 64
    deployment = DeploymentIdentity(
        provider_id="audio-test", runtime_artifact_sha256=digest("1"),
        adapter_artifact_sha256=digest("2"), model_artifact_sha256=digest("3"),
        execution_config_sha256=digest("4"), quantization="none",
    )
    contract = ServingContract(
        deployment_revision=deployment.revision, capability=capability,
        operation_schema="audio-v1", validation_evidence_sha256=digest("7"),
    )
    profile = LogicalServingProfile(
        profile_id="audio-test", deployment_revision=deployment.revision,
        serving_contract_revision=contract.revision, capability=capability,
        operation_schema="audio-v1",
    )
    binding = ServingJobBinding.from_resolved(resolve_profile(profile, contract, deployment))
    advertisement = WorkerServingAdvertisement(
        deployment_revision=deployment.revision,
        runtime_instance=RuntimeInstance(deployment.revision, epoch), contracts=(contract,),
    )
    return binding, advertisement


def setup_claim(repo, now, capability="audio.synthesize", *, deadline_seconds=120):
    binding, serving = serving_values(capability)
    for worker_id in ("owner", "other"):
        repo.register_worker(WorkerRegistration(
            spec=WorkerSpec(worker_id=worker_id, worker_class="cpu-test",
                            resources=ResourceShape(), capabilities=frozenset({capability})),
            serving=serving,
        ), now=now)
    queued = repo.submit_job(JobSubmission(
        capability=capability, serving=binding,
        deadline_at=now + timedelta(seconds=deadline_seconds), max_attempts=3,
        payload={"private": "private-audio-payload"},
    ), now=now)
    claim = repo.claim_next_job("owner", runtime_instance_epoch=EPOCH, now=now)
    assert claim is not None and claim.job_id == queued.job_id
    assert claim.status is JobStatus.RUNNING and claim.attempts == 1
    assert claim.lease_token and claim.claimed_runtime_instance_epoch == EPOCH
    return claim


def credentials(claim):
    return dict(worker_id="owner", attempt=claim.attempts,
                lease_token=claim.lease_token, runtime_instance_epoch=EPOCH)


def authorize(repo, claim, **changes):
    args = credentials(claim)
    args.update(changes)
    return repo._authorize_audio_artifact_attempt(claim.job_id, **args)


def snapshot(repo, claim):
    return (repo.get_job(claim.job_id), repo.get_worker("owner"),
            repo.get_worker("other"), repo.list_job_events(claim.job_id))


def rejected(repo, claim, *, job_id=None, **changes):
    before = snapshot(repo, claim)
    args = credentials(claim)
    args.update(changes)
    with pytest.raises(ConflictError) as caught:
        repo._authorize_audio_artifact_attempt(job_id or claim.job_id, **args)
    # A constant error prevents job existence, owner, payload, and secret leaks.
    assert str(caught.value) == REJECTION
    assert caught.value.args == (REJECTION,)
    for secret in (claim.lease_token, EPOCH, "private-audio-payload", claim.job_id):
        assert secret not in repr(caught.value)
    assert snapshot(repo, claim) == before


@pytest.fixture
def clock(monkeypatch):
    value = SimpleNamespace(now=datetime(2030, 1, 1, tzinfo=timezone.utc))
    # Existing clock sources only: no production API injection or DB mocks.
    monkeypatch.setattr("astrumweaver.control.repository.utc_now", lambda: value.now)
    monkeypatch.setattr("astrumweaver.control.postgres.utc_now", lambda: value.now)
    return value


@pytest.fixture
def authority_repo():
    return InMemoryControlRepository(lease_seconds=30)


class AuthorityCases:
    @pytest.mark.asyncio
    async def test_worker_inspection_does_not_disclose_other_claim(self, authority_repo, clock):
        import httpx
        from astrumweaver.control.api import create_app
        claim = setup_claim(authority_repo, clock.now)
        app = create_app(authority_repo, client_token="client-secret", worker_token="worker-secret")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://control") as client:
            response = await client.get(
                f"/v1/workers/other/jobs/{claim.job_id}",
                headers={"authorization": "Bearer worker-secret"},
            )
            assert response.status_code == 404
            for secret in (claim.lease_token, EPOCH, "private-audio-payload", "owner"):
                assert secret not in response.text
            own = await client.get(
                f"/v1/workers/owner/jobs/{claim.job_id}",
                headers={"authorization": "Bearer worker-secret"},
            )
            assert own.status_code == 200
            assert "lease_token" not in own.json()
            assert "authorization_token" not in own.json()
            assert claim.lease_token not in own.text

    @pytest.mark.parametrize("capability", ["audio.synthesize", "audio.transcribe"])
    def test_current_claim_returns_none_not_a_transferable_grant(self, authority_repo, clock, capability):
        repo = authority_repo
        claim = setup_claim(repo, clock.now, capability)
        before = snapshot(repo, claim)
        assert authorize(repo, claim) is None
        assert authorize(repo, claim) is None  # A check is not consumption/publication.
        assert snapshot(repo, claim) == before
        # Generic v0.1.2 Job JSON intentionally still carries its lease token.
        assert job_record_to_dict(repo.get_job(claim.job_id))["lease_token"] == claim.lease_token

    @pytest.mark.parametrize("changes", [
        {"worker_id": "other"}, {"worker_id": "unknown-worker"},
        {"lease_token": ""}, {"lease_token": " \t"}, {"lease_token": "wrong-secret"},
        {"lease_token": None}, {"attempt": 0}, {"attempt": -1}, {"attempt": 2},
        {"attempt": True}, {"attempt": False}, {"attempt": 1.0}, {"attempt": "1"},
        {"runtime_instance_epoch": ""}, {"runtime_instance_epoch": " "},
        {"runtime_instance_epoch": None}, {"runtime_instance_epoch": NEXT_EPOCH},
    ])
    def test_bad_identity_is_redacted_and_read_only(self, authority_repo, clock, changes):
        claim = setup_claim(authority_repo, clock.now)
        rejected(authority_repo, claim, **changes)

    def test_unknown_job_has_same_secret_free_error(self, authority_repo, clock):
        claim = setup_claim(authority_repo, clock.now)
        rejected(authority_repo, claim, job_id="00000000-0000-4000-8000-000000000000")

    @pytest.mark.parametrize("capability", ["llm.chat", "embedding.embed", "decision.system_one"])
    def test_non_audio_current_claim_is_not_authorized(self, authority_repo, clock, capability):
        claim = setup_claim(authority_repo, clock.now, capability)
        rejected(authority_repo, claim)

    @pytest.mark.parametrize("state,allowed", [(WorkerState.DRAINING, True), (WorkerState.OFFLINE, False)])
    def test_lifecycle(self, authority_repo, clock, state, allowed):
        repo = authority_repo
        claim = setup_claim(repo, clock.now)
        repo.set_worker_state("owner", state, runtime_instance_epoch=EPOCH, now=clock.now)
        if allowed:
            before = snapshot(repo, claim)
            assert authorize(repo, claim) is None
            assert snapshot(repo, claim) == before
        else:
            rejected(repo, claim)
        repo.cancel_job(claim.job_id, now=clock.now)
        rejected(repo, claim)  # DRAINING never revives a released claim.

    @pytest.mark.parametrize("boundary", ["lease", "deadline"])
    @pytest.mark.parametrize("offset,allowed", [(-1, True), (0, False), (1, False)])
    def test_strict_time_boundaries(self, authority_repo, clock, boundary, offset, allowed):
        repo = authority_repo
        claim = setup_claim(repo, clock.now, deadline_seconds=10 if boundary == "deadline" else 120)
        end = claim.deadline_at if boundary == "deadline" else claim.lease_expires_at
        clock.now = end + timedelta(microseconds=offset)
        if allowed:
            assert authorize(repo, claim) is None
        else:
            rejected(repo, claim)

    @pytest.mark.parametrize("transition", ["cancel", "fail", "success", "retry", "recover", "rotate"])
    def test_transitions_invalidate_old_authority(self, authority_repo, clock, transition):
        repo = authority_repo
        claim = setup_claim(repo, clock.now)
        assert authorize(repo, claim) is None
        args = credentials(claim)
        args.pop("attempt")
        if transition == "cancel":
            repo.cancel_job(claim.job_id, now=clock.now)
        elif transition == "success":
            repo.complete_job(claim.job_id, JobResult(text="done"), now=clock.now, **args)
        elif transition in {"fail", "retry"}:
            repo.fail_job(claim.job_id, "failure", retryable=transition == "retry", now=clock.now, **args)
        else:
            clock.now = claim.lease_expires_at
            assert [j.job_id for j in repo.recover_expired_jobs(now=clock.now)] == [claim.job_id]
        rejected(repo, claim)
        if transition in {"retry", "recover", "rotate"}:
            epoch = EPOCH
            if transition == "rotate":
                epoch = NEXT_EPOCH
                current = repo.get_worker("owner")
                _, serving = serving_values(claim.capability, epoch)
                repo.register_worker(WorkerRegistration(spec=current.spec, serving=serving), now=clock.now)
            newer = repo.claim_next_job("owner", runtime_instance_epoch=epoch, now=clock.now)
            assert newer is not None and newer.attempts == 2
            assert newer.lease_token != claim.lease_token
            rejected(repo, claim)
            rejected(repo, newer, attempt=1, runtime_instance_epoch=epoch)
            if epoch != EPOCH:
                rejected(repo, newer)
            assert authorize(repo, newer, runtime_instance_epoch=epoch) is None


class TestInMemoryAudioAuthority(AuthorityCases):
    pass


@pytest.mark.parametrize("field,value", [
    ("serving", None), ("serving", {"capability": "audio.synthesize"}),
    ("claimed_runtime_instance_epoch", None), ("claimed_runtime_instance_epoch", NEXT_EPOCH),
    ("claimed_deployment_revision", "sha256:" + "a" * 64),
    ("claimed_serving_contract_revision", "sha256:" + "b" * 64),
    ("lease_token", ""), ("lease_expires_at", None), ("deadline_at", None),
    ("attempts", 0), ("status", JobStatus.QUEUED),
])
def test_corrupt_memory_claim_fails_closed(authority_repo, clock, field, value):
    claim = setup_claim(authority_repo, clock.now)
    damaged = copy(claim)
    # Deliberate invariant bypass models corrupt authoritative state, not input validation.
    object.__setattr__(damaged, field, value)
    authority_repo._jobs[claim.job_id] = damaged
    rejected(authority_repo, claim)


@pytest.mark.parametrize("damage", ["unsupported", "epoch", "no-serving", "incompatible"])
def test_corrupt_memory_worker_fails_closed(authority_repo, clock, damage):
    claim = setup_claim(authority_repo, clock.now)
    worker = copy(authority_repo.get_worker("owner"))
    if damage == "unsupported":
        object.__setattr__(worker, "state", "STARTING")
    elif damage == "no-serving":
        object.__setattr__(worker, "serving", None)
    else:
        _, advertisement = serving_values("llm.chat" if damage == "incompatible" else claim.capability,
                                           NEXT_EPOCH if damage == "epoch" else EPOCH)
        object.__setattr__(worker, "serving", advertisement)
    authority_repo._workers["owner"] = worker
    rejected(authority_repo, claim)
