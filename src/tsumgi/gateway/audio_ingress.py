"""Bounded, pre-admission-only audio ingress; no routes or artifact authority.

Call on a fresh ASGI request before any body/form/JSON consumer. Transport/server
allocation limits remain the integrator's responsibility. All retained body data
is bounded here, but a server may already have allocated an incoming ASGI chunk.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
import json
import math
import re
import struct
from typing import Protocol

from .audio import (
    SPEECH_OPERATION_SCHEMA,
    TRANSCRIPTION_OPERATION_SCHEMA,
    AudioGatewayError,
    AudioProfileCatalog,
    ValidatedSpeechRequest,
    ValidatedTranscriptionMetadata,
    validate_speech_request,
    validate_transcription_metadata,
)


class IngressRequest(Protocol):
    scope: Mapping[str, object]

    async def receive(self) -> dict: ...


def _error(code: str, field: str = "request", status: int = 400) -> AudioGatewayError:
    return AudioGatewayError(code, field, status_code=status)


def _bound(size: int, cap: int, field: str) -> None:
    if size > cap:
        raise _error("request_too_large", field, 413)


@dataclass(frozen=True, slots=True)
class AudioIngressLimits:
    """Explicit operator caps, never inferred from a caller's model selection.

    Hard ceilings are parser safety ceilings, not recommended production policy.
    request_bytes includes the complete multipart envelope; metadata_bytes is the
    sum of raw metadata values (not filenames or MIME headers).
    """

    request_bytes: int
    input_bytes: int
    file_bytes: int
    metadata_bytes: int
    part_header_bytes: int
    total_header_bytes: int
    receive_events: int
    idle_timeout_seconds: float
    total_timeout_seconds: float

    def __post_init__(self) -> None:
        ceilings = {
            "request_bytes": 16 * 1024 * 1024,
            "input_bytes": 1024 * 1024,
            "file_bytes": 16 * 1024 * 1024,
            "metadata_bytes": 16 * 1024,
            "part_header_bytes": 8192,
            "total_header_bytes": 32768,
            "receive_events": 65536,
        }
        for name, ceiling in ceilings.items():
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= ceiling:
                raise _error("invalid_audio_configuration", "ingress_limits", 500)
        for name in ("idle_timeout_seconds", "total_timeout_seconds"):
            value = getattr(self, name)
            if (
                type(value) not in (int, float) or not 0 < value <= 60
                or not math.isfinite(value)
            ):
                raise _error("invalid_audio_configuration", "ingress_limits", 500)
        if self.idle_timeout_seconds > self.total_timeout_seconds:
            raise _error("invalid_audio_configuration", "ingress_limits", 500)


@dataclass(frozen=True, slots=True)
class PreAdmissionTranscription:
    """Untrusted audio bytes, NOT a job, artifact, or decoded-audio validation.

    #174/#177 binding, codec qualification, duration and decoded-size checks are
    mandatory before admission. The structural WAV check is not that authority.
    Filenames are checked and discarded; there is no path or URL in this result.
    """

    metadata: ValidatedTranscriptionMetadata
    wav_bytes: bytes
    request_size_bytes: int
    metadata_size_bytes: int


def _caps(catalog: AudioProfileCatalog, schema: str, limits: AudioIngressLimits) -> dict:
    profiles = [p for p in catalog.profiles if p.resolved.profile.operation_schema == schema]
    if not profiles:
        raise _error("unsupported_operation", "model", 503)
    names = ("request_bytes", "input_bytes") if schema == SPEECH_OPERATION_SCHEMA else (
        "request_bytes", "file_bytes", "metadata_bytes",
    )
    # A profile is unknown until parsing finishes. No client assertion can relax
    # the explicit global cap; the exact selected profile is checked afterward.
    return {name: min(getattr(limits, name), max(
        p.resolved.effective_limits[name] for p in profiles
    )) for name in names}


_TOKEN = re.compile(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_JSON_TYPE = re.compile(rb'application/json(?: *; *charset=(?:utf-8|"utf-8"))?\Z', re.I)
_MULTIPART_TYPE = re.compile(
    rb'multipart/form-data *; *boundary=(?:([A-Za-z0-9_-]{1,70})|"([A-Za-z0-9_-]{1,70})")\Z',
    re.I,
)
_DISPOSITION = re.compile(
    rb'form-data; *name="(model|language|response_format|file)"'
    rb'(?:; *filename="([A-Za-z0-9][A-Za-z0-9_-]{0,63}\.wav)")?\Z',
)


def _request_headers(request: IngressRequest, limits: AudioIngressLimits) -> dict[bytes, bytes]:
    raw = request.scope.get("headers")
    if type(raw) not in (list, tuple) or len(raw) > 64:
        raise _error("invalid_headers", "headers")
    result = {}
    total = 0
    for item in raw:
        if type(item) not in (tuple, list) or len(item) != 2:
            raise _error("invalid_headers", "headers")
        name, value = item
        if type(name) is not bytes or type(value) is not bytes:
            raise _error("invalid_headers", "headers")
        total += len(name) + len(value) + 4
        _bound(total, limits.total_header_bytes, "headers")
        if (
            not _TOKEN.fullmatch(name)
            or any(c < 32 or c > 126 for c in value)
        ):
            raise _error("invalid_headers", "headers")
        name = name.lower()
        if name in result:
            raise _error("duplicate_headers", "headers")
        result[name] = value
    if b"content-encoding" in result or b"content-transfer-encoding" in result:
        raise _error("unsupported_encoding", "headers", 415)
    transfer = result.get(b"transfer-encoding")
    if transfer is not None and (
        transfer.lower() != b"chunked" or b"content-length" in result
    ):
        raise _error("invalid_headers", "headers")
    return result


async def _read_body(
    request: IngressRequest, headers: dict[bytes, bytes], cap: int,
    limits: AudioIngressLimits,
) -> bytes:
    length = headers.get(b"content-length")
    declared = None
    if length is not None:
        # Bound decimal parsing too; reject signs, spaces, lists and huge ints.
        if not 1 <= len(length) <= 10 or not length.isdigit():
            raise _error("invalid_content_length", "headers")
        declared = int(length)
        _bound(declared, cap, "request_bytes")
        if declared == 0:
            raise _error("empty_body")
    body = bytearray()
    try:
        async with asyncio.timeout(limits.total_timeout_seconds):
            for _ in range(limits.receive_events):
                message = await asyncio.wait_for(
                    request.receive(), timeout=limits.idle_timeout_seconds,
                )
                if type(message) is not dict:
                    raise _error("invalid_body")
                if message.get("type") == "http.disconnect":
                    raise _error("request_disconnected")
                if message.get("type") != "http.request":
                    raise _error("invalid_body")
                chunk = message.get("body", b"")
                more = message.get("more_body", False)
                if type(chunk) is not bytes or type(more) is not bool:
                    raise _error("invalid_body")
                size = len(body) + len(chunk)
                _bound(size, cap, "request_bytes")
                if declared is not None and size > declared:
                    raise _error("content_length_mismatch", "request_bytes")
                body.extend(chunk)
                if not more:
                    if declared is not None and size != declared:
                        raise _error("content_length_mismatch", "request_bytes")
                    if not body:
                        raise _error("empty_body")
                    return bytes(body)
            raise _error("too_many_receive_events")
    except TimeoutError:
        raise _error("request_timeout", "request", 408) from None


def _utf8(raw: bytes, field: str) -> str:
    try:
        return raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise _error("invalid_utf8", field) from None


def _json_object(raw: bytes) -> dict:
    text = _utf8(raw, "request")
    # The shared schemas are flat string objects with exactly four members.
    # Reject nesting and excess members before invoking the recursive decoder
    # (including nesting in unknown fields). Delimiters in strings do not count.
    quoted = escaped = False
    depth = commas = 0
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if char == "[" or depth > 1:
                raise _error("invalid_json")
        elif char in "]}":
            depth -= 1
            if depth < 0:
                raise _error("invalid_json")
        elif char == ",":
            commas += 1
            if commas > 3:
                raise _error("invalid_json")

    def pairs(items: list[tuple[str, object]]) -> dict:
        result = {}
        for key, value in items:
            if key in result:
                raise _error("duplicate_fields")
            # Escaped surrogate code points are not Unicode scalar values.
            for item in (key, value):
                if type(item) is str:
                    try:
                        item.encode("utf-8", errors="strict")
                    except UnicodeEncodeError:
                        raise _error("invalid_utf8", "request") from None
            result[key] = value
        return result

    def no_number(_: str) -> object:
        raise _error("invalid_json")

    try:
        value = json.loads(
            text, object_pairs_hook=pairs, parse_constant=no_number,
            parse_int=no_number, parse_float=no_number,
        )
    except (ValueError, RecursionError) as exc:
        if isinstance(exc, AudioGatewayError):
            raise
        raise _error("invalid_json") from None
    if type(value) is not dict:
        raise _error("invalid_json")
    return value


async def parse_speech_ingress(
    request: IngressRequest, catalog: AudioProfileCatalog, *, limits: AudioIngressLimits,
) -> ValidatedSpeechRequest:
    """Measure bytes, strictly decode JSON, then reuse the shared validator."""
    caps = _caps(catalog, SPEECH_OPERATION_SCHEMA, limits)
    headers = _request_headers(request, limits)
    if not _JSON_TYPE.fullmatch(headers.get(b"content-type", b"")):
        raise _error("unsupported_media_type", "content_type", 415)
    raw = await _read_body(request, headers, caps["request_bytes"], limits)
    result = validate_speech_request(_json_object(raw), catalog, request_size_bytes=len(raw))
    _bound(result.input_size_bytes, caps["input_bytes"], "input")
    return result


def _part_headers(raw: bytes) -> tuple[str, bool]:
    headers = {}
    for line in raw.split(b"\r\n"):
        name, sep, value = line.partition(b":")
        if not sep or not _TOKEN.fullmatch(name):
            raise _error("invalid_multipart", "headers")
        name = name.lower()
        if name not in (b"content-disposition", b"content-type") or name in headers:
            # Includes all Content-Transfer-Encoding values, even "binary".
            raise _error("invalid_multipart", "headers")
        value = value.strip(b" ")
        if any(c < 32 or c > 126 for c in value):
            raise _error("invalid_multipart", "headers")
        headers[name] = value
    match = _DISPOSITION.fullmatch(headers.get(b"content-disposition", b""))
    if match is None:
        raise _error("invalid_multipart", "headers")
    name = match[1].decode("ascii")
    is_file = name == "file"
    if is_file != (match[2] is not None):
        raise _error("invalid_multipart", "headers")
    mime = headers.get(b"content-type", b"").lower()
    if is_file:
        if mime != b"audio/wav":
            raise _error("unsupported_media_type", "file", 415)
    elif b"content-type" in headers and mime not in (b"text/plain", b"text/plain; charset=utf-8"):
        raise _error("unsupported_media_type", "metadata", 415)
    return name, is_file


def _multipart(
    raw: bytes, boundary: bytes, caps: dict, limits: AudioIngressLimits,
) -> tuple[dict[str, str], bytes, int]:
    marker = b"--" + boundary
    if not raw.startswith(marker + b"\r\n"):
        raise _error("invalid_multipart")
    position = len(marker) + 2
    seen = set()
    metadata = {}
    metadata_size = header_size = 0
    audio = b""
    for _ in range(4):
        # Search only the allowed window; never slice an unbounded header block.
        end = raw.find(b"\r\n\r\n", position, position + limits.part_header_bytes + 4)
        if end < 0:
            raise _error("invalid_multipart", "headers")
        header_size += end - position + 4
        _bound(header_size, limits.total_header_bytes, "headers")
        name, is_file = _part_headers(raw[position:end])
        if name in seen:
            raise _error("duplicate_fields", "multipart")
        seen.add(name)
        start = end + 4
        end = raw.find(b"\r\n" + marker, start)
        if end < 0:
            raise _error("invalid_multipart")
        size = end - start
        if is_file:
            _bound(size, caps["file_bytes"], "file_bytes")
            if size == 0:
                raise _error("empty_audio", "file")
            audio = raw[start:end]
        else:
            metadata_size += size
            _bound(metadata_size, caps["metadata_bytes"], "metadata_bytes")
            metadata[name] = _utf8(raw[start:end], "metadata")
        position = end + 2 + len(marker)
        if raw[position:position + 2] == b"--":
            if len(raw) != position + 4 or raw[position:position + 4] != b"--\r\n" or seen != {
                "model", "language", "response_format", "file",
            }:
                raise _error("invalid_multipart")
            return metadata, audio, metadata_size
        if raw[position:position + 2] != b"\r\n":
            raise _error("invalid_multipart")
        position += 2
    raise _error("too_many_parts", "multipart")


def _check_wav(raw: bytes) -> None:
    # Intentionally tiny container subset: fmt(PCM16) then one nonempty data
    # chunk, no ancillary chunks, padding, trailing bytes or extended formats.
    # No audio samples are decoded and no duration/decoded-size authority exists.
    if (
        len(raw) < 46 or raw[:4] != b"RIFF" or raw[8:16] != b"WAVEfmt "
        or int.from_bytes(raw[4:8], "little") != len(raw) - 8
        or int.from_bytes(raw[16:20], "little") != 16
        or raw[36:40] != b"data"
    ):
        raise _error("unsupported_audio", "file", 415)
    codec, channels, rate, byte_rate, alignment, bits = struct.unpack_from("<HHIIHH", raw, 20)
    size = int.from_bytes(raw[40:44], "little")
    if (
        codec != 1 or channels not in (1, 2) or bits != 16
        or not 8000 <= rate <= 48000 or alignment != channels * 2
        or byte_rate != rate * alignment or size != len(raw) - 44
        or size == 0 or size % alignment
    ):
        raise _error("unsupported_audio", "file", 415)


async def parse_transcription_ingress(
    request: IngressRequest, catalog: AudioProfileCatalog, *, limits: AudioIngressLimits,
) -> PreAdmissionTranscription:
    """Strict multipart transport precheck only; never submit this as a job."""
    caps = _caps(catalog, TRANSCRIPTION_OPERATION_SCHEMA, limits)
    headers = _request_headers(request, limits)
    match = _MULTIPART_TYPE.fullmatch(headers.get(b"content-type", b""))
    if match is None:
        raise _error("unsupported_media_type", "content_type", 415)
    raw = await _read_body(request, headers, caps["request_bytes"], limits)
    body, audio, metadata_size = _multipart(raw, match[1] or match[2], caps, limits)
    metadata = validate_transcription_metadata(body, catalog, metadata_size_bytes=metadata_size)
    selected = metadata.transport_limits
    _bound(len(raw), selected["request_bytes"], "request_bytes")
    _bound(len(audio), selected["file_bytes"], "file_bytes")
    _check_wav(audio)
    return PreAdmissionTranscription(metadata, audio, len(raw), metadata_size)
