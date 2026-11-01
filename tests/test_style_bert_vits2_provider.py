"""#177 pinned Style-Bert-VITS2 mocked async inference, no weights/GPU/network."""

import asyncio
from dataclasses import replace
from hashlib import sha256
import math

import pytest

from astrumweaver.execution import ArtifactRef, JobExecutionError, JobRequest, JobResult, ResidencyReport
from astrumweaver.runtime.providers.style_bert_vits2 import (
    STYLE_BERT_VITS2_PROVIDER_ID,
    StyleBertVits2ModelPin, StyleBertVits2Voice, StyleBertVits2ProviderConfig,
    StyleBertVits2AsyncModelApi, StyleBertVits2Executor,
)
from test_audio_input_channel import WAV

SEMANTIC = "sha256:" + "5" * 64


def pin(**changes):
    values = dict(
        model_id="pinned-jp-voice", model_filename="pinned_e160.safetensors",
        model_sha256="a" * 64, config_sha256="b" * 64,
        style_vectors_sha256="c" * 64, runtime_revision="d" * 40,
        adapter_revision="e" * 40,
    )
    return StyleBertVits2ModelPin(**(values | changes))


def config(**changes):
    values = dict(
        model=pin(),
        voices=(StyleBertVits2Voice(
            alias="pinned-style", speaker_id=1,
            style_id="calm", style="Neutral",
        ),),
        required_semantic_revision=SEMANTIC,
        max_text_bytes=1024, max_output_bytes=4096,
        max_duration_seconds=30,
        synthesis_timeout_seconds=0.3, stage_timeout_seconds=0.3,
    )
    return StyleBertVits2ProviderConfig(**(values | changes))


class FakeAsyncProcessApi:
    def __init__(self):
        self.calls = []
        self.result = WAV
        self.fail = False
        self.delay = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancel_observed = False

    async def synthesize(self, *, model, text, speaker_id, style):
        self.calls.append((model, text, speaker_id, style))
        self.started.set()
        try:
            if self.delay:
                await self.release.wait()
        except asyncio.CancelledError:
            self.cancel_observed = True
            raise
        if self.fail:
            raise RuntimeError("private model path, raw transcript, GPU info")
        return self.result


class FakeMedia:
    def __init__(self):
        self.calls = []
        self.fail = False
        self.bad_ref = False
        self.delay = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def stage_output(self, wav_bytes, *, max_bytes):
        self.calls.append((wav_bytes, max_bytes))
        self.started.set()
        if self.delay:
            await self.release.wait()
        if self.fail:
            raise RuntimeError("private Worker lease and client token")
        return ArtifactRef(
            uri="tsumgi-audio:" + "1" * 64,
            media_type="audio/wav",
            digest="sha256:" + ("0" * 64 if self.bad_ref else sha256(wav_bytes).hexdigest()),
            size_bytes=len(wav_bytes),
        )


def job(*, text="散歩をします", speaker="1", style="calm", alias="pinned-style", capability="audio.synthesize", metadata=None, **extra):
    return JobRequest(
        job_id="stt-not-allowed-tts-job",
        capability=capability,
        payload={"input": text,
                 "voice": {"alias": alias, "speaker_id": speaker, "style_id": style},
                 "response_format": "wav", **extra},
        metadata={"serving_semantic_revision": SEMANTIC} if metadata is None else metadata,
    )


@pytest.mark.asyncio
async def test_pinned_model_voice_style_and_one_stage_produce_only_artifact_metadata():
    session, media = FakeAsyncProcessApi(), FakeMedia()
    ex = StyleBertVits2Executor(config=config(), api=session)
    assert ex.capabilities == {"audio.synthesize"}
    result = await ex.execute_audio(job(), media)
    assert isinstance(result, JobResult)
    assert result.text is None and not result.outputs
    assert len(result.artifacts) == 1 and len(media.calls) == 1
    assert media.calls == [(WAV, 4096)]
    assert session.calls == [(config().model, "散歩をします", 1, "Neutral")]
    assert result.artifacts[0].digest == "sha256:" + sha256(WAV).hexdigest()
    assert result.metadata["sample_rate_hz"] > 0
    assert result.metrics["audio_duration_seconds"] > 0
    assert "散歩をします" not in str(result)
    assert "RIFF" not in str(result)
    assert isinstance(await ex.residency(), ResidencyReport)
    assert not (await ex.residency()).items


@pytest.mark.asyncio
async def test_generic_execute_not_supported_and_non_audio_never_gets_model():
    api, media = FakeAsyncProcessApi(), FakeMedia()
    ex = StyleBertVits2Executor(config=config(), api=api)
    for capability, expected in (("audio.synthesize", "audio_media_required"),
                                 ("llm.chat", "unsupported_capability")):
        with pytest.raises(JobExecutionError) as caught:
            await ex.execute(job(capability=capability))
        assert caught.value.code == expected and not caught.value.retryable
    with pytest.raises(JobExecutionError) as caught:
        await ex.execute_audio(job(capability="text.embed"), media)
    assert caught.value.code == "unsupported_capability"
    assert not api.calls and not media.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"alias": "unreviewed"}, {"speaker": "9"}, {"style": "unapproved"},
    {"text": ""}, {"text": "  "}, {"text": "日本語" * 500},
    {"metadata": {}}, {"extra": "unreviewed"},
])
async def test_invalid_input_fails_before_model_api_or_media(change):
    api, media = FakeAsyncProcessApi(), FakeMedia()
    ex = StyleBertVits2Executor(config=config(), api=api)
    with pytest.raises(JobExecutionError) as caught:
        await ex.execute_audio(job(**change), media)
    assert not caught.value.retryable
    assert not api.calls and not media.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [
    "bad_riff", "trailing_data", "too_big", "wrong_type", "model_exception",
    "stage_exception", "invalid_ref",
])
async def test_model_output_and_stage_ref_fail_closed_without_leaks(kind):
    api, media = FakeAsyncProcessApi(), FakeMedia()
    if kind == "bad_riff":
        api.result = b"this is not WAV"
    if kind == "trailing_data":
        api.result = WAV + b"EXTRA-SENTINEL"
    if kind == "too_big":
        api.result = WAV + b"x" * 4096
    if kind == "wrong_type":
        api.result = "bad output"
    if kind == "model_exception":
        api.fail = True
    if kind == "stage_exception":
        media.fail = True
    if kind == "invalid_ref":
        media.bad_ref = True
    ex = StyleBertVits2Executor(config=config(), api=api)
    with pytest.raises(JobExecutionError) as caught:
        await ex.execute_audio(job(text="private transcript not logged"), media)
    assert caught.value.retryable is False
    for secret in ("private", "transcript", "GPU", "token", "path"):
        assert secret not in str(caught.value)
    assert len(media.calls) == (1 if kind in ("stage_exception", "invalid_ref") else 0)


@pytest.mark.asyncio
async def test_async_runner_cancellation_propagates_and_task_is_cleared():
    api, media = FakeAsyncProcessApi(), FakeMedia()
    api.delay = True
    ex = StyleBertVits2Executor(config=config(), api=api)
    request = job()
    running = asyncio.create_task(ex.execute_audio(request, media))
    await asyncio.wait_for(api.started.wait(), 1)
    await ex.cancel(request.job_id)
    with pytest.raises(asyncio.CancelledError):
        await running
    assert api.cancel_observed
    assert not media.calls
    assert not ex._inflight
    # This only proves cancellation of the fake coroutine; it DOES NOT
    # establish termination/reaping of an actual sync inference process.


@pytest.mark.asyncio
async def test_async_inference_and_stage_timeout_never_produce_success():
    api, media = FakeAsyncProcessApi(), FakeMedia()
    api.delay = True
    ex = StyleBertVits2Executor(config=config(synthesis_timeout_seconds=0.01), api=api)
    with pytest.raises(JobExecutionError) as caught:
        await ex.execute_audio(job(), media)
    assert caught.value.code == "audio_synthesis_timeout"
    assert not media.calls
    api = FakeAsyncProcessApi()
    media.delay = True
    ex = StyleBertVits2Executor(config=config(stage_timeout_seconds=0.01), api=api)
    with pytest.raises(JobExecutionError) as caught:
        await ex.execute_audio(job(), media)
    assert caught.value.code == "audio_stage_timeout"
    assert len(media.calls) == 1


@pytest.mark.asyncio
async def test_concurrent_duplicate_claim_same_job_fails_without_restaging():
    api, media = FakeAsyncProcessApi(), FakeMedia()
    api.delay = True
    ex = StyleBertVits2Executor(config=config(), api=api)
    running = asyncio.create_task(ex.execute_audio(job(), media))
    await asyncio.wait_for(api.started.wait(), 1)
    with pytest.raises(JobExecutionError) as caught:
        await ex.execute_audio(job(), media)
    assert caught.value.code == "audio_job_inflight"
    api.release.set()
    await running
    assert len(media.calls) == 1


def test_invalid_model_pins_and_unreviewed_speaker_styles_do_not_instantiate():
    for patch in (
        {"model_sha256": "badsha"},
        {"config_sha256": "0" * 63},
        {"style_vectors_sha256": "private/path"},
        {"model_filename": "../model.safetensors"},
        {"model_filename": "/tmp/a.safetensors"},
        {"model_filename": ".secret.onnx"},
        {"runtime_revision": "unverified"},
    ):
        with pytest.raises(ValueError, match="Invalid Style-Bert-VITS2 source configuration"):
            pin(**patch)
    for patch in (
        {"required_semantic_revision": None},
        {"required_semantic_revision": "unknown"},
        {"max_output_bytes": 11},
        {"max_text_bytes": 0},
        {"voices": ()},
        {"voices": (config().voices[0], config().voices[0])},
    ):
        with pytest.raises(ValueError, match="Invalid Style-Bert-VITS2 source configuration"):
            config(**patch)
    with pytest.raises(ValueError):
        replace(config().voices[0], style="\nprivate")
    with pytest.raises(ValueError):
        replace(config().voices[0], speaker_id=True)


def test_synchronous_tts_model_infer_is_never_called_on_worker_loop():
    class SyncSession:
        def synthesize(self, **kwargs):
            raise AssertionError("synchronous model infer called")
    with pytest.raises(ValueError, match="requires an async model session"):
        StyleBertVits2Executor(config=config(), api=SyncSession())


@pytest.mark.asyncio
@pytest.mark.parametrize("offset,broken", [
    (20, b"\x03\x00"),                 # float PCM, not integer PCM16
    (22, b"\x03\x00"),                 # unsupported channels
    (24, b"\x01\x00\x00\x00"),       # illegal sample rate
    (32, b"\x01\x00"),                 # invalid frame alignment
    (34, b"\x08\x00"),                 # unsupported sample bits
])
async def test_malformed_pcm_fmt_is_rejected_without_media_stage(offset, broken):
    api, media = FakeAsyncProcessApi(), FakeMedia()
    wav = bytearray(WAV)
    wav[offset:offset + len(broken)] = broken
    api.result = bytes(wav)
    ex = StyleBertVits2Executor(config=config(), api=api)
    with pytest.raises(JobExecutionError) as caught:
        await ex.execute_audio(job(), media)
    assert caught.value.code == "invalid_audio_output"
    assert not media.calls


@pytest.mark.asyncio
async def test_duration_over_configured_limit_is_rejected():
    api, media = FakeAsyncProcessApi(), FakeMedia()
    ex = StyleBertVits2Executor(config=config(max_duration_seconds=0.00001), api=api)
    with pytest.raises(JobExecutionError) as caught:
        await ex.execute_audio(job(), media)
    assert caught.value.code == "audio_output_exceeded"
    assert not media.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", [
    lambda r: replace(r, uri="file:///private/model"),
    lambda r: replace(r, media_type="audio/mpeg"),
    lambda r: replace(r, size_bytes=False),
    lambda r: replace(r, name="raw-user-input"),
    lambda r: replace(r, metadata={"secret": "private"}),
])
async def test_unexpected_media_receipt_rejected_before_job_complete(mutation):
    api = FakeAsyncProcessApi()
    media = FakeMedia()

    async def modified_stage(wav_bytes, *, max_bytes):
        receipt = await FakeMedia.stage_output(media, wav_bytes, max_bytes=max_bytes)
        return mutation(receipt)

    media.stage_output = modified_stage
    ex = StyleBertVits2Executor(config=config(), api=api)
    with pytest.raises(JobExecutionError) as caught:
        await ex.execute_audio(job(), media)
    assert caught.value.code == "invalid_audio_output"
    assert len(media.calls) == 1
    assert "private" not in str(caught.value)


@pytest.mark.asyncio
async def test_cancellation_during_staging_does_not_return_jobresult():
    api, media = FakeAsyncProcessApi(), FakeMedia()
    media.delay = True
    ex = StyleBertVits2Executor(config=config(), api=api)
    task = asyncio.create_task(ex.execute_audio(job(), media))
    await asyncio.wait_for(media.started.wait(), 1)
    await ex.cancel(job().job_id)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(media.calls) == 1
    assert ex._inflight == {}
    # No promise that a real IPC process or an already-sent Control POST was
    # rolled back. WorkerRuntime checks the live attempt at publication.


@pytest.mark.asyncio
async def test_executor_can_reuse_after_a_timed_out_mock_operation():
    api, media = FakeAsyncProcessApi(), FakeMedia()
    api.delay = True
    ex = StyleBertVits2Executor(config=config(synthesis_timeout_seconds=0.01), api=api)
    with pytest.raises(JobExecutionError) as caught:
        await ex.execute_audio(job(), media)
    assert caught.value.code == "audio_synthesis_timeout"
    assert ex._inflight == {} and media.calls == []
    api.delay = False
    result = await ex.execute_audio(job(), media)
    assert isinstance(result, JobResult) and len(result.artifacts) == 1
    assert len(media.calls) == 1


def test_unqualified_source_executor_cannot_advertise_ja_wav():
    from astrumweaver.serving import (
        DeploymentIdentity, ServingContract, ServingDeploymentDeclaration,
    )
    from astrumweaver.worker.runtime import require_executor_serving_features

    def digest(letter):
        return "sha256:" + letter * 64

    dep = DeploymentIdentity(
        provider_id="style-bert-vits2",
        runtime_artifact_sha256=digest("1"),
        adapter_artifact_sha256=digest("2"),
        model_artifact_sha256=digest("3"),
        execution_config_sha256=digest("4"), quantization="synthetic",
    )
    def declaration(features):
        return ServingDeploymentDeclaration(
            deployment=dep,
            contracts=(ServingContract(
                deployment_revision=dep.revision,
                capability="audio.synthesize",
                operation_schema="openai-audio-speech-v1",
                validation_evidence_sha256=digest("5"),
                features=frozenset(features),
            ),),
        )

    executor = StyleBertVits2Executor(config=config(), api=FakeAsyncProcessApi())
    assert not hasattr(executor, "serving_features")
    # A fake coroutine/model digest is not evidence of loaded model readiness.
    # Reviewed ja/wav contracts must remain ineligible at bare factory entry.
    with pytest.raises(RuntimeError, match="ja"):
        require_executor_serving_features(executor, declaration({"ja", "wav"}))
    with pytest.raises(RuntimeError, match="unverified-native-feature"):
        require_executor_serving_features(
            executor, declaration({"ja", "wav", "unverified-native-feature"})
        )
