"""FastAPI transport for the TSUMGI Control Plane."""

from __future__ import annotations

import asyncio
import json
import re
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from starlette.requests import ClientDisconnect

from ..audio import AudioOutputChannel
from ..audio.input_channel import AudioInputChannel

from ..control.auth import ClientAuthMode
from ..control.models import WorkerState
from ..control.worker_auth import WorkerCredentials
from ..control.audio_epoch import validate_fence_arguments, validate_fence_lookup
from ..control.audio_prepare import (
    validate_prepare_arguments,
    validate_prepare_lookup,
    validate_release_arguments,
)
from ..control.repository import (
    ConflictError,
    ControlRepository,
    DeadlineExceededError,
    EventBufferFull,
    NoCompatibleDeployment,
    NotFoundError,
    OverloadedError,
    RepositoryError,
    StorageUnavailable,
)
from ..control.serde import job_result_from_dict
from ..transport import (
    JOB_EVENTS_EXTENSION,
    PROTOCOL_VERSION,
    SERVING_EXTENSION,
    _extensions,
    _require_job_events_extension,
    job_event_record_to_dict,
    job_record_to_dict,
    job_submission_from_dict,
    worker_heartbeat_from_dict,
    worker_record_to_dict,
    worker_registration_from_dict,
)


def _bearer_token(request: Request) -> str | None:
    headers = request.headers.getlist("authorization")
    if len(headers) != 1:
        return None
    header = headers[0]
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token or not token.isascii():
        return None
    return token


def _require_token(request: Request, expected: str, authority: str) -> None:
    supplied = _bearer_token(request)
    if not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"{authority} authorization required",
        )


async def _json_v1(request: Request) -> dict[str, Any]:
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="JSON object is required")
    if body.get("protocol_version") != PROTOCOL_VERSION:
        raise HTTPException(
            status_code=409,
            detail=f"unsupported protocol version; expected {PROTOCOL_VERSION}",
        )
    return body


async def _optional_json_v1(request: Request) -> dict[str, Any]:
    if not await request.body():
        return {}
    return await _json_v1(request)


def _unique_fence_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    if len({name for name, _ in pairs}) != len(pairs):
        raise ValueError("duplicate fence field")
    return dict(pairs)


def _unique_release_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    if len({name for name, _ in pairs}) != len(pairs):
        raise ValueError("duplicate release field")
    return dict(pairs)


def _unique_prepare_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    if len({name for name, _ in pairs}) != len(pairs):
        raise ValueError("duplicate preparation field")
    return dict(pairs)


def _serving_epoch(body: dict[str, Any]) -> str | None:
    extensions = _extensions(body)
    raw = body.get("runtime_instance_epoch")
    if raw is None:
        return None
    if SERVING_EXTENSION not in extensions:
        raise ValueError("serving-bindings-v1 extension is required")
    epoch = str(raw).strip()
    if not epoch:
        raise ValueError("runtime_instance_epoch must not be blank")
    return epoch


def _audio_claim_headers(request: Request) -> tuple[int, str, str]:
    """Request metadata only; raw WAV bytes never enter a JSON envelope."""
    raw_attempt = request.headers.get("x-audio-attempt", "")
    lease = request.headers.get("x-audio-lease-token", "")
    epoch = request.headers.get("x-audio-runtime-epoch", "")
    if (not raw_attempt or len(raw_attempt) > 10
            or not raw_attempt.isascii() or not raw_attempt.isdecimal()
            or not lease.strip() or not epoch.strip()):
        raise HTTPException(status_code=422, detail="audio claim headers required")
    return int(raw_attempt), lease, epoch


async def _bounded_audio_body(request: Request, max_bytes: int) -> bytes:
    declared = request.headers.get("content-length")
    expected = None
    if declared is not None:
        if (len(declared) > 20
                or not declared.isascii() or not declared.isdecimal()):
            raise HTTPException(status_code=422, detail="invalid audio content length")
        expected = int(declared)
        if expected > max_bytes:
            raise HTTPException(status_code=413, detail="audio output too large")
    payload = bytearray()
    try:
        async for chunk in request.stream():
            if len(payload) + len(chunk) > max_bytes:
                raise HTTPException(status_code=413, detail="audio output too large")
            payload.extend(chunk)
    except ClientDisconnect as exc:
        raise HTTPException(status_code=400, detail="incomplete audio output") from exc
    if expected is not None and len(payload) != expected:
        raise HTTPException(status_code=400, detail="incomplete audio output")
    return bytes(payload)


def create_app(
    repository: ControlRepository,
    *,
    client_token: str | None,
    worker_token: str | None,
    client_auth: ClientAuthMode | str = ClientAuthMode.BEARER,
    maintenance_interval_seconds: float = 5.0,
    audio_output_channel: AudioOutputChannel | None = None,
    audio_input_channel: AudioInputChannel | None = None,
    audio_epoch_operator_token: str | None = None,
    worker_credentials: WorkerCredentials | None = None,
    audio_preparation_ttl_seconds: int | None = None,
) -> FastAPI:
    mode = ClientAuthMode(client_auth)
    if audio_output_channel is not None:
        if (not isinstance(audio_output_channel, AudioOutputChannel)
                or audio_output_channel.repository is not repository):
            raise ValueError("audio channel must belong to this Control repository")
        if mode is not ClientAuthMode.BEARER:
            raise ValueError("audio output requires client bearer authentication")
    if audio_input_channel is not None:
        if (not isinstance(audio_input_channel, AudioInputChannel)
                or audio_input_channel.repository is not repository
                or mode is not ClientAuthMode.BEARER):
            raise ValueError("input channel must use this repository and client bearer")
    if worker_credentials is not None:
        if not isinstance(worker_credentials, WorkerCredentials) or mode is not ClientAuthMode.BEARER:
            raise ValueError("per-worker authentication requires credentials and client bearer authentication")
        worker_credentials.reject_token_collisions(
            (worker_token, client_token, audio_epoch_operator_token)
        )
    elif not worker_token:
        raise ValueError("worker_token is required")
    if mode is ClientAuthMode.BEARER:
        if not client_token:
            raise ValueError(
                "client_token is required when client_auth=bearer"
            )
        if worker_token and secrets.compare_digest(client_token, worker_token):
            raise ValueError("client_token and worker_token must be distinct")
    if maintenance_interval_seconds <= 0:
        raise ValueError("maintenance_interval_seconds must be positive")
    if audio_epoch_operator_token is not None:
        if (mode is not ClientAuthMode.BEARER
                or not isinstance(audio_epoch_operator_token, str)
                or not 16 <= len(audio_epoch_operator_token) <= 512
                or re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", audio_epoch_operator_token) is None
                or audio_epoch_operator_token in (client_token, worker_token)):
            raise ValueError("audio epoch fencing requires a distinct operator bearer token")

    async def maintenance_loop() -> None:
        while True:
            await asyncio.sleep(maintenance_interval_seconds)
            # Local TTLs do not depend on a successful Job lookup. Reap each
            # spool independently before entering any DB-dependent recovery.
            for channel in (audio_output_channel, audio_input_channel):
                if channel is not None:
                    try:
                        await asyncio.to_thread(channel.reap_expired)
                    except Exception:
                        # Failed deletion stays charged; retry next interval.
                        continue
            if audio_output_channel is not None:
                try:
                    await asyncio.to_thread(audio_output_channel.reap_consumed)
                except Exception:
                    pass
            try:
                await asyncio.to_thread(repository.expire_stale_workers)
                await asyncio.to_thread(repository.expire_deadline_jobs)
                await asyncio.to_thread(repository.recover_expired_jobs)
                if audio_output_channel is not None:
                    await asyncio.to_thread(audio_output_channel.reap_abandoned)
            except Exception:
                # Repository/API calls still expose current storage failures.
                # Maintenance must not terminate the server process.
                pass
            if audio_input_channel is not None:
                try:
                    # Inputs of terminal Jobs are released here, not at the TTL.
                    await asyncio.to_thread(audio_input_channel.reap_terminal)
                except Exception:
                    continue

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(maintenance_loop())
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            # Release pooled database connections (PostgreSQL repository).
            close = getattr(repository, "close", None)
            if callable(close):
                await asyncio.to_thread(close)

    app = FastAPI(
        title="TSUMGI Control API",
        version=PROTOCOL_VERSION,
        lifespan=lifespan,
    )

    def require_client(request: Request) -> None:
        if mode is ClientAuthMode.NONE:
            return
        assert client_token is not None
        _require_token(request, client_token, "client")

    def require_worker(request: Request) -> str | None:
        if worker_credentials is None:
            assert worker_token is not None
            _require_token(request, worker_token, "worker")
            return None
        principal = worker_credentials.authenticate(_bearer_token(request))
        if principal is None:
            raise HTTPException(status_code=401, detail="worker authorization required")
        # Authentication establishes identity independently of any request ID.
        # Bind every path-based Worker route before parsing bodies or touching
        # repositories/spools. Registration binds its body identity below.
        target = request.path_params.get("worker_id")
        if target is not None and target != principal:
            raise HTTPException(status_code=403, detail="worker identity mismatch")
        return principal

    if audio_preparation_ttl_seconds is not None:
        # Preparation is a Worker-owned, non-admitting record. Without separate
        # per-Worker identities one Worker could prepare another's epoch.
        if (type(audio_preparation_ttl_seconds) is not int
                or not 1 <= audio_preparation_ttl_seconds <= 300
                or worker_credentials is None):
            raise ValueError("audio epoch preparation requires per-worker credentials and a 1-300 s ttl")

        @app.middleware("http")
        async def no_store_epoch_preparation(request: Request, call_next):
            response = await call_next(request)
            if "/audio-epochs/" in request.url.path and request.url.path.startswith("/v1/workers/"):
                response.headers["cache-control"] = "no-store"
            return response

        @app.post(
            "/v1/workers/{worker_id}/audio-epochs/{epoch}/prepare",
            dependencies=[Depends(require_worker)],
        )
        async def prepare_audio_epoch(worker_id: str, epoch: str, request: Request) -> JSONResponse:
            # Records an intent only: it admits no Worker, epoch, claim or activation.
            # Bearer-only (no ambient browser credentials), like the other Worker
            # routes, so the body is judged on its content, not its content type.
            try:
                async with asyncio.timeout(5):
                    raw = await _bounded_audio_body(request, 2048)
                body = json.loads(raw, object_pairs_hook=_unique_prepare_fields)
                if not isinstance(body, dict) or set(body) != {
                    "protocol_version", "qualification_revision",
                    "expected_intent_revision", "prepare_request_id",
                } or body["protocol_version"] != PROTOCOL_VERSION:
                    raise ValueError
                validate_prepare_arguments(
                    worker_id, epoch, body["qualification_revision"],
                    body["expected_intent_revision"], body["prepare_request_id"],
                    audio_preparation_ttl_seconds,
                )
            except TimeoutError:
                raise HTTPException(status_code=408, detail="audio epoch preparation timed out") from None
            except (ValueError, TypeError, RecursionError):
                raise HTTPException(status_code=422, detail="invalid audio epoch preparation request") from None
            record = await asyncio.to_thread(
                repository.prepare_audio_epoch, worker_id, epoch,
                qualification_revision=body["qualification_revision"],
                expected_intent_revision=body["expected_intent_revision"],
                prepare_request_id=body["prepare_request_id"],
                ttl_seconds=audio_preparation_ttl_seconds,
            )
            return JSONResponse(
                {"protocol_version": PROTOCOL_VERSION, **record.to_dict()},
                headers={"cache-control": "no-store"},
            )

        @app.get(
            "/v1/workers/{worker_id}/audio-epochs/{epoch}",
            dependencies=[Depends(require_worker)],
        )
        async def read_audio_epoch_preparation(worker_id: str, epoch: str) -> JSONResponse:
            try:
                validate_prepare_lookup(worker_id, epoch)
            except (ValueError, TypeError):
                raise HTTPException(status_code=422, detail="invalid audio epoch preparation request") from None
            record = await asyncio.to_thread(repository.get_audio_epoch_preparation, worker_id, epoch)
            return JSONResponse(
                {"protocol_version": PROTOCOL_VERSION, **record.to_dict()},
                headers={"cache-control": "no-store"},
            )

    if audio_epoch_operator_token is not None:
        @app.middleware("http")
        async def no_store_epoch_authority(request: Request, call_next):
            response = await call_next(request)
            if request.url.path.startswith("/v1/admin/audio-epochs/"):
                response.headers["cache-control"] = "no-store"
            return response

        def require_epoch_operator(request: Request) -> None:
            supplied = _bearer_token(request)
            if supplied is None or not supplied.isascii():
                raise HTTPException(status_code=401, detail="audio epoch operator authorization required")
            _require_token(request, audio_epoch_operator_token, "audio epoch operator")

        @app.post(
            "/v1/admin/audio-epochs/{worker_id}/{epoch}/fence",
            dependencies=[Depends(require_epoch_operator)],
        )
        async def fence_audio_epoch(worker_id: str, epoch: str, request: Request) -> JSONResponse:
            # Revocation-only: no qualification, activation or cleanup grant.
            if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
                raise HTTPException(status_code=415, detail="audio epoch fence requires JSON")
            try:
                async with asyncio.timeout(5):
                    raw = await _bounded_audio_body(request, 2048)
                body = json.loads(raw, object_pairs_hook=_unique_fence_fields)
                if not isinstance(body, dict) or set(body) != {
                    "protocol_version", "expected_deployment_revision",
                    "expected_serving_contract_revision", "fence_request_id",
                } or body["protocol_version"] != PROTOCOL_VERSION:
                    raise ValueError
                validate_fence_arguments(
                    worker_id, epoch, body["expected_deployment_revision"],
                    body["expected_serving_contract_revision"], body["fence_request_id"],
                )
            except TimeoutError:
                raise HTTPException(status_code=408, detail="audio epoch fence request timed out") from None
            except (ValueError, TypeError, RecursionError):
                raise HTTPException(status_code=422, detail="invalid audio epoch fence request") from None
            record = await asyncio.to_thread(
                repository.fence_audio_epoch, worker_id, epoch,
                expected_deployment_revision=body["expected_deployment_revision"],
                expected_serving_contract_revision=body["expected_serving_contract_revision"],
                fence_request_id=body["fence_request_id"],
            )
            return JSONResponse(
                {"protocol_version": PROTOCOL_VERSION, **record.to_dict()},
                headers={"cache-control": "no-store"},
            )

        @app.get(
            "/v1/admin/audio-epochs/{worker_id}/{epoch}",
            dependencies=[Depends(require_epoch_operator)],
        )
        async def read_audio_epoch_fence(worker_id: str, epoch: str) -> JSONResponse:
            try:
                validate_fence_lookup(worker_id, epoch)
            except (ValueError, TypeError):
                raise HTTPException(status_code=422, detail="invalid audio epoch fence request") from None
            record = await asyncio.to_thread(repository.get_audio_epoch_fence, worker_id, epoch)
            return JSONResponse(
                {"protocol_version": PROTOCOL_VERSION, **record.to_dict()},
                headers={"cache-control": "no-store"},
            )

        @app.post(
            "/v1/admin/audio-epochs/{worker_id}/{epoch}/release",
            dependencies=[Depends(require_epoch_operator)],
        )
        async def release_audio_epoch_preparation(
            worker_id: str, epoch: str, request: Request
        ) -> JSONResponse:
            # Ends a preparation's hold on the Worker; no activation or admission grant.
            if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
                raise HTTPException(status_code=415, detail="audio epoch release requires JSON")
            try:
                async with asyncio.timeout(5):
                    raw = await _bounded_audio_body(request, 2048)
                body = json.loads(raw, object_pairs_hook=_unique_release_fields)
                if not isinstance(body, dict) or set(body) != {
                    "protocol_version", "release_request_id",
                } or body["protocol_version"] != PROTOCOL_VERSION:
                    raise ValueError
                validate_release_arguments(worker_id, epoch, body["release_request_id"])
            except TimeoutError:
                raise HTTPException(status_code=408, detail="audio epoch release request timed out") from None
            except (ValueError, TypeError, RecursionError):
                raise HTTPException(status_code=422, detail="invalid audio epoch release request") from None
            record = await asyncio.to_thread(
                repository.release_audio_epoch_preparation, worker_id, epoch,
                release_request_id=body["release_request_id"],
            )
            return JSONResponse(
                {"protocol_version": PROTOCOL_VERSION, **record.to_dict()},
                headers={"cache-control": "no-store"},
            )

        @app.get(
            "/v1/admin/audio-epochs/{worker_id}/{epoch}/release",
            dependencies=[Depends(require_epoch_operator)],
        )
        async def read_audio_epoch_preparation_release(worker_id: str, epoch: str) -> JSONResponse:
            try:
                validate_prepare_lookup(worker_id, epoch)
            except (ValueError, TypeError):
                raise HTTPException(status_code=422, detail="invalid audio epoch release request") from None
            record = await asyncio.to_thread(
                repository.get_audio_epoch_preparation_release, worker_id, epoch
            )
            return JSONResponse(
                {"protocol_version": PROTOCOL_VERSION, **record.to_dict()},
                headers={"cache-control": "no-store"},
            )

    @app.exception_handler(NotFoundError)
    async def not_found_handler(_: Request, exc: NotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(DeadlineExceededError)
    async def deadline_handler(_: Request, exc: DeadlineExceededError) -> JSONResponse:
        return JSONResponse(status_code=408, content={"detail": str(exc)})

    @app.exception_handler(NoCompatibleDeployment)
    async def no_compatible_handler(
        _: Request, exc: NoCompatibleDeployment
    ) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.exception_handler(OverloadedError)
    async def overloaded_handler(_: Request, exc: OverloadedError) -> JSONResponse:
        return JSONResponse(status_code=429, content={"detail": str(exc)})

    @app.exception_handler(EventBufferFull)
    async def event_buffer_handler(_: Request, exc: EventBufferFull) -> JSONResponse:
        return JSONResponse(status_code=429, content={"detail": str(exc)})

    @app.exception_handler(ConflictError)
    async def conflict_handler(_: Request, exc: ConflictError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(StorageUnavailable)
    async def storage_handler(_: Request, __: StorageUnavailable) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": "storage unavailable"})

    @app.exception_handler(RepositoryError)
    async def repository_handler(_: Request, __: RepositoryError) -> JSONResponse:
        return JSONResponse(status_code=500, content={"detail": "repository error"})

    @app.exception_handler(Exception)
    async def unexpected_handler(_: Request, __: Exception) -> JSONResponse:
        return JSONResponse(status_code=500, content={"detail": "internal server error"})

    @app.get("/v1/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "protocol_version": PROTOCOL_VERSION,
            "extensions": [SERVING_EXTENSION, JOB_EVENTS_EXTENSION],
        }

    @app.get("/v1/ready")
    async def ready() -> dict[str, Any]:
        await asyncio.to_thread(repository.check_storage)
        return {
            "ready": True,
            "protocol_version": PROTOCOL_VERSION,
            "extensions": [SERVING_EXTENSION, JOB_EVENTS_EXTENSION],
        }

    if audio_input_channel is not None:
        @app.post(
            "/v1/audio/inputs", status_code=201,
            dependencies=[Depends(require_client)],
        )
        async def stage_audio_input(request: Request) -> dict[str, Any]:
            if request.headers.get("content-type", "").strip().lower() != "audio/wav":
                raise HTTPException(status_code=415, detail="audio/wav required")
            content = await _bounded_audio_body(request, audio_input_channel.max_bytes)
            try:
                return await asyncio.to_thread(audio_input_channel.stage, content)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

        @app.post(
            "/v1/audio/inputs/jobs", status_code=201,
            dependencies=[Depends(require_client)],
        )
        async def submit_audio_input_job(request: Request) -> dict[str, Any]:
            try:
                body = await _json_v1(request)
                input_uri = body.pop("audio_input_uri")
                submission = job_submission_from_dict(body)
            except (KeyError, TypeError, ValueError) as exc:
                raise HTTPException(status_code=422, detail="invalid audio input submission") from exc
            record = await asyncio.to_thread(
                audio_input_channel.submit, submission, artifact_uri=input_uri,
            )
            return job_record_to_dict(record)

        @app.get(
            "/v1/workers/{worker_id}/jobs/{job_id}/audio/input",
            dependencies=[Depends(require_worker)],
        )
        async def download_audio_input(
            worker_id: str, job_id: str, request: Request,
        ) -> Response:
            attempt, lease, epoch = _audio_claim_headers(request)
            content = await asyncio.to_thread(
                audio_input_channel.read_for_worker,
                job_id, worker_id=worker_id, attempt=attempt,
                lease_token=lease, runtime_instance_epoch=epoch,
            )
            return Response(
                content=content, media_type="audio/wav",
                headers={"cache-control": "no-store"},
            )

    @app.post("/v1/jobs", status_code=201, dependencies=[Depends(require_client)])
    async def submit_job(request: Request) -> dict[str, Any]:
        try:
            submission = job_submission_from_dict(await _json_v1(request))
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if audio_input_channel is not None and (
            submission.capability == "audio.transcribe"
            or "audio_input" in submission.payload
        ):
            # Opted-in audio jobs must pass trusted staged-input admission.
            # A client-supplied JSON reference cannot establish file ownership.
            raise HTTPException(
                status_code=422, detail="use staged audio input admission",
            )
        record = await asyncio.to_thread(repository.submit_job, submission)
        return job_record_to_dict(record)

    @app.get("/v1/jobs/{job_id}", dependencies=[Depends(require_client)])
    async def get_job(job_id: str) -> dict[str, Any]:
        record = await asyncio.to_thread(repository.get_job, job_id)
        return job_record_to_dict(record)

    @app.post("/v1/jobs/{job_id}/cancel", dependencies=[Depends(require_client)])
    async def cancel_job(job_id: str) -> dict[str, Any]:
        record = await asyncio.to_thread(repository.cancel_job, job_id)
        return job_record_to_dict(record)

    @app.post("/v1/workers/register", status_code=201, dependencies=[Depends(require_worker)])
    async def register_worker(
        request: Request, principal: str | None = Depends(require_worker),
    ) -> dict[str, Any]:
        try:
            payload = await _json_v1(request)
            if principal is not None:
                spec = payload.get("spec")
                if not isinstance(spec, dict) or spec.get("worker_id") != principal:
                    raise HTTPException(status_code=403, detail="worker identity mismatch")
            recovery = payload.get("recovery", False)
            if type(recovery) is not bool:
                raise TypeError("recovery must be boolean")
            registration = worker_registration_from_dict(payload)
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if principal is not None and registration.spec.worker_id != principal:
            raise HTTPException(status_code=403, detail="worker identity mismatch")
        record = await asyncio.to_thread(
            repository.register_worker, registration, recovery=recovery
        )
        return worker_record_to_dict(record)

    @app.post("/v1/workers/{worker_id}/heartbeat", dependencies=[Depends(require_worker)])
    async def heartbeat_worker(worker_id: str, request: Request) -> dict[str, Any]:
        try:
            heartbeat = worker_heartbeat_from_dict(await _json_v1(request))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        record = await asyncio.to_thread(
            repository.heartbeat_worker, worker_id, heartbeat
        )
        return worker_record_to_dict(record)

    @app.post("/v1/workers/{worker_id}/state", dependencies=[Depends(require_worker)])
    async def set_worker_state(worker_id: str, request: Request) -> dict[str, Any]:
        try:
            body = await _json_v1(request)
            state_value = body["state"]
            state = WorkerState(str(state_value))
            runtime_instance_epoch = _serving_epoch(body)
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        record = await asyncio.to_thread(
            repository.set_worker_state,
            worker_id,
            state,
            runtime_instance_epoch=runtime_instance_epoch,
        )
        return worker_record_to_dict(record)

    @app.post("/v1/workers/{worker_id}/jobs/claim", dependencies=[Depends(require_worker)])
    async def claim_job(worker_id: str, request: Request) -> Response:
        try:
            body = await _optional_json_v1(request)
            runtime_instance_epoch = _serving_epoch(body)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        record = await asyncio.to_thread(
            repository.claim_next_job,
            worker_id,
            runtime_instance_epoch=runtime_instance_epoch,
        )
        if record is None:
            return Response(status_code=204)
        return JSONResponse(status_code=200, content=job_record_to_dict(record))

    @app.post(
        "/v1/workers/{worker_id}/jobs/{job_id}/events",
        dependencies=[Depends(require_worker)],
    )
    async def append_job_event(
        worker_id: str,
        job_id: str,
        request: Request,
    ) -> dict[str, Any]:
        try:
            body = await _json_v1(request)
            _require_job_events_extension(body)
            lease_token = str(body["lease_token"])
            runtime_instance_epoch = _serving_epoch(body)
            kind = str(body["kind"]).strip()
            payload = body.get("payload")
            if not kind:
                raise ValueError("event kind must not be blank")
            if not isinstance(payload, dict):
                raise TypeError("event payload must be an object")
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        record = await asyncio.to_thread(
            repository.append_job_event,
            job_id,
            worker_id=worker_id,
            lease_token=lease_token,
            runtime_instance_epoch=runtime_instance_epoch,
            kind=kind,
            payload=payload,
        )
        return job_event_record_to_dict(record)

    @app.get(
        "/v1/workers/{worker_id}/jobs/{job_id}",
        dependencies=[Depends(require_worker)],
    )
    async def inspect_worker_job(worker_id: str, job_id: str) -> dict[str, Any]:
        record = await asyncio.to_thread(repository.get_job, job_id)
        visible = {
            "protocol_version": PROTOCOL_VERSION,
            "job_id": record.job_id,
            "status": record.status.value,
            "assigned_worker_id": record.assigned_worker_id,
            "lease_expires_at": (
                None if record.lease_expires_at is None else record.lease_expires_at.isoformat()
            ),
            "updated_at": record.updated_at.isoformat(),
        }
        if record.assigned_worker_id not in (None, worker_id):
            raise HTTPException(status_code=404, detail="job not visible to this worker")
        return visible

    @app.post(
        "/v1/workers/{worker_id}/jobs/{job_id}/complete",
        dependencies=[Depends(require_worker)],
    )
    async def complete_job(worker_id: str, job_id: str, request: Request) -> dict[str, Any]:
        try:
            body = await _json_v1(request)
            lease_token = str(body["lease_token"])
            runtime_instance_epoch = _serving_epoch(body)
            result = job_result_from_dict(body["result"])
            if result is None:
                raise ValueError("result is required")
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        # Non-audio Jobs must not depend on availability of an optional audio
        # spool. Capability is immutable; the selected repository completion
        # still checks Worker/lease/attempt authority atomically at effect time.
        complete = repository.complete_job
        if audio_output_channel is not None:
            existing = await asyncio.to_thread(repository.get_job, job_id)
            if existing.capability.startswith("audio."):
                complete = audio_output_channel.complete_job
        record = await asyncio.to_thread(
            complete,
            job_id,
            result,
            worker_id=worker_id,
            lease_token=lease_token,
            runtime_instance_epoch=runtime_instance_epoch,
        )
        return job_record_to_dict(record)

    @app.post(
        "/v1/workers/{worker_id}/jobs/{job_id}/fail",
        dependencies=[Depends(require_worker)],
    )
    async def fail_job(worker_id: str, job_id: str, request: Request) -> dict[str, Any]:
        try:
            body = await _json_v1(request)
            lease_token = str(body["lease_token"])
            runtime_instance_epoch = _serving_epoch(body)
            error = body.get("error", "executor failed")
            if not isinstance(error, (str, dict)):
                raise TypeError("error must be a string or object")
            retryable = bool(body.get("retryable", False))
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        record = await asyncio.to_thread(
            repository.fail_job,
            job_id,
            error,
            retryable=retryable,
            worker_id=worker_id,
            lease_token=lease_token,
            runtime_instance_epoch=runtime_instance_epoch,
        )
        return job_record_to_dict(record)

    if audio_output_channel is not None:
        @app.post(
            "/v1/workers/{worker_id}/jobs/{job_id}/audio/output",
            status_code=201,
            dependencies=[Depends(require_worker)],
        )
        async def stage_audio_output(
            worker_id: str, job_id: str, request: Request,
        ) -> dict[str, Any]:
            if request.headers.get("content-type", "").strip().lower() != "audio/wav":
                raise HTTPException(status_code=415, detail="audio/wav required")
            attempt, lease, epoch = _audio_claim_headers(request)
            # Reject invalid attempts before receiving large request bodies.
            await asyncio.to_thread(
                audio_output_channel.authorize, job_id, worker_id=worker_id,
                attempt=attempt, lease_token=lease,
                runtime_instance_epoch=epoch,
            )
            content = await _bounded_audio_body(request, audio_output_channel.max_bytes)
            try:
                ref = await asyncio.to_thread(
                    audio_output_channel.stage,
                    job_id, worker_id=worker_id, attempt=attempt,
                    lease_token=lease, runtime_instance_epoch=epoch,
                    data=content,
                )
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return {
                "uri": ref.uri, "media_type": ref.media_type,
                "digest": ref.digest, "size_bytes": ref.size_bytes,
                "name": ref.name, "metadata": dict(ref.metadata),
            }

        @app.get(
            "/v1/jobs/{job_id}/audio/output/{artifact_id}",
            dependencies=[Depends(require_client)],
        )
        async def read_audio_output(job_id: str, artifact_id: str) -> Response:
            content = await asyncio.to_thread(
                audio_output_channel.read_published, job_id, artifact_id,
            )
            return Response(
                content=content, media_type="audio/wav",
                headers={"cache-control": "no-store"},
            )

    return app


__all__ = ["create_app"]
