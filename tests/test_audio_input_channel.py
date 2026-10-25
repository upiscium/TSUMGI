"""Issue #174 narrow staging/admission component tests."""

from types import SimpleNamespace
from datetime import UTC, datetime, timedelta

import pytest

from astrumweaver.audio.artifact_spool import ArtifactSpool
from astrumweaver.audio.input_channel import AudioInputChannel
from astrumweaver.control.models import JobSubmission
from astrumweaver.control.repository import ConflictError, NotFoundError
from test_audio_artifact_authority_postgres import isolated_database
from test_audio_artifact_authority import serving_values


def transcribe_submission(idempotency_key="input-r1"):
    binding, _ = serving_values("audio.transcribe")
    return JobSubmission(
        capability="audio.transcribe", serving=binding,
        deadline_at=datetime.now(UTC) + timedelta(seconds=120),
        idempotency_key=idempotency_key,
    )


WAV = b"RIFF" + (12).to_bytes(4, "little") + b"WAVE" + b"abcd"


class FakeRepository:
    def __init__(self, error=None):
        self.error = error
        self.submissions = []

    def submit_job(self, submission):
        self.submissions.append(submission)
        if self.error:
            raise self.error
        return SimpleNamespace(job_id="job-staged")


def make_channel(tmp_path, repo):
    root = tmp_path / "input"
    root.mkdir(mode=0o700)
    spool = ArtifactSpool(root, max_bytes=64, ttl_seconds=300,
                          max_total_bytes=128, max_artifacts=2)
    return AudioInputChannel(repo, spool, max_bytes=64)


def test_stage_and_submit_keeps_only_reference_metadata(tmp_path):
    repo = FakeRepository()
    channel = make_channel(tmp_path, repo)
    ref = channel.stage(WAV)
    assert ref["uri"].startswith("tsumgi-audio-input:")
    assert ref["size_bytes"] == len(WAV)
    assert WAV not in repr(ref).encode()
    request = transcribe_submission()
    job = channel.submit(request, artifact_uri=ref["uri"])
    assert job.job_id == "job-staged"
    submitted = repo.submissions[0]
    assert submitted.payload["audio_input"] == ref
    assert WAV not in repr(submitted.payload).encode()
    assert channel.submit(request, artifact_uri=ref["uri"]).job_id == job.job_id


@pytest.mark.parametrize("body", [b"", b"broken", b"RIFFxxxxbad!"])
def test_invalid_audio_does_not_allocate(tmp_path, body):
    channel = make_channel(tmp_path, FakeRepository())
    with pytest.raises(ValueError):
        channel.stage(body)
    assert channel._inputs == {}


def test_failed_job_admission_removes_staging_and_releases_quota(tmp_path):
    repo = FakeRepository(ValueError("admission failed"))
    channel = make_channel(tmp_path, repo)
    ref = channel.stage(WAV)
    with pytest.raises(ValueError, match="admission failed"):
        channel.submit(transcribe_submission(),
                       artifact_uri=ref["uri"])
    assert channel._inputs == {}
    assert channel.spool._records == {}
    repo.error = None
    next_ref = channel.stage(WAV)
    assert next_ref["uri"] != ref["uri"]


def test_invalid_binding_does_not_touch_staged_input(tmp_path):
    channel = make_channel(tmp_path, FakeRepository())
    ref = channel.stage(WAV)
    with pytest.raises(ConflictError):
        channel.submit(JobSubmission(capability="audio.synthesize"),
                       artifact_uri=ref["uri"])
    assert len(channel._inputs) == 1
    channel.discard(ref["uri"])
    assert channel._inputs == {}
    with pytest.raises(NotFoundError):
        channel.discard(ref["uri"])


def test_finite_quotas_required(tmp_path):
    root = tmp_path / "input"
    root.mkdir(mode=0o700)
    spool = ArtifactSpool(root, max_bytes=64, ttl_seconds=30)
    with pytest.raises(ValueError, match="finite"):
        AudioInputChannel(FakeRepository(), spool, max_bytes=64)

@pytest.mark.asyncio
async def test_authenticated_input_api_and_current_worker_download(tmp_path):
    from datetime import UTC, datetime, timedelta
    import httpx
    from astrumweaver.control import InMemoryControlRepository
    from astrumweaver.control.api import create_app
    from astrumweaver.transport import PROTOCOL_VERSION, SERVING_EXTENSION
    from test_audio_artifact_authority import EPOCH, setup_claim

    repo = InMemoryControlRepository(lease_seconds=60)
    claimed = setup_claim(repo, datetime.now(UTC), capability="audio.transcribe")
    channel = make_channel(tmp_path, repo)
    app = create_app(
        repo, client_token="client-secret", worker_token="worker-secret",
        audio_input_channel=channel,
    )
    client_headers = {"authorization": "Bearer client-secret"}
    worker_headers = {"authorization": "Bearer worker-secret"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://control",
    ) as http:
        assert (await http.post(
            "/v1/audio/inputs", content=WAV, headers=worker_headers,
        )).status_code == 401
        staged = await http.post(
            "/v1/audio/inputs", content=WAV,
            headers={**client_headers, "content-type": "audio/wav"},
        )
        assert staged.status_code == 201, staged.text
        reference = staged.json()
        body = {
            "protocol_version": PROTOCOL_VERSION,
            "extensions": [SERVING_EXTENSION],
            "capability": "audio.transcribe",
            "serving": claimed.serving.to_dict(),
            "deadline_at": (datetime.now(UTC) + timedelta(seconds=120)).isoformat(),
            "idempotency_key": "transcribe-input-api-r1",
            "audio_input_uri": reference["uri"],
        }
        admitted = await http.post(
            "/v1/audio/inputs/jobs", json=body, headers=client_headers,
        )
        assert admitted.status_code == 201, admitted.text
        job_id = admitted.json()["job_id"]
        assert WAV not in admitted.content
        assert admitted.json()["payload"]["audio_input"] == reference
        next_job = repo.claim_next_job("other", runtime_instance_epoch=EPOCH)
        assert next_job and next_job.job_id == job_id
        endpoint = f"/v1/workers/other/jobs/{job_id}/audio/input"
        claim_headers = {
            **worker_headers, "x-audio-attempt": str(next_job.attempts),
            "x-audio-lease-token": next_job.lease_token,
            "x-audio-runtime-epoch": EPOCH,
        }
        assert (await http.get(endpoint, headers=client_headers)).status_code == 401
        assert (await http.get(endpoint, headers={
            **claim_headers, "x-audio-lease-token": "wrong",
        })).status_code == 409
        download = await http.get(endpoint, headers=claim_headers)
        assert download.status_code == 200, download.text
        assert download.content == WAV
        repo.cancel_job(job_id)
        assert (await http.get(endpoint, headers=claim_headers)).status_code == 409

def test_postgres_input_admission_and_failure_cleanup(tmp_path, isolated_database):
    from datetime import UTC, datetime, timedelta
    from astrumweaver.control import PostgresControlRepository
    from test_audio_artifact_authority import EPOCH, setup_claim

    repo = PostgresControlRepository(isolated_database, lease_seconds=60)
    existing = setup_claim(repo, datetime.now(UTC), capability="audio.transcribe")
    channel = make_channel(tmp_path, repo)
    ref = channel.stage(WAV)

    # A valid durable serving submission references only metadata.
    submission = JobSubmission(
        capability="audio.transcribe",
        serving=existing.serving,
        deadline_at=datetime.now(UTC) + timedelta(seconds=120),
        idempotency_key="pg-audio-input-r1",
    )
    job = channel.submit(submission, artifact_uri=ref["uri"])
    stored = repo.get_job(job.job_id)
    assert stored.payload["audio_input"] == ref
    assert WAV not in repr(stored.payload).encode()
    claim = repo.claim_next_job("other", runtime_instance_epoch=EPOCH)
    assert claim and claim.job_id == job.job_id
    assert channel.read_for_worker(
        job.job_id, worker_id="other", attempt=claim.attempts,
        lease_token=claim.lease_token, runtime_instance_epoch=EPOCH,
    ) == WAV
    repo.cancel_job(job.job_id)
    with pytest.raises(ConflictError):
        channel.read_for_worker(
            job.job_id, worker_id="other", attempt=claim.attempts,
            lease_token=claim.lease_token, runtime_instance_epoch=EPOCH,
        )

    # Invalid Job admission must delete a separate staged input.
    second = channel.stage(WAV)
    invalid = JobSubmission(
        capability="audio.transcribe",
        serving=existing.serving,
        deadline_at=datetime.now(UTC) - timedelta(seconds=1),
        idempotency_key="pg-invalid-audio-r1",
    )
    with pytest.raises(Exception):
        channel.submit(invalid, artifact_uri=second["uri"])
    assert second["uri"].removeprefix("tsumgi-audio-input:") not in channel._inputs


def test_ambiguous_commit_preserves_input_for_idempotent_retry(tmp_path):
    from astrumweaver.control.repository import StorageUnavailable
    repo = FakeRepository(StorageUnavailable("commit outcome unknown"))
    channel = make_channel(tmp_path, repo)
    ref = channel.stage(WAV)
    request = transcribe_submission("stable-commit-retry")
    with pytest.raises(StorageUnavailable):
        channel.submit(request, artifact_uri=ref["uri"])
    ident = ref["uri"].removeprefix("tsumgi-audio-input:")
    assert ident in channel._inputs
    assert channel._inputs[ident].job_id is None
    assert channel._inputs[ident].submission.idempotency_key == request.idempotency_key
    assert channel._inputs[ident].submission.payload["audio_input"] == ref
    assert ident in channel.spool._records

    # Reconciliation through the repository's existing idempotency identity.
    repo.error = None
    assert channel.submit(request, artifact_uri=ref["uri"]).job_id == "job-staged"
    assert channel._inputs[ident].job_id == "job-staged"


def test_nonserving_or_nonidempotent_submission_fails_before_consuming_input(tmp_path):
    channel = make_channel(tmp_path, FakeRepository())
    ref = channel.stage(WAV)
    for submission in (
        JobSubmission(capability="audio.transcribe"),
        JobSubmission(capability="audio.transcribe",
                      serving=transcribe_submission().serving,
                      deadline_at=datetime.now(UTC) + timedelta(seconds=120)),
    ):
        with pytest.raises(ConflictError):
            channel.submit(submission, artifact_uri=ref["uri"])
    assert len(channel._inputs) == 1


def test_bound_input_cannot_be_destroyed_or_rebound_by_bad_retry(tmp_path):
    from dataclasses import replace
    repo = FakeRepository()
    channel = make_channel(tmp_path, repo)
    ref = channel.stage(WAV)
    first = transcribe_submission("job-a-key")
    assert channel.submit(first, artifact_uri=ref["uri"]).job_id == "job-staged"
    ident = ref["uri"].removeprefix("tsumgi-audio-input:")
    before_calls = len(repo.submissions)
    for invalid in (
        transcribe_submission("job-b-key"),
        replace(first, payload={"changed": "request"}),
        replace(first, priority=5),
    ):
        with pytest.raises(ConflictError):
            channel.submit(invalid, artifact_uri=ref["uri"])
        assert len(repo.submissions) == before_calls
        assert channel.spool._records[ident][2] == ref["digest"].removeprefix("sha256:")
        assert channel._inputs[ident].job_id == "job-staged"
    assert channel.submit(first, artifact_uri=ref["uri"]).job_id == "job-staged"


def test_ambiguous_result_protects_receipt_from_mismatched_retry(tmp_path):
    from astrumweaver.control.repository import StorageUnavailable
    repo = FakeRepository(StorageUnavailable("unknown commit outcome"))
    channel = make_channel(tmp_path, repo)
    ref = channel.stage(WAV)
    first = transcribe_submission("maybe-committed-key")
    with pytest.raises(StorageUnavailable):
        channel.submit(first, artifact_uri=ref["uri"])
    calls = len(repo.submissions)
    with pytest.raises(ConflictError):
        channel.submit(transcribe_submission("different-key"), artifact_uri=ref["uri"])
    assert len(repo.submissions) == calls
    assert ref["uri"].removeprefix("tsumgi-audio-input:") in channel._inputs
    repo.error = None
    assert channel.submit(first, artifact_uri=ref["uri"]).job_id == "job-staged"


def test_input_ttl_must_cover_admitted_job_deadline(tmp_path):
    from dataclasses import replace
    repo = FakeRepository()
    channel = make_channel(tmp_path, repo)
    ref = channel.stage(WAV)
    request = transcribe_submission("too-long")
    late = replace(request, deadline_at=datetime.now(UTC) + timedelta(seconds=400))
    with pytest.raises(ConflictError, match="lifetime insufficient"):
        channel.submit(late, artifact_uri=ref["uri"])
    assert repo.submissions == []
    assert ref["uri"].removeprefix("tsumgi-audio-input:") not in channel._inputs
    # New inputs remain admissible when the deadline is inside the TTL.
    fresh = channel.stage(WAV)
    assert channel.submit(transcribe_submission("short-enough"),
                          artifact_uri=fresh["uri"]).job_id == "job-staged"


def test_pending_ambiguous_admission_cannot_be_discarded(tmp_path):
    from astrumweaver.control.repository import StorageUnavailable
    repo = FakeRepository(StorageUnavailable("unknown outcome"))
    channel = make_channel(tmp_path, repo)
    ref = channel.stage(WAV)
    request = transcribe_submission("pending-immutable")
    with pytest.raises(StorageUnavailable):
        channel.submit(request, artifact_uri=ref["uri"])
    with pytest.raises(NotFoundError):
        channel.discard(ref["uri"])
    ident = ref["uri"].removeprefix("tsumgi-audio-input:")
    assert ident in channel._inputs
    assert ident in channel.spool._records
    repo.error = None
    assert channel.submit(request, artifact_uri=ref["uri"]).job_id == "job-staged"


def test_identical_idempotent_replay_after_job_deadline(tmp_path, monkeypatch):
    from datetime import timedelta
    from types import SimpleNamespace
    from astrumweaver.audio import input_channel as module

    repo = FakeRepository()
    channel = make_channel(tmp_path, repo)
    ref = channel.stage(WAV)
    request = transcribe_submission("late-idempotent-response")
    original_job = channel.submit(request, artifact_uri=ref["uri"])
    assert original_job.job_id == "job-staged"
    # Only the wall clock advances beyond deadline. The temporary artifact
    # itself remains within its independent monotonic 300-second TTL.
    later = request.deadline_at + timedelta(seconds=5)
    monkeypatch.setattr(module, "datetime", SimpleNamespace(now=lambda tz: later))
    assert channel.submit(request, artifact_uri=ref["uri"]).job_id == original_job.job_id
