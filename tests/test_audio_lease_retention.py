"""#318 G2 (audio claim lease) and G7 (text retention): one set of cases, run
against the in-memory repository here and against PostgreSQL in
test_audio_lease_retention_postgres.py with identical expectations.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from tsumgi import ResourceShape, WorkerSpec
from tsumgi.control import (
    InMemoryControlRepository, JobStatus, JobSubmission, WorkerRegistration,
)
from tsumgi.control.models import WorkerHeartbeat
from tsumgi.execution import JobResult
from test_audio_artifact_authority import EPOCH, serving_values


@pytest.fixture
def clock(monkeypatch):
    value = SimpleNamespace(now=datetime(2030, 1, 1, tzinfo=timezone.utc))
    monkeypatch.setattr("tsumgi.control.repository.utc_now", lambda: value.now)
    monkeypatch.setattr("tsumgi.control.postgres.utc_now", lambda: value.now)
    return value


@pytest.fixture
def make_repo():
    return lambda **kw: InMemoryControlRepository(**kw)


def audio_worker(repo, now, capability):
    binding, serving = serving_values(capability)
    repo.register_worker(WorkerRegistration(
        spec=WorkerSpec(worker_id="owner", worker_class="cpu-test",
                        resources=ResourceShape(), capabilities=frozenset({capability})),
        serving=serving), now=now)
    return binding


def submit_audio(repo, now, capability="audio.synthesize", *, payload=None,
                 deadline=120, key=None):
    binding = audio_worker(repo, now, capability) if "owner" not in {
        w.spec.worker_id for w in repo.list_workers()} else serving_values(capability)[0]
    return repo.submit_job(JobSubmission(
        capability=capability, serving=binding,
        deadline_at=now + timedelta(seconds=deadline), max_attempts=1,
        payload={"input": "private spoken text", "voice": {"alias": "a"},
                 "response_format": "wav"} if payload is None else payload,
        idempotency_key=key), now=now)


class LeaseRetentionCases:
    # ---------------- G2: audio claim lease ----------------

    def test_audio_job_gets_the_audio_lease_and_renewal_keeps_it(self, make_repo, clock):
        repo = make_repo(lease_seconds=300, audio_lease_seconds=20)
        job = submit_audio(repo, clock.now)
        claim = repo.claim_next_job("owner", runtime_instance_epoch=EPOCH, now=clock.now)
        assert claim.lease_expires_at == clock.now + timedelta(seconds=20)
        clock.now += timedelta(seconds=10)
        repo.heartbeat_worker("owner", WorkerHeartbeat(
            active_job_id=job.job_id, lease_token=claim.lease_token,
            runtime_instance_epoch=EPOCH), now=clock.now)
        assert repo.get_job(job.job_id).lease_expires_at == clock.now + timedelta(seconds=20)

    def test_unconfirmed_audio_failure_ends_before_the_request_deadline(self, make_repo, clock):
        repo = make_repo(lease_seconds=300, audio_lease_seconds=20)
        job = submit_audio(repo, clock.now, deadline=120)
        repo.claim_next_job("owner", runtime_instance_epoch=EPOCH, now=clock.now)
        # The Worker vanished or could not confirm: no complete/fail reaches Control.
        assert repo.recover_expired_jobs(now=clock.now + timedelta(seconds=19)) == []
        recovered = repo.recover_expired_jobs(now=clock.now + timedelta(seconds=21))
        assert [r.job_id for r in recovered] == [job.job_id]
        final = repo.get_job(job.job_id)
        assert final.status is JobStatus.FAILED  # the Gateway maps this to 502 at ~21 s
        assert final.finished_at < job.deadline_at

    def test_without_the_option_audio_keeps_the_generic_lease(self, make_repo, clock):
        repo = make_repo(lease_seconds=60)
        submit_audio(repo, clock.now)
        claim = repo.claim_next_job("owner", runtime_instance_epoch=EPOCH, now=clock.now)
        assert claim.lease_expires_at == clock.now + timedelta(seconds=60)

    def test_non_audio_job_keeps_the_generic_lease(self, make_repo, clock):
        repo = make_repo(lease_seconds=300, audio_lease_seconds=20)
        repo.register_worker(WorkerRegistration(spec=WorkerSpec(
            worker_id="plain", worker_class="cpu-test", resources=ResourceShape(),
            capabilities=frozenset({"generic.echo"}))), now=clock.now)
        job = repo.submit_job(JobSubmission(capability="generic.echo", payload={"x": 1}),
                              now=clock.now)
        claim = repo.claim_next_job("plain", now=clock.now)
        assert claim.job_id == job.job_id
        assert claim.lease_expires_at == clock.now + timedelta(seconds=300)

    @pytest.mark.parametrize("value", [0, -1, True, 1.5, "20"])
    def test_audio_lease_is_validated(self, make_repo, value):
        with pytest.raises(ValueError):
            make_repo(audio_lease_seconds=value)


class TestInMemory(LeaseRetentionCases):
    pass
