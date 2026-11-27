import asyncio
import hashlib
import io
import threading
import wave
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import pytest
from tsumgi.execution import JobExecutionError, JobRequest
from tsumgi.gateway.audio import audio_semantic_revision
from tsumgi.runtime.providers.faster_whisper import FasterWhisperExecutor

def pcm():
    b=io.BytesIO()
    with wave.open(b,"wb") as w:
        w.setparams((1,2,16000,0,"NONE","")); w.writeframes(bytes(20))
    return b.getvalue()

def case(data):
    ref=dict(uri="tsumgi-audio-input:"+"1"*64,digest="sha256:"+hashlib.sha256(data).hexdigest(),
             size_bytes=len(data),media_type="audio/wav")
    job=JobRequest(job_id="test",capability="audio.transcribe",
       payload=dict(language="ja",response_format="json",audio_input=ref),
       metadata=dict(serving_semantic_revision=audio_semantic_revision("model-stt")))
    return job,SimpleNamespace(read_input=AsyncMock(return_value=data))

async def test_result_text_only():
    job,media=case(pcm()); backend=Mock(); backend.transcribe.return_value="散歩。"
    result=await FasterWhisperExecutor(backend).execute_audio(job,media)
    assert result.text=="散歩。" and not result.artifacts and not result.outputs
    backend.transcribe.assert_called_once()

async def test_invalid_audio_never_reaches_backend():
    job,media=case(pcm()[:-1]); backend=Mock()
    with pytest.raises(JobExecutionError): await FasterWhisperExecutor(backend).execute_audio(job,media)
    backend.transcribe.assert_not_called()

async def test_cached_executor_rejects_backend_stopped_after_decode():
    job, media = case(pcm())
    class Backend:
        alive = True
        def transcribe(self, _):
            self.alive = False
            return "late"
        def close(self):
            self.alive = False
    with pytest.raises(JobExecutionError):
        await FasterWhisperExecutor(Backend()).execute_audio(job, media)

async def test_cancel_closes_backend_and_latches_failure():
    entered,release=threading.Event(),threading.Event()
    class Backend:
        alive=True
        def transcribe(self,_):
            entered.set(); release.wait(3); return "late"
        def close(self):
            self.alive=False
            release.set()
    backend=Backend(); ex=FasterWhisperExecutor(backend); job,media=case(pcm())
    task=asyncio.create_task(ex.execute_audio(job,media))
    assert await asyncio.to_thread(entered.wait,2)
    await ex.cancel(job.job_id)
    with pytest.raises(asyncio.CancelledError): await task
    assert not backend.alive
    with pytest.raises(JobExecutionError): await ex.execute_audio(replace(job,job_id="next"),media)
    assert not backend.alive

async def test_cancel_retains_decode_handle_until_late_thread_exception_observed():
    entered, release = threading.Event(), threading.Event()

    class Backend:
        alive = True
        def transcribe(self, _):
            entered.set()
            release.wait(3)
            raise RuntimeError("late native failure")
        def close(self):
            self.alive = False

    backend = Backend()
    executor = FasterWhisperExecutor(backend)
    job, media = case(pcm())
    task = asyncio.create_task(executor.execute_audio(job, media))
    assert await asyncio.to_thread(entered.wait, 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    decode = executor._decode
    assert decode is not None and not decode.done()

    release.set()
    with pytest.raises(RuntimeError, match="late native failure"):
        await decode
    await asyncio.sleep(0)
    assert executor._decode is None


async def test_revoke_timeout_retains_cleanup_for_retry(monkeypatch):
    entered, release = threading.Event(), threading.Event()

    class Backend:
        def close(self):
            entered.set()
            release.wait(3)

    monkeypatch.setattr("tsumgi.runtime.providers.faster_whisper._CLEANUP_TIMEOUT", 0.02)
    executor = FasterWhisperExecutor(Backend())
    await executor._revoke()
    cleanup = executor._cleanup_task
    assert await asyncio.to_thread(entered.wait, 2)
    assert cleanup is executor._cleanup_task and not cleanup.done()
    release.set()
    await cleanup
    await executor._revoke()
    assert executor._cleanup_task is cleanup


async def test_cached_executor_rejects_late_result_after_stop_generation():
    from tsumgi.runtime.contracts import RuntimeHealth, RuntimeHealthState
    from tsumgi.runtime.providers.faster_whisper_managed import _ReadyBoundExecutor

    entered, release = threading.Event(), threading.Event()

    class Runtime:
        _generation = 1
        async def health(self):
            return RuntimeHealth(state=RuntimeHealthState.READY, ready=True)

    class Backend:
        alive = True
        def transcribe(self, _):
            entered.set()
            release.wait(3)
            return "late result"
        def close(self):
            self.alive = False

    runtime = Runtime()
    executor = _ReadyBoundExecutor(Backend(), runtime)
    job, media = case(pcm())
    task = asyncio.create_task(executor.execute_audio(job, media))
    assert await asyncio.to_thread(entered.wait, 2)
    runtime._generation += 1
    release.set()
    with pytest.raises(JobExecutionError, match="runtime unavailable"):
        await task
