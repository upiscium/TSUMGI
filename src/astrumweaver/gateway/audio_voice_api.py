"""Opt-in, read-only HTTP projection of an already-reviewed audio catalog.

No runtime discovery, availability assertion, admission or daemon mounting.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from ..control.auth import ClientAuthMode
from .api import _require_client
from .audio import (
    SPEECH_OPERATION_SCHEMA,
    AudioGatewayError,
    AudioGatewayProfile,
    AudioProfileCatalog,
)


_TOKEN = re.compile(r"[A-Za-z0-9._~+/-]+=*\Z")
_BAD_ESCAPE = re.compile(r"%(?![A-Fa-f0-9]{2})")
_MAX_QUERY_BYTES = 1024


def _require_voice_client(request: Request, token: str) -> None:
    # The shared helper compares tokens in constant time. Guard its string-only
    # compare_digest against non-ASCII and reject ambiguous/multiple headers.
    headers = request.headers.getlist("authorization")
    if len(headers) != 1:
        raise AudioGatewayError("unauthorized", "authorization", status_code=401)
    scheme, separator, supplied = headers[0].partition(" ")
    if (
        scheme.lower() != "bearer" or separator != " "
        or _TOKEN.fullmatch(supplied) is None
    ):
        raise AudioGatewayError("unauthorized", "authorization", status_code=401)
    try:
        _require_client(request, mode=ClientAuthMode.BEARER, token=token)
    except HTTPException:
        raise AudioGatewayError(
            "unauthorized", "authorization", status_code=401,
        ) from None


def _model_query(request: Request) -> str:
    # Do not use a scalar FastAPI query argument: it silently selects one of
    # duplicate values. Bound and strictly decode the original ASGI query bytes.
    raw = request.scope.get("query_string", b"")
    if len(raw) > _MAX_QUERY_BYTES:
        raise AudioGatewayError("request_too_large", "query", status_code=413)
    try:
        query = raw.decode("ascii")
        if _BAD_ESCAPE.search(query):
            raise ValueError
        pairs = parse_qsl(
            query, keep_blank_values=True, strict_parsing=True,
            encoding="utf-8", errors="strict", max_num_fields=16,
        )
    except (UnicodeError, ValueError):
        raise AudioGatewayError("invalid_request", "query") from None
    if any(key != "model" for key, _ in pairs):
        raise AudioGatewayError("unknown_fields", "query")
    if not pairs:
        raise AudioGatewayError("missing_fields", "model")
    if len(pairs) != 1:
        raise AudioGatewayError("duplicate_fields", "model")
    return pairs[0][1]


def _voices_response(profile: AudioGatewayProfile) -> dict[str, object]:
    resolved = profile.resolved
    # Public allowlist only. Never serialize the deployment, whole catalog or
    # operator manifest into a response, even if future schemas add fields.
    return {
        "object": "list",
        "model": profile.profile_id,
        "data": [{
            "alias": voice.alias,
            "speaker_id": voice.speaker_id,
            "style_id": voice.style_id,
            "locale": voice.locale,
            "license_id": voice.license_id,
            "attribution": voice.attribution,
        } for voice in profile.voices],
        "x_astrumweaver": {
            "backend_kind": profile.backend_kind,
            "capability": resolved.profile.capability,
            "operation_schema": resolved.profile.operation_schema,
            "profile_revision": resolved.profile_revision,
            "deployment_revision": resolved.deployment.revision,
            "serving_contract_revision": resolved.contract.revision,
            "semantic_revision": resolved.contract.semantic_revision,
        },
    }


def create_audio_voice_router(
    catalog: AudioProfileCatalog,
    *,
    client_auth: ClientAuthMode | str,
    client_token: str | None,
) -> APIRouter:
    """Explicitly inject reviewed configuration and bearer-only credentials.

    This factory intentionally cannot inherit permissive non-audio auth. It
    neither loads a manifest nor mounts itself in Control or any daemon.
    """
    try:
        mode = ClientAuthMode(client_auth)
    except (TypeError, ValueError):
        raise ValueError("audio voice catalog requires bearer authentication") from None
    if mode is not ClientAuthMode.BEARER:
        raise ValueError("audio voice catalog requires bearer authentication")
    if type(client_token) is not str or _TOKEN.fullmatch(client_token) is None:
        raise ValueError("audio voice catalog requires a valid bearer token")
    if type(catalog) is not AudioProfileCatalog:
        raise AudioGatewayError(
            "invalid_audio_configuration", "catalog", status_code=500,
        )
    # Revalidate and detach once, before exposing any route. Missing TTS voices,
    # unsupported backends and inconsistent pins fail closed at construction.
    reviewed = AudioProfileCatalog.from_dict(catalog.to_dict())
    router = APIRouter()

    @router.get("/v1/audio/voices")
    async def voices(request: Request) -> JSONResponse:
        headers = {"Cache-Control": "no-store"}
        try:
            # Authenticate before query validation/model lookup; anonymous
            # callers cannot use errors to enumerate configured profiles.
            _require_voice_client(request, client_token)
            model = _model_query(request)
            profile = reviewed.get(model, operation_schema=SPEECH_OPERATION_SCHEMA)
            return JSONResponse(content=_voices_response(profile), headers=headers)
        except AudioGatewayError as exc:
            if exc.status_code == 401:
                headers["WWW-Authenticate"] = "Bearer"
            return JSONResponse(
                status_code=exc.status_code, content=exc.to_dict(), headers=headers,
            )

    return router


__all__ = ["create_audio_voice_router"]
