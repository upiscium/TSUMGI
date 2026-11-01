"""#176 VOICEVOX provider-local contract: fake engine, no host/network/model."""

import asyncio
from dataclasses import replace
from hashlib import sha256

import pytest

from astrumweaver.execution import ArtifactRef, JobExecutionError, JobRequest, JobResult, ResidencyReport
from astrumweaver.runtime.providers.voicevox import (
    VOICEVOX_PROVIDER_ID, VoicevoxAdapterConfig, VoicevoxEngineApi,
    VoicevoxExecutor, VoicevoxVoicePin,
)
from test_audio_input_channel import WAV


UUID = "c3b5b742-0000-4000-a000-000000000001"
SEMANTIC = "sha256:" + "5" * 64


def config(**changes):
    pinned = VoicevoxVoicePin(
        alias="reviewed-a", speaker_uuid=UUID, style_id=3,
        license_id="synthetic-license", attribution="Synthetic attribution only",
    )
    values = dict(
        engine_version="test-version.1",
        engine_artifact_sha256="sha256:" + "a" * 64,
        adapter_artifact_sha256="sha256:" + "b" * 64,
        model_artifact_sha256="sha256:" + "c" * 64,
        semantic_revision=SEMANTIC,
        voices=(pinned,),
        max_text_bytes=1024, max_query_bytes=2048,
        max_output_bytes=4096,
        max_duration_seconds=30,
        synthesis_timeout_seconds=0.3, stage_timeout_seconds=0.3,
    )
    return VoicevoxAdapterConfig(**(values | changes))


class FakeVoicevox:
    """No HTTP; only an explicit injectable engine contract."""
    def __init__(self):
        self.return_version = "test-version.1"
        self.return_speakers = [{"speaker_uuid": UUID, "styles": [{"id": 3, "name": "mock"}]}]
        self.return_query = {"accent_phrases": [], "outputSamplingRate": 24000, "outputStereo": False}
        self.return_wav = WAV
        self.fail = None
        self.delay = False
        self.invocations = []
        self.called_synthesis = asyncio.Event()
        self.release = asyncio.Event()

    async def version(self):
        self.invocations.append("version")
        if self.fail == "version":
            raise RuntimeError("private engine/token/version")
        return self.return_version

    async def speakers(self):
        self.invocations.append("speakers")
        if self.fail == "speakers":
            raise RuntimeError("private engine/token/speakers")
        return self.return_speakers

    async def audio_query(self, *, text, speaker):
        self.invocations.append(("audio_query", text, speaker))
        if self.fail == "audio_query":
            raise RuntimeError("private engine/token/query")
        return self.return_query

    async def synthesis(self, *, query, speaker):
        self.invocations.append(("synthesis", query, speaker))
        self.called_synthesis.set()
        if self.delay:
            await self.release.wait()
        if self.fail == "synthesis":
            raise RuntimeError("private engine/token/synthesis")
        return self.return_wav


class FakeMedia:
    def __init__(self):
        self.calls = []
        self.fail = False
        self.invalid_receipt = False

    async def stage_output(self, wav_bytes, *, max_bytes):
        self.calls.append((wav_bytes, max_bytes))
        if self.fail:
            raise RuntimeError("private lease token, WAV secret")
        return ArtifactRef(
            uri="tsumgi-audio:" + "f" * 64,
            media_type="audio/wav",
            digest="sha256:" + ("0" * 64 if self.invalid_receipt else sha256(wav_bytes).hexdigest()),
            size_bytes=len(wav_bytes),
        )


def job(*, text="散歩をします", alias="reviewed-a", speaker=UUID, style="3", capability="audio.synthesize", metadata=None, **extra):
    return JobRequest(
        job_id="synthetic-audio-job",
        capability=capability,
        payload={"input": text, "voice": {"alias": alias, "speaker_id": speaker, "style_id": style},
                 "response_format": "wav", **extra},
        metadata={"serving_semantic_revision": SEMANTIC} if metadata is None else metadata,
    )


@pytest.mark.asyncio
async def test_exact_pinned_voice_query_synthesis_stages_once_and_serializes_no_audio():
    api, media = FakeVoicevox(), FakeMedia()
    ex = VoicevoxExecutor(config=config(), api=api)
    assert isinstance(ex, VoicevoxEngineApi) is False
    assert ex.capabilities == {"audio.synthesize"}
    result = await ex.execute_audio(job(), media)
    assert type(result) is JobResult
    assert result.outputs == {} and result.text is None
    assert len(result.artifacts) == 1
    assert media.calls == [(WAV, 4096)]
    assert api.invocations == [
        "version", "speakers", ("audio_query", "散歩をします", 3),
        ("synthesis", api.return_query, 3),
    ]
    assert result.artifacts[0].digest == "sha256:" + sha256(WAV).hexdigest()
    assert result.metadata["sample_rate_hz"] > 0
    assert result.metrics["audio_duration_seconds"] > 0
    assert "RIFF" not in str(result)
    assert "散歩をします" not in str(result)
    assert isinstance(await ex.residency(), ResidencyReport)
    assert not (await ex.residency()).items


@pytest.mark.asyncio
async def test_media_required_and_non_audio_rejected():
    ex = VoicevoxExecutor(config=config(), api=FakeVoicevox())
    for request, expected in (
        (job(), "media_required"),
        (job(capability="llm.chat"), "unsupported_capability"),
    ):
        with pytest.raises(JobExecutionError) as error:
            await ex.execute(request)
        assert error.value.code == expected and not error.value.retryable
    media = FakeMedia()
    with pytest.raises(JobExecutionError, match="only"):
        await ex.execute_audio(job(capability="text.embed"), media)
    assert not media.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("modification", [
    {"alias": "unknown"},
    {"speaker": "c3b5b742-0000-4000-a000-000000000002"},
    {"style": "2"},
    {"text": ""},
    {"text": " \t "},
    {"text": "日本語" * 500},
    {"metadata": {}},
    {"extra": "unreviewed"},
])
async def test_invalid_job_does_not_call_engine_or_media(modification):
    api, media = FakeVoicevox(), FakeMedia()
    ex = VoicevoxExecutor(config=config(), api=api)
    request = job(**modification)
    with pytest.raises(JobExecutionError) as caught:
        await ex.execute_audio(request, media)
    assert not caught.value.retryable
    assert api.invocations == [] and media.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [
    "bad_version", "missing_style", "duplicate_speaker", "invalid_speakers",
    "query_no_accents", "query_nan", "query_large", "wrong_type_wav",
    "invalid_wav", "trailing_wav", "too_big_wav", "error_version",
    "error_query", "error_synthesis", "stage_error", "invalid_receipt",
])
async def test_engine_and_artifact_fail_closed_without_raw_content(kind):
    api, media = FakeVoicevox(), FakeMedia()
    if kind == "bad_version":
        api.return_version = "drifted-version"
    if kind == "missing_style":
        api.return_speakers = [{"speaker_uuid": UUID, "styles": [{"id": 9}]}]
    if kind == "duplicate_speaker":
        api.return_speakers = api.return_speakers * 2
    if kind == "invalid_speakers":
        api.return_speakers = "private-WAV-sentinel"
    if kind == "query_no_accents":
        api.return_query = {"something_else": True}
    if kind == "query_nan":
        api.return_query = {"accent_phrases": [], "float": float("nan")}
    if kind == "query_large":
        api.return_query = {"accent_phrases": [], "private": "x" * 3000}
    if kind == "wrong_type_wav":
        api.return_wav = "WAV-not-binary"
    if kind == "invalid_wav":
        api.return_wav = b"evil" * 20
    if kind == "trailing_wav":
        api.return_wav = WAV + b"private"
    if kind == "too_big_wav":
        api.return_wav = WAV + b"x" * 4096
    if kind == "error_version":
        api.fail = "version"
    if kind == "error_query":
        api.fail = "audio_query"
    if kind == "error_synthesis":
        api.fail = "synthesis"
    if kind == "stage_error":
        media.fail = True
    if kind == "invalid_receipt":
        media.invalid_receipt = True
    ex = VoicevoxExecutor(config=config(), api=api)
    with pytest.raises(JobExecutionError) as caught:
        await ex.execute_audio(job(text="private-job-transcript"), media)
    error = caught.value
    assert error.retryable is False
    for secret in ("private-job-transcript", "private", "WAV", "token"):
        assert secret not in str(error)
    if kind in ("stage_error", "invalid_receipt"):
        assert len(media.calls) == 1
    else:
        assert len(media.calls) == 0


@pytest.mark.asyncio
async def test_cancellation_and_timeout_preserve_single_stage_boundary():
    api, media = FakeVoicevox(), FakeMedia()
    api.delay = True
    ex = VoicevoxExecutor(config=config(), api=api)
    request = job()
    pending = asyncio.create_task(ex.execute_audio(request, media))
    await asyncio.wait_for(api.called_synthesis.wait(), 1)
    await ex.cancel(request.job_id)
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert not media.calls
    assert ex._inflight == {}
    # A genuinely asynchronous injected provider can time out without staging.
    tiny = VoicevoxExecutor(config=config(synthesis_timeout_seconds=0.01), api=api)
    with pytest.raises(JobExecutionError) as caught:
        await tiny.execute_audio(job(), media)
    assert caught.value.code == "synthesis_timeout"
    assert not media.calls


@pytest.mark.asyncio
async def test_duplicate_job_is_rejected_before_second_engine_call():
    api, media = FakeVoicevox(), FakeMedia()
    api.delay = True
    ex = VoicevoxExecutor(config=config(), api=api)
    first = asyncio.create_task(ex.execute_audio(job(), media))
    await asyncio.wait_for(api.called_synthesis.wait(), 1)
    with pytest.raises(JobExecutionError) as caught:
        await ex.execute_audio(job(), media)
    assert caught.value.code == "already_running"
    api.release.set()
    await first
    assert len(media.calls) == 1


def test_invalid_declared_pins_and_sync_api_rejected_without_i_o():
    for patch in (
        {"semantic_revision": "unreviewed"},
        {"engine_artifact_sha256": "sha256:invalid"},
        {"max_output_bytes": 32},
        {"max_text_bytes": 0},
        {"voices": ()},
        {"voices": (config().voices[0], config().voices[0])},
    ):
        with pytest.raises(ValueError, match="VOICEVOX adapter configuration is invalid"):
            config(**patch)
    with pytest.raises(ValueError):
        replace(config().voices[0], speaker_uuid="raw-private-model-path")

    class Synchronous:
        def version(self):
            raise AssertionError("must not touch engine")

    with pytest.raises(ValueError, match="VOICEVOX adapter configuration is invalid"):
        VoicevoxExecutor(config=config(), api=Synchronous())


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["version", "speakers", "audio_query", "synthesis"])
async def test_malicious_engine_jobexecutionerror_is_redacted_and_not_retryable(endpoint):
    api, media = FakeVoicevox(), FakeMedia()

    async def attack(**kwargs):
        raise JobExecutionError(
            "engine-says-retry", "private-origin-url raw-tts-text token=secret",
            retryable=True,
        )

    setattr(api, endpoint, attack)
    ex = VoicevoxExecutor(config=config(), api=api)
    with pytest.raises(JobExecutionError) as caught:
        await ex.execute_audio(job(), media)
    assert caught.value.code == "engine_unavailable"
    assert not caught.value.retryable
    for secret in ("private-origin-url", "raw-tts-text", "secret", "engine-says-retry"):
        assert secret not in str(caught.value)
    assert not media.calls


@pytest.mark.asyncio
async def test_global_style_id_conflict_across_speaker_uuids_is_rejected():
    api, media = FakeVoicevox(), FakeMedia()
    api.return_speakers = [
        {"speaker_uuid": UUID, "styles": [{"id": 3}]},
        {"speaker_uuid": "c3b5b742-0000-4000-a000-000000000002", "styles": [{"id": 3}]},
    ]
    ex = VoicevoxExecutor(config=config(), api=api)
    with pytest.raises(JobExecutionError) as caught:
        await ex.execute_audio(job(), media)
    assert caught.value.code == "voice_unavailable"
    assert not media.calls
    assert not any(isinstance(entry, tuple) for entry in api.invocations)


@pytest.mark.asyncio
async def test_exponentially_shared_audio_query_structure_is_bounded_before_encoding():
    api, media = FakeVoicevox(), FakeMedia()
    shared = ["bounded"]
    for _ in range(7):
        shared = [shared] * 30
    # Seven small Python lists would serialize into an exponentially large
    # JSON response if no structural node/size budget ran first.
    api.return_query = {"accent_phrases": shared}
    ex = VoicevoxExecutor(config=config(), api=api)
    with pytest.raises(JobExecutionError) as caught:
        await ex.execute_audio(job(), media)
    assert caught.value.code == "invalid_query"
    assert not media.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed", [
    {"accent_phrases": ["x" * 3000]},
    {"accent_phrases": [], "untrusted": b"not-json"},
    {"accent_phrases": [], "untrusted": {0: "bad non-string key"}},
])
async def test_invalid_audio_query_response_rejected_before_synthesis(malformed):
    api, media = FakeVoicevox(), FakeMedia()
    api.return_query = malformed
    ex = VoicevoxExecutor(config=config(), api=api)
    with pytest.raises(JobExecutionError) as caught:
        await ex.execute_audio(job(), media)
    assert caught.value.code == "invalid_query"
    assert not media.calls
    assert not any(isinstance(entry, tuple) and entry[0] == "synthesis" for entry in api.invocations)


def test_unqualified_source_executor_cannot_advertise_ja_wav():
    from astrumweaver.serving import (
        DeploymentIdentity, ServingContract, ServingDeploymentDeclaration,
    )
    from astrumweaver.worker.runtime import require_executor_serving_features

    def digest(letter):
        return "sha256:" + letter * 64

    dep = DeploymentIdentity(
        provider_id="voicevox",
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

    executor = VoicevoxExecutor(config=config(), api=FakeVoicevox())
    assert not hasattr(executor, "serving_features")
    # The Worker daemon must reject an unqualified source-only executor even
    # when its method set and pinned voice config appear structurally valid.
    with pytest.raises(RuntimeError, match="ja"):
        require_executor_serving_features(executor, declaration({"ja", "wav"}))
    with pytest.raises(RuntimeError, match="unverified-native-feature"):
        require_executor_serving_features(
            executor, declaration({"ja", "wav", "unverified-native-feature"})
        )
