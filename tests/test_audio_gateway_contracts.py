from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
import json

import pytest

from astrumweaver.gateway.audio import (
    AUDIO_CATALOG_SCHEMA,
    SPEECH_OPERATION_SCHEMA,
    TRANSCRIPTION_OPERATION_SCHEMA,
    AudioGatewayError,
    AudioGatewayProfile,
    AudioProfileCatalog,
    VoiceAlias,
    audio_semantic_revision,
    validate_speech_request,
    validate_transcription_metadata,
)
from astrumweaver.serving import (
    DeploymentIdentity,
    LogicalServingProfile,
    ServingContract,
    resolve_profile,
)


def digest(char: str) -> str:
    return "sha256:" + char * 64


def profile(kind="voicevox", *, limits=None, profile_limits=None):
    """Synthesized declarations only: no real voices, artifacts or licenses."""
    stt = kind == "model-stt"
    voices = () if stt else (VoiceAlias(
        alias="sample-a" if kind == "voicevox" else "sample-b",
        speaker_id="synthetic-speaker", style_id="0", locale="ja",
        license_id="synthetic-license-v1", attribution="Synthetic test attribution",
    ),)
    deployment = DeploymentIdentity(
        provider_id="voicevox" if kind == "voicevox" else "synthetic-model-provider",
        runtime_artifact_sha256=digest("1"),
        adapter_artifact_sha256=digest("2"),
        model_artifact_sha256=digest({"voicevox": "3", "model-tts": "4",
                                      "model-stt": "5"}[kind]),
        execution_config_sha256=digest("6"), quantization="test-only",
    )
    operation = TRANSCRIPTION_OPERATION_SCHEMA if stt else SPEECH_OPERATION_SCHEMA
    capability = "audio.transcribe" if stt else "audio.synthesize"
    contract = ServingContract(
        deployment_revision=deployment.revision, capability=capability,
        operation_schema=operation, validation_evidence_sha256=digest("7"),
        semantic_revision=audio_semantic_revision(kind, voices),
        features=frozenset({"ja", "json" if stt else "wav"}),
        limits=limits if limits is not None else (
            {"file_bytes": 1024, "metadata_bytes": 256, "request_bytes": 2048}
            if stt else {"input_bytes": 12, "request_bytes": 1024}
        ),
    )
    logical = LogicalServingProfile(
        profile_id="synthetic-" + kind, deployment_revision=deployment.revision,
        serving_contract_revision=contract.revision, capability=capability,
        operation_schema=operation,
        required_features=frozenset({"ja", "json" if stt else "wav"}),
        limits={} if profile_limits is None else profile_limits,
    )
    return AudioGatewayProfile(resolve_profile(logical, contract, deployment), kind, voices)


@pytest.fixture
def catalog():
    return AudioProfileCatalog(tuple(profile(kind) for kind in (
        "voicevox", "model-tts", "model-stt",
    )))


def speech(**changes):
    return {"model": "synthetic-voicevox", "input": "日本語",
            "voice": "sample-a", "response_format": "wav", **changes}


def metadata(**changes):
    return {"model": "synthetic-model-stt", "language": "ja",
            "response_format": "json", **changes}


def test_catalog_round_trip_is_canonical_and_detached(catalog):
    manifest = catalog.to_dict()
    assert manifest["schema_version"] == AUDIO_CATALOG_SCHEMA
    loaded = AudioProfileCatalog.from_dict(json.loads(json.dumps(manifest)))
    assert loaded == catalog
    assert loaded.to_dict() == manifest
    for original in catalog.profiles:
        selected = loaded.get(original.profile_id,
                              operation_schema=original.resolved.contract.operation_schema)
        assert selected.binding == original.binding
        assert selected.resolved.deployment.model_artifact_sha256 == (
            original.resolved.deployment.model_artifact_sha256
        )
    manifest["profiles"][0]["profile"]["limits"]["request_bytes"] = 1
    assert loaded.to_dict() != manifest
    assert AudioProfileCatalog(list(reversed(catalog.profiles))) == catalog


def test_configuration_is_deeply_immutable(catalog):
    profiles = list(catalog.profiles)
    copied = AudioProfileCatalog(profiles)
    profiles.clear()
    assert copied == catalog
    value = profile()
    mutable_voices = list(value.voices)
    copied_profile = replace(value, voices=mutable_voices)
    mutable_voices.clear()
    assert copied_profile.voices == value.voices
    for target, name, replacement in (
        (catalog, "profiles", ()), (value, "backend_kind", "model-tts"),
        (value.voices[0], "style_id", "other"),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(target, name, replacement)
    with pytest.raises(TypeError):
        value.resolved.effective_limits["input_bytes"] = 999


def test_exact_selection_and_preserved_input(catalog):
    result = validate_speech_request(
        speech(input=" 日本語 "), catalog, request_size_bytes=100,
    )
    assert result.input == " 日本語 "
    assert result.input_size_bytes == 11
    assert result.response_format == "wav"
    assert result.profile.backend_kind == "voicevox"
    assert result.voice.speaker_id == "synthetic-speaker"
    assert result.voice.style_id == "0"
    assert result.voice.locale == "ja"
    assert result.voice.license_id == "synthetic-license-v1"
    assert result.voice.attribution == "Synthetic test attribution"
    other = validate_speech_request(
        speech(model="synthetic-model-tts", voice="sample-b", input="ok"),
        catalog, request_size_bytes=100,
    )
    assert other.profile.backend_kind == "model-tts"
    assert other.profile.binding != result.profile.binding


@pytest.mark.parametrize("body,code,field", [
    (speech(model="missing"), "model_not_found", "model"),
    (speech(model="synthetic-model-stt"), "unsupported_model", "model"),
    (speech(model=" synthetic-voicevox"), "invalid_request", "model"),
    (speech(model="https://private.invalid"), "invalid_request", "model"),
    (speech(model="x" * 129), "invalid_request", "model"),
    (speech(model=None), "invalid_request", "model"),
    (speech(voice="sample-b"), "unsupported_voice", "voice"),
    (speech(model="synthetic-model-tts", voice="sample-a"), "unsupported_voice", "voice"),
    (speech(voice="default"), "unsupported_voice", "voice"),
    (speech(voice="x" * 129), "invalid_request", "voice"),
    (speech(voice=None), "invalid_request", "voice"),
    (speech(response_format="mp3"), "unsupported_format", "response_format"),
    (speech(response_format="pcm"), "unsupported_format", "response_format"),
    (speech(response_format="WAV"), "unsupported_format", "response_format"),
    (speech(response_format=None), "unsupported_format", "response_format"),
    (speech(input=""), "invalid_request", "input"),
    (speech(input=" \n\t　"), "invalid_request", "input"),
    (speech(input=[]), "invalid_request", "input"),
    (speech(input=12), "invalid_request", "input"),
    (speech(input="\ud800"), "invalid_utf8", "input"),
    (speech(input="a" * 13), "request_too_large", "input"),
    (speech(input="あ" * 5), "request_too_large", "input"),
])
def test_speech_rejections_never_fallback(catalog, body, code, field):
    before = catalog.to_dict()
    with pytest.raises(AudioGatewayError) as error:
        validate_speech_request(body, catalog, request_size_bytes=100)
    assert (error.value.code, error.value.field_name) == (code, field)
    assert error.value.status_code == (
        404 if code == "model_not_found" else 413 if code == "request_too_large" else 400
    )
    assert catalog.to_dict() == before


@pytest.mark.parametrize("field", [
    "backend_url", "profile", "speed", "stream", "speaker", "language", "private-value",
])
def test_speech_unknown_fields_are_rejected_and_redacted(catalog, field):
    with pytest.raises(AudioGatewayError) as error:
        validate_speech_request(speech(**{field: "PRIVATE-SENTINEL"}), catalog,
                                request_size_bytes=100)
    assert error.value.code == "unknown_fields"
    assert field not in str(error.value)
    assert "PRIVATE-SENTINEL" not in json.dumps(error.value.to_dict())


@pytest.mark.parametrize("field", ["model", "input", "voice", "response_format"])
def test_speech_requires_all_explicit_fields(catalog, field):
    body = speech()
    del body[field]
    with pytest.raises(AudioGatewayError) as error:
        validate_speech_request(body, catalog, request_size_bytes=100)
    assert error.value.code == "missing_fields"


@pytest.mark.parametrize("body", [None, [], "{}", 1, True, {1: "private"}])
def test_only_json_objects_are_accepted(catalog, body):
    with pytest.raises(AudioGatewayError):
        validate_speech_request(body, catalog, request_size_bytes=100)
    with pytest.raises(AudioGatewayError):
        validate_transcription_metadata(body, catalog, metadata_size_bytes=100)


@pytest.mark.parametrize("size", [0, -1, True, 1.5, None, "100", 1025])
def test_request_measurements_fail_closed(catalog, size):
    with pytest.raises(AudioGatewayError):
        validate_speech_request(speech(input="ok"), catalog, request_size_bytes=size)


def test_inclusive_utf8_and_request_bounds_and_profile_tightening(catalog):
    result = validate_speech_request(speech(input="あ" * 4), catalog,
                                    request_size_bytes=1024)
    assert result.input_size_bytes == 12
    with pytest.raises(AudioGatewayError):
        validate_speech_request(speech(input="あ"), catalog, request_size_bytes=2)
    tightened = AudioProfileCatalog((profile(profile_limits={"input_bytes": 3}),))
    assert validate_speech_request(speech(input="あ"), tightened,
                                   request_size_bytes=100).input_size_bytes == 3
    with pytest.raises(AudioGatewayError) as error:
        validate_speech_request(speech(input="ああ"), tightened, request_size_bytes=100)
    assert error.value.code == "request_too_large"


def test_transcription_metadata_is_separate_and_does_not_accept_files(catalog):
    result = validate_transcription_metadata(metadata(), catalog, metadata_size_bytes=256)
    assert result.language == "ja"
    assert result.response_format == "json"
    assert result.profile.resolved.contract.operation_schema == TRANSCRIPTION_OPERATION_SCHEMA
    assert result.transport_limits == {
        "file_bytes": 1024, "metadata_bytes": 256, "request_bytes": 2048,
    }
    with pytest.raises(TypeError):
        result.transport_limits["file_bytes"] = 99999
    for body in (metadata(file=b"not a WAV"), metadata(file_url="https://private.invalid"),
                 metadata(prompt="private"), metadata(codec="wav")):
        with pytest.raises(AudioGatewayError) as error:
            validate_transcription_metadata(body, catalog, metadata_size_bytes=100)
        assert error.value.code == "unknown_fields"


@pytest.mark.parametrize("body,code", [
    (metadata(model="synthetic-voicevox"), "unsupported_model"),
    (metadata(model="missing"), "model_not_found"),
    (metadata(language="en"), "unsupported_language"),
    (metadata(language=None), "unsupported_language"),
    (metadata(response_format="verbose_json"), "unsupported_format"),
    (metadata(response_format="text"), "unsupported_format"),
    (metadata(response_format=None), "unsupported_format"),
    ({"model": "synthetic-model-stt", "response_format": "json"}, "missing_fields"),
])
def test_transcription_metadata_rejections(catalog, body, code):
    with pytest.raises(AudioGatewayError) as error:
        validate_transcription_metadata(body, catalog, metadata_size_bytes=100)
    assert error.value.code == code


@pytest.mark.parametrize("size", [False, 0, -1, 1.5, None, 257])
def test_transcription_metadata_measurement_rejections(catalog, size):
    with pytest.raises(AudioGatewayError):
        validate_transcription_metadata(metadata(), catalog, metadata_size_bytes=size)


@pytest.mark.parametrize("change", [
    {"alias": "other"}, {"speaker_id": "other"}, {"style_id": "1"},
    {"license_id": "other-license"}, {"attribution": "Other attribution"},
])
def test_voice_semantics_require_new_pinned_contract(change):
    original = profile()
    changed = (replace(original.voices[0], **change),)
    assert audio_semantic_revision("voicevox", changed) != (
        original.resolved.contract.semantic_revision
    )
    with pytest.raises(AudioGatewayError) as error:
        replace(original, voices=changed)
    assert error.value.field_name == "semantic_revision"


def test_voice_order_is_not_semantic_but_duplicate_aliases_are_invalid():
    first = profile().voices[0]
    second = replace(first, alias="another")
    assert audio_semantic_revision("voicevox", (first, second)) == (
        audio_semantic_revision("voicevox", (second, first))
    )
    with pytest.raises(AudioGatewayError):
        audio_semantic_revision("voicevox", (first, first))


@pytest.mark.parametrize("change", [
    {"locale": "en"}, {"attribution": ""}, {"attribution": " "},
    {"attribution": "x" * 2049}, {"attribution": "あ" * 683},
    {"attribution": "private\nlog"}, {"attribution": "\ud800"},
    {"speaker_id": "http://private.invalid"}, {"style_id": True},
    {"alias": ""}, {"license_id": ""},
])
def test_voice_configuration_is_bounded_and_explicit(change):
    with pytest.raises(AudioGatewayError):
        replace(profile().voices[0], **change)


@pytest.mark.parametrize("kind,limits", [
    ("voicevox", {}),
    ("voicevox", {"input_bytes": 12}),
    ("voicevox", {"input_bytes": 12, "request_bytes": 12}),
    ("voicevox", {"input_bytes": 13, "request_bytes": 12}),
    ("voicevox", {"input_bytes": 12, "request_bytes": 100, "unknown": 1}),
    ("model-stt", {"file_bytes": 1024, "request_bytes": 2048}),
    ("model-stt", {"file_bytes": 100, "metadata_bytes": 50, "request_bytes": 150}),
])
def test_missing_unknown_and_inconsistent_audio_limits(kind, limits):
    with pytest.raises(AudioGatewayError):
        profile(kind, limits=limits)


@pytest.mark.parametrize("value", [True, 0, -1, 1.5, "12", None, 2**63])
def test_catalog_sanitizes_malformed_quantities(value):
    manifest = AudioProfileCatalog((profile(),)).to_dict()
    manifest["profiles"][0]["contract"]["limits"]["input_bytes"] = value
    with pytest.raises(AudioGatewayError) as error:
        AudioProfileCatalog.from_dict(manifest)
    assert error.value.code == "invalid_audio_configuration"
    assert error.value.status_code == 500


@pytest.mark.parametrize("part,field,value", [
    ("profile", "serving_contract_revision", digest("a")),
    ("profile", "deployment_revision", digest("b")),
    ("contract", "deployment_revision", digest("b")),
    ("contract", "semantic_revision", digest("b")),
    ("profile", "operation_schema", "other-operation-v1"),
    ("contract", "features", []),
    ("deployment", "provider_id", "unreviewed-provider"),
    ("deployment", "model_artifact_sha256", digest("b")),
    ("deployment", "backend_url", "PRIVATE-SENTINEL"),
    ("profile", "unknown", "PRIVATE-SENTINEL"),
    ("contract", "unknown", "PRIVATE-SENTINEL"),
])
def test_mismatched_pins_and_unknown_nested_config_fail_closed(part, field, value):
    manifest = AudioProfileCatalog((profile(),)).to_dict()
    manifest["profiles"][0][part][field] = value
    with pytest.raises(AudioGatewayError) as error:
        AudioProfileCatalog.from_dict(manifest)
    assert "PRIVATE-SENTINEL" not in json.dumps(error.value.to_dict())
    assert error.value.__cause__ is None


def test_rebound_but_wrong_audio_semantics_are_rejected():
    original = profile()
    for changes in (
        {"semantic_revision": digest("a")}, {"features": frozenset({"ja"})},
        {"operation_schema": "wrong-operation-v1"}, {"capability": "llm.chat"},
    ):
        contract = replace(original.resolved.contract, **changes)
        logical = replace(
            original.resolved.profile, serving_contract_revision=contract.revision,
            capability=contract.capability, operation_schema=contract.operation_schema,
            required_features=frozenset(),
        )
        resolved = resolve_profile(logical, contract, original.resolved.deployment)
        with pytest.raises(AudioGatewayError):
            replace(original, resolved=resolved)


def test_catalog_rejects_relabeling_voicevox_model_as_model_tts():
    voicevox = profile()
    tts = profile("model-tts")
    # Rebind all existing serving pins so only the audio identity rule can fail.
    deployment = replace(tts.resolved.deployment,
                         model_artifact_sha256=voicevox.resolved.deployment.model_artifact_sha256)
    contract = replace(tts.resolved.contract, deployment_revision=deployment.revision)
    logical = replace(tts.resolved.profile, deployment_revision=deployment.revision,
                      serving_contract_revision=contract.revision)
    tts = replace(tts, resolved=resolve_profile(logical, contract, deployment))
    with pytest.raises(AudioGatewayError):
        AudioProfileCatalog((voicevox, tts))
    with pytest.raises(AudioGatewayError):
        replace(voicevox, backend_kind="model-tts")


@pytest.mark.parametrize("transform", [
    lambda data: {**data, "backend_url": "PRIVATE-SENTINEL"},
    lambda data: {**data, "schema_version": "unknown"},
    lambda data: {**data, "profiles": []},
    lambda data: {**data, "profiles": {}},
    lambda data: {**data, "profiles": data["profiles"] * 2},
    lambda data: {**data, "profiles": [{**data["profiles"][0], "voices": []}]},
    lambda data: {**data, "profiles": [{**data["profiles"][0], "backend_kind": "auto"}]},
])
def test_catalog_is_explicit_strict_and_has_no_discovery_defaults(transform):
    manifest = AudioProfileCatalog((profile(),)).to_dict()
    with pytest.raises(AudioGatewayError) as error:
        AudioProfileCatalog.from_dict(transform(deepcopy(manifest)))
    assert "PRIVATE-SENTINEL" not in str(error.value)


def test_stt_disallows_voice_data_and_tts_requires_it():
    with pytest.raises(AudioGatewayError):
        replace(profile("model-stt"), voices=profile().voices)
    with pytest.raises(AudioGatewayError):
        replace(profile(), voices=())
    with pytest.raises(AudioGatewayError):
        AudioProfileCatalog(())
