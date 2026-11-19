"""#215 B6b: source-only bounded VOICEVOX loopback HTTP/1.1 transport.

No runtime registry integration, process owner, deployment, model access or
engine launch. /audio_query places source text in the native URL query; real
VOICEVOX access logging must be disabled/redacted before operational use.
This transport has no HTTP client/framework request logger or auto retries.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit

from .voicevox import VoicevoxAdapterConfig, _validate_query

_JSON_TYPE = "application/json"
_WAV_TYPE = "audio/wav"
_MAX_VERSION = 256
_MAX_SPEAKERS = 256 * 1024
_MAX_HEADER = 8192
_MAX_LINE = 16384
_CHUNK_LINE = re.compile(rb"[0-9a-fA-F]{1,8}\r\n\Z")
_CONTENT_LENGTH = re.compile(r"(?:0|[1-9][0-9]{0,9})\Z")


class VoicevoxHttpError(RuntimeError):
    """Fixed diagnostic; never preserve private text, URL or HTTP exceptions."""

    def __init__(self) -> None:
        super().__init__("VOICEVOX loopback HTTP transport unavailable")


def _invalid() -> ValueError:
    return ValueError("VOICEVOX HTTP transport configuration is invalid")


@dataclass(frozen=True, slots=True)
class VoicevoxHttpConfig:
    """Operator declaration, not a verified engine/port ownership certificate."""

    endpoint: str
    adapter: VoicevoxAdapterConfig
    request_timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        if (
            type(self.endpoint) is not str
            or type(self.adapter) is not VoicevoxAdapterConfig
        ):
            raise _invalid()
        try:
            parsed = urlsplit(self.endpoint)
            port = parsed.port
        except ValueError:
            raise _invalid() from None
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or port is None
            or not 1024 <= port <= 65535
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or self.endpoint != f"http://127.0.0.1:{port}"
        ):
            raise _invalid()
        if (
            type(self.request_timeout_seconds) not in (float, int)
            or not math.isfinite(self.request_timeout_seconds)
            or not 0 < self.request_timeout_seconds <= 300
        ):
            raise _invalid()


def _decoded_json(raw: bytes) -> object:
    """Reject duplicate keys and non-JSON constants before returning input."""

    def unique_keys(pairs: list[tuple[str, object]]) -> dict:
        result: dict[str, object] = {}
        for k, v in pairs:
            if k in result:
                raise ValueError("duplicate")
            result[k] = v
        return result

    def no_constant(_item: str) -> None:
        raise ValueError("constant")

    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=unique_keys,
            parse_constant=no_constant,
        )
    except (ValueError, UnicodeError, RecursionError, TypeError):
        raise VoicevoxHttpError() from None


async def _read_bounded_body(
    reader: asyncio.StreamReader,
    headers: dict[str, str],
    *,
    max_bytes: int,
) -> bytes:
    chunks = bytearray()
    transfer = headers.get("transfer-encoding")
    length = headers.get("content-length")
    if transfer is not None and length is not None:
        raise VoicevoxHttpError()
    if transfer is not None:
        if transfer.lower() != "chunked":
            raise VoicevoxHttpError()
        while True:
            line = await reader.readline()
            if _CHUNK_LINE.fullmatch(line) is None:
                raise VoicevoxHttpError()
            size = int(line[:-2], 16)
            if size == 0:
                # No trailers: a future compatibility change needs explicit review.
                if await reader.readexactly(2) != b"\r\n":
                    raise VoicevoxHttpError()
                break
            if size > max_bytes - len(chunks):
                raise VoicevoxHttpError()
            chunks.extend(await reader.readexactly(size))
            if await reader.readexactly(2) != b"\r\n":
                raise VoicevoxHttpError()
    elif length is not None:
        if _CONTENT_LENGTH.fullmatch(length) is None:
            raise VoicevoxHttpError()
        count = int(length)
        if count > max_bytes:
            raise VoicevoxHttpError()
        chunks.extend(await reader.readexactly(count))
    else:
        while True:
            part = await reader.read(min(4096, max_bytes - len(chunks) + 1))
            if not part:
                break
            if len(part) > max_bytes - len(chunks):
                raise VoicevoxHttpError()
            chunks.extend(part)
    if not chunks:
        raise VoicevoxHttpError()
    return bytes(chunks)


async def _read_response(
    reader: asyncio.StreamReader,
    *,
    media_type: str,
    max_bytes: int,
) -> bytes:
    head = await reader.readuntil(b"\r\n\r\n")
    if len(head) > _MAX_HEADER:
        raise VoicevoxHttpError()
    lines = head[:-4].split(b"\r\n")
    # Do not accept redirects, errors, interim responses or protocol upgrades.
    if not lines or re.fullmatch(rb"HTTP/1\.[01] 200(?: [^\r\n]*)?", lines[0]) is None:
        raise VoicevoxHttpError()
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, colon, value = line.partition(b":")
        if colon != b":" or not re.fullmatch(rb"[a-zA-Z0-9-]{1,64}", name):
            raise VoicevoxHttpError()
        key = name.decode("ascii").lower()
        if key in headers:  # Content-Length and Content-Type ambiguity forbidden.
            raise VoicevoxHttpError()
        try:
            headers[key] = value.decode("ascii").strip(" \t")
        except UnicodeDecodeError:
            raise VoicevoxHttpError() from None
    content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if (
        content_type != media_type
        or headers.get("content-encoding", "identity").lower() != "identity"
    ):
        raise VoicevoxHttpError()
    return await _read_bounded_body(reader, headers, max_bytes=max_bytes)


class VoicevoxLoopbackHttpApi:
    """Genuine async HTTP calls, explicitly not evidence of owned server identity.

    No general-purpose HTTP client, proxy, headers, response caching, redirect
    handling, request logging, background retries or arbitrary URL parameters.
    Raw text necessarily appears in the VOICEVOX native /audio_query URL on the
    wire. Operators MUST separately disable/redact engine access logs.
    """

    def __init__(self, config: VoicevoxHttpConfig) -> None:
        if type(config) is not VoicevoxHttpConfig:
            raise _invalid()
        self._config = config
        self._port = urlsplit(config.endpoint).port
        self._closed = False
        self._active_writers: set[asyncio.StreamWriter] = set()

    async def _request(
        self,
        method: str,
        target: str,
        *,
        content: bytes = b"",
        max_bytes: int,
        media_type: str,
    ) -> bytes:
        if (
            self._closed
            or method not in {"GET", "POST"}
            or type(target) is not str
            or not target.startswith("/")
            or not target.isascii()
            or len(target) > _MAX_LINE
            or any(ord(c) < 33 or ord(c) > 126 for c in target)
            or type(content) is not bytes
            or len(content) > self._config.adapter.max_query_bytes
            or media_type not in (_JSON_TYPE, _WAV_TYPE)
            or type(max_bytes) is not int
            or max_bytes <= 0
        ):
            raise VoicevoxHttpError()
        # The only HTTP authority is the *configured* literal loopback IPv4
        # endpoint. asyncio bypasses environment proxies and HTTPX URL logging.
        request = (
            f"{method} {target} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{self._port}\r\n"
            "Connection: close\r\n"
            f"Accept: {media_type}\r\n"
            f"Content-Length: {len(content)}\r\n"
            + ("Content-Type: application/json\r\n" if content else "")
            + "\r\n"
        ).encode("ascii") + content
        writer: asyncio.StreamWriter | None = None
        try:
            async with asyncio.timeout(self._config.request_timeout_seconds):
                reader, writer = await asyncio.open_connection(
                    "127.0.0.1", self._port, limit=_MAX_HEADER
                )
                if self._closed:
                    raise VoicevoxHttpError()
                self._active_writers.add(writer)
                writer.write(request)
                await writer.drain()
                response = await _read_response(
                    reader,
                    media_type=media_type,
                    max_bytes=max_bytes,
                )
                if self._closed:
                    raise VoicevoxHttpError()
                return response
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - native URL/body/error redaction
            raise VoicevoxHttpError() from None
        finally:
            if writer is not None:
                self._active_writers.discard(writer)
                writer.close()

    async def bounded_json(self, *, path: str, max_bytes: int) -> bytes:
        if (
            path == "/version"
            and type(max_bytes) is int
            and 0 < max_bytes <= _MAX_VERSION
        ) or (
            path == "/speakers"
            and type(max_bytes) is int
            and 0 < max_bytes <= _MAX_SPEAKERS
        ):
            return await self._request(
                "GET",
                path,
                max_bytes=max_bytes,
                media_type=_JSON_TYPE,
            )
        raise VoicevoxHttpError()

    async def version(self) -> str:
        raw = await self.bounded_json(path="/version", max_bytes=_MAX_VERSION)
        value = _decoded_json(raw)
        if type(value) is not str or not 1 <= len(value) <= 128:
            raise VoicevoxHttpError()
        return value

    async def speakers(self) -> object:
        raw = await self.bounded_json(path="/speakers", max_bytes=_MAX_SPEAKERS)
        value = _decoded_json(raw)
        if type(value) is not list:
            raise VoicevoxHttpError()
        return value

    async def audio_query(self, *, text: str, speaker: int) -> object:
        adapter = self._config.adapter
        if (
            type(text) is not str
            or not text.strip()
            or len(text) > adapter.max_text_bytes
            or not _speaker(speaker)
        ):
            raise VoicevoxHttpError()
        try:
            if len(text.encode("utf-8")) > adapter.max_text_bytes:
                raise VoicevoxHttpError()
            target = "/audio_query?" + urlencode({"text": text, "speaker": speaker})
        except (UnicodeError, ValueError, TypeError):
            raise VoicevoxHttpError() from None
        data = await self._request(
            "POST",
            target,
            max_bytes=adapter.max_query_bytes,
            media_type=_JSON_TYPE,
        )
        return _decoded_json(data)

    async def synthesis(self, *, query: dict, speaker: int) -> bytes:
        if not _speaker(speaker):
            raise VoicevoxHttpError()
        try:
            _validate_query(query, self._config.adapter.max_query_bytes)
            body = json.dumps(
                query,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        except Exception:  # noqa: BLE001 - sanitized input regardless of source
            raise VoicevoxHttpError() from None
        if len(body) > self._config.adapter.max_query_bytes:
            raise VoicevoxHttpError()
        return await self._request(
            "POST",
            f"/synthesis?speaker={speaker}",
            content=body,
            max_bytes=self._config.adapter.max_output_bytes,
            media_type=_WAV_TYPE,
        )

    async def close(self) -> None:
        self._closed = True
        for writer in tuple(self._active_writers):
            writer.close()


def _speaker(speaker: object) -> bool:
    return type(speaker) is int and 0 <= speaker < 2**31
