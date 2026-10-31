"""Bounded synthetic ASGI/Control/#174 tests; no provider, DB or network I/O."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
import json
import struct
from threading import Event
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from starlette.requests import Request

from astrumweaver import ResourceShape, WorkerSpec
from astrumweaver.audio import ArtifactSpool
from astrumweaver.audio.input_channel import AudioInputChannel
from astrumweaver.control import InMemoryControlRepository, JobStatus, WorkerRegistration
from astrumweaver.control.models import utc_now
from astrumweaver.control.repository import (
    ConflictError, NoCompatibleDeployment, NotFoundError, OverloadedError, StorageUnavailable,
)
from astrumweaver.execution import ArtifactRef, JobResult
from astrumweaver.gateway.audio import AudioGatewayError, AudioProfileCatalog
from astrumweaver.gateway.audio_ingress import AudioIngressLimits
from astrumweaver.gateway.audio_transcriptions_api import (
    AudioTranscriptionsLimits, AudioTranscriptionsService, create_audio_transcriptions_router,
)
from astrumweaver.serving import RuntimeInstance, WorkerServingAdvertisement
from astrumweaver.transport import job_record_to_dict
from test_audio_voice_api import AUTH, TOKEN, make_profile


EPOCH = "12345678-1234-4234-9234-123456789abc"
WAV = (b"RIFF" + struct.pack("<I", 38) + b"WAVEfmt "
       + struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16)
       + b"data" + struct.pack("<I", 2) + b"\x00\x00")
INGRESS = AudioIngressLimits(
    request_bytes=1024, input_bytes=128, file_bytes=256, metadata_bytes=256,
    part_header_bytes=256, total_header_bytes=2048, receive_events=16,
    idle_timeout_seconds=0.5, total_timeout_seconds=1,
)
LIMITS = AudioTranscriptionsLimits(
    text_bytes=128, request_timeout_seconds=1.5, max_pending_admissions=1,
    poll_interval_seconds=0.005,
)


def multipart(*, model="synthetic-model-stt", language="ja", response_format="json",
              data=WAV, filename="sample.wav", mime="audio/wav", extra=None):
    fields = [("model", model), ("response_format", response_format)]
    if language is not None:
        fields.append(("language", language))
    if extra is not None:
        fields.append(extra)
    parts = [b'--test\r\nContent-Disposition: form-data; name="' + key.encode()
             + b'"\r\n\r\n' + value.encode() + b"\r\n" for key, value in fields]
    parts.append(b'--test\r\nContent-Disposition: form-data; name="file"; filename="'
                 + filename.encode() + b'"\r\nContent-Type: ' + mime.encode()
                 + b"\r\n\r\n" + data + b"\r\n")
    return b"".join(parts) + b"--test--\r\n"


class RecordingRepository(InMemoryControlRepository):
    def __init__(self):
        super().__init__(lease_seconds=60)
        self.submissions = []
        self.cancellations = []
        self.readbacks = []
        self.after_submit = None

    def submit_job(self, submission, **kwargs):
        self.submissions.append(submission)
        record = super().submit_job(submission, **kwargs)
        if self.after_submit:
            self.after_submit(record)
        return record

    def cancel_job(self, job_id, **kwargs):
        self.cancellations.append(job_id)
        return super().cancel_job(job_id, **kwargs)

    def get_job_by_idempotency_key(self, key, **kwargs):
        self.readbacks.append(key)
        return super().get_job_by_idempotency_key(key, **kwargs)


@pytest.fixture
def env(tmp_path):
    repo = RecordingRepository()
    profile = make_profile("model-stt")
    resolved = profile.resolved
    repo.register_worker(WorkerRegistration(
        spec=WorkerSpec(worker_id=profile.profile_id, worker_class="synthetic-cpu",
                        resources=ResourceShape(), capabilities=frozenset({"audio.transcribe"})),
        serving=WorkerServingAdvertisement(
            deployment_revision=resolved.deployment.revision,
            runtime_instance=RuntimeInstance(resolved.deployment.revision, EPOCH),
            contracts=(resolved.contract,),
        ),
    ))
    root = tmp_path / "transcription-spool"
    root.mkdir(mode=0o700)
    spool = ArtifactSpool(root, max_bytes=256, max_total_bytes=1024,
                          max_artifacts=4, ttl_seconds=60)
    channel = AudioInputChannel(repo, spool, max_bytes=256, max_decoded_bytes=128,
                                max_duration_seconds=1)
    return SimpleNamespace(repo=repo, profile=profile, catalog=AudioProfileCatalog((profile,)),
                           spool=spool, channel=channel)


def router(env, **overrides):
    options = dict(client_auth="bearer", client_token=TOKEN, ingress_limits=INGRESS, limits=LIMITS)
    options.update(overrides)
    return create_audio_transcriptions_router(env.catalog, env.repo, env.channel, **options)


def app(env):
    instance = FastAPI()
    instance.include_router(router(env))
    return instance


async def post(instance, *, body=None, headers=None, path="/v1/audio/transcriptions"):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=instance),
                                 base_url="http://synthetic") as client:
        return await client.post(path, content=multipart() if body is None else body,
                                 headers={**AUTH, "Content-Type": "multipart/form-data; boundary=test"}
                                 if headers is None else headers)


def raw_request(*, disconnect=None, body=None):
    sent = False
    raw = multipart() if body is None else body
    disconnect = asyncio.Event() if disconnect is None else disconnect

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": raw, "more_body": False}
        await disconnect.wait()
        return {"type": "http.disconnect"}

    return Request({"type": "http", "method": "POST", "path": "/v1/audio/transcriptions",
                    "query_string": b"", "headers": [
                        (b"content-type", b"multipart/form-data; boundary=test"),
                        (b"content-length", str(len(raw)).encode()),
                    ]}, receive)


async def wait_until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.001)


async def drain(service):
    await wait_until(lambda: service.in_flight == 0 and not service.operations)


def claim(env, record):
    claimed = env.repo.claim_next_job(env.profile.profile_id, runtime_instance_epoch=EPOCH)
    assert claimed is not None and claimed.job_id == record.job_id
    credentials = dict(worker_id=env.profile.profile_id, lease_token=claimed.lease_token,
                       runtime_instance_epoch=EPOCH)
    return claimed, credentials


def finish(env, record, result=None):
    claimed, credentials = claim(env, record)
    assert env.channel.read_for_worker(record.job_id, attempt=claimed.attempts, **credentials) == WAV
    env.repo.complete_job(record.job_id, JobResult(text=" 日本語\n") if result is None else result,
                          **credentials)


def assert_error(response, status, code=None):
    assert response.status_code == status, response.text
    assert response.headers["cache-control"] == "no-store"
    if code is not None:
        assert response.json()["error"]["code"] == code
    for private in (TOKEN, EPOCH, "private-repository-error", "https://private.invalid"):
        assert private not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["ja", None])
async def test_success_exact_identity_and_fenced_input_before_ack(env, language):
    env.repo.after_submit = lambda record: finish(env, record)
    before = env.catalog.to_dict()
    started = utc_now()
    response = await post(app(env), body=multipart(language=language))
    assert response.status_code == 200, response.text
    assert response.json() == {"text": " 日本語\n"}
    assert response.headers["content-type"] == "application/json"
    assert response.headers["cache-control"] == "no-store"
    assert len(env.repo.submissions) == 1
    submission = env.repo.submissions[0]
    assert submission.capability == "audio.transcribe"
    assert submission.serving == env.profile.binding
    assert submission.max_attempts == 1
    assert submission.idempotency_key.startswith("audio-transcriptions:")
    assert len(submission.idempotency_key) == len("audio-transcriptions:") + 64
    assert started < submission.deadline_at <= utc_now() + timedelta(seconds=1.5)
    assert submission.payload.keys() == {"language", "response_format", "audio_input"}
    assert submission.payload["language"] == "ja" and submission.payload["response_format"] == "json"
    receipt = submission.payload["audio_input"]
    assert receipt["uri"].startswith("tsumgi-audio-input:")
    assert receipt["media_type"] == "audio/wav" and receipt["size_bytes"] == len(WAV)
    assert receipt["digest"].startswith("sha256:")
    durable = env.repo.list_jobs()[0]
    assert durable.status is JobStatus.SUCCEEDED and durable.attempts == 1
    assert WAV not in json.dumps(job_record_to_dict(durable)).encode()
    assert env.catalog.to_dict() == before
    assert not env.repo.readbacks and not env.repo.cancellations
    # No unsafe post-admission deletion. TTL reap remains the channel authority.
    with pytest.raises(NotFoundError):
        env.channel.discard(receipt["uri"])
    expired = env.spool._clock() + 61
    env.spool._clock = lambda: expired
    assert env.channel.reap_expired() == 1


@pytest.mark.asyncio
async def test_repeated_http_requests_have_distinct_internal_identity(env):
    env.repo.after_submit = lambda record: finish(env, record)
    instance = app(env)
    assert (await post(instance)).status_code == 200
    assert (await post(instance)).status_code == 200
    assert len({s.idempotency_key for s in env.repo.submissions}) == 2
    assert len({s.payload["audio_input"]["uri"] for s in env.repo.submissions}) == 2


@pytest.mark.asyncio
async def test_router_captures_detached_reviewed_catalog(env):
    instance = app(env)
    env.repo.after_submit = lambda record: finish(env, record)
    object.__setattr__(env.catalog, "profiles", (make_profile("voicevox"),))
    assert (await post(instance)).status_code == 200
    assert env.repo.submissions[0].serving == env.profile.binding


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [
    {}, {"Authorization": "Basic private"}, {"Authorization": "Bearer wrong"},
    [("Authorization", "Bearer " + TOKEN), ("Authorization", "Bearer " + TOKEN)],
])
async def test_auth_before_receive(env, headers):
    instance = app(env)

    async def protected(scope, receive, send):
        async def forbidden():
            pytest.fail("unauthenticated body read")
        await instance(scope, forbidden, send)

    response = await post(protected, body=b"invalid", headers=headers)
    assert_error(response, 401, "unauthorized")
    assert response.headers["www-authenticate"] == "Bearer"
    assert not env.repo.submissions and not env.spool._records


@pytest.mark.asyncio
@pytest.mark.parametrize("changes,status", [
    ({"model": "unknown"}, 404), ({"language": "en"}, 400),
    ({"response_format": "text"}, 400), ({"filename": "../private.wav"}, 400),
    ({"filename": "sample.flac", "mime": "audio/flac", "data": b"fLaC"}, 400),
    ({"mime": "audio/flac"}, 415), ({"data": b"fLaC"}, 415),
    ({"data": WAV[:-1]}, 415), ({"extra": ("prompt", "private")}, 400),
    ({"extra": ("model", "duplicate")}, 400), ({"data": b"x" * 257}, 413),
    ({"model": "x" * 1025}, 413),
])
async def test_strict_rejection_before_staging_or_submission(env, changes, status):
    response = await post(app(env), body=multipart(**changes))
    assert_error(response, status)
    assert not env.repo.submissions and not env.spool._records


@pytest.mark.asyncio
async def test_decoded_authority_rejects_before_stage(env):
    env.channel.max_decoded_bytes = 1  # synthetic stricter operator policy
    assert_error(await post(app(env)), 415, "unsupported_audio")
    assert not env.repo.submissions and not env.spool._records


@pytest.mark.asyncio
async def test_transport_and_query_rejection(env):
    instance = app(env)
    assert_error(await post(instance, path="/v1/audio/transcriptions?model=other"), 400)
    assert_error(await post(instance, headers={**AUTH, "Content-Type": "application/json"}), 415)
    assert_error(await post(instance, headers={**AUTH,
        "Content-Type": "multipart/form-data; boundary=test", "Content-Encoding": "gzip"}), 415)
    assert not env.repo.submissions and not env.spool._records


@pytest.mark.parametrize("overrides", [
    {"client_auth": "none"}, {"client_token": None}, {"client_token": "bad token"},
    {"ingress_limits": None}, {"limits": None},
    {"ingress_limits": replace(INGRESS, file_bytes=257)},
])
def test_factory_fails_closed(env, overrides):
    with pytest.raises(ValueError):
        router(env, **overrides)


def test_factory_catalog_and_repository_revalidated(env, monkeypatch):
    env.catalog = AudioProfileCatalog((make_profile("voicevox"),))
    with pytest.raises(ValueError, match="transcription profile"):
        router(env)
    env.catalog = AudioProfileCatalog((env.profile,))
    monkeypatch.setattr(env.repo, "get_job_by_idempotency_key", None)
    with pytest.raises(ValueError, match="durable repository"):
        router(env)
    env.repo = RecordingRepository()
    with pytest.raises(ValueError, match="same-repository"):
        router(env)


@pytest.mark.parametrize("field,value", [
    ("text_bytes", True), ("text_bytes", 0), ("text_bytes", 1024 * 1024 + 1),
    ("request_timeout_seconds", float("nan")), ("request_timeout_seconds", float("inf")),
    ("request_timeout_seconds", 10 ** 1000), ("request_timeout_seconds", True),
    ("max_pending_admissions", 0), ("poll_interval_seconds", 2),
])
def test_invalid_limits(field, value):
    with pytest.raises(ValueError):
        replace(LIMITS, **{field: value})


@pytest.mark.asyncio
@pytest.mark.parametrize("exception,status", [(NoCompatibleDeployment, 503), (OverloadedError, 429)])
async def test_definite_refusal_channel_cleans_first_input(env, monkeypatch, exception, status):
    def refuse(submission):
        env.repo.submissions.append(submission)
        raise exception("private-repository-error")
    monkeypatch.setattr(env.repo, "submit_job", refuse)
    assert_error(await post(app(env)), status)
    assert len(env.repo.submissions) == 1
    assert not env.spool._records and not env.repo.readbacks


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["lost-ack", "missing", "unavailable", "mismatch"])
async def test_uncertain_commit_never_resubmits_or_discards(env, monkeypatch, fault):
    original = env.repo.submit_job

    def uncertain(submission):
        if fault in {"lost-ack", "mismatch"}:
            record = original(submission)
            if fault == "mismatch":
                env.repo._jobs[record.job_id] = record.with_updates(priority=1)
        else:
            env.repo.submissions.append(submission)
        raise StorageUnavailable("private-repository-error")

    monkeypatch.setattr(env.repo, "submit_job", uncertain)
    if fault == "unavailable":
        def unavailable(key, **kwargs):
            env.repo.readbacks.append(key)
            raise StorageUnavailable("private-repository-error")
        monkeypatch.setattr(env.repo, "get_job_by_idempotency_key", unavailable)
    service = AudioTranscriptionsService(env.repo, env.channel, LIMITS)
    with pytest.raises(AudioGatewayError, match="admission_unavailable"):
        await service.complete(raw_request(), env.catalog, INGRESS)
    await drain(service)
    assert len(env.repo.submissions) == 1 and len(env.spool._records) == 1
    assert len(env.repo.readbacks) == (1 if fault in {"lost-ack", "mismatch"} else 3)
    if fault == "lost-ack":
        assert env.repo.list_jobs()[0].status is JobStatus.CANCELLED
        assert service.unresolved_outcomes == 0
    else:
        assert not env.repo.cancellations and service.unresolved_outcomes == 1


@pytest.mark.asyncio
async def test_lost_ack_worker_rebind_requires_current_authority(env, monkeypatch):
    original = env.repo.submit_job
    observed = []

    def lost_ack(submission):
        record = original(submission)
        claimed, credentials = claim(env, record)
        with pytest.raises(ConflictError):
            env.channel.read_for_worker(record.job_id, attempt=claimed.attempts,
                **{**credentials, "runtime_instance_epoch": "22345678-1234-4234-9234-123456789abc"})
        observed.append(env.channel.read_for_worker(record.job_id, attempt=claimed.attempts,
                                                     **credentials))
        raise StorageUnavailable("private-repository-error")

    monkeypatch.setattr(env.repo, "submit_job", lost_ack)
    assert_error(await post(app(env)), 503, "admission_unavailable")
    assert observed == [WAV]
    assert env.repo.list_jobs()[0].status is JobStatus.CANCELLED
    assert len(env.repo.submissions) == 1


@pytest.mark.asyncio
async def test_pending_input_rejects_same_uri_digest_with_different_request(env, monkeypatch):
    original = env.repo.submit_job

    def lost_ack(submission):
        record = original(submission)
        claimed, credentials = claim(env, record)
        env.repo._jobs[record.job_id] = claimed.with_updates(priority=1)
        with pytest.raises(NotFoundError):
            env.channel.read_for_worker(record.job_id, attempt=claimed.attempts, **credentials)
        env.repo._jobs[record.job_id] = claimed
        raise StorageUnavailable("private-repository-error")

    monkeypatch.setattr(env.repo, "submit_job", lost_ack)
    assert_error(await post(app(env)), 503, "admission_unavailable")
    assert env.repo.list_jobs()[0].status is JobStatus.CANCELLED


@pytest.mark.asyncio
async def test_lost_ack_even_after_success_is_fail_closed(env, monkeypatch):
    original = env.repo.submit_job
    env.repo.after_submit = lambda record: finish(env, record)

    def lost_ack(submission):
        original(submission)
        raise StorageUnavailable("private-repository-error")

    monkeypatch.setattr(env.repo, "submit_job", lost_ack)
    assert_error(await post(app(env)), 503, "admission_unavailable")
    assert env.repo.list_jobs()[0].status is JobStatus.SUCCEEDED
    assert len(env.repo.submissions) == 1 and len(env.spool._records) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["ack", "malformed-ack", "poll"])
async def test_wrong_identity_never_returns_text(env, monkeypatch, fault):
    method = "get_job" if fault == "poll" else "submit_job"
    original = getattr(env.repo, method)

    def wrong(*args, **kwargs):
        record = original(*args, **kwargs)
        return None if fault == "malformed-ack" else record.with_updates(priority=1)

    monkeypatch.setattr(env.repo, method, wrong)
    assert_error(await post(app(env)), 502 if fault == "poll" else 503)
    assert env.repo.list_jobs()[0].status is JobStatus.CANCELLED
    assert len(env.repo.submissions) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("result,status", [
    (JobResult(text=""), 200), (JobResult(text="あ" * 42), 200),
    (JobResult(text="あ" * 43), 502), (JobResult(text="\ud800"), 502),
    (JobResult(), 502), (JobResult(text="x", outputs={"text": "different"}), 502),
    (JobResult(text="x", artifacts=(ArtifactRef("https://private.invalid"),)), 502),
])
async def test_narrow_bounded_terminal_text(env, result, status):
    env.repo.after_submit = lambda record: finish(env, record, result)
    response = await post(app(env))
    assert response.status_code == status, response.text
    if status == 200:
        assert response.json() == {"text": result.text}
    else:
        assert_error(response, 502, "invalid_transcription_output")


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", [JobStatus.CANCELLED, JobStatus.FAILED])
async def test_terminal_failures(env, terminal):
    def terminate(record):
        if terminal is JobStatus.CANCELLED:
            env.repo.cancel_job(record.job_id)
        else:
            _, credentials = claim(env, record)
            env.repo.fail_job(record.job_id, "private-repository-error", retryable=True, **credentials)
    env.repo.after_submit = terminate
    assert_error(await post(app(env)), 409 if terminal is JobStatus.CANCELLED else 502)
    assert env.repo.list_jobs()[0].status is terminal
    assert env.repo.list_jobs()[0].attempts <= 1


@pytest.mark.asyncio
@pytest.mark.parametrize("phase,exit_kind", [
    ("stage", "task-cancel"), ("stage-discard-error", "task-cancel"),
    ("submit", "disconnect"), ("submit", "timeout"),
    ("lost-ack", "task-cancel"), ("poll", "disconnect"),
])
async def test_late_io_keeps_slot_and_cleans_by_admission_state(env, monkeypatch, phase, exit_kind):
    entered, release = Event(), Event()
    disconnect = asyncio.Event()
    staging = phase.startswith("stage")
    target = env.channel if staging else env.repo
    method = "stage" if staging else ("get_job" if phase == "poll" else "submit_job")
    original = getattr(target, method)
    if phase == "stage-discard-error":
        def failed_discard(uri):
            raise OSError("private-repository-error")
        monkeypatch.setattr(env.channel, "discard", failed_discard)

    def held(*args, **kwargs):
        value = original(*args, **kwargs)
        entered.set()
        if not release.wait(4):
            raise RuntimeError("test did not release I/O")
        if phase == "lost-ack":
            raise StorageUnavailable("private-repository-error")
        return value

    monkeypatch.setattr(target, method, held)
    service = AudioTranscriptionsService(env.repo, env.channel,
        replace(LIMITS, request_timeout_seconds=0.2))
    pending = asyncio.create_task(service.complete(raw_request(disconnect=disconnect), env.catalog, INGRESS))
    try:
        await wait_until(entered.is_set)
        if exit_kind == "task-cancel":
            pending.cancel()
        elif exit_kind == "disconnect":
            disconnect.set()
        with pytest.raises(asyncio.CancelledError if exit_kind == "task-cancel" else AudioGatewayError):
            await pending
        assert service.in_flight == 1
        with pytest.raises(AudioGatewayError, match="admission_overloaded"):
            await service.complete(raw_request(), env.catalog, INGRESS)
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        await drain(service)
    assert service.unresolved_outcomes == 0
    assert service.cleanup_failures == (1 if phase == "stage-discard-error" else 0)
    if staging:
        assert not env.repo.submissions
        assert len(env.spool._records) == (1 if phase == "stage-discard-error" else 0)
    else:
        assert len(env.repo.submissions) == 1 and len(env.spool._records) == 1
        assert env.repo.list_jobs()[0].status is JobStatus.CANCELLED


@pytest.mark.asyncio
async def test_late_successful_poll_cannot_return_text_after_disconnect(env, monkeypatch):
    entered, release = Event(), Event()
    disconnect = asyncio.Event()
    env.repo.after_submit = lambda record: finish(env, record)
    original_submit, original_get = env.repo.submit_job, env.repo.get_job

    def held_get(job_id):
        entered.set()
        if not release.wait(4):
            raise RuntimeError("test did not release read")
        return original_get(job_id)

    def submit_then_hold(submission):
        record = original_submit(submission)
        monkeypatch.setattr(env.repo, "get_job", held_get)
        return record

    monkeypatch.setattr(env.repo, "submit_job", submit_then_hold)
    service = AudioTranscriptionsService(env.repo, env.channel, LIMITS)
    pending = asyncio.create_task(service.complete(raw_request(disconnect=disconnect), env.catalog, INGRESS))
    try:
        await wait_until(entered.is_set)
        disconnect.set()
        with pytest.raises(AudioGatewayError, match="client_disconnected"):
            await pending
        assert service.in_flight == 1
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        await drain(service)
    assert env.repo.list_jobs()[0].status is JobStatus.SUCCEEDED
    assert service.unresolved_outcomes == 0 and len(env.repo.submissions) == 1


@pytest.mark.asyncio
async def test_running_cancel_fences_late_input_and_completion(env):
    state = {}

    def running(record):
        claimed, credentials = claim(env, record)
        state.update(record=record, claimed=claimed, credentials=credentials)

    env.repo.after_submit = running
    service = AudioTranscriptionsService(env.repo, env.channel,
        replace(LIMITS, request_timeout_seconds=0.1))
    with pytest.raises(AudioGatewayError, match="deadline_exceeded"):
        await service.complete(raw_request(), env.catalog, INGRESS)
    await drain(service)
    record = state["record"]
    assert env.repo.get_job(record.job_id).status is JobStatus.CANCELLED
    with pytest.raises(ConflictError):
        env.channel.read_for_worker(record.job_id, attempt=state["claimed"].attempts,
                                     **state["credentials"])
    with pytest.raises(ConflictError):
        env.repo.complete_job(record.job_id, JobResult(text="late"), **state["credentials"])


@pytest.mark.asyncio
async def test_failed_cancellation_has_finite_calls_and_unresolved_evidence(env, monkeypatch):
    def unavailable(job_id, **kwargs):
        env.repo.cancellations.append(job_id)
        raise StorageUnavailable("private-repository-error")
    monkeypatch.setattr(env.repo, "cancel_job", unavailable)
    service = AudioTranscriptionsService(env.repo, env.channel,
        replace(LIMITS, request_timeout_seconds=0.1))
    with pytest.raises(AudioGatewayError, match="deadline_exceeded"):
        await service.complete(raw_request(), env.catalog, INGRESS)
    await drain(service)
    assert len(env.repo.cancellations) == 3 and service.unresolved_outcomes == 1
    assert len(env.spool._records) == 1


@pytest.mark.asyncio
async def test_opt_in_route_only(env):
    instance = FastAPI()
    assert (await post(instance)).status_code == 404
    assert [(r.path, r.methods) for r in router(env).routes] == [
        ("/v1/audio/transcriptions", {"POST"}),
    ]
