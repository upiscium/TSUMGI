"""Isolated VOICEVOX TTS source adapter; fake-engine tests, no runtime registration.

The source-only adapter expects a reviewed engine session to be injected. It
never launches VOICEVOX, downloads a model, opens sockets or attests readiness.
The Worker owns artifact and claim/lease/epoch authority through AudioJobMedia.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
import re
import struct
from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from uuid import UUID

from ...execution import (
    ArtifactRef, AudioJobExecutor, AudioJobMedia, JobExecutionError,
    JobRequest, JobResult, ResidencyReport,
)


VOICEVOX_PROVIDER_ID = "voicevox"
_SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z")
_MAX_WAV = 16 * 1024 * 1024
_ERRORS = {
    "unsupported_capability": "VOICEVOX supports audio synthesis only",
    "media_required": "Audio synthesis requires attempt-scoped media",
    "invalid_request": "VOICEVOX audio request is invalid",
    "voice_mismatch": "VOICEVOX voice mapping is invalid",
    "serving_mismatch": "VOICEVOX serving identity is invalid",
    "text_exceeded": "VOICEVOX text exceeds the configured limit",
    "already_running": "VOICEVOX job is already running",
    "engine_unavailable": "VOICEVOX engine is unavailable",
    "engine_identity_mismatch": "VOICEVOX engine identity is invalid",
    "voice_unavailable": "VOICEVOX reviewed voice is unavailable",
    "synthesis_timeout": "VOICEVOX synthesis timed out",
    "invalid_query": "VOICEVOX engine query is invalid",
    "invalid_output": "VOICEVOX audio output is invalid",
    "output_exceeded": "VOICEVOX audio output exceeds the configured limit",
    "stage_failed": "VOICEVOX audio output staging failed",
}


def _error(code: str) -> JobExecutionError:
    return JobExecutionError(code, _ERRORS[code], retryable=False)


def _invalid_config() -> ValueError:
    return ValueError("VOICEVOX adapter configuration is invalid")


def _uuid(text: object) -> bool:
    if type(text) is not str:
        return False
    try:
        uid = UUID(text)
    except (ValueError, AttributeError):
        return False
    return str(uid) == text and uid.int != 0


def _id(text: object) -> bool:
    return type(text) is str and _ID.fullmatch(text) is not None


@dataclass(frozen=True, slots=True)
class VoicevoxVoicePin:
    """One exact public alias → engine speaker UUID and global style ID."""

    alias: str
    speaker_uuid: str
    style_id: int
    license_id: str
    attribution: str

    def __post_init__(self) -> None:
        if (
            not _id(self.alias) or not _uuid(self.speaker_uuid)
            or type(self.style_id) is not int or not 0 <= self.style_id < 2**31
            or not _id(self.license_id)
            or type(self.attribution) is not str
            or not 1 <= len(self.attribution) <= 1024
            or not self.attribution.strip()
            or any(ord(c) < 32 or ord(c) == 127 for c in self.attribution)
        ):
            raise _invalid_config()
        try:
            self.attribution.encode("utf-8")
        except UnicodeEncodeError:
            raise _invalid_config() from None


@dataclass(frozen=True, slots=True)
class VoicevoxAdapterConfig:
    """Operator-declared pins only; no downloaded or verified model artifact."""

    engine_version: str
    engine_artifact_sha256: str
    adapter_artifact_sha256: str
    model_artifact_sha256: str
    semantic_revision: str
    voices: tuple[VoicevoxVoicePin, ...]
    max_text_bytes: int = 8192
    max_query_bytes: int = 256 * 1024
    max_output_bytes: int = _MAX_WAV
    max_duration_seconds: float = 120.0
    synthesis_timeout_seconds: float = 60.0
    stage_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if (
            type(self.engine_version) is not str
            or _VERSION.fullmatch(self.engine_version) is None
            or any(type(x) is not str or _SHA.fullmatch(x) is None for x in (
                self.engine_artifact_sha256, self.adapter_artifact_sha256,
                self.model_artifact_sha256, self.semantic_revision,
            ))
            or type(self.voices) is not tuple or not 1 <= len(self.voices) <= 64
            or any(type(x) is not VoicevoxVoicePin for x in self.voices)
            or len({x.alias for x in self.voices}) != len(self.voices)
            or len({(x.speaker_uuid, x.style_id) for x in self.voices}) != len(self.voices)
            or any(type(x) is not int or not a <= x <= b for x, a, b in (
                (self.max_text_bytes, 1, 8192),
                (self.max_query_bytes, 64, 256 * 1024),
                (self.max_output_bytes, 46, _MAX_WAV),
            ))
            or any(type(v) not in (int, float) or not math.isfinite(v) or not 0 < v <= m
                   for v, m in (
                       (self.max_duration_seconds, 120),
                       (self.synthesis_timeout_seconds, 300),
                       (self.stage_timeout_seconds, 300),
                   ))
        ):
            raise _invalid_config()


@runtime_checkable
class VoicevoxEngineApi(Protocol):
    """Trusted async engine session; no real HTTP or lifecycle here.

    A future implementation must bind to a reviewed loopback/private endpoint
    and disable native raw text query logging; /audio_query uses native POST
    with text and speaker query parameters. These mockable methods are not
    runtime readiness or authenticity evidence.
    """

    async def version(self) -> str: ...
    async def speakers(self) -> object: ...
    async def audio_query(self, *, text: str, speaker: int) -> object: ...
    async def synthesis(self, *, query: dict, speaker: int) -> bytes: ...


def _review_speakers(value: object, voice: VoicevoxVoicePin) -> bool:
    if type(value) is not list or not 1 <= len(value) <= 1024:
        return False
    matches = 0
    selectors = 0
    for speaker in value:
        if type(speaker) is not dict:
            return False
        uuid = speaker.get("speaker_uuid")
        styles = speaker.get("styles")
        if not _uuid(uuid) or type(styles) is not list or not 1 <= len(styles) <= 256:
            return False
        style_ids = []
        for style in styles:
            if (type(style) is not dict or type(style.get("id")) is not int
                    or style["id"] < 0 or style["id"] >= 2**31):
                return False
            style_ids.append(style["id"])
        if len(style_ids) != len(set(style_ids)):
            return False
        if voice.style_id in style_ids:
            # The actual /audio_query and /synthesis selector is the global
            # numeric style, NOT the speaker UUID. It must be unambiguous.
            selectors += 1
            if uuid != voice.speaker_uuid:
                return False
        if uuid == voice.speaker_uuid:
            if voice.style_id not in style_ids:
                return False
            matches += 1
    return matches == 1 and selectors == 1


def _validate_query(value: object, max_bytes: int) -> dict:
    """Reject exponential/shared JSON expansion before bounded serialization.

    The injected source API is trusted to cap HTTP bytes separately, but a
    malformed or pathological decoded object must not cause an unbounded
    synchronous json.dumps allocation on the Worker heartbeat event loop.
    """
    if (type(value) is not dict or type(value.get("accent_phrases")) is not list):
        raise _error("invalid_query")
    remaining = 2048
    ancestors: set[int] = set()

    def check(item: object, depth: int) -> None:
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > 10:
            raise _error("invalid_query")
        if type(item) is str:
            if len(item) > max_bytes:
                raise _error("invalid_query")
            try:
                if len(item.encode("utf-8")) > max_bytes:
                    raise _error("invalid_query")
            except UnicodeEncodeError:
                raise _error("invalid_query") from None
            return
        if item is None or type(item) is bool:
            return
        if type(item) is int:
            if abs(item) > 2**63 - 1:
                raise _error("invalid_query")
            return
        if type(item) is float:
            if not math.isfinite(item):
                raise _error("invalid_query")
            return
        if type(item) not in (dict, list):
            raise _error("invalid_query")
        if len(item) > 256 or id(item) in ancestors:
            raise _error("invalid_query")
        ancestors.add(id(item))
        try:
            if type(item) is dict:
                for key, child in item.items():
                    if type(key) is not str:
                        raise _error("invalid_query")
                    check(key, depth + 1)
                    check(child, depth + 1)
            else:
                for child in item:
                    check(child, depth + 1)
        finally:
            ancestors.remove(id(item))

    check(value, 0)
    # iterencode emits at most one bounded string per chunk because the
    # traversal has already checked all leaf byte sizes and node counts.
    counted = 0
    try:
        for chunk in json.JSONEncoder(
            ensure_ascii=False, allow_nan=False, separators=(",", ":"),
        ).iterencode(value):
            counted += len(chunk.encode("utf-8"))
            if counted > max_bytes:
                raise _error("invalid_query")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise _error("invalid_query") from None
    if counted < 2:
        raise _error("invalid_query")
    return value


def _wav_meta(value: object, config: VoicevoxAdapterConfig) -> dict[str, float | int]:
    # The Gateway accepts only the canonical RIFF/WAVE 44-byte PCM16 subset.
    if type(value) is not bytes:
        raise _error("invalid_output")
    if len(value) > config.max_output_bytes:
        raise _error("output_exceeded")
    if (len(value) < 46 or value[:4] != b"RIFF"
            or value[8:16] != b"WAVEfmt "
            or int.from_bytes(value[4:8], "little") != len(value) - 8
            or int.from_bytes(value[16:20], "little") != 16
            or value[36:40] != b"data"):
        raise _error("invalid_output")
    codec, channels, rate, byte_rate, align, bits = struct.unpack_from("<HHIIHH", value, 20)
    payload = int.from_bytes(value[40:44], "little")
    if (codec != 1 or channels not in (1, 2) or bits != 16
            or not 8000 <= rate <= 48000 or align != channels * 2
            or byte_rate != rate * align or payload != len(value) - 44
            or payload == 0 or payload % align):
        raise _error("invalid_output")
    frames = payload // align
    if frames > rate * config.max_duration_seconds:
        raise _error("output_exceeded")
    return {"sample_rate_hz": rate, "channels": channels,
            "sample_frames": frames, "duration_seconds": frames / rate,
            "output_bytes": len(value)}


class VoicevoxExecutor(AudioJobExecutor):
    """Bounded source adapter with an injected async VOICEVOX mock/session.

    No package/engine/model has been verified, thus this executor must not be
    registered or advertised as ready before a separately reviewed runtime.
    """

    capabilities = frozenset({"audio.synthesize"})
    # Deliberately do NOT expose serving_features before a separately reviewed
    # ManagedRuntime provides actual engine/voice readiness. The daemon must
    # reject a bare executor.factory attempting to advertise audio.

    def __init__(self, *, config: VoicevoxAdapterConfig, api: VoicevoxEngineApi) -> None:
        if type(config) is not VoicevoxAdapterConfig:
            raise _invalid_config()
        for method in ("version", "speakers", "audio_query", "synthesis"):
            if not inspect.iscoroutinefunction(getattr(api, method, None)):
                raise _invalid_config()
        self._config = config
        self._api = api
        self._inflight: dict[str, asyncio.Task[JobResult]] = {}

    async def execute(self, job: JobRequest) -> JobResult:
        if job.capability != "audio.synthesize":
            raise _error("unsupported_capability")
        raise _error("media_required")

    def _request(self, job: JobRequest) -> tuple[str, VoicevoxVoicePin]:
        if job.capability != "audio.synthesize":
            raise _error("unsupported_capability")
        if (set(job.payload) != {"input", "voice", "response_format"}
                or job.payload.get("response_format") != "wav"
                or type(job.payload.get("response_format")) is not str):
            raise _error("invalid_request")
        voice = job.payload["voice"]
        if (type(voice) is not dict or set(voice) != {"alias", "speaker_id", "style_id"}
                or any(type(v) is not str for v in voice.values())):
            raise _error("invalid_request")
        pin = next((v for v in self._config.voices if v.alias == voice["alias"]), None)
        if (pin is None or voice["speaker_id"] != pin.speaker_uuid
                or voice["style_id"] != str(pin.style_id)):
            raise _error("voice_mismatch")
        if job.metadata.get("serving_semantic_revision") != self._config.semantic_revision:
            raise _error("serving_mismatch")
        text = job.payload["input"]
        if type(text) is not str or not text or not text.strip():
            raise _error("invalid_request")
        if len(text) > self._config.max_text_bytes:
            raise _error("text_exceeded")
        try:
            size = len(text.encode("utf-8"))
        except UnicodeEncodeError:
            raise _error("invalid_request") from None
        if size > self._config.max_text_bytes:
            raise _error("text_exceeded")
        return text, pin

    async def execute_audio(self, job: JobRequest, media: AudioJobMedia) -> JobResult:
        text, pin = self._request(job)
        if job.job_id in self._inflight:
            raise _error("already_running")
        task = asyncio.create_task(self._synthesize(text, pin, media))
        self._inflight[job.job_id] = task
        try:
            return await task
        finally:
            self._inflight.pop(job.job_id, None)

    async def _engine_call(self, method: str, **kwargs):
        """The engine is untrusted for public errors, including exception types."""
        try:
            return await getattr(self._api, method)(**kwargs)
        except asyncio.CancelledError:
            raise
        except Exception:
            # An injected engine can raise JobExecutionError(retryable=True)
            # with raw source text. Never forward it to the Job result.
            raise _error("engine_unavailable") from None

    async def _synthesize(self, text: str, voice: VoicevoxVoicePin, media: AudioJobMedia) -> JobResult:
        try:
            async with asyncio.timeout(self._config.synthesis_timeout_seconds):
                version = await self._engine_call("version")
                if type(version) is not str or version != self._config.engine_version:
                    raise _error("engine_identity_mismatch")
                speakers = await self._engine_call("speakers")
                if not _review_speakers(speakers, voice):
                    raise _error("voice_unavailable")
                query = _validate_query(
                    await self._engine_call("audio_query", text=text, speaker=voice.style_id),
                    self._config.max_query_bytes,
                )
                wav = await self._engine_call(
                    "synthesis", query=query, speaker=voice.style_id,
                )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            raise _error("synthesis_timeout") from None
        except JobExecutionError:
            raise
        except Exception:
            raise _error("engine_unavailable") from None
        metrics = _wav_meta(wav, self._config)
        try:
            async with asyncio.timeout(self._config.stage_timeout_seconds):
                ref = await media.stage_output(wav, max_bytes=self._config.max_output_bytes)
        except asyncio.CancelledError:
            raise
        except Exception:
            # The Worker session keeps a sticky media fault, so an uncertain
            # upload is abandoned, not retried/terminally acknowledged.
            raise _error("stage_failed") from None
        if (type(ref) is not ArtifactRef
                or re.fullmatch(r"tsumgi-audio:[0-9a-f]{64}", ref.uri) is None
                or ref.media_type != "audio/wav"
                or type(ref.size_bytes) is not int or ref.size_bytes != len(wav)
                or ref.digest != "sha256:" + hashlib.sha256(wav).hexdigest()
                or ref.name is not None or ref.metadata):
            raise _error("invalid_output")
        return JobResult(
            artifacts=(ref,), metadata=metrics,
            metrics={"audio_duration_seconds": metrics["duration_seconds"]},
        )

    async def cancel(self, job_id: str) -> None:
        task = self._inflight.get(job_id)
        if task is not None and not task.done():
            task.cancel()

    async def residency(self) -> ResidencyReport:
        # No engine/model has been observed or started by this source slice.
        return ResidencyReport()
