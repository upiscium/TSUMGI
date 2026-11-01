"""Async client for the AstrumWeaver v1 Control protocol."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re

from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import quote

import httpx

from ..control.models import WorkerState
from ..control.serde import job_result_to_dict, worker_spec_to_dict
from ..execution import ArtifactRef, JobRequest, JobResult
from ..serving import WorkerServingAdvertisement
from ..transport import JOB_EVENTS_EXTENSION, PROTOCOL_VERSION, SERVING_EXTENSION


_AUDIO_ERROR = "audio media transfer failed"
_AUDIO_RECEIPT_MAX_BYTES = 64 * 1024


def _audio_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(_AUDIO_ERROR)
        result[key] = value
    return result


def _audio_json_constant(_: str) -> Any:
    raise ValueError(_AUDIO_ERROR)


class ControlTransportError(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"control transport error {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    request: JobRequest
    lease_token: str
    lease_expires_at: str | None
    attempts: int


class ControlClient:
    def __init__(
        self,
        base_url: str,
        worker_token: str,
        *,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not base_url:
            raise ValueError("base_url is required")
        if not worker_token:
            raise ValueError("worker_token is required")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("Control request timeout must be finite and positive")
        self.timeout_seconds = timeout_seconds
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"authorization": f"Bearer {worker_token}"},
            timeout=timeout_seconds,
            transport=transport,
        )

    async def __aenter__(self) -> "ControlClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            async with asyncio.timeout(self.timeout_seconds):
                response = await self._client.request(method, path, **kwargs)
        except (httpx.HTTPError, TimeoutError) as exc:
            raise ControlTransportError(0, "control unavailable") from exc
        if response.status_code >= 400:
            try:
                detail = str(response.json().get("detail", "request failed"))
            except Exception:
                detail = "request failed"
            raise ControlTransportError(response.status_code, detail)
        return response

    @staticmethod
    def _object(response: httpx.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError as exc:
            raise ControlTransportError(502, "Control returned invalid JSON") from exc
        if not isinstance(body, dict):
            raise ControlTransportError(502, "Control response must be an object")
        raw_extensions = body.get("extensions") or ()
        if (
            isinstance(raw_extensions, str)
            or not isinstance(raw_extensions, (list, tuple, set, frozenset))
        ):
            raise ControlTransportError(502, "Control response extensions are invalid")
        extensions = frozenset(str(item) for item in raw_extensions)
        if extensions - {SERVING_EXTENSION, JOB_EVENTS_EXTENSION}:
            raise ControlTransportError(
                502, "Control response uses an unsupported extension"
            )
        serving_fields = (
            "serving",
            "claimed_deployment_revision",
            "claimed_serving_contract_revision",
            "claimed_runtime_instance_epoch",
            "runtime_instance_epoch",
        )
        if (
            any(body.get(field) is not None for field in serving_fields)
            and SERVING_EXTENSION not in extensions
        ):
            raise ControlTransportError(
                502, "Control response omitted the serving extension"
            )
        return body

    async def require_extension(self, extension: str) -> None:
        response = await self._request("GET", "/v1/ready")
        body = self._object(response)
        if body.get("protocol_version") != PROTOCOL_VERSION:
            raise ControlTransportError(502, "Control protocol version is invalid")
        extensions = frozenset(str(item) for item in body.get("extensions") or ())
        if extension not in extensions:
            raise ControlTransportError(
                409, f"Control does not support required extension: {extension}"
            )

    async def register(
        self,
        *,
        spec,
        max_concurrency: int,
        metadata: Mapping[str, Any] | None = None,
        serving: WorkerServingAdvertisement | None = None,
        recovery: bool = False,
    ) -> dict[str, Any]:
        if serving is not None:
            await self.require_extension(SERVING_EXTENSION)
        response = await self._request(
            "POST",
            "/v1/workers/register",
            json={
                "protocol_version": PROTOCOL_VERSION,
                "spec": worker_spec_to_dict(spec),
                "max_concurrency": max_concurrency,
                **({"recovery": True} if recovery else {}),
                "metadata": dict(metadata or {}),
                "serving": None if serving is None else serving.to_dict(),
                "extensions": [] if serving is None else [SERVING_EXTENSION],
            },
        )
        return self._object(response)

    async def heartbeat(
        self,
        worker_id: str,
        *,
        active_job_id: str | None = None,
        lease_token: str | None = None,
        state: WorkerState | None = None,
        runtime_instance_epoch: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = await self._request(
            "POST",
            f"/v1/workers/{worker_id}/heartbeat",
            json={
                "protocol_version": PROTOCOL_VERSION,
                "active_job_id": active_job_id,
                "lease_token": lease_token,
                "state": None if state is None else state.value,
                "runtime_instance_epoch": runtime_instance_epoch,
                "extensions": (
                    [] if runtime_instance_epoch is None else [SERVING_EXTENSION]
                ),
                "metadata": dict(metadata or {}),
            },
        )
        return self._object(response)

    async def set_state(
        self,
        worker_id: str,
        state: WorkerState,
        *,
        runtime_instance_epoch: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "protocol_version": PROTOCOL_VERSION,
            "state": state.value,
        }
        if runtime_instance_epoch is not None:
            body["runtime_instance_epoch"] = runtime_instance_epoch
            body["extensions"] = [SERVING_EXTENSION]
        response = await self._request(
            "POST",
            f"/v1/workers/{worker_id}/state",
            json=body,
        )
        return self._object(response)

    async def claim(
        self,
        worker_id: str,
        *,
        runtime_instance_epoch: str | None = None,
    ) -> ClaimedJob | None:
        kwargs: dict[str, Any] = {}
        if runtime_instance_epoch is not None:
            kwargs["json"] = {
                "protocol_version": PROTOCOL_VERSION,
                "runtime_instance_epoch": runtime_instance_epoch,
                "extensions": [SERVING_EXTENSION],
            }
        response = await self._request(
            "POST",
            f"/v1/workers/{worker_id}/jobs/claim",
            **kwargs,
        )
        if response.status_code == 204:
            return None
        body = self._object(response)
        lease_token = body.get("lease_token")
        if not isinstance(lease_token, str) or not lease_token:
            raise ControlTransportError(502, "claim response lacks lease token")
        metadata: dict[str, Any] = {
            "attempt": int(body.get("attempts", 0)),
            "lease_expires_at": body.get("lease_expires_at"),
        }
        if body.get("serving") is not None:
            if SERVING_EXTENSION not in set(body.get("extensions") or ()):
                raise ControlTransportError(
                    502, "claim response lacks serving extension marker"
                )
            metadata.update(
                {
                    "deadline_at": body.get("deadline_at"),
                    "serving": body.get("serving"),
                    "claimed_deployment_revision": body.get("claimed_deployment_revision"),
                    "claimed_serving_contract_revision": body.get("claimed_serving_contract_revision"),
                    "claimed_runtime_instance_epoch": body.get("claimed_runtime_instance_epoch"),
                }
            )
        return ClaimedJob(
            request=JobRequest(
                job_id=str(body["job_id"]),
                capability=str(body["capability"]),
                payload=body.get("payload") or {},
                metadata=metadata,
            ),
            lease_token=lease_token,
            lease_expires_at=body.get("lease_expires_at"),
            attempts=int(body.get("attempts", 0)),
        )

    @staticmethod
    def _audio_claim(
        worker_id: str, claimed: ClaimedJob, capability: str, max_bytes: int,
    ) -> tuple[str, dict[str, str]]:
        """Validate locally without inventing or normalizing claim authority."""
        if (type(max_bytes) is not int or not 1 <= max_bytes <= 16 * 1024 * 1024
                or not isinstance(claimed, ClaimedJob)
                or claimed.request.capability != capability
                or type(claimed.attempts) is not int or claimed.attempts <= 0
                or len(str(claimed.attempts)) > 10):
            raise ControlTransportError(422, _AUDIO_ERROR)
        epoch = claimed.request.metadata.get("claimed_runtime_instance_epoch")
        serving = claimed.request.metadata.get("serving")
        if not isinstance(serving, Mapping) or not serving:
            raise ControlTransportError(422, _AUDIO_ERROR)
        for value in (claimed.lease_token, epoch):
            if (type(value) is not str or not value.strip()
                    or not value.isascii()
                    or any(ord(char) < 32 or ord(char) == 127 for char in value)):
                raise ControlTransportError(422, _AUDIO_ERROR)
        for value in (worker_id, claimed.request.job_id):
            if (type(value) is not str or not value.strip()
                    or value in {".", ".."} or "/" in value or "\\" in value):
                raise ControlTransportError(422, _AUDIO_ERROR)
        path = (
            f"/v1/workers/{quote(worker_id, safe='')}/jobs/"
            f"{quote(claimed.request.job_id, safe='')}/audio"
        )
        return path, {
            "x-audio-attempt": str(claimed.attempts),
            "x-audio-lease-token": claimed.lease_token,
            "x-audio-runtime-epoch": epoch,
            "accept-encoding": "identity",
        }

    @staticmethod
    def _audio_reference(
        body: Any, prefix: str, max_bytes: int,
    ) -> ArtifactRef:
        fields = {"uri", "media_type", "digest", "size_bytes", "name", "metadata"}
        if not isinstance(body, Mapping) or set(body) - fields:
            raise ControlTransportError(502, _AUDIO_ERROR)
        uri, digest, size = body.get("uri"), body.get("digest"), body.get("size_bytes")
        name, metadata = body.get("name"), body.get("metadata", {})
        if (type(uri) is not str or re.fullmatch(re.escape(prefix) + r"[0-9a-f]{64}", uri) is None
                or body.get("media_type") != "audio/wav"
                or type(digest) is not str or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
                or type(size) is not int or not 0 < size <= max_bytes
                or (name is not None and (
                    type(name) is not str or not name or name != name.strip()))
                or not isinstance(metadata, Mapping)):
            raise ControlTransportError(502, _AUDIO_ERROR)
        return ArtifactRef(
            uri=uri, media_type="audio/wav", digest=digest, size_bytes=size,
            name=name, metadata=metadata,
        )

    @staticmethod
    async def _audio_body(
        response: httpx.Response, max_bytes: int, *, expected_size: int | None = None,
    ) -> bytes:
        # Never decompress a peer-controlled body or buffer an error body.
        if response.headers.get("content-encoding", "identity") != "identity":
            raise ControlTransportError(502, _AUDIO_ERROR)
        declared = response.headers.get("content-length")
        length = None
        if declared is not None:
            if (len(declared) > 20 or not declared.isascii()
                    or not declared.isdecimal()):
                raise ControlTransportError(502, _AUDIO_ERROR)
            length = int(declared)
            if length > max_bytes or (expected_size is not None and length != expected_size):
                raise ControlTransportError(502, _AUDIO_ERROR)
        payload = bytearray()
        async for chunk in response.aiter_raw():
            if (len(payload) + len(chunk) > max_bytes
                    or (expected_size is not None and len(payload) + len(chunk) > expected_size)):
                raise ControlTransportError(502, _AUDIO_ERROR)
            payload.extend(chunk)
        if ((length is not None and len(payload) != length)
                or (expected_size is not None and len(payload) != expected_size)):
            raise ControlTransportError(502, _AUDIO_ERROR)
        return bytes(payload)

    async def download_audio_input(
        self, worker_id: str, claimed: ClaimedJob, *, max_bytes: int,
    ) -> bytes:
        """Retrieve only this serving transcription attempt's bounded WAV input."""
        try:
            path, headers = self._audio_claim(worker_id, claimed, "audio.transcribe", max_bytes)
            reference = self._audio_reference(
                claimed.request.payload.get("audio_input"), "tsumgi-audio-input:", max_bytes,
            )
            async with asyncio.timeout(self.timeout_seconds):
                async with self._client.stream("GET", path + "/input", headers=headers) as response:
                    if response.status_code != 200:
                        raise ControlTransportError(response.status_code, _AUDIO_ERROR)
                    if response.headers.get("content-type") != "audio/wav":
                        raise ControlTransportError(502, _AUDIO_ERROR)
                    data = await self._audio_body(
                        response, max_bytes, expected_size=reference.size_bytes,
                    )
                    if "sha256:" + hashlib.sha256(data).hexdigest() != reference.digest:
                        raise ControlTransportError(502, _AUDIO_ERROR)
                    return data
        except ControlTransportError as exc:
            raise ControlTransportError(exc.status_code, _AUDIO_ERROR) from None
        except (httpx.HTTPError, httpx.StreamError, TimeoutError, ValueError, TypeError):
            raise ControlTransportError(0, _AUDIO_ERROR) from None

    async def stage_audio_output(
        self, worker_id: str, claimed: ClaimedJob, wav_bytes: bytes, *, max_bytes: int,
    ) -> ArtifactRef:
        """Stage one bounded synthesis WAV; never complete a Job or retry a POST."""
        try:
            path, headers = self._audio_claim(worker_id, claimed, "audio.synthesize", max_bytes)
            if (type(wav_bytes) is not bytes or not 12 <= len(wav_bytes) <= max_bytes
                    or wav_bytes[:4] != b"RIFF" or wav_bytes[8:12] != b"WAVE"):
                raise ControlTransportError(422, _AUDIO_ERROR)
            headers["content-type"] = "audio/wav"
            digest = "sha256:" + hashlib.sha256(wav_bytes).hexdigest()
            async with asyncio.timeout(self.timeout_seconds):
                async with self._client.stream(
                    "POST", path + "/output", headers=headers, content=wav_bytes,
                ) as response:
                    if response.status_code != 201:
                        raise ControlTransportError(response.status_code, _AUDIO_ERROR)
                    if response.headers.get("content-type") != "application/json":
                        raise ControlTransportError(502, _AUDIO_ERROR)
                    raw = await self._audio_body(response, _AUDIO_RECEIPT_MAX_BYTES)
                    body = json.loads(
                        raw.decode("utf-8"), object_pairs_hook=_audio_json_object,
                        parse_constant=_audio_json_constant,
                    )
                    reference = self._audio_reference(body, "tsumgi-audio:", max_bytes)
                    if reference.size_bytes != len(wav_bytes) or reference.digest != digest:
                        raise ControlTransportError(502, _AUDIO_ERROR)
                    return reference
        except ControlTransportError as exc:
            raise ControlTransportError(exc.status_code, _AUDIO_ERROR) from None
        except (httpx.HTTPError, httpx.StreamError, TimeoutError, ValueError, TypeError, RecursionError):
            raise ControlTransportError(0, _AUDIO_ERROR) from None

    async def publish_event(
        self,
        worker_id: str,
        job_id: str,
        lease_token: str,
        *,
        kind: str,
        payload: Mapping[str, Any],
        runtime_instance_epoch: str | None = None,
    ) -> dict[str, Any]:
        extensions = [JOB_EVENTS_EXTENSION]
        if runtime_instance_epoch is not None:
            extensions.append(SERVING_EXTENSION)
        response = await self._request(
            "POST",
            f"/v1/workers/{worker_id}/jobs/{job_id}/events",
            json={
                "protocol_version": PROTOCOL_VERSION,
                "extensions": extensions,
                "lease_token": lease_token,
                "runtime_instance_epoch": runtime_instance_epoch,
                "kind": kind,
                "payload": dict(payload),
            },
        )
        return self._object(response)

    async def inspect_job(self, worker_id: str, job_id: str) -> dict[str, Any]:
        response = await self._request(
            "GET", f"/v1/workers/{worker_id}/jobs/{job_id}"
        )
        return self._object(response)

    async def complete(
        self,
        worker_id: str,
        job_id: str,
        lease_token: str,
        result: JobResult,
        *,
        runtime_instance_epoch: str | None = None,
    ) -> dict[str, Any]:
        response = await self._request(
            "POST",
            f"/v1/workers/{worker_id}/jobs/{job_id}/complete",
            json={
                "protocol_version": PROTOCOL_VERSION,
                "lease_token": lease_token,
                "runtime_instance_epoch": runtime_instance_epoch,
                "extensions": (
                    [] if runtime_instance_epoch is None else [SERVING_EXTENSION]
                ),
                "result": job_result_to_dict(result),
            },
        )
        return self._object(response)

    async def fail(
        self,
        worker_id: str,
        job_id: str,
        lease_token: str,
        *,
        error: str | Mapping[str, Any],
        retryable: bool,
        runtime_instance_epoch: str | None = None,
    ) -> dict[str, Any]:
        response = await self._request(
            "POST",
            f"/v1/workers/{worker_id}/jobs/{job_id}/fail",
            json={
                "protocol_version": PROTOCOL_VERSION,
                "lease_token": lease_token,
                "runtime_instance_epoch": runtime_instance_epoch,
                "extensions": (
                    [] if runtime_instance_epoch is None else [SERVING_EXTENSION]
                ),
                "error": dict(error) if isinstance(error, Mapping) else error,
                "retryable": retryable,
            },
        )
        return self._object(response)
