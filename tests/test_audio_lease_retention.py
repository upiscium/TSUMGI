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


    # ---------------- G7: bounded retention of audio text ----------------

    def _finish(self, repo, clock, job, claim, result):
        repo.complete_job(job.job_id, result, worker_id="owner",
                          lease_token=claim.lease_token,
                          runtime_instance_epoch=EPOCH, now=clock.now)

    def test_tts_input_is_redacted_after_the_retention_and_nothing_else_changes(
        self, make_repo, clock,
    ):
        from tsumgi.control.repository import AUDIO_TEXT_REDACTED
        repo = make_repo()
        job = submit_audio(repo, clock.now)
        claim = repo.claim_next_job("owner", runtime_instance_epoch=EPOCH, now=clock.now)
        self._finish(repo, clock, job, claim, JobResult(metrics={"seconds": 1.5}))
        before = repo.get_job(job.job_id)
        clock.now += timedelta(seconds=3599)
        assert repo.redact_audio_job_text(3600, now=clock.now) == 0
        assert repo.get_job(job.job_id) == before
        clock.now += timedelta(seconds=2)
        assert repo.redact_audio_job_text(3600, now=clock.now) == 1
        after = repo.get_job(job.job_id)
        assert after.payload["input"] == AUDIO_TEXT_REDACTED
        assert after.payload["voice"] == {"alias": "a"}
        assert after.payload["response_format"] == "wav"
        for field in ("job_id", "status", "capability", "created_at", "started_at",
                      "finished_at", "attempts", "serving", "deadline_at", "result"):
            assert getattr(after, field) == getattr(before, field), field
        assert "private spoken text" not in repr(after)
        assert repo.redact_audio_job_text(3600, now=clock.now) == 0  # idempotent

    def test_stt_transcript_is_redacted_and_the_digest_payload_is_kept(self, make_repo, clock):
        from tsumgi.control.repository import AUDIO_TEXT_REDACTED
        repo = make_repo()
        receipt = {"uri": "tsumgi-audio-input:x", "media_type": "audio/wav",
                   "digest": "sha256:" + "a" * 64, "size_bytes": 10}
        job = submit_audio(repo, clock.now, "audio.transcribe", payload={
            "language": "ja", "response_format": "json", "audio_input": receipt})
        claim = repo.claim_next_job("owner", runtime_instance_epoch=EPOCH, now=clock.now)
        self._finish(repo, clock, job, claim, JobResult(text=" 秘密の文字起こし\n"))
        assert repo.get_job(job.job_id).result.text == " 秘密の文字起こし\n"
        clock.now += timedelta(seconds=10)
        assert repo.redact_audio_job_text(5, now=clock.now) == 1
        after = repo.get_job(job.job_id)
        assert after.result.text == AUDIO_TEXT_REDACTED
        assert after.status is JobStatus.SUCCEEDED
        assert dict(after.payload["audio_input"]) == receipt
        assert "秘密" not in repr(after)

    @pytest.mark.parametrize("terminal", ["failed", "cancelled"])
    def test_failed_and_cancelled_tts_text_is_redacted_too(self, make_repo, clock, terminal):
        from tsumgi.control.repository import AUDIO_TEXT_REDACTED
        repo = make_repo()
        job = submit_audio(repo, clock.now)
        claim = repo.claim_next_job("owner", runtime_instance_epoch=EPOCH, now=clock.now)
        if terminal == "failed":
            repo.fail_job(job.job_id, "boom", retryable=False, worker_id="owner",
                          lease_token=claim.lease_token, runtime_instance_epoch=EPOCH,
                          now=clock.now)
        else:
            repo.cancel_job(job.job_id, now=clock.now)
        clock.now += timedelta(seconds=100)
        assert repo.redact_audio_job_text(60, now=clock.now) == 1
        assert repo.get_job(job.job_id).payload["input"] == AUDIO_TEXT_REDACTED

    def test_non_terminal_and_non_audio_jobs_are_never_touched(self, make_repo, clock):
        repo = make_repo()
        queued = submit_audio(repo, clock.now)  # queued, not terminal
        repo.register_worker(WorkerRegistration(spec=WorkerSpec(
            worker_id="plain", worker_class="cpu-test", resources=ResourceShape(),
            capabilities=frozenset({"generic.echo"}))), now=clock.now)
        generic = repo.submit_job(JobSubmission(
            capability="generic.echo", payload={"input": "keep me"}), now=clock.now)
        repo.cancel_job(generic.job_id, now=clock.now)
        clock.now += timedelta(days=30)
        assert repo.redact_audio_job_text(1, now=clock.now) == 0
        assert repo.get_job(queued.job_id).payload["input"] == "private spoken text"
        assert repo.get_job(generic.job_id).payload["input"] == "keep me"

    def test_limit_bounds_one_pass_and_arguments_are_validated(self, make_repo, clock):
        repo = make_repo()
        jobs = [submit_audio(repo, clock.now, key=f"k{i}") for i in range(3)]
        for job in jobs:
            repo.cancel_job(job.job_id, now=clock.now)
        clock.now += timedelta(seconds=10)
        assert repo.redact_audio_job_text(1, now=clock.now, limit=2) == 2
        assert repo.redact_audio_job_text(1, now=clock.now, limit=2) == 1
        for bad in (-1, True, 1.5):
            with pytest.raises(ValueError):
                repo.redact_audio_job_text(bad)


class TestInMemory(LeaseRetentionCases):
    pass
