"""FastAPI edge router for the bounded Stage A chat gateway."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import secrets
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.requests import ClientDisconnect

from ..control.auth import ClientAuthMode
from ..control.models import JobStatus, JobSubmission, utc_now
from ..control.repository import (
    ConflictError,
    ControlRepository,
    DeadlineExceededError,
    NoCompatibleDeployment,
    OverloadedError,
    StorageUnavailable,
)
from .chat import (
    ChatGatewayError,
    ChatProfileCatalog,
    compile_chat_request,
    normalize_chat_completion,
    normalize_chat_stream_chunk,
)


_log = logging.getLogger(__name__)
# Post-admission cancellation gets a separate bounded resource budget.
_POSTADMIT_CANCEL_TIMEOUT_SECONDS = 5


def load_chat_catalog(path: str) -> ChatProfileCatalog:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("cannot load chat gateway profile catalog") from exc
    if not isinstance(value, dict):
        raise RuntimeError("chat gateway profile catalog must contain an object")
    try:
        return ChatProfileCatalog.from_dict(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("chat gateway profile catalog is invalid") from exc


def _error(
    status_code: int,
    *,
    code: str,
    message: str,
    error_type: str = "invalid_request_error",
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "message": message,
                "type": error_type,
                "param": None,
                "code": code,
            }
        },
    )


def _require_client(
    request: Request,
    *,
    mode: ClientAuthMode,
    token: str | None,
) -> None:
    if mode is ClientAuthMode.NONE:
        return
    assert token is not None
    header = request.headers.get("authorization", "")
    scheme, _, supplied = header.partition(" ")
    if (
        scheme.lower() != "bearer"
        or not supplied
        or not secrets.compare_digest(supplied, token)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="client authorization required",
        )


class _OwnedChatStream:
    """Durable Job owner that also works before the first SSE iteration."""

    def __init__(
        self,
        iterator: AsyncIterator[str],
        job_id: str,
        cancel,
    ) -> None:
        self._iterator = iterator
        self.job_id = job_id
        self._cancel = cancel

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        return await anext(self._iterator)

    async def aclose(self) -> None:
        try:
            await self._iterator.aclose()
        finally:
            # Async generators do not run finally when aclose() precedes
            # their first anext(). Response lifecycle owns this extra gate.
            await self._cancel(self.job_id)


class _OwnedChatStreamingResponse(StreamingResponse):
    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self.body_iterator.aclose()


class ChatGatewayService:
    def __init__(
        self,
        repository: ControlRepository,
        catalog: ChatProfileCatalog,
        *,
        poll_interval_seconds: float = 0.05,
        max_pending_admissions: int = 8,
        max_postadmit_cancellations: int = 8,
    ) -> None:
        if type(max_pending_admissions) is not int or max_pending_admissions < 1:
            raise ValueError("max_pending_admissions must be a positive integer")
        if type(max_postadmit_cancellations) is not int or max_postadmit_cancellations < 1:
            raise ValueError("max_postadmit_cancellations must be a positive integer")
        if (
            isinstance(poll_interval_seconds, bool)
            or not isinstance(poll_interval_seconds, (int, float))
            or not math.isfinite(float(poll_interval_seconds))
            or poll_interval_seconds <= 0
        ):
            raise ValueError("poll_interval_seconds must be finite and positive")
        self.repository = repository
        self.catalog = catalog
        self.poll_interval_seconds = float(poll_interval_seconds)
        # A nonblocking per-service gate: reserve before starting a thread.
        # Cancellation transfers its permit to late cleanup until completion.
        self.max_pending_admissions = max_pending_admissions
        self._admissions_in_flight = 0
        # Separate bounded pre-admission ingress (HTTP body/JSON parsing).
        # This count is per-Gateway, not a cluster-wide or auth boundary.
        self._ingress_in_flight = 0
        self._unexpected_cleanup_failures = 0
        # Exhausted readback/cancel acknowledgement: explicitly unresolved,
        # never counted as a confirmed cancellation.
        self._unresolved_admissions = 0
        # A cancelled request must not lose a Job created by a synchronous
        # submission thread that completes after the request task has exited.
        self._pending_admission_cleanup: set[asyncio.Task[None]] = set()
        self.max_postadmit_cancellations = max_postadmit_cancellations
        self._postadmit_cancellations: dict[str, asyncio.Task[Any]] = {}
        self._unresolved_cancellations = 0

    def _cleanup_finished(self, cleanup: asyncio.Task[None]) -> None:
        self._pending_admission_cleanup.discard(cleanup)
        self._admissions_in_flight -= 1
        try:
            cleanup.result()
        except (asyncio.CancelledError, Exception):
            # Task failure, not proof of durable cancellation outcome.
            self._unexpected_cleanup_failures += 1
            self._unresolved_admissions += 1
            _log.error("chat admission cleanup failed; durable outcome UNKNOWN")

    def _delegate_cleanup(self, cleanup: asyncio.Task[None]) -> None:
        self._pending_admission_cleanup.add(cleanup)
        cleanup.add_done_callback(self._cleanup_finished)

    @staticmethod
    async def _watch_admission_disconnect(request: Request) -> None:
        # Only start after request.body() has consumed the complete upload.
        # Do not use is_disconnected() here: its nonblocking receive probe can
        # miss a disconnect while a synchronous admission is in progress.
        while True:
            message = await request.receive()
            if message["type"] == "http.disconnect":
                return
            # Some ASGI adapters repeat an empty http.request after EOF.
            # Never busy-loop on one of those adapters.
            await asyncio.sleep(0.01)

    @staticmethod
    def _watch_finished(watcher: asyncio.Task[None]) -> None:
        if not watcher.cancelled() and watcher.exception() is not None:
            _log.warning("chat admission disconnect watcher failed")

    async def _cancel_after_admission(
        self, submit_task: asyncio.Task[Any], key: str
    ) -> None:
        try:
            submitted = await submit_task
        except asyncio.CancelledError:
            # Never misreport a cancelled cleanup task as a successful
            # durable cancellation during service shutdown.
            raise
        except Exception:
            # Even an exception at COMMIT can leave a durable Job.
            await self._reconcile_admission(key)
            return
        if submitted.idempotency_key != key:
            self._unresolved_admissions += 1
            _log.error("chat admission submission identity mismatch; outcome UNKNOWN")
            return
        await self._cancel_admission_job(submitted.job_id)

    async def _reconcile_admission(self, key: str) -> None:
        for _ in range(3):
            try:
                record = await asyncio.to_thread(
                    self.repository.get_job_by_idempotency_key,
                    key,
                    admission_deadline_at=utc_now() + timedelta(seconds=2),
                )
            except Exception:
                continue
            if record is None:
                # A lost COMMIT acknowledgement can precede the server's
                # actual COMMIT. Even a primary read after the client thread
                # finished proves only absence *at read time*, not that a
                # transaction cannot commit later. Fail closed as UNKNOWN.
                continue
            if record.idempotency_key != key:
                self._unresolved_admissions += 1
                _log.error("chat admission readback identity mismatch; outcome UNKNOWN")
                return
            await self._cancel_admission_job(record.job_id)
            return
        self._unresolved_admissions += 1
        _log.error("chat admission readback absent or exhausted; outcome UNKNOWN")

    async def _cancel_admission_job(self, job_id: str) -> None:
        for _ in range(3):
            try:
                result = await asyncio.to_thread(
                    self.repository.cancel_job,
                    job_id,
                    admission_deadline_at=utc_now() + timedelta(seconds=2),
                )
            except Exception:
                continue
            if result.job_id == job_id and result.status is JobStatus.CANCELLED:
                return
            # Other terminal statuses cannot become CANCELLED by retrying.
            break
        self._unresolved_admissions += 1
        _log.error("chat admission cancel acknowledgement unconfirmed; outcome UNKNOWN")

    async def _cancel(self, job_id: str) -> None:
        # Reserve before spawning a DB thread. Duplicate calls share one task.
        operation = self._postadmit_cancellations.get(job_id)
        if operation is None:
            if len(self._postadmit_cancellations) >= self.max_postadmit_cancellations:
                self._unresolved_cancellations += 1
                _log.warning("chat cancellation budget exhausted; durable outcome unresolved")
                return
            operation = asyncio.create_task(
                asyncio.to_thread(
                    self.repository.cancel_job,
                    job_id,
                    admission_deadline_at=utc_now() + timedelta(seconds=_POSTADMIT_CANCEL_TIMEOUT_SECONDS),
                )
            )
            self._postadmit_cancellations[job_id] = operation

            def finished(task: asyncio.Task[Any]) -> None:
                self._postadmit_cancellations.pop(job_id, None)
                try:
                    observed = task.result()
                    if observed.job_id != job_id or observed.status is not JobStatus.CANCELLED:
                        # A terminal success/failure is not cancellation proof.
                        self._unresolved_cancellations += 1
                        _log.warning("chat cancellation not acknowledged as CANCELLED")
                except (Exception, asyncio.CancelledError):
                    # Do not expose private DB exception details or claim success.
                    self._unresolved_cancellations += 1
                    _log.warning("chat cancellation outcome unresolved")

            operation.add_done_callback(finished)
        try:
            # Bound the HTTP wait without cancelling the synchronous DB thread.
            # Its completion callback still owns the permit and disposition.
            await asyncio.wait_for(
                asyncio.shield(operation), timeout=_POSTADMIT_CANCEL_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            _log.warning("chat cancellation waiter timed out; DB outcome pending")
            return
        except Exception:
            # Callback is responsible for final disposition accounting.
            return

    @staticmethod
    def _terminal(record, *, profile_id: str) -> dict[str, object]:
        if record.status is JobStatus.SUCCEEDED:
            if record.result is None:
                raise ChatGatewayError(
                    "invalid_provider_response",
                    "chat backend succeeded without a result",
                    status_code=502,
                )
            return normalize_chat_completion(
                record.result.outputs,
                profile_id=profile_id,
                job_id=record.job_id,
                created=int(record.created_at.timestamp()),
            )

        if record.status is JobStatus.CANCELLED:
            raise ChatGatewayError(
                "request_cancelled",
                "chat request was cancelled",
                status_code=409,
            )

        if record.status is not JobStatus.FAILED:
            raise RuntimeError("terminal conversion called for a non-terminal job")

        error = dict(record.error or {})
        code = error.get("code")
        error_type = error.get("type")
        if code in {
            "context_length_exceeded",
            "invalid_chat_job",
            "output_limit_exceeded",
            "streaming_not_supported",
            "unsupported_chat_adapter",
            "unsupported_feature",
        }:
            message = error.get("message")
            raise ChatGatewayError(
                str(code),
                str(message) if isinstance(message, str) else "chat request rejected",
                status_code=400,
            )
        if error_type == "deadline_expired":
            raise ChatGatewayError(
                "deadline_exceeded",
                "chat request deadline expired",
                status_code=408,
            )
        raise ChatGatewayError(
            "backend_failure",
            "chat backend failed before producing a response",
            status_code=502,
        )

    async def _submit(
        self,
        request: Request,
        body: dict[str, object],
        *,
        request_size_bytes: int,
        max_attempts: int | None = None,
    ):
        model = body.get("model")
        if not isinstance(model, str):
            raise ChatGatewayError(
                "invalid_request",
                "model must be a configured profile ID",
            )
        profile = self.catalog.get(model)
        compiled = compile_chat_request(
            body,
            profile,
            request_size_bytes=request_size_bytes,
        )
        now = utc_now()
        deadline_at = now + timedelta(seconds=profile.request_timeout_seconds)
        submission = JobSubmission(
            capability="llm.chat",
            payload=compiled.payload,
            max_attempts=profile.max_attempts if max_attempts is None else max_attempts,
            deadline_at=deadline_at,
            serving=profile.binding,
            idempotency_key="chat-gateway:" + secrets.token_hex(32),
        )
        # Check and reserve synchronously before spawning asyncio.to_thread.
        # An exhausted service must not queue unlimited waiting Tasks.
        if self._admissions_in_flight >= self.max_pending_admissions:
            raise ChatGatewayError(
                "admission_overloaded",
                "chat admission concurrency limit reached",
                status_code=429,
            )
        self._admissions_in_flight += 1
        # Shield the synchronous admission task from HTTP task cancellation:
        # asyncio.to_thread cannot abort an already-running DB operation.
        operation = asyncio.to_thread(
            self.repository.submit_job,
            submission,
            now=now,
            admission_deadline_at=deadline_at,
        )
        try:
            submit_task = asyncio.create_task(operation)
        except BaseException:
            operation.close()
            self._admissions_in_flight -= 1
            raise
        delegated_slot = False
        # Unit-level service callers may use lightweight request stubs. Real
        # HTTP callers arrive here only after request.body() is fully parsed.
        disconnect: asyncio.Task[None] | None = None
        try:
            if isinstance(request, Request) and hasattr(request, "_body"):
                watch_operation = self._watch_admission_disconnect(request)
                try:
                    disconnect = asyncio.create_task(watch_operation)
                except BaseException:
                    watch_operation.close()
                    # The submit task already exists: retain its permit and
                    # reconcile it even if watcher setup fails unexpectedly.
                    cleanup = asyncio.create_task(
                        self._cancel_after_admission(submit_task, submission.idempotency_key)
                    )
                    self._delegate_cleanup(cleanup)
                    delegated_slot = True
                    raise
                disconnect.add_done_callback(self._watch_finished)
            try:
                if disconnect is not None:
                    done, _ = await asyncio.wait(
                        (submit_task, disconnect), return_when=asyncio.FIRST_COMPLETED
                    )
                    if disconnect in done and not disconnect.cancelled() and disconnect.exception() is None:
                        # The disconnect wins even when a COMMIT raced it. The
                        # cleanup owns the thread and permit from this point.
                        disconnect.result()
                        cleanup = asyncio.create_task(
                            self._cancel_after_admission(submit_task, submission.idempotency_key)
                        )
                        self._delegate_cleanup(cleanup)
                        delegated_slot = True
                        raise ChatGatewayError(
                            "client_disconnected", "chat client disconnected", status_code=499
                        )
                record = await asyncio.shield(submit_task)
            except asyncio.CancelledError:
                # Cleanup takes over the permit; client propagation stays prompt.
                if not delegated_slot:
                    cleanup = asyncio.create_task(
                        self._cancel_after_admission(submit_task, submission.idempotency_key)
                    )
                    self._delegate_cleanup(cleanup)
                    delegated_slot = True
                raise
            except DeadlineExceededError as exc:
                raise ChatGatewayError(
                    "deadline_exceeded",
                    "chat request deadline expired before admission",
                    status_code=408,
                ) from exc
            except OverloadedError as exc:
                raise ChatGatewayError(
                    "overloaded",
                    "all compatible chat replicas are busy",
                    status_code=429,
                ) from exc
            except NoCompatibleDeployment as exc:
                raise ChatGatewayError(
                    "no_compatible_deployment",
                    "no compatible chat deployment is available",
                    status_code=503,
                ) from exc
            except ConflictError as exc:
                raise ChatGatewayError(
                    "admission_conflict",
                    "chat request conflicts with durable admission state",
                    status_code=409,
                ) from exc
            except ChatGatewayError:
                raise
            except Exception as exc:
                # Any exception from submit_job can be a lost COMMIT ack;
                # never treat it as proof that no durable Job was inserted.
                reconciliation = asyncio.create_task(
                    self._reconcile_admission(submission.idempotency_key)
                )
                try:
                    await asyncio.shield(reconciliation)
                except asyncio.CancelledError:
                    self._delegate_cleanup(reconciliation)
                    delegated_slot = True
                    raise
                raise ChatGatewayError(
                    "admission_unavailable",
                    "durable chat admission unavailable or unconfirmed",
                    status_code=503,
                ) from exc
            return profile, record
        finally:
            if disconnect is not None:
                disconnect.cancel()
                try:
                    # Do not hand receive() to StreamingResponse until the
                    # admission watcher has actually stopped reading it.
                    await asyncio.gather(disconnect, return_exceptions=True)
                except asyncio.CancelledError:
                    if not delegated_slot:
                        cleanup = asyncio.create_task(
                            self._cancel_after_admission(submit_task, submission.idempotency_key)
                        )
                        self._delegate_cleanup(cleanup)
                        delegated_slot = True
                    raise
            if not delegated_slot:
                self._admissions_in_flight -= 1

    async def complete(
        self,
        request: Request,
        body: dict[str, object],
        *,
        request_size_bytes: int,
    ) -> dict[str, object]:
        profile, record = await self._submit(
            request,
            body,
            request_size_bytes=request_size_bytes,
        )
        loop = asyncio.get_running_loop()
        stop_at = loop.time() + profile.request_timeout_seconds
        job_id = record.job_id
        try:
            while True:
                current = await asyncio.to_thread(self.repository.get_job, job_id)
                if current.status in {
                    JobStatus.SUCCEEDED,
                    JobStatus.FAILED,
                    JobStatus.CANCELLED,
                }:
                    return self._terminal(current, profile_id=profile.profile_id)

                if await request.is_disconnected():
                    await self._cancel(job_id)
                    raise ChatGatewayError(
                        "client_disconnected",
                        "chat client disconnected",
                        status_code=499,
                    )

                remaining = stop_at - loop.time()
                if remaining <= 0:
                    await self._cancel(job_id)
                    raise ChatGatewayError(
                        "gateway_timeout",
                        "chat request exceeded the bounded gateway deadline",
                        status_code=504,
                    )
                await asyncio.sleep(min(self.poll_interval_seconds, remaining))
        except asyncio.CancelledError:
            # An already-started cancellation is owned by the service, not
            # by the just-cancelled HTTP caller.
            if job_id not in self._postadmit_cancellations:
                await self._cancel(job_id)
            raise


    async def stream(
        self,
        request: Request,
        body: dict[str, object],
        *,
        request_size_bytes: int,
    ) -> AsyncIterator[str]:
        profile, record = await self._submit(
            request,
            body,
            request_size_bytes=request_size_bytes,
            max_attempts=1,
        )
        loop = asyncio.get_running_loop()
        stop_at = loop.time() + profile.request_timeout_seconds
        job_id = record.job_id
        created = int(record.created_at.timestamp())

        async def generate() -> AsyncIterator[str]:
            sequence = 0
            terminal = False
            try:
                while True:
                    if await request.is_disconnected():
                        await self._cancel(job_id)
                        return

                    events = await asyncio.to_thread(
                        self.repository.list_job_events,
                        job_id,
                        after_sequence=sequence,
                        limit=128,
                    )
                    for event in events:
                        if event.kind != "chat.completion.chunk":
                            await self._cancel(job_id)
                            yield _stream_error(
                                "invalid_stream_event",
                                "chat backend emitted an unsupported event",
                            )
                            return
                        try:
                            chunk = normalize_chat_stream_chunk(
                                event.payload,
                                profile_id=profile.profile_id,
                                job_id=job_id,
                                created=created,
                            )
                        except ChatGatewayError:
                            await self._cancel(job_id)
                            yield _stream_error(
                                "invalid_provider_response",
                                "chat backend emitted an invalid stream chunk",
                            )
                            return
                        sequence = event.sequence
                        yield _stream_data(chunk)

                    current = await asyncio.to_thread(
                        self.repository.get_job,
                        job_id,
                    )
                    if current.status in {
                        JobStatus.SUCCEEDED,
                        JobStatus.FAILED,
                        JobStatus.CANCELLED,
                    }:
                        tail = await asyncio.to_thread(
                            self.repository.list_job_events,
                            job_id,
                            after_sequence=sequence,
                            limit=128,
                        )
                        if tail:
                            continue
                        if current.status is JobStatus.SUCCEEDED:
                            terminal = True
                            yield "data: [DONE]\n\n"
                            return
                        if current.status is JobStatus.CANCELLED:
                            terminal = True
                            yield _stream_error(
                                "request_cancelled",
                                "chat request was cancelled",
                            )
                            return
                        error = dict(current.error or {})
                        code = error.get("code")
                        error_type = error.get("type")
                        public_code = (
                            str(code)
                            if code in {
                                "context_length_exceeded",
                                "event_buffer_full",
                                "invalid_chat_job",
                                "output_limit_exceeded",
                                "unsupported_chat_adapter",
                                "unsupported_feature",
                            }
                            else (
                                "deadline_exceeded"
                                if error_type == "deadline_expired"
                                else "backend_failure"
                            )
                        )
                        terminal = True
                        yield _stream_error(
                            public_code,
                            "chat stream terminated before durable success",
                        )
                        return

                    remaining = stop_at - loop.time()
                    if remaining <= 0:
                        await self._cancel(job_id)
                        yield _stream_error(
                            "gateway_timeout",
                            "chat stream exceeded the bounded gateway deadline",
                        )
                        return
                    await asyncio.sleep(
                        min(self.poll_interval_seconds, remaining)
                    )
            except asyncio.CancelledError:
                raise
            finally:
                # ASGI may close the body iterator after a client disconnect
                # instead of cancelling a task blocked inside anext(). An
                # abandoned response must never leave its durable Job RUNNING.
                # Preserve a naturally terminal Job; cancellation remains
                # best-effort and the Worker lease/deadline is the backstop.
                if not terminal:
                    await self._cancel(job_id)

        return _OwnedChatStream(generate(), job_id, self._cancel)


def _stream_data(value: dict[str, object]) -> str:
    return "data: " + json.dumps(
        value,
        separators=(",", ":"),
        ensure_ascii=False,
    ) + "\n\n"


def _stream_error(code: str, message: str) -> str:
    return _stream_data(
        {
            "error": {
                "message": message,
                "type": "server_error",
                "code": code,
            }
        }
    )


def create_chat_router(
    repository: ControlRepository,
    catalog: ChatProfileCatalog,
    *,
    client_auth: ClientAuthMode | str,
    client_token: str | None,
    poll_interval_seconds: float = 0.05,
    max_pending_admissions: int = 8,
    max_postadmit_cancellations: int = 8,
) -> APIRouter:
    mode = ClientAuthMode(client_auth)
    if mode is ClientAuthMode.BEARER and not client_token:
        raise ValueError("client_token is required when chat gateway uses bearer auth")

    service = ChatGatewayService(
        repository,
        catalog,
        poll_interval_seconds=poll_interval_seconds,
        max_pending_admissions=max_pending_admissions,
        max_postadmit_cancellations=max_postadmit_cancellations,
    )
    router = APIRouter()

    def require_client(request: Request) -> None:
        _require_client(request, mode=mode, token=client_token)

    @router.get("/v1/models", dependencies=[Depends(require_client)])
    async def models() -> dict[str, object]:
        return catalog.models_response()

    @router.post(
        "/v1/chat/completions",
        dependencies=[Depends(require_client)],
    )
    async def chat_completions(request: Request):
        # Bound concurrent body readers/parsers BEFORE consuming or buffering
        # input. The pending-admission budget protects the later DB threads.
        if service._ingress_in_flight >= service.max_pending_admissions:
            return _error(
                429,
                code="admission_overloaded",
                message="chat ingress concurrency limit reached",
            )
        service._ingress_in_flight += 1
        try:
            maximum = max(
                int(profile.resolved.effective_limits["request_bytes"])
                for profile in catalog.profiles
            )
            ingress_timeout = max(
                float(profile.request_timeout_seconds) for profile in catalog.profiles
            )
            parts: list[bytes] = []
            received = 0

            async def read_limited_body() -> None:
                nonlocal received
                async for chunk in request.stream():
                    received += len(chunk)
                    if received > maximum:
                        raise OverflowError("chat request body over configured limit")
                    if chunk:
                        parts.append(chunk)

            try:
                # request.body() would buffer the entire upload before its
                # size is checked, allowing an unbounded memory allocation.
                await asyncio.wait_for(read_limited_body(), timeout=ingress_timeout)
            except OverflowError:
                return _error(
                    413,
                    code="request_too_large",
                    message="chat request exceeds every configured profile byte limit",
                )
            except asyncio.TimeoutError:
                return _error(
                    408,
                    code="request_timeout",
                    message="chat request upload timed out",
                )
            except ClientDisconnect:
                return _error(
                    499,
                    code="client_disconnected",
                    message="chat client disconnected during upload",
                )

            raw = b"".join(parts)
            # The already-accepted Stage3 admission disconnect watcher may
            # safely own receive() after upload. Match Starlette body()'s
            # cached-body invariant so subsequent Request access works.
            request._body = raw
            try:
                value = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                return _error(
                    400,
                    code="invalid_json",
                    message="chat request body must be valid JSON",
                )
            if not isinstance(value, dict):
                return _error(
                    400,
                    code="invalid_request",
                    message="chat request body must be a JSON object",
                )
        finally:
            service._ingress_in_flight -= 1
        try:
            if value.get("stream") is True:
                iterator = await service.stream(
                    request,
                    value,
                    request_size_bytes=len(raw),
                )
                return _OwnedChatStreamingResponse(
                    iterator,
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "X-Accel-Buffering": "no",
                    },
                )
            result = await service.complete(
                request,
                value,
                request_size_bytes=len(raw),
            )
        except ChatGatewayError as exc:
            return _error(
                exc.status_code,
                code=exc.code,
                message=str(exc),
                error_type=(
                    "server_error"
                    if exc.status_code >= 500
                    else "invalid_request_error"
                ),
            )
        return JSONResponse(status_code=200, content=result)

    return router


__all__ = [
    "ChatGatewayService",
    "create_chat_router",
    "load_chat_catalog",
]
