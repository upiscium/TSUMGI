"""Opt-in providerless transcription ingress; Control/#174 retain authority.

Spool staging and Job admission are NOT atomic. No provider, model, daemon or
Worker download endpoint is installed here. Ownership below is process-local.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import timedelta
import math
import secrets

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..audio.input_channel import AudioInputChannel
from ..audio.pcm16 import validate_pcm16_wav
from ..control.auth import ClientAuthMode
from ..control.models import JobRecord, JobStatus, JobSubmission, utc_now
from ..control.repository import (
    _submission_matches, ConflictError, DeadlineExceededError,
    NoCompatibleDeployment, OverloadedError,
)
from .audio import AudioGatewayError, AudioProfileCatalog, TRANSCRIPTION_OPERATION_SCHEMA
from .audio_ingress import (
    AudioIngressLimits, _caps, _MULTIPART_TYPE, _part_headers, _read_body,
    _request_headers, parse_transcription_ingress,
)
from .audio_voice_api import _TOKEN, _require_voice_client


def _error(code: str, status: int = 400) -> AudioGatewayError:
    return AudioGatewayError(code, "request", status_code=status)


@dataclass(frozen=True, slots=True)
class AudioTranscriptionsLimits:
    text_bytes: int
    request_timeout_seconds: float
    max_pending_admissions: int
    poll_interval_seconds: float

    def __post_init__(self) -> None:
        if type(self.text_bytes) is not int or not 1 <= self.text_bytes <= 1024 * 1024:
            raise ValueError("invalid transcription text limit")
        if (type(self.max_pending_admissions) is not int
                or not 1 <= self.max_pending_admissions <= 1024):
            raise ValueError("invalid transcription concurrency limit")
        for value in (self.request_timeout_seconds, self.poll_interval_seconds):
            if (type(value) not in (int, float) or not 0 < value <= 300
                    or not math.isfinite(value)):
                raise ValueError("invalid transcription time limit")
        if self.poll_interval_seconds > self.request_timeout_seconds:
            raise ValueError("invalid transcription polling interval")


def _default_language(raw: bytes, boundary: bytes, limits: AudioIngressLimits) -> bytes:
    """Only identify strict part names; shared ingress revalidates the envelope.

    No generic MIME parser or filename/path interpretation. At most four bounded
    headers are visited. Synthesized bytes count against shared parser caps too.
    """
    marker = b"--" + boundary
    if not raw.startswith(marker + b"\r\n"):
        raise _error("invalid_multipart")
    position = len(marker) + 2
    names = set()
    for _ in range(4):
        end = raw.find(b"\r\n\r\n", position, position + limits.part_header_bytes + 4)
        if end < 0:
            raise _error("invalid_multipart")
        name, _ = _part_headers(raw[position:end])
        if name in names:
            raise _error("duplicate_fields")
        names.add(name)
        end = raw.find(b"\r\n" + marker, end + 4)
        if end < 0:
            raise _error("invalid_multipart")
        position = end + 2 + len(marker)
        if len(raw) == position + 4 and raw[position:position + 4] == b"--\r\n":
            if names == {"model", "response_format", "file"}:
                part = (marker + b'\r\nContent-Disposition: form-data; name="language"'
                        b"\r\n\r\nja\r\n")
                return part + raw
            return raw
        if raw[position:position + 2] != b"\r\n":
            raise _error("invalid_multipart")
        position += 2
    raise _error("invalid_multipart")


async def _parse_ingress(request, catalog, limits):
    # Bound the original upload BEFORE making a bounded replay with a default.
    headers = _request_headers(request, limits)
    match = _MULTIPART_TYPE.fullmatch(headers.get(b"content-type", b""))
    if match is None:
        raise AudioGatewayError("unsupported_media_type", "content_type", status_code=415)
    caps = _caps(catalog, TRANSCRIPTION_OPERATION_SCHEMA, limits)
    raw = await _read_body(request, headers, caps["request_bytes"], limits)
    raw = _default_language(raw, match[1] or match[2], limits)
    replay_headers = [(k, v) for k, v in headers.items()
                      if k not in (b"content-length", b"transfer-encoding")]
    replay_headers.append((b"content-length", str(len(raw)).encode("ascii")))

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    replay = Request({**request.scope, "headers": replay_headers}, receive)
    return await parse_transcription_ingress(replay, catalog, limits=limits)


class AudioTranscriptionsService:
    """A bounded slot outlives HTTP cancellation until late I/O is accounted for.

    Threads cannot be forcibly stopped. Cleanup has bounded call counts, not an
    unconditional wall-clock guarantee. Never cancel these internal operations
    during normal request teardown; they own their staged input and admission.
    """

    def __init__(self, repository, channel: AudioInputChannel, limits: AudioTranscriptionsLimits):
        self.repository = repository
        self.channel = channel
        self.limits = limits
        self.in_flight = 0
        self.unresolved_outcomes = 0
        self.cleanup_failures = 0
        self.operations: set[asyncio.Task] = set()

    @staticmethod
    def _matches(record, submission) -> bool:
        try:
            return (isinstance(record, JobRecord)
                    and record.idempotency_key == submission.idempotency_key
                    and _submission_matches(record, submission))
        except (AttributeError, TypeError, ValueError):
            return False

    async def _cancel(self, record, submission) -> None:
        for _ in range(3):
            try:
                cancelled = await asyncio.to_thread(
                    self.repository.cancel_job, record.job_id,
                    admission_deadline_at=utc_now() + timedelta(seconds=0.6),
                )
                if (cancelled.job_id == record.job_id and self._matches(cancelled, submission)
                        and cancelled.status in {
                            JobStatus.CANCELLED, JobStatus.FAILED, JobStatus.SUCCEEDED,
                        }):
                    return
            except Exception:
                continue
            break
        self.unresolved_outcomes += 1

    async def _reconcile(self, submission) -> None:
        for _ in range(3):
            try:
                record = await asyncio.to_thread(
                    self.repository.get_job_by_idempotency_key, submission.idempotency_key,
                    admission_deadline_at=utc_now() + timedelta(seconds=0.6),
                )
                if record is None:
                    continue
                if self._matches(record, submission):
                    await self._cancel(record, submission)
                    return
            except Exception:
                continue
            break
        # Neither absence nor readback failure proves rollback. No resubmission,
        # no discard, no private receipt rewrite, and no Worker impersonation.
        self.unresolved_outcomes += 1

    async def _discard(self, uri) -> None:
        try:
            await asyncio.to_thread(self.channel.discard, uri)
        except Exception:
            # Only called before submit was entered. Failed deletion remains
            # bounded by spool quota/TTL, not reported as successful cleanup.
            self.cleanup_failures += 1

    def _text(self, result) -> dict:
        # One unambiguous canonical JobResult.text, never arbitrary outputs,
        # artifacts, backend metadata, timestamps, or URLs in the HTTP envelope.
        if result is None or result.artifacts or result.outputs:
            raise _error("invalid_transcription_output", 502)
        text = result.text
        if type(text) is not str or len(text) > self.limits.text_bytes:
            raise _error("invalid_transcription_output", 502)
        try:
            size = len(text.encode("utf-8", errors="strict"))
        except UnicodeEncodeError:
            raise _error("invalid_transcription_output", 502) from None
        if size > self.limits.text_bytes:
            raise _error("invalid_transcription_output", 502)
        # Empty transcription is valid silence; preserve Unicode/whitespace.
        return {"text": text}

    async def _execute(self, validated, submission, abort, stop) -> dict:
        uri = None
        entered_submit = False
        record = None
        expected = None
        try:
            try:
                await asyncio.to_thread(
                    validate_pcm16_wav, validated.wav_bytes,
                    max_wire_bytes=self.channel.max_bytes,
                    max_decoded_bytes=self.channel.max_decoded_bytes,
                    max_duration_seconds=self.channel.max_duration_seconds,
                )
            except ValueError:
                raise AudioGatewayError("unsupported_audio", "file", status_code=415) from None
            if abort.is_set() or asyncio.get_running_loop().time() >= stop:
                raise _error("deadline_exceeded", 504)
            receipt = await asyncio.to_thread(self.channel.stage, validated.wav_bytes)
            uri = receipt["uri"]
            if abort.is_set() or asyncio.get_running_loop().time() >= stop:
                raise _error("deadline_exceeded", 504)
            expected = replace(submission, payload={**submission.payload, "audio_input": receipt})
            entered_submit = True
            try:
                admitted = await asyncio.to_thread(self.channel.submit, submission, artifact_uri=uri)
            except (DeadlineExceededError, OverloadedError, NoCompatibleDeployment) as exc:
                # Channel owns definite first-admission cleanup. Its deletion
                # can itself fail; do not reach into its private receipt table.
                code, status = ("deadline_exceeded", 408) if isinstance(exc, DeadlineExceededError) else (
                    ("overloaded", 429) if isinstance(exc, OverloadedError)
                    else ("no_compatible_deployment", 503)
                )
                raise _error(code, status) from None
            except Exception as exc:
                await self._reconcile(expected)
                raise _error("admission_conflict" if isinstance(exc, ConflictError)
                             else "admission_unavailable",
                             409 if isinstance(exc, ConflictError) else 503) from None
            if not self._matches(admitted, expected):
                await self._reconcile(expected)
                raise _error("admission_unavailable", 503)
            record = admitted
            while True:
                if abort.is_set() or asyncio.get_running_loop().time() >= stop:
                    raise _error("deadline_exceeded", 504)
                current = await asyncio.to_thread(self.repository.get_job, record.job_id)
                if abort.is_set() or asyncio.get_running_loop().time() >= stop:
                    raise _error("deadline_exceeded", 504)
                if current.job_id != record.job_id or not self._matches(current, expected):
                    raise _error("invalid_job_result", 502)
                if current.status is JobStatus.CANCELLED:
                    raise _error("request_cancelled", 409)
                if current.status is JobStatus.FAILED:
                    raise _error("backend_failure", 502)
                if current.status is JobStatus.SUCCEEDED:
                    return self._text(current.result)
                await asyncio.sleep(min(self.limits.poll_interval_seconds, max(
                    0, stop - asyncio.get_running_loop().time(),
                )))
        except BaseException:
            if record is not None:
                await self._cancel(record, expected)
            raise
        finally:
            if uri is not None and not entered_submit:
                await self._discard(uri)
            # Once submit is entered, discard is forbidden even after cancel or
            # success. #174 retains bound/pending inputs until verified TTL reap.

    def _finished(self, task) -> None:
        self.operations.discard(task)
        self.in_flight -= 1
        if not task.cancelled():
            task.exception()  # consume detached exceptions without logging inputs

    @staticmethod
    async def _disconnect(request):
        while True:
            message = await request.receive()
            if message.get("type") == "http.disconnect":
                return
            await asyncio.sleep(0.01)

    async def complete(self, request, catalog, ingress_limits) -> dict:
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
                validated = await _parse_ingress(request, catalog, ingress_limits)
                if loop.time() >= stop:
                    raise _error("deadline_exceeded", 408)
                submission = JobSubmission(
                    capability="audio.transcribe", serving=validated.metadata.profile.binding,
                    payload={"language": "ja", "response_format": "json"},
                    deadline_at=deadline, max_attempts=1,
                    idempotency_key="audio-transcriptions:" + secrets.token_hex(32),
                )
                watcher = asyncio.create_task(self._disconnect(request))
                operation = asyncio.create_task(self._execute(validated, submission, abort, stop))
                self.operations.add(operation)
                operation.add_done_callback(self._finished)
                transferred = True
                done, _ = await asyncio.wait((operation, watcher), return_when=asyncio.FIRST_COMPLETED)
                if watcher in done:
                    raise _error("client_disconnected", 499)
                result = operation.result()
                if loop.time() >= stop:
                    raise _error("deadline_exceeded", 504)
                return result
        except TimeoutError:
            raise _error("deadline_exceeded", 504) from None
        finally:
            abort.set()
            if watcher is not None:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
            if not transferred:
                self.in_flight -= 1


def create_audio_transcriptions_router(
    catalog: AudioProfileCatalog, repository, audio_input_channel: AudioInputChannel, *,
    client_auth: ClientAuthMode | str, client_token: str | None,
    ingress_limits: AudioIngressLimits, limits: AudioTranscriptionsLimits,
) -> APIRouter:
    """Explicit constructor only: no configuration discovery or daemon mount."""
    if (ClientAuthMode(client_auth) is not ClientAuthMode.BEARER
            or type(client_token) is not str or _TOKEN.fullmatch(client_token) is None):
        raise ValueError("audio transcriptions requires bearer authentication")
    if type(catalog) is not AudioProfileCatalog:
        raise ValueError("audio transcriptions requires reviewed catalog")
    reviewed = AudioProfileCatalog.from_dict(catalog.to_dict())
    if not any(p.resolved.profile.operation_schema == TRANSCRIPTION_OPERATION_SCHEMA
               for p in reviewed.profiles):
        raise ValueError("audio transcriptions requires a transcription profile")
    if type(ingress_limits) is not AudioIngressLimits or type(limits) is not AudioTranscriptionsLimits:
        raise ValueError("audio transcriptions requires explicit limits")
    if (not isinstance(audio_input_channel, AudioInputChannel)
            or audio_input_channel.repository is not repository
            or ingress_limits.file_bytes > audio_input_channel.max_bytes):
        raise ValueError("audio transcriptions requires same-repository bounded input authority")
    if not all(callable(getattr(repository, name, None)) for name in (
        "submit_job", "get_job", "get_job_by_idempotency_key", "cancel_job",
    )):
        raise ValueError("audio transcriptions requires durable repository operations")
    service = AudioTranscriptionsService(repository, audio_input_channel, limits)
    router = APIRouter()

    @router.post("/v1/audio/transcriptions")
    async def transcriptions(request: Request) -> JSONResponse:
        headers = {"Cache-Control": "no-store"}
        try:
            _require_voice_client(request, client_token)
            if request.scope.get("query_string", b""):
                raise _error("unsupported_query")
            result = await service.complete(request, reviewed, ingress_limits)
            return JSONResponse(result, headers=headers)
        except AudioGatewayError as exc:
            if exc.status_code == 401:
                headers["WWW-Authenticate"] = "Bearer"
            return JSONResponse(exc.to_dict(), status_code=exc.status_code, headers=headers)
        except Exception:
            exc = _error("audio_unavailable", 503)
            return JSONResponse(exc.to_dict(), status_code=503, headers=headers)

    return router


__all__ = ["AudioTranscriptionsLimits", "AudioTranscriptionsService",
           "create_audio_transcriptions_router"]
