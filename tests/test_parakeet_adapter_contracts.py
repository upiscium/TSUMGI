"""Provider contract negatives; decoder calls are mocks, never native inference."""
from dataclasses import replace
import io
from types import SimpleNamespace
from unittest.mock import Mock
import wave

import pytest

from tsumgi.execution import JobExecutionError
from tsumgi.runtime.providers.parakeet import ParakeetExecutor
from test_parakeet import case, pcm


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
        await ParakeetExecutor(child).execute_audio(job, media)
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
        await ParakeetExecutor(child).execute_audio(job, media)
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
        await ParakeetExecutor(child).execute_audio(job, media)
    child.transcribe.assert_not_called()


@pytest.mark.parametrize("text", [None, "", "  ", "\ud800", "あ" * 5462])
async def test_invalid_text_is_redacted_and_instance_is_revoked(text):
    job, media = case(pcm())
    child = backend(text)
    executor = ParakeetExecutor(child)
    with pytest.raises(JobExecutionError) as caught:
        await executor.execute_audio(job, media)
    assert str(caught.value) == "Parakeet transcription rejected"
    child.close.assert_called_once()
    with pytest.raises(JobExecutionError):
        await executor.execute_audio(replace(job, job_id="next"), media)
    child.transcribe.assert_called_once()
