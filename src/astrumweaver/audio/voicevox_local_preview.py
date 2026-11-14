"""Explicit local-only VOICEVOX TTS preview (Issue #247).

This is an ISOLATED developer/operator preview, NOT the production Gateway,
a Worker, Control admission, a RuntimeProvider or a verified engine identity.
Never import/mount this app from production entrypoints.

VOICEVOX is installed, started and licensed independently by its operator.
Its native /audio_query?text= URL may appear in engine or proxy access logs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import secrets
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from ..gateway.audio_ingress import _check_wav
from ..runtime.providers.voicevox import _validate_query
from ..runtime.providers.voicevox_discovery import (
    VoicevoxDiscoveryConfig,
    VoicevoxDiscoveryProbe,
    VoicevoxDiscoveryStatus,
)
from ..runtime.providers.voicevox_http import (
    VoicevoxHttpError,
    _decoded_json,
    _read_response,
)

_TOKEN = re.compile(r"[A-Za-z0-9._~+/-]{24,256}=*\Z")
_MAX_QUERY = 32 * 1024
_MAX_TARGET = 8192
_MAX_REQUEST = 4096


def _bad_config() -> ValueError:
    return ValueError("VOICEVOX local preview configuration is invalid")


def _canonical_uuid(value: object) -> bool:
    if type(value) is not str:
        return False
    try:
        ident = UUID(value)
    except (ValueError, TypeError, AttributeError):
        return False
    return ident.int != 0 and str(ident) == value


@dataclass(frozen=True, slots=True, repr=False)
class LocalVoicevoxPreviewConfig:
    """Operator-only risk acceptance; none of these fields grants trust."""

    endpoint: str
    speaker_uuid: str
    style_id: int
    bearer_token: str
    enabled: bool = False
    accept_unverified_engine: bool = False
    accept_native_text_url_logging: bool = False
    timeout_seconds: float = 15.0
    max_text_bytes: int = 1024
    max_output_bytes: int = 8 * 1024 * 1024

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise _bad_config()
        if not self.enabled:
            return  # Disabled configuration has no network or foreign inspection.
        try:
            VoicevoxDiscoveryConfig(
                enabled=True,
                endpoint=self.endpoint,
                timeout_seconds=min(self.timeout_seconds, 5.0),
            )
        except (ValueError, TypeError, OverflowError):
            raise _bad_config() from None
        if (
            type(self.accept_unverified_engine) is not bool
            or not self.accept_unverified_engine
            or type(self.accept_native_text_url_logging) is not bool
            or not self.accept_native_text_url_logging
            or not _canonical_uuid(self.speaker_uuid)
            or type(self.style_id) is not int
            or not 0 <= self.style_id < 2**31
            or type(self.bearer_token) is not str
            or _TOKEN.fullmatch(self.bearer_token) is None
            or type(self.max_text_bytes) is not int
            or not 1 <= self.max_text_bytes <= 2048
            or type(self.max_output_bytes) is not int
            or not 46 <= self.max_output_bytes <= 16 * 1024 * 1024
            or type(self.timeout_seconds) not in (int, float)
            or not math.isfinite(self.timeout_seconds)
            or not 1 <= self.timeout_seconds <= 60
        ):
            raise _bad_config()


def _error(code: str, status: int) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": "VOICEVOX local preview unavailable"}},
        status_code=status,
        headers={"cache-control": "no-store"},
    )


def _authorized(request: Request, token: str) -> bool:
    headers = request.headers.getlist("authorization")
    if len(headers) != 1:
        return False
    scheme, space, value = headers[0].partition(" ")
    if scheme.lower() != "bearer" or space != " " or _TOKEN.fullmatch(value) is None:
        return False
    try:
        return secrets.compare_digest(value, token)
    except (TypeError, UnicodeError):
        return False


async def _bounded_request_body(request: Request) -> bytes:
    headers = request.headers
    if (
        len(headers.getlist("content-type")) != 1
        or headers.get("content-type", "").lower() != "application/json"
        or headers.get("content-encoding") is not None
        or headers.get("content-transfer-encoding") is not None
        or len(headers.getlist("content-length")) > 1
    ):
        raise ValueError("bad headers")
    length = headers.get("content-length")
    if length is not None and (
        not length.isascii()
        or not length.isdecimal()
        or len(length) > 5
        or int(length) > _MAX_REQUEST
    ):
        raise OverflowError("body too large")
    data = bytearray()
    count = 0
    async for chunk in request.stream():
        count += 1
        if count > 32 or len(data) + len(chunk) > _MAX_REQUEST:
            raise OverflowError("body too large")
        data.extend(chunk)
    if not data or (length is not None and len(data) != int(length)):
        raise ValueError("body malformed")
    return bytes(data)


async def _loopback_post(
    port: int, path: str, *, body: bytes, accept: str, max_bytes: int
) -> bytes:
    if (
        type(path) is not str or not path.isascii()
        or not path.startswith(("/audio_query?", "/synthesis?"))
        or not 1 <= len(path) <= _MAX_TARGET
        or type(body) is not bytes or len(body) > _MAX_QUERY
        or accept not in ("application/json", "audio/wav")
    ):
        raise ValueError("bad native request")
    writer: asyncio.StreamWriter | None = None
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port, limit=8192)
        request = (
            f"POST {path} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            "Connection: close\r\n"
            f"Accept: {accept}\r\n"
            f"Content-Length: {len(body)}\r\n"
            + ("Content-Type: application/json\r\n" if body else "")
            + "\r\n"
        ).encode("ascii") + body
        writer.write(request)
        await writer.drain()
        return await _read_response(reader, media_type=accept, max_bytes=max_bytes)
    finally:
        if writer is not None:
            writer.close()


def create_local_voicevox_preview_app(config: LocalVoicevoxPreviewConfig) -> FastAPI:
    """Creates a separate localhost-only preview. Never mount in real Gateway."""
    if type(config) is not LocalVoicevoxPreviewConfig or not config.enabled:
        raise _bad_config()
    # Reconstruct from explicit operator fields to avoid captured reflective drift.
    cfg = LocalVoicevoxPreviewConfig(
        endpoint=config.endpoint,
        speaker_uuid=config.speaker_uuid,
        style_id=config.style_id,
        bearer_token=config.bearer_token,
        enabled=config.enabled,
        accept_unverified_engine=config.accept_unverified_engine,
        accept_native_text_url_logging=config.accept_native_text_url_logging,
        timeout_seconds=config.timeout_seconds,
        max_text_bytes=config.max_text_bytes,
        max_output_bytes=config.max_output_bytes,
    )
    probe = VoicevoxDiscoveryProbe(
        VoicevoxDiscoveryConfig(
            enabled=True,
            endpoint=cfg.endpoint,
            timeout_seconds=min(cfg.timeout_seconds, 5.0),
        )
    )
    port = urlsplit(cfg.endpoint).port
    assert port is not None
    in_flight = asyncio.Lock()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    async def observe():
        observation = await probe.probe_once()
        if (
            observation.status is not VoicevoxDiscoveryStatus.COMPATIBLE
            or observation.snapshot is None
        ):
            raise RuntimeError("local engine unobserved")
        return observation.snapshot

    @app.get("/preview/audio/voices")
    async def voices(request: Request) -> Response:
        if not _authorized(request, cfg.bearer_token):
            return _error("unauthorized", 401)
        if request.scope.get("query_string", b""):
            return _error("invalid_request", 400)
        try:
            async with asyncio.timeout(cfg.timeout_seconds):
                observed = await observe()
            return JSONResponse(
                {
                    "object": "list",
                    "data": [
                        {"speaker_id": item.speaker_uuid, "style_id": item.style_id}
                        for item in observed.styles
                    ],
                    "preview_only": True,
                    "control_authoritative": False,
                    "ready": False,
                    "rights_verified": False,
                },
                headers={"cache-control": "no-store"},
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return _error("engine_unavailable", 503)

    @app.post("/preview/audio/speech")
    async def speech(request: Request) -> Response:
        if not _authorized(request, cfg.bearer_token):
            return _error("unauthorized", 401)
        if request.scope.get("query_string", b""):
            return _error("invalid_request", 400)
        if in_flight.locked():
            return _error("busy", 429)
        async with in_flight:
            try:
                async with asyncio.timeout(cfg.timeout_seconds):
                    try:
                        raw = await _bounded_request_body(request)
                    except OverflowError:
                        return _error("request_too_large", 413)
                    except (ValueError, TypeError):
                        return _error("invalid_request", 400)
                    try:
                        parsed = _decoded_json(raw)
                    except VoicevoxHttpError:
                        return _error("invalid_request", 400)
                    if type(parsed) is not dict or set(parsed) != {"input"}:
                        return _error("invalid_request", 400)
                    text = parsed["input"]
                    if type(text) is not str or not text.strip():
                        return _error("invalid_request", 400)
                    if len(text) > cfg.max_text_bytes:
                        return _error("request_too_large", 413)
                    try:
                        encoded = text.encode("utf-8")
                    except UnicodeError:
                        return _error("invalid_request", 400)
                    if len(encoded) > cfg.max_text_bytes:
                        return _error("request_too_large", 413)
                    observed = await observe()
                    if not any(
                        x.speaker_uuid == cfg.speaker_uuid and x.style_id == cfg.style_id
                        for x in observed.styles
                    ):
                        return _error("voice_unavailable", 503)
                    native_path = "/audio_query?" + urlencode(
                        {"text": text, "speaker": cfg.style_id}
                    )
                    query_bytes = await _loopback_post(
                        port, native_path, body=b"",
                        accept="application/json", max_bytes=_MAX_QUERY,
                    )
                    query = _decoded_json(query_bytes)
                    _validate_query(query, _MAX_QUERY)
                    query_body = json.dumps(
                        query, ensure_ascii=False, separators=(",", ":"), allow_nan=False
                    ).encode("utf-8")
                    wav = await _loopback_post(
                        port, f"/synthesis?speaker={cfg.style_id}", body=query_body,
                        accept="audio/wav", max_bytes=cfg.max_output_bytes,
                    )
                    _check_wav(wav)
                    return Response(
                        wav, media_type="audio/wav",
                        headers={
                            "cache-control": "no-store",
                            "x-tsumgi-voicevox-preview-only": "true",
                        },
                    )
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                return _error("timeout", 504)
            except Exception:  # noqa: BLE001 - never echo native URL, text or errors
                return _error("engine_unavailable", 503)

    return app


def main() -> None:
    """Run a dedicated local-only preview process, NEVER a production Worker."""
    parser = argparse.ArgumentParser(description="TSUMGI local-only VOICEVOX preview")
    parser.add_argument("--endpoint", default="http://127.0.0.1:50021")
    parser.add_argument("--list-voices", action="store_true")
    parser.add_argument("--speaker-uuid")
    parser.add_argument("--style-id", type=int)
    parser.add_argument("--port", type=int, default=9811)
    parser.add_argument("--accept-unverified-engine", action="store_true")
    parser.add_argument("--accept-native-text-url-logging", action="store_true")
    args = parser.parse_args()
    if args.list_voices:
        async def list_observed() -> None:
            probe = VoicevoxDiscoveryProbe(VoicevoxDiscoveryConfig(
                enabled=True, endpoint=args.endpoint,
            ))
            result = await probe.probe_once()
            if result.status is not VoicevoxDiscoveryStatus.COMPATIBLE or not result.snapshot:
                raise SystemExit("VOICEVOX local endpoint not observed")
            for item in result.snapshot.styles:
                print(f"speaker_uuid={item.speaker_uuid} style_id={item.style_id}")
            print("Observation only; not proof of engine identity or voice rights.")
        asyncio.run(list_observed())
        return
    if args.speaker_uuid is None or args.style_id is None:
        parser.error("--speaker-uuid and --style-id are required")
    if not 1024 <= args.port <= 65535:
        parser.error("--port must be 1024..65535")
    config = LocalVoicevoxPreviewConfig(
        enabled=True, endpoint=args.endpoint, speaker_uuid=args.speaker_uuid,
        style_id=args.style_id,
        bearer_token=os.environ.get("TSUMGI_VOICEVOX_PREVIEW_TOKEN", ""),
        accept_unverified_engine=args.accept_unverified_engine,
        accept_native_text_url_logging=args.accept_native_text_url_logging,
    )
    import uvicorn

    uvicorn.run(
        create_local_voicevox_preview_app(config),
        host="127.0.0.1", port=args.port, log_level="warning", access_log=False,
    )


if __name__ == "__main__":
    main()
