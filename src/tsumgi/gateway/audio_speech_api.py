"""Explicitly mounted, providerless Speech ingress and durable result service.

No provider discovery, daemon mounting, or binary Job payloads. The injected
Control repository and output channel remain the only execution/output authority.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
import math
import secrets

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from ..audio.output_channel import AudioOutputChannel
from ..control.auth import ClientAuthMode
from ..control.models import JobStatus, JobSubmission, utc_now
from ..control.repository import (
    ConflictError, DeadlineExceededError, NoCompatibleDeployment, NotFoundError,
    OverloadedError,
)
from .audio import AudioGatewayError, AudioProfileCatalog, SPEECH_OPERATION_SCHEMA
from .audio_ingress import AudioIngressLimits, _check_wav, parse_speech_ingress
from .audio_voice_api import _TOKEN, _require_voice_client


def _error(code: str, status: int) -> AudioGatewayError:
    return AudioGatewayError(code, "request", status_code=status)


@dataclass(frozen=True, slots=True)
class AudioSpeechLimits:
    """Independent operator output, lifetime, and per-service concurrency caps."""

    output_bytes: int
    request_timeout_seconds: float
    max_pending_admissions: int
    poll_interval_seconds: float

    def __post_init__(self) -> None:
        if type(self.output_bytes) is not int or not 46 <= self.output_bytes <= 16 * 1024 * 1024:
            raise ValueError("invalid speech output limit")
        if (type(self.max_pending_admissions) is not int
                or not 1 <= self.max_pending_admissions <= 1024):
            raise ValueError("invalid speech concurrency limit")
        for value in (self.request_timeout_seconds, self.poll_interval_seconds):
            if (type(value) not in (int, float) or not 0 < value <= 300
                    or not math.isfinite(value)):
                raise ValueError("invalid speech time limit")
        if self.poll_interval_seconds > self.request_timeout_seconds:
            raise ValueError("invalid speech polling interval")


class AudioSpeechService:
    """One bounded slot owns the entire request, including late DB cleanup.

    HTTP cancellation cannot stop a synchronous repository thread. A detached
    operation retains its slot until it finishes and attempts bounded-call-count cleanup;
    the service never silently resubmits or treats missing readback as rollback.
    """

    def __init__(self, repository, channel: AudioOutputChannel, limits: AudioSpeechLimits):
        self.repository = repository
        self.channel = channel
        self.limits = limits
        self.in_flight = 0
        self.unresolved_outcomes = 0
        self.operations: set[asyncio.Task] = set()

    @staticmethod
    def _matches(record, submission: JobSubmission) -> bool:
        return (
            record.idempotency_key == submission.idempotency_key
            and record.capability == submission.capability
            and record.serving == submission.serving
            and record.deadline_at == submission.deadline_at
            and record.payload == submission.payload
            and record.max_attempts == submission.max_attempts
            and record.requirements == submission.requirements
            and record.priority == submission.priority
        )

    async def _cancel(self, job_id: str) -> None:
        for _ in range(3):
            try:
                record = await asyncio.to_thread(
                    self.repository.cancel_job, job_id,
                    admission_deadline_at=utc_now() + timedelta(seconds=0.6),
                )
            except Exception:
                continue
            if record.job_id == job_id and record.status in {
                JobStatus.CANCELLED, JobStatus.FAILED, JobStatus.SUCCEEDED,
            }:
                return
            break
        self.unresolved_outcomes += 1

    async def _reconcile(self, submission: JobSubmission) -> None:
        for _ in range(3):
            try:
                record = await asyncio.to_thread(
                    self.repository.get_job_by_idempotency_key,
                    submission.idempotency_key,
                    admission_deadline_at=utc_now() + timedelta(seconds=0.6),
                )
            except Exception:
                continue
            if record is None:
                continue
            if self._matches(record, submission):
                await self._cancel(record.job_id)
                return
            break
        # Absence after a lost COMMIT acknowledgement is not rollback evidence.
        self.unresolved_outcomes += 1

    async def _execute(self, submission: JobSubmission, abort: asyncio.Event, stop: float) -> bytes:
        job_id = None
        try:
            try:
                record = await asyncio.to_thread(
                    self.repository.submit_job, submission,
                    admission_deadline_at=submission.deadline_at,
                )
            except (DeadlineExceededError, OverloadedError, NoCompatibleDeployment) as exc:
                if isinstance(exc, DeadlineExceededError):
                    code, status = "deadline_exceeded", 408
                elif isinstance(exc, OverloadedError):
                    code, status = "overloaded", 429
                else:
                    code, status = "no_compatible_deployment", 503
                raise _error(code, status) from None
            except Exception as exc:
                await self._reconcile(submission)
                raise _error(
                    "admission_conflict" if isinstance(exc, ConflictError) else "admission_unavailable",
                    409 if isinstance(exc, ConflictError) else 503,
                ) from None
            if not self._matches(record, submission):
                await self._reconcile(submission)
                raise _error("admission_unavailable", 503)
            job_id = record.job_id
            while True:
                if abort.is_set() or asyncio.get_running_loop().time() >= stop:
                    raise _error("deadline_exceeded", 504)
                current = await asyncio.to_thread(self.repository.get_job, job_id)
                if abort.is_set() or asyncio.get_running_loop().time() >= stop:
                    raise _error("deadline_exceeded", 504)
                if current.job_id != job_id or not self._matches(current, submission):
                    raise _error("invalid_job_result", 502)
                if current.status is JobStatus.CANCELLED:
                    raise _error("request_cancelled", 409)
                if current.status is JobStatus.FAILED:
                    raise _error("backend_failure", 502)
                if current.status is JobStatus.SUCCEEDED:
                    result = current.result
                    if result is None or len(result.artifacts) != 1:
                        raise _error("invalid_audio_output", 502)
                    ref = result.artifacts[0]
                    if (ref.media_type != "audio/wav" or not ref.uri.startswith("tsumgi-audio:")
                            or not ref.uri[len("tsumgi-audio:"):]
                            or type(ref.size_bytes) is not int
                            or not 46 <= ref.size_bytes <= self.limits.output_bytes):
                        raise _error("invalid_audio_output", 502)
                    try:
                        data = await asyncio.to_thread(
                            self.channel.read_published, job_id, ref.uri[len("tsumgi-audio:"):],
                        )
                    except (NotFoundError, ValueError):
                        raise _error("invalid_audio_output", 502) from None
                    if type(data) is not bytes or len(data) != ref.size_bytes:
                        raise _error("invalid_audio_output", 502)
                    try:
                        # Reuse the existing narrow PCM16 RIFF/WAVE policy.
                        _check_wav(data)
                    except AudioGatewayError:
                        raise _error("invalid_audio_output", 502) from None
                    if abort.is_set() or asyncio.get_running_loop().time() >= stop:
                        raise _error("deadline_exceeded", 504)
                    return data
                await asyncio.sleep(min(self.limits.poll_interval_seconds, max(
                    0, stop - asyncio.get_running_loop().time(),
                )))
        except BaseException:
            if job_id is not None:
                await self._cancel(job_id)
            raise

    def _finished(self, task: asyncio.Task) -> None:
        self.operations.discard(task)
        self.in_flight -= 1
        # Retrieve even detached failures; never log raw repository exceptions.
        if not task.cancelled():
            task.exception()

    @staticmethod
    async def _disconnect(request: Request) -> None:
        while True:
            message = await request.receive()
            if message.get("type") == "http.disconnect":
                return
            await asyncio.sleep(0.01)

    async def complete(self, request: Request, catalog, ingress_limits) -> bytes:
        if self.in_flight >= self.limits.max_pending_admissions:
            raise _error("admission_overloaded", 429)
        self.in_flight += 1
        transferred = False
        watcher = None
        abort = asyncio.Event()
        loop = asyncio.get_running_loop()
        stop = loop.time() + self.limits.request_timeout_seconds
        deadline = utc_now() + timedelta(seconds=self.limits.request_timeout_seconds)
        try:
            async with asyncio.timeout_at(stop):
                validated = await parse_speech_ingress(request, catalog, limits=ingress_limits)
                if loop.time() >= stop:
                    raise _error("deadline_exceeded", 408)
                submission = JobSubmission(
                    capability="audio.synthesize", serving=validated.profile.binding,
                    payload={"input": validated.input, "voice": {
                        "alias": validated.voice.alias,
                        "speaker_id": validated.voice.speaker_id,
                        "style_id": validated.voice.style_id,
                    }, "response_format": "wav"},
                    deadline_at=deadline, max_attempts=1,
                    idempotency_key="audio-speech:" + secrets.token_hex(32),
                )
                watcher = asyncio.create_task(self._disconnect(request))
                operation = asyncio.create_task(self._execute(submission, abort, stop))
                self.operations.add(operation)
                operation.add_done_callback(self._finished)
                transferred = True
                done, _ = await asyncio.wait((operation, watcher), return_when=asyncio.FIRST_COMPLETED)
                if watcher in done:
                    raise _error("client_disconnected", 499)
                data = operation.result()
                if loop.time() >= stop:
                    raise _error("deadline_exceeded", 504)
                return data
        except TimeoutError:
            raise _error("deadline_exceeded", 504) from None
        finally:
            abort.set()
            if watcher is not None:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
            if not transferred:
                self.in_flight -= 1


def create_audio_speech_router(
    catalog: AudioProfileCatalog, repository, audio_output_channel: AudioOutputChannel, *,
    client_auth: ClientAuthMode | str, client_token: str | None,
    ingress_limits: AudioIngressLimits, limits: AudioSpeechLimits,
) -> APIRouter:
    """Fail closed at construction; no configuration loading or implicit mount."""
    if (ClientAuthMode(client_auth) is not ClientAuthMode.BEARER
            or type(client_token) is not str or _TOKEN.fullmatch(client_token) is None):
        raise ValueError("audio speech requires bearer authentication")
    if type(catalog) is not AudioProfileCatalog:
        raise ValueError("audio speech requires reviewed catalog")
    reviewed = AudioProfileCatalog.from_dict(catalog.to_dict())
    if not any(p.resolved.profile.operation_schema == SPEECH_OPERATION_SCHEMA for p in reviewed.profiles):
        raise ValueError("audio speech requires a speech profile")
    if type(ingress_limits) is not AudioIngressLimits or type(limits) is not AudioSpeechLimits:
        raise ValueError("audio speech requires explicit limits")
    if (not isinstance(audio_output_channel, AudioOutputChannel)
            or audio_output_channel.repository is not repository
            or limits.output_bytes > audio_output_channel.max_bytes):
        raise ValueError("audio speech requires same-repository bounded output authority")
    if not all(callable(getattr(repository, name, None)) for name in (
        "submit_job", "get_job", "get_job_by_idempotency_key", "cancel_job",
    )):
        raise ValueError("audio speech requires durable repository operations")
    service = AudioSpeechService(repository, audio_output_channel, limits)
    router = APIRouter()

    @router.post("/v1/audio/speech")
    async def speech(request: Request) -> Response:
        headers = {"Cache-Control": "no-store"}
        try:
            _require_voice_client(request, client_token)
            if request.scope.get("query_string", b""):
                raise _error("unsupported_query", 400)
            data = await service.complete(request, reviewed, ingress_limits)
            return Response(data, media_type="audio/wav", headers=headers)
        except AudioGatewayError as exc:
            if exc.status_code == 401:
                headers["WWW-Authenticate"] = "Bearer"
            return JSONResponse(exc.to_dict(), status_code=exc.status_code, headers=headers)
        except Exception:
            exc = _error("audio_unavailable", 503)
            return JSONResponse(exc.to_dict(), status_code=exc.status_code, headers=headers)

    return router


__all__ = ["AudioSpeechLimits", "create_audio_speech_router"]
