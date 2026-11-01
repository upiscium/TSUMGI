"""#180 B3a: bounded Worker↔Control WAV transport, synthetic authority only."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import hashlib
import json

import httpx
import pytest

from astrumweaver import ResourceShape, WorkerSpec
from astrumweaver.audio.artifact_spool import ArtifactSpool
from astrumweaver.audio.input_channel import AudioInputChannel
from astrumweaver.audio.output_channel import AudioOutputChannel
from astrumweaver.control import InMemoryControlRepository, JobSubmission, WorkerRegistration
from astrumweaver.control.api import create_app
from astrumweaver.execution import JobRequest, JobResult
from astrumweaver.worker.client import ClaimedJob, ControlClient, ControlTransportError
from test_audio_artifact_authority import EPOCH, serving_values, setup_claim
from test_audio_input_channel import WAV


WORKER = "owner"
LIMIT = 4096


def _claimed(record, *, capability: str | None = None, payload=None, epoch=EPOCH, lease=None, attempt=None):
    return ClaimedJob(
        request=JobRequest(
            job_id=record.job_id,
            capability=capability or record.capability,
            payload=record.payload if payload is None else payload,
            metadata={
                "serving": record.serving.to_dict(),
                "claimed_runtime_instance_epoch": epoch,
            },
        ),
        attempts=record.attempts if attempt is None else attempt,
        lease_token=record.lease_token if lease is None else lease,
        lease_expires_at=None,
    )


def _spool(tmp_path, label: str):
    root = tmp_path / label
    root.mkdir(mode=0o700)
    return ArtifactSpool(
        root, max_bytes=LIMIT, ttl_seconds=300,
        max_total_bytes=4 * LIMIT, max_artifacts=8,
    )


def _control(repo, tmp_path, *, input_channel=None, output_channel=None):
    return create_app(
        repo, worker_token="worker-secret", client_token="client-secret",
        audio_input_channel=input_channel, audio_output_channel=output_channel,
    )


@pytest.mark.asyncio
async def test_worker_upload_is_fenced_to_claim_and_published_only_after_commit(tmp_path):
    repo = InMemoryControlRepository()
    job = setup_claim(repo, datetime.now(UTC), capability="audio.synthesize")
    channel = AudioOutputChannel(repo, _spool(tmp_path, "output"), max_bytes=LIMIT)
    app = _control(repo, tmp_path, output_channel=channel)
    transport = httpx.ASGITransport(app=app)
    async with ControlClient("http://control", "worker-secret", transport=transport) as client:
        claim = _claimed(job)
        ref = await client.stage_audio_output(WORKER, claim, WAV, max_bytes=LIMIT)
        assert ref.uri.startswith("tsumgi-audio:")
        assert ref.media_type == "audio/wav"
        assert ref.size_bytes == len(WAV)
        assert ref.digest == "sha256:" + hashlib.sha256(WAV).hexdigest()
        assert not WAV in repr(repo.get_job(job.job_id)).encode("utf-8")
        async with httpx.AsyncClient(transport=transport, base_url="http://control") as public:
            route = f"/v1/jobs/{job.job_id}/audio/output/{ref.uri.removeprefix('tsumgi-audio:')}"
            hidden = await public.get(route, headers={"authorization": "Bearer client-secret"})
            assert hidden.status_code == 404
        completed = await client.complete(
            WORKER, job.job_id, job.lease_token,
            JobResult(artifacts=(ref,)), runtime_instance_epoch=EPOCH,
        )
        assert completed["status"] == "succeeded"
        async with httpx.AsyncClient(transport=transport, base_url="http://control") as public:
            visible = await public.get(route, headers={"authorization": "Bearer client-secret"})
            assert visible.status_code == 200 and visible.content == WAV


@pytest.mark.asyncio
async def test_worker_input_download_matches_trusted_job_reference(tmp_path):
    repo = InMemoryControlRepository()
    now = datetime.now(UTC)
    binding, advertisement = serving_values("audio.transcribe")
    repo.register_worker(WorkerRegistration(
        spec=WorkerSpec(
            worker_id=WORKER, worker_class="cpu-test", resources=ResourceShape(),
            capabilities=frozenset({"audio.transcribe"}),
        ), serving=advertisement,
    ), now=now)
    channel = AudioInputChannel(
        repo, _spool(tmp_path, "input"), max_bytes=LIMIT,
        max_decoded_bytes=LIMIT, max_duration_seconds=120,
    )
    staged = channel.stage(WAV)
    admitted = channel.submit(JobSubmission(
        capability="audio.transcribe", serving=binding,
        payload={"language": "ja"}, idempotency_key="b3a-synthetic-unique",
        deadline_at=now + timedelta(seconds=90),
    ), artifact_uri=staged["uri"])
    assert admitted.payload["audio_input"] == staged
    job = repo.claim_next_job(WORKER, runtime_instance_epoch=EPOCH, now=now + timedelta(seconds=2))
    assert job is not None and job.job_id == admitted.job_id
    app = _control(repo, tmp_path, input_channel=channel)
    async with ControlClient(
        "http://control", "worker-secret", transport=httpx.ASGITransport(app=app),
    ) as client:
        data = await client.download_audio_input(WORKER, _claimed(job), max_bytes=LIMIT)
        assert data == WAV
        for invalid in (
            _claimed(job, lease="wrong-lease"),
            _claimed(job, epoch="22345678-1234-4234-9234-123456789abc"),
            _claimed(job, attempt=job.attempts + 1),
        ):
            with pytest.raises(ControlTransportError) as caught:
                await client.download_audio_input(WORKER, invalid, max_bytes=LIMIT)
            assert caught.value.status_code in (409, 404)
            assert "wrong-lease" not in str(caught.value)
            assert "b3a-synthetic-unique" not in str(caught.value)


def _synthetic_claim(*, capability="audio.transcribe", payload=None):
    expected = {
        "uri": "tsumgi-audio-input:" + "a" * 64,
        "media_type": "audio/wav",
        "digest": "sha256:" + hashlib.sha256(WAV).hexdigest(),
        "size_bytes": len(WAV),
    }
    return ClaimedJob(
        request=JobRequest(
            job_id="synthetic-job", capability=capability,
            payload={"audio_input": expected} if payload is None else payload,
            metadata={"serving": {"test": True}, "claimed_runtime_instance_epoch": EPOCH},
        ), lease_token="synthetic-lease", attempts=1, lease_expires_at=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    httpx.Response(500, text="private-WAV-and-token"),
    httpx.Response(200, headers={"content-type": "audio/wav"}, stream=httpx.ByteStream(b"x" * (len(WAV) + 1))),
    httpx.Response(200, headers={"content-type": "text/plain"}, stream=httpx.ByteStream(WAV)),
    httpx.Response(200, headers={"content-type": "audio/wav", "content-encoding": "gzip"}, stream=httpx.ByteStream(WAV)),
    httpx.Response(200, headers={"content-type": "audio/wav"}, stream=httpx.ByteStream(b"0" * len(WAV))),
])
async def test_download_bad_responses_rejected_without_raw_leaks(response):
    calls = []
    def handle(request):
        calls.append(request)
        assert request.headers["authorization"] == "Bearer worker-secret"
        assert request.headers["x-audio-attempt"] == "1"
        assert request.headers["x-audio-runtime-epoch"] == EPOCH
        assert request.headers["x-audio-lease-token"] == "synthetic-lease"
        return response
    transport = httpx.MockTransport(handle)
    async with ControlClient("http://fake", "worker-secret", transport=transport) as client:
        with pytest.raises(ControlTransportError) as error:
            await client.download_audio_input(WORKER, _synthetic_claim(), max_bytes=LIMIT)
        assert "private-WAV-and-token" not in str(error.value)
        assert "synthetic-lease" not in str(error.value)
        assert len(calls) == 1


@pytest.mark.asyncio
async def test_locally_invalid_claim_or_ref_never_sends_request():
    def no_call(request):
        pytest.fail("invalid media operation made an HTTP request")
    async with ControlClient("http://fake", "worker-secret", transport=httpx.MockTransport(no_call)) as client:
        for invalid in (
            _synthetic_claim(capability="debug.echo"),
            _synthetic_claim(payload={}),
            _synthetic_claim(payload={"audio_input": {"uri": "bad", "size_bytes": len(WAV)}}),
            replace(_synthetic_claim(), attempts=0),
        ):
            with pytest.raises(ControlTransportError):
                await client.download_audio_input(WORKER, invalid, max_bytes=LIMIT)
        with pytest.raises(ControlTransportError):
            await client.download_audio_input(WORKER, _synthetic_claim(), max_bytes=16 * 1024 * 1024 + 1)
        with pytest.raises(ControlTransportError):
            await client.stage_audio_output(WORKER, _synthetic_claim(), WAV, max_bytes=LIMIT)
        synth = _synthetic_claim(capability="audio.synthesize")
        for invalid_wav in (b"", b"not-wav", b"RIFF" + b"x" * 40):
            with pytest.raises(ControlTransportError):
                await client.stage_audio_output(WORKER, synth, invalid_wav, max_bytes=LIMIT)


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt", [
    {"uri": "tsumgi-audio:" + "f" * 64, "media_type": "audio/wav", "digest": "sha256:" + "0" * 64, "size_bytes": len(WAV)},
    {"uri": "tsumgi-audio:" + "f" * 64, "media_type": "audio/wav", "digest": "sha256:" + hashlib.sha256(WAV).hexdigest(), "size_bytes": len(WAV) + 1},
    {"uri": "file:///private/path", "media_type": "audio/wav", "digest": "sha256:" + hashlib.sha256(WAV).hexdigest(), "size_bytes": len(WAV)},
])
async def test_output_receipt_mismatch_rejected_without_retries(receipt):
    calls = []
    def handle(request):
        calls.append(request)
        assert request.headers["content-type"] == "audio/wav"
        assert request.content == WAV
        return httpx.Response(201, headers={"content-type": "application/json"}, stream=httpx.ByteStream(json.dumps(receipt).encode()))
    async with ControlClient("http://fake", "worker-secret", transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ControlTransportError) as caught:
            await client.stage_audio_output(WORKER, _synthetic_claim(capability="audio.synthesize"), WAV, max_bytes=LIMIT)
        assert caught.value.status_code == 502
    assert len(calls) == 1
