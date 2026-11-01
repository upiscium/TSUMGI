"""Source-only Style-Bert-VITS2 audio executor; no model imports or runner.

The injected async session is a trust boundary, not an implementation of
upstream's synchronous TTSModel.infer. Coroutine cancellation is NOT evidence
that an inference process stopped. A future runner must independently own,
bound, terminate and reap its isolated process before deployment is authorized.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import math
import re
import struct
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ...execution import (
    ArtifactRef,
    AudioJobExecutor,
    AudioJobMedia,
    JobExecutionError,
    JobRequest,
    JobResult,
    ResidencyReport,
)


STYLE_BERT_VITS2_PROVIDER_ID = "style-bert-vits2"
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_REVISION = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64}|sha256:[0-9a-f]{64})\Z")
_MAX_BYTES = 16 * 1024 * 1024
_MESSAGES = {
    "unsupported_capability": "Style-Bert-VITS2 supports audio synthesis only",
    "audio_media_required": "Audio synthesis requires attempt-scoped media",
    "invalid_audio_job": "Audio synthesis request is invalid",
    "audio_voice_mismatch": "Audio voice does not match the configured mapping",
    "audio_semantics_mismatch": "Audio serving semantics do not match configuration",
    "audio_input_exceeded": "Audio synthesis input exceeds the configured limit",
    "audio_job_inflight": "Audio job is already executing",
    "audio_synthesis_timeout": "Audio synthesis timed out",
    "audio_synthesis_failed": "Audio synthesis failed",
    "invalid_audio_output": "Audio synthesis output is invalid",
    "audio_output_exceeded": "Audio synthesis output exceeds the configured limit",
    "audio_stage_timeout": "Audio output staging timed out",
    "audio_stage_failed": "Audio output staging failed",
}


def _error(code: str) -> JobExecutionError:
    return JobExecutionError(code, _MESSAGES[code], retryable=False)


def _config_error() -> ValueError:
    return ValueError("Invalid Style-Bert-VITS2 source configuration")


def _matches(value: object, pattern: re.Pattern[str]) -> bool:
    return type(value) is str and pattern.fullmatch(value) is not None


def _positive_int(value: object, maximum: int) -> bool:
    return type(value) is int and 1 <= value <= maximum


def _finite_positive(value: object, maximum: float) -> bool:
    return (type(value) in (int, float) and 0 < value <= maximum
            and math.isfinite(value))


@dataclass(frozen=True, slots=True)
class StyleBertVits2ModelPin:
    """Operator-selected identity and expected bytes, never filesystem paths.

    This source slice validates pin syntax only. The future session must verify
    the actual artifacts and runtime against every pin before inference.
    """

    model_id: str
    model_filename: str
    model_sha256: str
    config_sha256: str  # config.json
    style_vectors_sha256: str  # style_vectors.npy
    runtime_revision: str
    adapter_revision: str

    def __post_init__(self) -> None:
        if (not _matches(self.model_id, _ID)
                or type(self.model_filename) is not str
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.(?:safetensors|onnx)", self.model_filename) is None
                or ".." in self.model_filename
                or any(not _matches(value, _SHA256) for value in (
                    self.model_sha256, self.config_sha256, self.style_vectors_sha256,
                ))
                or any(not _matches(value, _REVISION) for value in (
                    self.runtime_revision, self.adapter_revision,
                ))):
            raise _config_error()


@dataclass(frozen=True, slots=True)
class StyleBertVits2Voice:
    """Exact public alias/IDs to native speaker integer and style string."""

    alias: str
    speaker_id: int
    style_id: str
    style: str

    def __post_init__(self) -> None:
        if (not _matches(self.alias, _ID) or not _matches(self.style_id, _ID)
                or type(self.speaker_id) is not int
                or not 0 <= self.speaker_id <= 2**31 - 1
                or type(self.style) is not str or not 1 <= len(self.style) <= 128
                or self.style != self.style.strip()
                or any(ord(char) < 32 or ord(char) == 127 for char in self.style)):
            raise _config_error()
        try:
            self.style.encode("utf-8")
        except UnicodeEncodeError:
            raise _config_error() from None


@dataclass(frozen=True, slots=True)
class StyleBertVits2ProviderConfig:
    model: StyleBertVits2ModelPin
    voices: tuple[StyleBertVits2Voice, ...]
    required_semantic_revision: str
    max_text_bytes: int = 8192
    max_output_bytes: int = _MAX_BYTES
    max_duration_seconds: float = 120.0
    synthesis_timeout_seconds: float = 60.0
    stage_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        # No mutable mapping/list or nested unvalidated configuration survives.
        if (type(self.model) is not StyleBertVits2ModelPin
                or type(self.voices) is not tuple or not 1 <= len(self.voices) <= 128
                or any(type(voice) is not StyleBertVits2Voice for voice in self.voices)
                or len({voice.alias for voice in self.voices}) != len(self.voices)
                or not _positive_int(self.max_text_bytes, 8192)
                or not _positive_int(self.max_output_bytes, _MAX_BYTES)
                or self.max_output_bytes < 46
                or not _finite_positive(self.max_duration_seconds, 120)
                or not _finite_positive(self.synthesis_timeout_seconds, 300)
                or not _finite_positive(self.stage_timeout_seconds, 300)
                or not _matches(
                    self.required_semantic_revision, re.compile(r"sha256:[0-9a-f]{64}\Z"),
                )):
            raise _config_error()


@runtime_checkable
class StyleBertVits2AsyncModelApi(Protocol):
    """Genuinely async IPC to an owned isolated inference subprocess.

    Implementations MUST NOT call synchronous TTSModel.infer on the event loop
    or use to_thread as a substitute for cancellable process ownership. They
    must verify all model pins, cap IPC output before buffering, propagate task
    cancellation, and independently enforce process kill/reap on timeout/cancel.
    No real implementation is supplied or authorized by this source slice.
    """

    async def synthesize(
        self, *, model: StyleBertVits2ModelPin, text: str, speaker_id: int, style: str,
    ) -> bytes: ...


def _wav_metadata(data: object, config: StyleBertVits2ProviderConfig) -> dict[str, int | float]:
    if type(data) is not bytes:
        raise _error("invalid_audio_output")
    if len(data) > config.max_output_bytes:
        raise _error("audio_output_exceeded")
    # Match the speech gateway's deliberately narrow, canonical PCM16 subset.
    # Reject ancillary chunks too: they can carry arbitrary content/paths.
    if (len(data) < 46 or data[:4] != b"RIFF" or data[8:16] != b"WAVEfmt "
            or int.from_bytes(data[4:8], "little") != len(data) - 8
            or int.from_bytes(data[16:20], "little") != 16
            or data[36:40] != b"data"):
        raise _error("invalid_audio_output")
    codec, channels, rate, byte_rate, alignment, bits = struct.unpack_from("<HHIIHH", data, 20)
    size = int.from_bytes(data[40:44], "little")
    if (codec != 1 or channels not in (1, 2) or bits != 16
            or not 8000 <= rate <= 48000 or alignment != channels * 2
            or byte_rate != rate * alignment or size != len(data) - 44
            or size == 0 or size % alignment):
        raise _error("invalid_audio_output")
    frames = size // alignment
    if frames > rate * config.max_duration_seconds:
        raise _error("audio_output_exceeded")
    return {"sample_rate_hz": rate, "channels": channels, "sample_frames": frames,
            "duration_seconds": frames / rate, "output_bytes": len(data)}


class StyleBertVits2Executor(AudioJobExecutor):
    """Independent injected-session executor, intentionally not registered."""

    capabilities = frozenset({"audio.synthesize"})
    # No serving_features until a reviewed ManagedRuntime verifies model
    # artifacts and process readiness. A bare executor.factory is not ready.

    def __init__(
        self, *, config: StyleBertVits2ProviderConfig, api: StyleBertVits2AsyncModelApi,
    ) -> None:
        if type(config) is not StyleBertVits2ProviderConfig:
            raise _config_error()
        # Reject synchronous entrypoints BEFORE calling them. This structural
        # check cannot prove an async function actually yields or owns a process.
        if not inspect.iscoroutinefunction(getattr(api, "synthesize", None)):
            raise ValueError("Style-Bert-VITS2 requires an async model session")
        self._config = config
        self._api = api
        self._inflight: dict[str, asyncio.Task[JobResult]] = {}

    async def execute(self, job: JobRequest) -> JobResult:
        if job.capability != "audio.synthesize":
            raise _error("unsupported_capability")
        raise _error("audio_media_required")

    def _request(self, job: JobRequest) -> tuple[str, StyleBertVits2Voice]:
        if job.capability != "audio.synthesize":
            raise _error("unsupported_capability")
        if set(job.payload) != {"input", "voice", "response_format"}:
            raise _error("invalid_audio_job")
        if (type(job.payload["response_format"]) is not str
                or job.payload["response_format"] != "wav"):
            raise _error("invalid_audio_job")
        revision = job.metadata.get("serving_semantic_revision")
        if (type(revision) is not str
                or revision != self._config.required_semantic_revision):
            raise _error("audio_semantics_mismatch")
        raw_voice = job.payload["voice"]
        if (type(raw_voice) is not dict
                or set(raw_voice) != {"alias", "speaker_id", "style_id"}
                or any(type(value) is not str for value in raw_voice.values())):
            raise _error("invalid_audio_job")
        voice = next((item for item in self._config.voices
                      if item.alias == raw_voice["alias"]), None)
        if (voice is None or raw_voice["speaker_id"] != str(voice.speaker_id)
                or raw_voice["style_id"] != voice.style_id):
            raise _error("audio_voice_mismatch")
        text = job.payload["input"]
        if type(text) is not str or not text:
            raise _error("invalid_audio_job")
        if len(text) > self._config.max_text_bytes:
            raise _error("audio_input_exceeded")
        try:
            size = len(text.encode("utf-8"))
        except UnicodeEncodeError:
            raise _error("invalid_audio_job") from None
        if size > self._config.max_text_bytes:
            raise _error("audio_input_exceeded")
        if not text.strip():
            raise _error("invalid_audio_job")
        return text, voice

    async def execute_audio(self, job: JobRequest, media: AudioJobMedia) -> JobResult:
        text, voice = self._request(job)
        if job.job_id in self._inflight:
            raise _error("audio_job_inflight")
        task = asyncio.create_task(self._synthesize(text, voice, media))
        self._inflight[job.job_id] = task
        try:
            return await task
        finally:
            self._inflight.pop(job.job_id, None)

    async def _synthesize(
        self, text: str, voice: StyleBertVits2Voice, media: AudioJobMedia,
    ) -> JobResult:
        try:
            async with asyncio.timeout(self._config.synthesis_timeout_seconds):
                wav = await self._api.synthesize(
                    model=self._config.model, text=text,
                    speaker_id=voice.speaker_id, style=voice.style,
                )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            raise _error("audio_synthesis_timeout") from None
        except Exception:
            raise _error("audio_synthesis_failed") from None
        metadata = _wav_metadata(wav, self._config)
        try:
            async with asyncio.timeout(self._config.stage_timeout_seconds):
                ref = await media.stage_output(wav, max_bytes=self._config.max_output_bytes)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            raise _error("audio_stage_timeout") from None
        except Exception:
            # The real Worker's media session retains a sticky transport fault;
            # it still abandons uncertain transfers, not terminally fails/retries.
            raise _error("audio_stage_failed") from None
        if (type(ref) is not ArtifactRef or type(ref.uri) is not str
                or re.fullmatch(r"tsumgi-audio:[0-9a-f]{64}", ref.uri) is None
                or ref.media_type != "audio/wav" or type(ref.size_bytes) is not int
                or ref.size_bytes != len(wav)
                or ref.digest != "sha256:" + hashlib.sha256(wav).hexdigest()
                or ref.name is not None or ref.metadata):
            raise _error("invalid_audio_output")
        return JobResult(artifacts=(ref,), metadata=metadata,
                         metrics={"audio_duration_seconds": metadata["duration_seconds"]})

    async def cancel(self, job_id: str) -> None:
        # Request task cancellation only. Do not wait indefinitely for an
        # untrusted session or report that a synchronous inference was killed.
        task = self._inflight.get(job_id)
        if task is not None and not task.done():
            task.cancel()

    async def residency(self) -> ResidencyReport:
        # No model has been loaded/observed by this source-only executor.
        return ResidencyReport()
