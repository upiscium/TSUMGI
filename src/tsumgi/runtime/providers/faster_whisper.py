"""Local-only Faster-Whisper adapter; native work belongs to an owned child.

Mirrors parakeet.py (kept untouched). Request contract: Japanese, JSON, canonical
mono 16 kHz PCM16 WAV, at most 30 s / 1 MiB; text-only result, UTF-8 capped."""
from __future__ import annotations
import asyncio
import hashlib
import io
import re
import wave
from typing import Protocol
from ...audio.pcm16 import validate_pcm16_wav
from ...execution import AudioJobExecutor, AudioJobMedia, JobExecutionError, JobRequest, JobResult, ResidencyReport
from ...gateway.audio import audio_semantic_revision

_WIRE=1<<20
_TEXT=16<<10
_SECONDS=30
_SEMANTIC=audio_semantic_revision("model-stt")
_EXECUTE_TIMEOUT=90
_CLEANUP_TIMEOUT=5

def valid_transcript(text,*,limit=_TEXT):
    """Non-empty UTF-8 (no lone surrogates), bounded, no control characters but whitespace."""
    if type(text) is not str or not text.strip() or len(text)>limit: return False
    try:
        if len(text.encode("utf-8"))>limit: return False
    except UnicodeError:
        return False
    return all(c in "\n\t" or not (ord(c)<32 or 127<=ord(c)<160) for c in text)

class ResidentDecoder(Protocol):
    def transcribe(self,wav:bytes)->str: ...
    def close(self)->None: ...

def _error(code):
    return JobExecutionError(code,"Faster-Whisper transcription rejected",retryable=False)

class FasterWhisperExecutor(AudioJobExecutor):
    capabilities=frozenset({"audio.transcribe"})
    def __init__(self,backend:ResidentDecoder):
        self._backend=backend
        self._job_id=None
        self._task=None
        self._decode=None
        self._closed=False
        self._failed=False
        self._lock=asyncio.Lock()
        self._cleanup_task=None

    @staticmethod
    def _observe(task):
        if not task.cancelled():
            task.exception()

    async def execute(self,job:JobRequest)->JobResult:
        raise _error("media_required")

    async def execute_audio(self,job:JobRequest,media:AudioJobMedia)->JobResult:
        if (self._closed or self._failed or not getattr(self._backend,"alive",True)):
            raise _error("runtime_unavailable")
        if (job.capability!="audio.transcribe" or set(job.payload)!={"language","response_format","audio_input"} or
            job.payload["language"]!="ja" or job.payload["response_format"]!="json" or
            job.metadata.get("serving_semantic_revision")!=_SEMANTIC):
            raise _error("invalid_request")
        ref=job.payload["audio_input"]
        if (type(ref) is not dict or set(ref)!={"uri","digest","size_bytes","media_type"} or
            type(ref["uri"]) is not str or not re.fullmatch(r"tsumgi-audio-input:[0-9a-f]{64}",ref["uri"]) or
            type(ref["digest"]) is not str or not re.fullmatch(r"sha256:[0-9a-f]{64}",ref["digest"]) or
            ref["media_type"]!="audio/wav" or type(ref["size_bytes"]) is not int or not 44<=ref["size_bytes"]<=_WIRE):
            raise _error("invalid_receipt")
        async with self._lock:
            if self._task is not None: raise _error("already_running")
            self._job_id=job.job_id
            self._task=asyncio.create_task(self._run(media,ref.copy()))
            task=self._task
        try:
            async with asyncio.timeout(_EXECUTE_TIMEOUT):
                result = await task
                if self._closed or self._failed or not getattr(self._backend, "alive", True):
                    raise _error("runtime_unavailable")
                return result
        except TimeoutError:
            task.cancel()
            await self._revoke()
            await asyncio.gather(task, return_exceptions=True)
            raise _error("transcription_timeout") from None
        except asyncio.CancelledError:
            task.cancel()
            await self._revoke()
            await asyncio.gather(task, return_exceptions=True)
            raise
        finally:
            async with self._lock:
                if self._task is task:
                    self._task=None; self._job_id=None

    async def _revoke(self):
        # Revoke admission synchronously before starting any potentially slow
        # thread cleanup, so no concurrent caller can reuse this executor.
        self._failed=True
        if self._cleanup_task is None:
            close=getattr(self._backend,"close",None)
            if close is not None:
                self._cleanup_task=asyncio.create_task(asyncio.to_thread(close))
            else:
                self._cleanup_task=asyncio.create_task(asyncio.sleep(0))
        try:
            async with asyncio.timeout(_CLEANUP_TIMEOUT):
                await asyncio.shield(self._cleanup_task)
        except TimeoutError:
            self._closed=True
            self._cleanup_task.add_done_callback(self._observe)
        except Exception:
            self._closed=True
            self._cleanup_task.add_done_callback(self._observe)
        except asyncio.CancelledError:
            # Preserve cancellation while leaving the cleanup task alive and
            # observed; the caller can retry/await close without losing it.
            self._cleanup_task.add_done_callback(self._observe)
            raise

    async def _run(self,media,ref):
        try:
            wav=await media.read_input(max_bytes=_WIRE)
            if type(wav) is not bytes or len(wav)!=ref["size_bytes"] or "sha256:"+hashlib.sha256(wav).hexdigest()!=ref["digest"]:
                raise ValueError()
            validate_pcm16_wav(wav,max_wire_bytes=_WIRE,max_decoded_bytes=32000*_SECONDS,max_duration_seconds=_SECONDS)
            with wave.open(io.BytesIO(wav)) as f:
                if f.getnchannels()!=1 or f.getframerate()!=16000: raise ValueError()
        except Exception:
            raise _error("invalid_input") from None
        native=asyncio.create_task(asyncio.to_thread(self._backend.transcribe,wav))
        self._decode=native
        native.add_done_callback(self._observe)
        native.add_done_callback(self._clear_decode)
        try:
            text=await asyncio.shield(native)
            if (self._closed or self._failed or not getattr(self._backend, "alive", True) or
                    not valid_transcript(text)):
                raise ValueError()
        except asyncio.CancelledError:
            raise
        except Exception:
            self._failed=True
            await self._revoke()
            raise _error("transcription_failed") from None
        return JobResult(text=text)

    def _clear_decode(self, task):
        if self._decode is task:
            self._decode = None

    async def cancel(self,job_id):
        async with self._lock:
            task=self._task if self._job_id==job_id else None
        if task is not None:
            self._failed=True
            task.cancel()
            await self._revoke()
            await asyncio.gather(task, return_exceptions=True)

    async def close(self):
        self._closed=True
        await self._revoke()

    async def residency(self):
        return ResidencyReport()
