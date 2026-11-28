"""Provider contract negatives; decoder calls are mocks, never native inference."""
from dataclasses import replace
import io
from types import SimpleNamespace
from unittest.mock import Mock
import wave

import pytest

from tsumgi.execution import JobExecutionError
from tsumgi.runtime.providers.faster_whisper import FasterWhisperExecutor
from test_faster_whisper import case, pcm


def backend(text="模擬"):
    return SimpleNamespace(alive=True, transcribe=Mock(return_value=text), close=Mock())


@pytest.mark.parametrize("change", [
    {"language": "en"}, {"response_format": "text"}, {"prompt": "not allowed"},
])
async def test_unreviewed_request_has_no_native_effect(change):
    job, media = case(pcm())
    job = replace(job, payload={**job.payload, **change})
    child = backend()
    with pytest.raises(JobExecutionError) as caught:
        await FasterWhisperExecutor(child).execute_audio(job, media)
    assert caught.value.code == "invalid_request"
    child.transcribe.assert_not_called()


@pytest.mark.parametrize("case_name", ["digest", "size", "uri", "media_type", "extra"])
async def test_receipt_must_match_exact_bytes(case_name):
    job, media = case(pcm())
    receipt = dict(job.payload["audio_input"])
    if case_name == "digest":
        receipt["digest"] = "sha256:" + "0" * 64
    elif case_name == "size":
        receipt["size_bytes"] += 2
    elif case_name == "uri":
        receipt["uri"] = "file:///private/input"
    elif case_name == "media_type":
        receipt["media_type"] = "audio/flac"
    else:
        receipt["private_path"] = "/private/input"
    job = replace(job, payload={**job.payload, "audio_input": receipt})
    child = backend()
    with pytest.raises(JobExecutionError):
        await FasterWhisperExecutor(child).execute_audio(job, media)
    child.transcribe.assert_not_called()


@pytest.mark.parametrize("channels,rate,width", [(2, 16000, 2), (1, 44100, 2), (1, 16000, 1)])
async def test_non_profile_pcm_is_not_silently_converted(channels, rate, width):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setparams((channels, width, rate, 0, "NONE", ""))
        wav.writeframes(bytes(40))
    job, media = case(buffer.getvalue())
    child = backend()
    with pytest.raises(JobExecutionError):
        await FasterWhisperExecutor(child).execute_audio(job, media)
    child.transcribe.assert_not_called()


@pytest.mark.parametrize("text", [None, "\ud800", "あ" * 5462])
async def test_invalid_text_is_redacted_and_instance_is_revoked(text):
    job, media = case(pcm())
    child = backend(text)
    executor = FasterWhisperExecutor(child)
    with pytest.raises(JobExecutionError) as caught:
        await executor.execute_audio(job, media)
    assert str(caught.value) == "Faster-Whisper transcription rejected"
    child.close.assert_called_once()
    with pytest.raises(JobExecutionError):
        await executor.execute_audio(replace(job, job_id="next"), media)
    child.transcribe.assert_called_once()


@pytest.mark.parametrize("text", ["a\x00b", "a\x1bb", "a\x7fb", "a\x85b"])
async def test_control_characters_in_transcript_are_rejected(text):
    job, media = case(pcm())
    child = backend(text)
    with pytest.raises(JobExecutionError):
        await FasterWhisperExecutor(child).execute_audio(job, media)
    child.close.assert_called_once()


async def test_newline_and_tab_are_allowed_and_text_is_returned_verbatim():
    job, media = case(pcm())
    result = await FasterWhisperExecutor(backend("一行目\n二行目\t終わり")).execute_audio(job, media)
    assert result.text == "一行目\n二行目\t終わり"


@pytest.mark.parametrize("field,value", [
    ("model", "other-model"), ("beam_size", 1), ("temperature", 0.5), ("task", "translate"),
    ("compute_type", "float16"), ("device", "cpu"), ("vad_filter", True), ("initial_prompt", "x"),
])
async def test_client_cannot_override_decoding_or_model(field, value):
    job, media = case(pcm())
    job = replace(job, payload={**job.payload, field: value})
    child = backend()
    with pytest.raises(JobExecutionError) as caught:
        await FasterWhisperExecutor(child).execute_audio(job, media)
    assert caught.value.code == "invalid_request"
    child.transcribe.assert_not_called()


async def test_stale_serving_semantic_revision_is_rejected():
    job, media = case(pcm())
    job = replace(job, metadata={"serving_semantic_revision": "sha256:" + "0" * 64})
    child = backend()
    with pytest.raises(JobExecutionError):
        await FasterWhisperExecutor(child).execute_audio(job, media)
    child.transcribe.assert_not_called()


@pytest.mark.parametrize("seconds,ok", [(30, True), (31, False)])
async def test_duration_limit_is_enforced_before_decode(seconds, ok):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", ""))
        wav.writeframes(bytes(32000 * seconds))
    job, media = case(buffer.getvalue())
    child = backend()
    if ok:
        await FasterWhisperExecutor(child).execute_audio(job, media)
        child.transcribe.assert_called_once()
    else:
        with pytest.raises(JobExecutionError):
            await FasterWhisperExecutor(child).execute_audio(job, media)
        child.transcribe.assert_not_called()


async def test_backend_failure_is_explicit_and_never_falls_back():
    job, media = case(pcm())
    child = SimpleNamespace(alive=True, transcribe=Mock(side_effect=RuntimeError("/private/path boom")), close=Mock())
    executor = FasterWhisperExecutor(child)
    with pytest.raises(JobExecutionError) as caught:
        await executor.execute_audio(job, media)
    assert caught.value.code == "transcription_failed" and not caught.value.retryable
    assert "private" not in str(caught.value)
    with pytest.raises(JobExecutionError) as again:
        await executor.execute_audio(replace(job, job_id="next"), media)
    assert again.value.code == "runtime_unavailable"
    child.transcribe.assert_called_once()


@pytest.mark.parametrize("text", ["", "  "])
async def test_no_speech_clip_returns_an_empty_transcript_without_revoking(text):
    job, media = case(pcm())
    child = backend(text)
    executor = FasterWhisperExecutor(child)
    result = await executor.execute_audio(job, media)
    assert result.text == text
    child.close.assert_not_called()
    again = await executor.execute_audio(replace(job, job_id="next"), media)
    assert again.text == text
