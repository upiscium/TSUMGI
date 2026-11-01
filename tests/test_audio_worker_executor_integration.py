"""#180 B3b: real WorkerRuntime→Control ASGI media authority, fake executors."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import time
from dataclasses import replace

import httpx
import pytest

from astrumweaver import ResourceShape, WorkerSpec
from astrumweaver.audio.artifact_spool import ArtifactSpool
from astrumweaver.audio.input_channel import AudioInputChannel
from astrumweaver.audio.output_channel import AudioOutputChannel
from astrumweaver.control import InMemoryControlRepository, JobSubmission, JobStatus, WorkerRegistration, WorkerState
from astrumweaver.control.api import create_app
from astrumweaver.execution import ArtifactRef, JobResult, ResidencyReport
from astrumweaver.worker.client import ControlClient, ControlTransportError
from astrumweaver.worker.runtime import WorkerRuntime, require_audio_executor
from test_audio_artifact_authority import EPOCH, serving_values
from test_audio_input_channel import WAV

LIMIT = 4096
OWNER = "owner"


def spool(tmp_path, name):
    root = tmp_path / name
    root.mkdir(mode=0o700)
    return ArtifactSpool(
        root, max_bytes=LIMIT, ttl_seconds=300.0,
        max_total_bytes=4 * LIMIT, max_artifacts=8,
    )


class AudioExecutor:
    def __init__(self, capability, mode="ok"):
        self.capabilities = frozenset({capability})
        self.mode = mode
        self.called = 0
        self.audio_called = 0
        self.cancelled = []
        self.data = None
        self.request_metadata = None
        self.media = None
        self.started = asyncio.Event()
        self.resume = asyncio.Event()
        self.resume.set()

    async def execute(self, request):
        self.called += 1
        raise AssertionError("audio dispatched via generic execute")

    async def execute_audio(self, job, media):
        self.audio_called += 1
        self.request_metadata = dict(job.metadata)
        self.media = media
        assert "claimed_runtime_instance_epoch" not in job.metadata
        assert "lease_token" not in job.metadata
        assert "worker_token" not in job.metadata
        self.started.set()
        await self.resume.wait()
        if job.capability == "audio.synthesize":
            ref = await media.stage_output(WAV, max_bytes=LIMIT)
            if self.mode == "wrong_ref":
                ref = replace(ref, uri="tsumgi-audio:" + "f" * 64)
            if self.mode == "wrong_outputs":
                return JobResult(artifacts=(ref,), outputs={"raw": WAV})
            if self.mode == "duplicate_stage":
                try:
                    await media.stage_output(WAV, max_bytes=LIMIT)
                except Exception:
                    pass
            if self.mode == "private_error":
                raise RuntimeError("raw/private/engine/path")
            return JobResult(artifacts=(ref,), metrics={"seconds": 0.5})
        self.data = await media.read_input(max_bytes=LIMIT)
        if self.mode == "oversized":
            return JobResult(text="日本" * 8192)
        if self.mode == "binary_metadata":
            return JobResult(text="散歩", metadata={"private": WAV})
        if self.mode == "private_error":
            raise RuntimeError("raw/private/audio/content")
        return JobResult(text="散歩", metrics={"duration_seconds": 0.2})

    async def cancel(self, job_id):
        self.cancelled.append(job_id)

    async def residency(self):
        return ResidencyReport()


class NonAudioExecutor:
    capabilities = frozenset({"test.work"})
    def __init__(self):
        self.called = 0
    async def execute(self, job):
        self.called += 1
        return JobResult(text="legacy-result")
    async def cancel(self, job_id):
        pass
    async def residency(self):
        return ResidencyReport()


def configure_runtime(spec, executor, client, serving=None):
    runtime = WorkerRuntime(
        spec=spec, max_concurrency=1, executor=executor,
        client=client, serving=serving,
        heartbeat_interval_seconds=0.5,
    )
    # Direct-entry tests replace the network registration ACK, not the
    # Control's durable Job claim / artifact attempt checks.
    runtime._registered = True
    runtime._control_state = WorkerState.ONLINE
    runtime._control_valid_until = time.monotonic() + 60
    runtime._next_heartbeat_at = time.monotonic() + 60
    return runtime


def registered(repo, capability):
    binding, serving = serving_values(capability)
    spec = WorkerSpec(
        worker_id=OWNER, worker_class="cpu-test", resources=ResourceShape(),
        capabilities=frozenset({capability}),
    )
    repo.register_worker(WorkerRegistration(spec=spec, serving=serving))
    return spec, binding, serving


async def claim_via_control(client, *, capability):
    claimed = await client.claim(OWNER, runtime_instance_epoch=EPOCH)
    assert claimed is not None
    assert claimed.request.capability == capability
    assert claimed.request.metadata["claimed_runtime_instance_epoch"] == EPOCH
    return claimed


@pytest.mark.asyncio
async def test_tts_real_claim_media_stage_and_terminal_publication(tmp_path):
    repo = InMemoryControlRepository(lease_seconds=30)
    spec, binding, serving = registered(repo, "audio.synthesize")
    repo.submit_job(JobSubmission(
        capability="audio.synthesize", serving=binding,
        deadline_at=datetime.now(UTC) + timedelta(seconds=90),
        max_attempts=1, payload={"input": "散歩", "response_format": "wav"},
    ))
    channel = AudioOutputChannel(repo, spool(tmp_path, "output"), max_bytes=LIMIT)
    app = create_app(repo, worker_token="worker-secret", client_token="client-secret", audio_output_channel=channel)
    transport = httpx.ASGITransport(app=app)
    executor = AudioExecutor("audio.synthesize")
    async with ControlClient("http://control", "worker-secret", transport=transport) as client:
        claimed = await claim_via_control(client, capability="audio.synthesize")
        runtime = configure_runtime(spec, executor, client, serving)
        await runtime._execute_claim(claimed)
        job = repo.get_job(claimed.request.job_id)
        assert job.status is JobStatus.SUCCEEDED
        assert executor.audio_called == 1 and executor.called == 0
        assert job.result is not None and len(job.result.artifacts) == 1
        artifact_id = job.result.artifacts[0].uri.removeprefix("tsumgi-audio:")
        assert len(artifact_id) == 64
        assert b"RIFF" not in str(job.payload).encode()
        assert b"RIFF" not in str(job.result).encode()
        async with httpx.AsyncClient(transport=transport, base_url="http://control") as public:
            response = await public.get(
                f"/v1/jobs/{job.job_id}/audio/output/{artifact_id}",
                headers={"authorization": "Bearer client-secret"},
            )
            assert response.status_code == 200 and response.content == WAV
        with pytest.raises(ControlTransportError):
            await executor.media.stage_output(WAV, max_bytes=LIMIT)


@pytest.mark.asyncio
async def test_stt_real_claim_reads_job_bound_input_and_completes_text(tmp_path):
    repo = InMemoryControlRepository(lease_seconds=30)
    spec, binding, serving = registered(repo, "audio.transcribe")
    channel = AudioInputChannel(
        repo, spool(tmp_path, "input"), max_bytes=LIMIT,
        max_decoded_bytes=LIMIT, max_duration_seconds=120,
    )
    staged = channel.stage(WAV)
    admitted = channel.submit(
        JobSubmission(
            capability="audio.transcribe", serving=binding,
            payload={"language": "ja"}, deadline_at=datetime.now(UTC) + timedelta(seconds=90),
            idempotency_key="b3b-stt-synthetic-unique", max_attempts=1,
        ), artifact_uri=staged["uri"],
    )
    app = create_app(repo, worker_token="worker-secret", client_token="client-secret", audio_input_channel=channel)
    executor = AudioExecutor("audio.transcribe")
    async with ControlClient("http://control", "worker-secret", transport=httpx.ASGITransport(app=app)) as client:
        claimed = await claim_via_control(client, capability="audio.transcribe")
        runtime = configure_runtime(spec, executor, client, serving)
        await runtime._execute_claim(claimed)
        finished = repo.get_job(admitted.job_id)
        assert finished.status is JobStatus.SUCCEEDED
        assert executor.data == WAV
        assert finished.result.text == "散歩" and finished.result.artifacts == ()
        assert executor.called == 0 and executor.audio_called == 1
        assert b"RIFF" not in str(finished.result).encode()
        assert not hasattr(executor.media, "lease_token")
        with pytest.raises(ControlTransportError):
            await executor.media.read_input(max_bytes=LIMIT)


@pytest.mark.asyncio
@pytest.mark.parametrize("capability,mode", [
    ("audio.synthesize", "wrong_ref"),
    ("audio.synthesize", "wrong_outputs"),
    ("audio.synthesize", "duplicate_stage"),
    ("audio.synthesize", "private_error"),
    ("audio.transcribe", "oversized"),
    ("audio.transcribe", "binary_metadata"),
    ("audio.transcribe", "private_error"),
])
async def test_invalid_audio_outputs_fail_closed_without_publication(tmp_path, capability, mode):
    repo = InMemoryControlRepository(lease_seconds=30)
    spec, binding, serving = registered(repo, capability)
    if capability == "audio.transcribe":
        input_channel = AudioInputChannel(repo, spool(tmp_path, "input"), max_bytes=LIMIT,
                                          max_decoded_bytes=LIMIT, max_duration_seconds=120)
        staged = input_channel.stage(WAV)
        job = input_channel.submit(JobSubmission(
            capability=capability, serving=binding,
            payload={"language": "ja"}, idempotency_key="b3b-invalid-" + mode,
            deadline_at=datetime.now(UTC) + timedelta(seconds=90), max_attempts=1,
        ), artifact_uri=staged["uri"])
        output_channel = None
    else:
        input_channel = None
        output_channel = AudioOutputChannel(repo, spool(tmp_path, "output"), max_bytes=LIMIT)
        job = repo.submit_job(JobSubmission(
            capability=capability, serving=binding,
            deadline_at=datetime.now(UTC) + timedelta(seconds=90), max_attempts=1,
        ))
    app = create_app(repo, worker_token="worker-secret", client_token="client-secret",
                     audio_input_channel=input_channel, audio_output_channel=output_channel)
    executor = AudioExecutor(capability, mode=mode)
    async with ControlClient("http://control", "worker-secret", transport=httpx.ASGITransport(app=app)) as client:
        claimed = await claim_via_control(client, capability=capability)
        runtime = configure_runtime(spec, executor, client, serving)
        await runtime._execute_claim(claimed)
    finished = repo.get_job(job.job_id)
    assert finished.status is JobStatus.FAILED
    assert finished.result is None
    assert "private" not in str(finished.error)
    assert "RIFF" not in str(finished.error)


@pytest.mark.asyncio
async def test_non_audio_executor_path_remains_generic(tmp_path):
    repo = InMemoryControlRepository(lease_seconds=30)
    spec = WorkerSpec(worker_id=OWNER, worker_class="cpu-test", resources=ResourceShape(),
                      capabilities=frozenset({"test.work"}))
    repo.register_worker(WorkerRegistration(spec=spec))
    job = repo.submit_job(JobSubmission(capability="test.work", payload={"x": 1}))
    app = create_app(repo, worker_token="worker-secret", client_token="client-secret")
    executor = NonAudioExecutor()
    async with ControlClient("http://control", "worker-secret", transport=httpx.ASGITransport(app=app)) as client:
        claimed = await client.claim(OWNER)
        assert claimed is not None
        runtime = configure_runtime(spec, executor, client)
        await runtime._execute_claim(claimed)
    assert executor.called == 1
    assert repo.get_job(job.job_id).result.text == "legacy-result"


def test_audio_executor_and_serving_missing_fail_before_registration():
    legacy = NonAudioExecutor()
    with pytest.raises(RuntimeError, match="async execute_audio"):
        require_audio_executor(legacy, frozenset({"audio.synthesize"}))
    spec = WorkerSpec(worker_id=OWNER, worker_class="test", resources=ResourceShape(),
                      capabilities=frozenset({"audio.synthesize"}))
    with pytest.raises(ValueError, match="serving advertisement"):
        WorkerRuntime(spec=spec, max_concurrency=1, executor=AudioExecutor("audio.synthesize"), client=None)


@pytest.mark.asyncio
async def test_stale_or_stopped_media_context_never_publishes(tmp_path):
    repo = InMemoryControlRepository(lease_seconds=30)
    spec, binding, serving = registered(repo, "audio.synthesize")
    job = repo.submit_job(JobSubmission(
        capability="audio.synthesize", serving=binding,
        deadline_at=datetime.now(UTC) + timedelta(seconds=90), max_attempts=1,
    ))
    output = AudioOutputChannel(repo, spool(tmp_path, "output"), max_bytes=LIMIT)
    app = create_app(repo, worker_token="worker-secret", client_token="client-secret", audio_output_channel=output)
    executor = AudioExecutor("audio.synthesize")
    executor.resume.clear()
    async with ControlClient("http://control", "worker-secret", transport=httpx.ASGITransport(app=app)) as client:
        claimed = await claim_via_control(client, capability="audio.synthesize")
        runtime = configure_runtime(spec, executor, client, serving)
        task = asyncio.create_task(runtime._execute_claim(claimed))
        await asyncio.wait_for(executor.started.wait(), 1)
        runtime.request_stop()
        executor.resume.set()
        with pytest.raises(ControlTransportError):
            await asyncio.wait_for(task, 1)
    assert repo.get_job(job.job_id).status is JobStatus.RUNNING
    assert not output._staged


@pytest.mark.asyncio
async def test_uncertain_output_receipt_abandons_attempt_without_retry_or_terminal_write(tmp_path):
    repo = InMemoryControlRepository(lease_seconds=30)
    spec, binding, serving = registered(repo, "audio.synthesize")
    record = repo.submit_job(JobSubmission(
        capability="audio.synthesize", serving=binding,
        deadline_at=datetime.now(UTC) + timedelta(seconds=90), max_attempts=1,
    ))
    output = AudioOutputChannel(repo, spool(tmp_path, "output"), max_bytes=LIMIT)
    app = create_app(repo, worker_token="worker-secret", client_token="client-secret", audio_output_channel=output)

    class LostAckClient(ControlClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.stage_count = 0
            self.terminal_count = 0

        async def stage_audio_output(self, *args, **kwargs):
            self.stage_count += 1
            await super().stage_audio_output(*args, **kwargs)
            # Simulate a dropped acknowledgement AFTER the Control stage has
            # committed. The private exception must not leak to Job data.
            raise ControlTransportError(0, "private-ack-unavailable")

        async def complete(self, *args, **kwargs):
            self.terminal_count += 1
            raise AssertionError("uncertain media stage must not complete a Job")

        async def fail(self, *args, **kwargs):
            self.terminal_count += 1
            raise AssertionError("uncertain stage must not cause a false failure acknowledgement")

    executor = AudioExecutor("audio.synthesize")
    async with LostAckClient(
        "http://control", "worker-secret", transport=httpx.ASGITransport(app=app),
    ) as client:
        claimed = await claim_via_control(client, capability="audio.synthesize")
        runtime = configure_runtime(spec, executor, client, serving)
        with pytest.raises(ControlTransportError) as caught:
            await runtime._execute_claim(claimed)
        assert "private-ack-unavailable" not in str(caught.value)
        assert client.stage_count == 1
        assert client.terminal_count == 0
        assert runtime.control_available is False
    assert repo.get_job(record.job_id).status is JobStatus.RUNNING
    assert len(output._staged) == 1
    assert repo.get_job(record.job_id).result is None


@pytest.mark.asyncio
async def test_cancelled_claim_during_tts_execution_never_stages_or_publishes(tmp_path):
    repo = InMemoryControlRepository(lease_seconds=30)
    spec, binding, serving = registered(repo, "audio.synthesize")
    record = repo.submit_job(JobSubmission(
        capability="audio.synthesize", serving=binding,
        deadline_at=datetime.now(UTC) + timedelta(seconds=90), max_attempts=1,
    ))
    output = AudioOutputChannel(repo, spool(tmp_path, "output"), max_bytes=LIMIT)
    app = create_app(repo, worker_token="worker-secret", client_token="client-secret", audio_output_channel=output)
    executor = AudioExecutor("audio.synthesize")
    executor.resume.clear()
    async with ControlClient(
        "http://control", "worker-secret", transport=httpx.ASGITransport(app=app),
    ) as client:
        claimed = await claim_via_control(client, capability="audio.synthesize")
        runtime = configure_runtime(spec, executor, client, serving)
        task = asyncio.create_task(runtime._execute_claim(claimed))
        await asyncio.wait_for(executor.started.wait(), 1)
        repo.cancel_job(record.job_id)
        executor.resume.set()
        try:
            await asyncio.wait_for(task, 1)
        except ControlTransportError:
            # 409 may be inspected/rethrown depending on cancellation state.
            pass
    assert repo.get_job(record.job_id).status is JobStatus.CANCELLED
    assert not output._staged
    assert repo.get_job(record.job_id).result is None


@pytest.mark.asyncio
async def test_stt_not_resubmitted_after_local_media_error_swallowed_by_executor(tmp_path):
    repo = InMemoryControlRepository(lease_seconds=30)
    spec, binding, serving = registered(repo, "audio.transcribe")
    source = AudioInputChannel(repo, spool(tmp_path, "input"), max_bytes=LIMIT,
                               max_decoded_bytes=LIMIT, max_duration_seconds=120)
    staged = source.stage(WAV)
    record = source.submit(JobSubmission(
        capability="audio.transcribe", serving=binding, payload={"language": "ja"},
        idempotency_key="b3b-swallow", deadline_at=datetime.now(UTC) + timedelta(seconds=90),
        max_attempts=1,
    ), artifact_uri=staged["uri"])
    app = create_app(repo, worker_token="worker-secret", client_token="client-secret", audio_input_channel=source)

    class SwallowExecutor(AudioExecutor):
        async def execute_audio(self, job, media):
            try:
                await media.read_input(max_bytes=1)  # Job receipt exceeds cap
            except Exception:
                pass
            return JobResult(text="made-up transcript")

    async with ControlClient("http://control", "worker-secret", transport=httpx.ASGITransport(app=app)) as client:
        claimed = await claim_via_control(client, capability="audio.transcribe")
        runtime = configure_runtime(spec, SwallowExecutor("audio.transcribe"), client, serving)
        with pytest.raises(ControlTransportError):
            await runtime._execute_claim(claimed)
    finished = repo.get_job(record.job_id)
    # An ambiguous media read cannot become a made-up transcript, and no
    # second attempt or fabricated durable failure acknowledgement is sent.
    assert finished.status is JobStatus.RUNNING
    assert finished.attempts == 1
    assert finished.result is None
    assert "made-up transcript" not in str(finished)
