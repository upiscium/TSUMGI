"""Providerless synthetic ASGI/Job/output tests; no DB, model or network service."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
import struct
from threading import Event
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from starlette.requests import Request

from astrumweaver import ResourceShape, WorkerSpec
from astrumweaver.audio import ArtifactSpool, AudioOutputChannel
from astrumweaver.control import (
    ConflictError, InMemoryControlRepository, JobStatus, WorkerRegistration,
)
from astrumweaver.control.auth import ClientAuthMode
from astrumweaver.control.models import utc_now
from astrumweaver.control.repository import (
    DeadlineExceededError, NoCompatibleDeployment, OverloadedError, StorageUnavailable,
)
from astrumweaver.execution import JobResult
from astrumweaver.gateway.audio import AudioGatewayError, AudioProfileCatalog
from astrumweaver.gateway.audio_ingress import AudioIngressLimits
from astrumweaver.gateway.audio_speech_api import (
    AudioSpeechLimits, AudioSpeechService, create_audio_speech_router,
)
from astrumweaver.serving import RuntimeInstance, WorkerServingAdvertisement
from astrumweaver.transport import job_record_to_dict
from test_audio_voice_api import AUTH, TOKEN, make_profile


EPOCH = "12345678-1234-4234-9234-123456789abc"
BODY = {
    "model": "synthetic-voicevox", "input": " 日本語の合成テスト\n",
    "voice": "sample-a", "response_format": "wav",
}
WAV = (
    b"RIFF" + struct.pack("<I", 38) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1,
    16000, 32000, 2, 16) + b"data" + struct.pack("<I", 2) + b"\x00\x00"
)
INGRESS = AudioIngressLimits(
    request_bytes=1024, input_bytes=128, file_bytes=256, metadata_bytes=256,
    part_header_bytes=256, total_header_bytes=2048, receive_events=16,
    idle_timeout_seconds=0.5, total_timeout_seconds=1,
)
LIMITS = AudioSpeechLimits(
    output_bytes=256, request_timeout_seconds=2, max_pending_admissions=1,
    poll_interval_seconds=0.005,
)


class RecordingRepository(InMemoryControlRepository):
    def __init__(self):
        super().__init__(lease_seconds=60)
        self.submissions = []
        self.cancellations = []
        self.readbacks = []
        self.after_submit = None

    def submit_job(self, submission, **kwargs):
        self.submissions.append((submission, kwargs))
        record = super().submit_job(submission, **kwargs)
        if self.after_submit is not None:
            self.after_submit(record)
        return record

    def cancel_job(self, job_id, **kwargs):
        self.cancellations.append((job_id, kwargs))
        return super().cancel_job(job_id, **kwargs)

    def get_job_by_idempotency_key(self, key, **kwargs):
        self.readbacks.append((key, kwargs))
        return super().get_job_by_idempotency_key(key, **kwargs)


@pytest.fixture
def env(tmp_path):
    repo = RecordingRepository()
    profiles = tuple(make_profile(kind) for kind in ("voicevox", "model-tts", "model-stt"))
    catalog = AudioProfileCatalog(profiles)
    for profile in profiles:
        resolved = profile.resolved
        repo.register_worker(WorkerRegistration(
            spec=WorkerSpec(
                worker_id=profile.profile_id, worker_class="synthetic-cpu",
                resources=ResourceShape(), capabilities=frozenset({resolved.profile.capability}),
            ),
            serving=WorkerServingAdvertisement(
                deployment_revision=resolved.deployment.revision,
                runtime_instance=RuntimeInstance(resolved.deployment.revision, EPOCH),
                contracts=(resolved.contract,),
            ),
        ))
    root = tmp_path / "speech-spool"
    root.mkdir(mode=0o700)
    spool = ArtifactSpool(root, max_bytes=512, max_total_bytes=2048,
                          max_artifacts=4, ttl_seconds=60)
    channel = AudioOutputChannel(repo, spool, max_bytes=512)
    return SimpleNamespace(repo=repo, catalog=catalog, channel=channel)


def router(env, **overrides):
    options = dict(client_auth=ClientAuthMode.BEARER, client_token=TOKEN,
                   ingress_limits=INGRESS, limits=LIMITS)
    options.update(overrides)
    return create_audio_speech_router(env.catalog, env.repo, env.channel, **options)


def app(env, **overrides):
    instance = FastAPI()
    instance.include_router(router(env, **overrides))
    return instance


async def post(instance, *, body=BODY, headers=AUTH, content=None, path="/v1/audio/speech"):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=instance), base_url="http://synthetic",
    ) as client:
        return await client.post(path, headers=headers, **(
            {"json": body} if content is None else {"content": content}
        ))


def assert_error(response, status, code, field="request"):
    assert response.status_code == status, response.text
    assert response.json() == AudioGatewayError(code, field, status_code=status).to_dict()
    assert response.headers["cache-control"] == "no-store"
    for private in (TOKEN, BODY["input"], "private-repository-error", EPOCH,
                    "synthetic-private-config", "synthetic-provider"):
        assert private not in response.text


def worker_id(env, record):
    return next(p.profile_id for p in env.catalog.profiles if p.binding == record.serving)


def claim(env, record):
    claimed = env.repo.claim_next_job(worker_id(env, record), runtime_instance_epoch=EPOCH)
    assert claimed is not None and claimed.job_id == record.job_id
    return claimed


def publish(env, record, *, data=WAV):
    claimed = claim(env, record)
    credentials = dict(worker_id=worker_id(env, record), lease_token=claimed.lease_token,
                       runtime_instance_epoch=EPOCH)
    ref = env.channel.stage(record.job_id, attempt=claimed.attempts, data=data, **credentials)
    env.channel.complete_job(record.job_id, JobResult(artifacts=(ref,)), **credentials)
    return ref


def raw_request(*, disconnect=None, body=None):
    raw = json.dumps(BODY if body is None else body, ensure_ascii=False).encode()
    sent = False
    never = asyncio.Event() if disconnect is None else disconnect

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": raw, "more_body": False}
        await never.wait()
        return {"type": "http.disconnect"}

    return Request({
        "type": "http", "method": "POST", "path": "/v1/audio/speech", "query_string": b"",
        "headers": [(b"content-type", b"application/json"),
                    (b"content-length", str(len(raw)).encode())],
    }, receive)


async def wait_until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.001)


async def drain(service):
    await wait_until(lambda: service.in_flight == 0 and not service.operations)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["voicevox", "model-tts"])
async def test_exact_pins_single_attempt_and_terminal_channel_wav(env, kind):
    before = env.catalog.to_dict()
    env.repo.after_submit = lambda record: publish(env, record)
    started = utc_now()
    response = await post(app(env), body={**BODY, "model": "synthetic-" + kind})
    assert response.status_code == 200, response.text
    assert response.content == WAV
    assert response.headers["content-type"] == "audio/wav"
    assert response.headers["cache-control"] == "no-store"
    assert len(env.repo.submissions) == 1
    submission, kwargs = env.repo.submissions[0]
    profile = next(p for p in env.catalog.profiles if p.profile_id == "synthetic-" + kind)
    voice = next(v for v in profile.voices if v.alias == BODY["voice"])
    assert submission.capability == "audio.synthesize"
    assert submission.serving == profile.binding
    assert submission.payload == {
        "input": BODY["input"], "voice": {"alias": voice.alias,
        "speaker_id": voice.speaker_id, "style_id": voice.style_id}, "response_format": "wav",
    }
    assert submission.max_attempts == 1
    assert submission.idempotency_key.startswith("audio-speech:")
    assert len(submission.idempotency_key) == len("audio-speech:") + 64
    assert started < submission.deadline_at <= utc_now() + timedelta(seconds=2)
    assert kwargs == {"admission_deadline_at": submission.deadline_at}
    durable = env.repo.list_jobs()[0]
    assert durable.status is JobStatus.SUCCEEDED and durable.attempts == 1
    assert WAV not in json.dumps(job_record_to_dict(durable)).encode()
    assert env.catalog.to_dict() == before
    assert not env.repo.cancellations and not env.repo.readbacks


@pytest.mark.asyncio
async def test_identical_http_requests_get_independent_internal_keys(env):
    env.repo.after_submit = lambda record: publish(env, record)
    instance = app(env)
    assert (await post(instance)).status_code == 200
    assert (await post(instance)).status_code == 200
    assert len({s.idempotency_key for s, _ in env.repo.submissions}) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [
    {}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic private"},
    [("Authorization", "Bearer " + TOKEN), ("Authorization", "Bearer " + TOKEN)],
])
async def test_auth_precedes_body_and_admission(env, headers):
    instance = app(env)

    async def no_body(scope, receive, send):
        async def forbidden_receive():
            pytest.fail("unauthenticated request consumed a body")
        await instance(scope, forbidden_receive, send)

    response = await post(no_body, headers=headers, content=b"private invalid JSON")
    assert_error(response, 401, "unauthorized", "authorization")
    assert response.headers["www-authenticate"] == "Bearer"
    assert not env.repo.submissions


@pytest.mark.parametrize("overrides", [
    {"client_auth": ClientAuthMode.NONE}, {"client_token": None},
    {"client_token": "bad token"}, {"client_token": "日本語"},
    {"ingress_limits": None}, {"limits": None}, {"limits": replace(LIMITS, output_bytes=513)},
])
def test_factory_rejects_unsafe_configuration(env, overrides):
    with pytest.raises(ValueError):
        router(env, **overrides)


def test_factory_requires_speech_catalog_and_same_repository(env):
    env.catalog = AudioProfileCatalog((make_profile("model-stt"),))
    with pytest.raises(ValueError, match="speech profile"):
        router(env)
    env.catalog = AudioProfileCatalog((make_profile("voicevox"),))
    env.repo = RecordingRepository()
    with pytest.raises(ValueError, match="same-repository"):
        router(env)


def test_factory_revalidates_catalog_and_repository_interface(env, monkeypatch):
    catalog = env.catalog
    env.catalog = catalog.to_dict()
    with pytest.raises(ValueError, match="reviewed catalog"):
        router(env)
    env.catalog = catalog
    monkeypatch.setattr(env.repo, "get_job_by_idempotency_key", None)
    with pytest.raises(ValueError, match="durable repository"):
        router(env)


@pytest.mark.parametrize("field,value", [
    ("output_bytes", True), ("output_bytes", 45), ("output_bytes", 16 * 1024 * 1024 + 1),
    ("max_pending_admissions", False), ("max_pending_admissions", 0),
    ("max_pending_admissions", 1025), ("request_timeout_seconds", float("nan")),
    ("request_timeout_seconds", float("inf")), ("request_timeout_seconds", True),
    ("request_timeout_seconds", 10 ** 1000),
    ("request_timeout_seconds", 0), ("request_timeout_seconds", 301),
    ("poll_interval_seconds", 0), ("poll_interval_seconds", 3),
])
def test_limits_fail_closed(field, value):
    with pytest.raises(ValueError):
        replace(LIMITS, **{field: value})


@pytest.mark.asyncio
@pytest.mark.parametrize("changes,status", [
    ({"model": "synthetic-model-stt"}, 400), ({"model": "unknown-profile"}, 404),
    ({"voice": "unreviewed"}, 400), ({"response_format": "mp3"}, 400),
    ({"input": " "}, 400), ({"input": "あ" * 43}, 413),
    ({"stream": True}, 400), ({"backend_url": "https://private.invalid"}, 400),
])
async def test_invalid_request_never_submits(env, changes, status):
    response = await post(app(env), body={**BODY, **changes})
    assert response.status_code == status, response.text
    assert response.headers["cache-control"] == "no-store"
    assert not env.repo.submissions and not env.repo.list_jobs()
    assert "https://private.invalid" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("content,headers,status", [
    (b'{"model":"a","model":"b"}', {**AUTH, "Content-Type": "application/json"}, 400),
    (b"x" * 1025, {**AUTH, "Content-Type": "application/json"}, 413),
    (b"{}", {**AUTH, "Content-Type": "text/plain"}, 415),
    (b"{}", {**AUTH, "Content-Type": "application/json", "Content-Encoding": "gzip"}, 415),
])
async def test_bounded_strict_ingress_precedes_admission(env, content, headers, status):
    response = await post(app(env), content=content, headers=headers)
    assert response.status_code == status
    assert not env.repo.submissions


@pytest.mark.asyncio
async def test_query_cannot_override_body(env):
    response = await post(app(env), path="/v1/audio/speech?backend_url=private")
    assert_error(response, 400, "unsupported_query")
    assert not env.repo.submissions


@pytest.mark.asyncio
@pytest.mark.parametrize("exception,status,code", [
    (DeadlineExceededError, 408, "deadline_exceeded"),
    (OverloadedError, 429, "overloaded"),
    (NoCompatibleDeployment, 503, "no_compatible_deployment"),
    (type("CapacitySubclass", (OverloadedError,), {}), 429, "overloaded"),
])
async def test_definitive_admission_refusal_is_sanitized_without_retry(
    env, monkeypatch, exception, status, code,
):
    calls = []

    def refuse(*args, **kwargs):
        calls.append(kwargs)
        raise exception("private-repository-error")

    monkeypatch.setattr(env.repo, "submit_job", refuse)
    response = await post(app(env))
    assert_error(response, status, code)
    assert len(calls) == 1
    assert not env.repo.readbacks and not env.repo.cancellations


@pytest.mark.asyncio
async def test_real_repository_no_matching_deployment_has_no_fallback(env):
    # Online workers with OTHER reviewed pins cannot satisfy the requested pin.
    env.catalog = AudioProfileCatalog((make_profile("voicevox", attribution="changed semantics"),))
    response = await post(app(env))
    assert_error(response, 503, "no_compatible_deployment")
    assert len(env.repo.submissions) == 1 and not env.repo.list_jobs()


@pytest.mark.asyncio
async def test_admission_identity_mismatch_reconciles_only_actual_submission(env, monkeypatch):
    original = env.repo.submit_job

    def wrong_ack(*args, **kwargs):
        return original(*args, **kwargs).with_updates(payload={"input": "other"})

    monkeypatch.setattr(env.repo, "submit_job", wrong_ack)
    assert_error(await post(app(env)), 503, "admission_unavailable")
    assert len(env.repo.submissions) == 1 and len(env.repo.readbacks) == 1
    assert env.repo.list_jobs()[0].status is JobStatus.CANCELLED


@pytest.mark.asyncio
async def test_poll_identity_mismatch_cannot_read_output(env, monkeypatch):
    original = env.repo.get_job

    def wrong_read(job_id):
        return original(job_id).with_updates(payload={"input": "other"})

    monkeypatch.setattr(env.repo, "get_job", wrong_read)
    monkeypatch.setattr(env.channel, "read_published", lambda *args: pytest.fail("read mismatched Job"))
    assert_error(await post(app(env)), 502, "invalid_job_result")
    assert len(env.repo.submissions) == 1 and len(env.repo.cancellations) == 1


@pytest.mark.asyncio
async def test_unexpected_storage_exception_is_not_exposed(env, monkeypatch):
    def unavailable(job_id):
        raise StorageUnavailable("private-repository-error")

    monkeypatch.setattr(env.repo, "get_job", unavailable)
    assert_error(await post(app(env)), 503, "audio_unavailable")
    assert len(env.repo.submissions) == 1 and len(env.repo.cancellations) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", [JobStatus.CANCELLED, JobStatus.FAILED])
async def test_durable_failure_or_cancellation_never_reads_output(env, monkeypatch, terminal):
    def finish(record):
        if terminal is JobStatus.CANCELLED:
            env.repo.cancel_job(record.job_id)
        else:
            claimed = claim(env, record)
            env.repo.fail_job(record.job_id, "private-repository-error", retryable=True,
                              worker_id=worker_id(env, record), lease_token=claimed.lease_token,
                              runtime_instance_epoch=EPOCH)

    env.repo.after_submit = finish
    monkeypatch.setattr(env.channel, "read_published", lambda *args: pytest.fail("read failed output"))
    response = await post(app(env))
    assert_error(response, 409 if terminal is JobStatus.CANCELLED else 502,
                 "request_cancelled" if terminal is JobStatus.CANCELLED else "backend_failure")
    durable = env.repo.list_jobs()[0]
    assert durable.status is terminal
    assert durable.attempts <= 1 and durable.max_attempts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [
    "missing", "multiple", "url", "empty-id", "size-missing", "size-small", "size-large",
    "size-float", "mime", "unstaged", "forged-digest", "foreign-job", "malformed-wav",
    "expired", "tampered",
])
async def test_only_exact_intact_bounded_terminal_channel_output_is_returned(env, fault):
    def finish(record):
        if fault == "malformed-wav":
            publish(env, record, data=b"RIFF" + b"\x00" * 4 + b"WAVE" + b"x" * 34)
            return
        ref = publish(env, record)
        durable = env.repo.get_job(record.job_id)
        if fault == "expired":
            expired = env.channel._spool._clock() + 60
            env.channel._spool._clock = lambda: expired
            return
        if fault == "tampered":
            artifact_id = ref.uri.removeprefix("tsumgi-audio:")
            # Corruption is injected through the spool's recorded private file.
            spool = env.channel._spool
            path = Path(spool._root) / spool._name / artifact_id
            path.write_bytes(WAV[:-2] + b"\x01\x00")
            return
        if fault == "missing":
            refs = ()
        elif fault == "multiple":
            refs = (ref, ref)
        else:
            changes = {
                "url": {"uri": "https://private.invalid/audio.wav"},
                "empty-id": {"uri": "tsumgi-audio:"}, "size-missing": {"size_bytes": None},
                "size-small": {"size_bytes": 45}, "size-large": {"size_bytes": 257},
                "size-float": {"size_bytes": float(len(WAV))},
                "mime": {"media_type": "audio/mpeg"}, "unstaged": {"uri": "tsumgi-audio:unknown"},
                "forged-digest": {"digest": "sha256:" + "0" * 64},
                "foreign-job": {"uri": "tsumgi-audio:foreign"},
            }[fault]
            if fault == "foreign-job":
                artifact_id = ref.uri.removeprefix("tsumgi-audio:")
                env.channel._staged[artifact_id] = replace(
                    env.channel._staged[artifact_id], job_id="foreign-job",
                )
                changes = {}
            refs = (replace(ref, **changes),)
        # Simulate a corrupt/legacy durable result bypassing channel completion.
        env.repo._jobs[record.job_id] = durable.with_updates(result=JobResult(artifacts=refs))

    env.repo.after_submit = finish
    response = await post(app(env))
    assert_error(response, 502, "invalid_audio_output")
    assert len(env.repo.submissions) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [bytearray(WAV), WAV[:-1]])
async def test_channel_return_type_and_size_are_rechecked(env, monkeypatch, data):
    env.repo.after_submit = lambda record: publish(env, record)
    monkeypatch.setattr(env.channel, "read_published", lambda *args: data)
    assert_error(await post(app(env)), 502, "invalid_audio_output")


@pytest.mark.asyncio
async def test_staged_output_is_never_polled_before_durable_success(env, monkeypatch):
    staged = Event()

    def stage_only(record):
        claimed = claim(env, record)
        env.channel.stage(record.job_id, worker_id=worker_id(env, record),
                          attempt=claimed.attempts, lease_token=claimed.lease_token,
                          runtime_instance_epoch=EPOCH, data=WAV)
        staged.set()

    env.repo.after_submit = stage_only
    monkeypatch.setattr(env.channel, "read_published", lambda *args: pytest.fail("early output read"))
    service = AudioSpeechService(env.repo, env.channel, replace(LIMITS, request_timeout_seconds=0.25))
    with pytest.raises(AudioGatewayError, match="deadline_exceeded"):
        await service.complete(raw_request(), env.catalog, INGRESS)
    await drain(service)
    assert staged.is_set()
    assert env.repo.list_jobs()[0].status is JobStatus.CANCELLED
    assert service.unresolved_outcomes == 0
    assert env.channel.reap_abandoned() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["lost-ack", "conflict", "missing", "unavailable", "mismatch"])
async def test_ambiguous_admission_readback_never_resubmits_or_cancels_other_identity(
    env, monkeypatch, failure,
):
    original = env.repo.submit_job

    def uncertain(submission, **kwargs):
        if failure in {"lost-ack", "conflict", "mismatch"}:
            record = original(submission, **kwargs)
            if failure == "mismatch":
                env.repo._jobs[record.job_id] = record.with_updates(payload={"input": "other"})
        else:
            env.repo.submissions.append((submission, kwargs))
        raise (ConflictError if failure == "conflict" else StorageUnavailable)("private-repository-error")

    monkeypatch.setattr(env.repo, "submit_job", uncertain)
    if failure == "unavailable":
        def unavailable(key, **kwargs):
            env.repo.readbacks.append((key, kwargs))
            raise StorageUnavailable("private-repository-error")
        monkeypatch.setattr(env.repo, "get_job_by_idempotency_key", unavailable)
    service = AudioSpeechService(env.repo, env.channel, LIMITS)
    with pytest.raises(AudioGatewayError) as caught:
        await service.complete(raw_request(), env.catalog, INGRESS)
    await drain(service)
    assert caught.value.code == ("admission_conflict" if failure == "conflict" else "admission_unavailable")
    assert len(env.repo.submissions) == 1
    assert len(env.repo.readbacks) == (3 if failure in {"missing", "unavailable"} else 1)
    assert all(kwargs["admission_deadline_at"].tzinfo is not None for _, kwargs in env.repo.readbacks)
    if failure in {"lost-ack", "conflict"}:
        assert env.repo.list_jobs()[0].status is JobStatus.CANCELLED
        assert service.unresolved_outcomes == 0
    else:
        assert not env.repo.cancellations
        assert service.unresolved_outcomes == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_kind", ["timeout", "disconnect", "task-cancel"])
async def test_late_admission_owns_slot_until_known_job_is_cancelled(env, monkeypatch, exit_kind):
    entered, release = Event(), Event()
    disconnect = asyncio.Event()
    original = env.repo.submit_job

    def held(submission, **kwargs):
        record = original(submission, **kwargs)
        entered.set()
        if not release.wait(5):
            raise RuntimeError("test did not release submission")
        return record

    monkeypatch.setattr(env.repo, "submit_job", held)
    service = AudioSpeechService(env.repo, env.channel, replace(LIMITS, request_timeout_seconds=0.25))
    pending = asyncio.create_task(service.complete(raw_request(disconnect=disconnect), env.catalog, INGRESS))
    try:
        await wait_until(entered.is_set)
        if exit_kind == "disconnect":
            disconnect.set()
        elif exit_kind == "task-cancel":
            pending.cancel()
        expected = asyncio.CancelledError if exit_kind == "task-cancel" else AudioGatewayError
        with pytest.raises(expected):
            await pending
        assert service.in_flight == 1 and len(service.operations) == 1
        with pytest.raises(AudioGatewayError, match="admission_overloaded"):
            await service.complete(raw_request(), env.catalog, INGRESS)
        assert len(env.repo.submissions) == 1
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        await drain(service)
    assert env.repo.list_jobs()[0].status is JobStatus.CANCELLED
    assert len(env.repo.cancellations) == 1
    assert service.unresolved_outcomes == 0


@pytest.mark.asyncio
async def test_late_poll_cannot_read_output_after_abort(env, monkeypatch):
    entered, release = Event(), Event()
    env.repo.after_submit = lambda record: publish(env, record)
    original = env.repo.get_job
    # Install only after submit's nested claim/stage/complete calls have finished.
    submit = env.repo.submit_job

    def held_read(job_id):
        entered.set()
        if not release.wait(5):
            raise RuntimeError("test did not release read")
        return original(job_id)

    def submit_then_hold(*args, **kwargs):
        record = submit(*args, **kwargs)
        monkeypatch.setattr(env.repo, "get_job", held_read)
        return record

    monkeypatch.setattr(env.repo, "submit_job", submit_then_hold)
    monkeypatch.setattr(env.channel, "read_published", lambda *args: pytest.fail("read after abort"))
    service = AudioSpeechService(env.repo, env.channel, replace(LIMITS, request_timeout_seconds=0.25))
    pending = asyncio.create_task(service.complete(raw_request(), env.catalog, INGRESS))
    try:
        await wait_until(entered.is_set)
        with pytest.raises(AudioGatewayError, match="deadline_exceeded"):
            await pending
        assert service.in_flight == 1
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        await drain(service)
    assert service.unresolved_outcomes == 0  # Already-SUCCEEDED is settled, not cancelled.


@pytest.mark.asyncio
async def test_finite_cancellation_retries_preserve_unresolved_evidence(env, monkeypatch):
    def unavailable(job_id, **kwargs):
        env.repo.cancellations.append((job_id, kwargs))
        raise StorageUnavailable("private-repository-error")

    monkeypatch.setattr(env.repo, "cancel_job", unavailable)
    service = AudioSpeechService(env.repo, env.channel, replace(LIMITS, request_timeout_seconds=0.25))
    with pytest.raises(AudioGatewayError, match="deadline_exceeded"):
        await service.complete(raw_request(), env.catalog, INGRESS)
    await drain(service)
    assert len(env.repo.cancellations) == 3
    assert all(kwargs["admission_deadline_at"].tzinfo is not None for _, kwargs in env.repo.cancellations)
    assert service.unresolved_outcomes == 1
    assert env.repo.list_jobs()[0].status is JobStatus.QUEUED


@pytest.mark.asyncio
async def test_cancellation_retries_can_settle_without_false_unresolved_evidence(env, monkeypatch):
    original = env.repo.cancel_job
    calls = []

    def transient(job_id, **kwargs):
        calls.append(job_id)
        if len(calls) < 3:
            raise StorageUnavailable("private-repository-error")
        return original(job_id, **kwargs)

    monkeypatch.setattr(env.repo, "cancel_job", transient)
    service = AudioSpeechService(env.repo, env.channel, replace(LIMITS, request_timeout_seconds=0.25))
    with pytest.raises(AudioGatewayError, match="deadline_exceeded"):
        await service.complete(raw_request(), env.catalog, INGRESS)
    await drain(service)
    assert len(calls) == 3 and service.unresolved_outcomes == 0
    assert env.repo.list_jobs()[0].status is JobStatus.CANCELLED


@pytest.mark.asyncio
async def test_ingress_timeout_releases_slot_without_submitting(env):
    service = AudioSpeechService(env.repo, env.channel, replace(LIMITS, request_timeout_seconds=0.05))
    request = raw_request()

    async def stalled_receive():
        await asyncio.Event().wait()

    request = Request(request.scope, stalled_receive)
    with pytest.raises(AudioGatewayError, match="deadline_exceeded"):
        await service.complete(request, env.catalog, INGRESS)
    assert service.in_flight == 0 and not service.operations
    assert not env.repo.submissions
    with pytest.raises(AudioGatewayError):
        await service.complete(raw_request(body={}), env.catalog, INGRESS)
    assert service.in_flight == 0 and not service.operations


@pytest.mark.asyncio
async def test_output_read_timeout_keeps_slot_until_late_read_settles(env, monkeypatch):
    entered, release = Event(), Event()
    env.repo.after_submit = lambda record: publish(env, record)
    original = env.channel.read_published

    def held_read(*args):
        entered.set()
        if not release.wait(5):
            raise RuntimeError("test did not release output read")
        return original(*args)

    monkeypatch.setattr(env.channel, "read_published", held_read)
    service = AudioSpeechService(env.repo, env.channel, replace(LIMITS, request_timeout_seconds=0.25))
    pending = asyncio.create_task(service.complete(raw_request(), env.catalog, INGRESS))
    try:
        await wait_until(entered.is_set)
        with pytest.raises(AudioGatewayError, match="deadline_exceeded"):
            await pending
        assert service.in_flight == 1
        with pytest.raises(AudioGatewayError, match="admission_overloaded"):
            await service.complete(raw_request(), env.catalog, INGRESS)
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        await drain(service)
    assert service.unresolved_outcomes == 0
    assert env.repo.list_jobs()[0].status is JobStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_late_lost_commit_ack_is_reconciled_after_http_deadline(env, monkeypatch):
    entered, release = Event(), Event()
    original = env.repo.submit_job

    def lost_ack(*args, **kwargs):
        original(*args, **kwargs)
        entered.set()
        if not release.wait(5):
            raise RuntimeError("test did not release lost acknowledgement")
        raise StorageUnavailable("private-repository-error")

    monkeypatch.setattr(env.repo, "submit_job", lost_ack)
    service = AudioSpeechService(env.repo, env.channel, replace(LIMITS, request_timeout_seconds=0.25))
    pending = asyncio.create_task(service.complete(raw_request(), env.catalog, INGRESS))
    try:
        await wait_until(entered.is_set)
        with pytest.raises(AudioGatewayError, match="deadline_exceeded"):
            await pending
        assert service.in_flight == 1 and not env.repo.readbacks
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        await drain(service)
    assert len(env.repo.submissions) == 1 and len(env.repo.readbacks) == 1
    assert env.repo.list_jobs()[0].status is JobStatus.CANCELLED
    assert service.unresolved_outcomes == 0


@pytest.mark.asyncio
async def test_router_is_opt_in_and_does_not_register_other_endpoints(env):
    instance = FastAPI()
    assert (await post(instance)).status_code == 404
    instance.include_router(router(env))
    assert [(r.path, r.methods) for r in router(env).routes if r.path.startswith("/v1/")] == [
        ("/v1/audio/speech", {"POST"}),
    ]
