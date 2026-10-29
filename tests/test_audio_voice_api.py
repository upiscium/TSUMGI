"""Synthetic ASGI requests only: no runtime, real voice, DB or network service."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

import httpx
import pytest
from fastapi import FastAPI

from astrumweaver.control.auth import ClientAuthMode
from astrumweaver.gateway.api import create_chat_router
from astrumweaver.gateway.audio import (
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
from astrumweaver.gateway.audio_voice_api import create_audio_voice_router
from astrumweaver.gateway.chat import (
    CHAT_OPERATION_SCHEMA,
    LLAMA_CPP_CHAT_ADAPTER,
    ChatGatewayProfile,
    ChatProfileCatalog,
)
from astrumweaver.serving import (
    DeploymentIdentity,
    LogicalServingProfile,
    ServingContract,
    resolve_profile,
)


TOKEN = "synthetic-client-token"
AUTH = {"Authorization": "Bearer " + TOKEN}


def digest(char):
    return "sha256:" + char * 64


def make_profile(kind, *, attribution="Synthetic attribution; not a voice license"):
    stt = kind == "model-stt"
    voices = () if stt else tuple(VoiceAlias(
        alias=alias, speaker_id="synthetic-speaker-" + kind,
        style_id=str(index), locale="ja", license_id="synthetic-license-v1",
        attribution=attribution,
    ) for index, alias in enumerate(("sample-z", "sample-a")))
    deployment = DeploymentIdentity(
        provider_id="voicevox" if kind == "voicevox" else "synthetic-provider",
        runtime_artifact_sha256=digest("1"), adapter_artifact_sha256=digest("2"),
        model_artifact_sha256=digest({
            "voicevox": "3", "model-tts": "4", "model-stt": "5",
        }[kind]),
        execution_config_sha256=digest("6"), quantization="synthetic-private-config",
    )
    operation = TRANSCRIPTION_OPERATION_SCHEMA if stt else SPEECH_OPERATION_SCHEMA
    capability = "audio.transcribe" if stt else "audio.synthesize"
    features = frozenset({"ja", "json" if stt else "wav"})
    contract = ServingContract(
        deployment_revision=deployment.revision, capability=capability,
        operation_schema=operation, validation_evidence_sha256=digest("7"),
        semantic_revision=audio_semantic_revision(kind, voices), features=features,
        limits=({"file_bytes": 1024, "metadata_bytes": 256, "request_bytes": 2048}
                if stt else {"input_bytes": 128, "request_bytes": 1024}),
    )
    logical = LogicalServingProfile(
        profile_id="synthetic-" + kind, deployment_revision=deployment.revision,
        serving_contract_revision=contract.revision, capability=capability,
        operation_schema=operation, required_features=features, limits={},
    )
    return AudioGatewayProfile(resolve_profile(logical, contract, deployment), kind, voices)


@pytest.fixture
def catalog():
    return AudioProfileCatalog(tuple(make_profile(kind) for kind in (
        "voicevox", "model-tts", "model-stt",
    )))


def make_app(catalog):
    app = FastAPI()
    app.include_router(create_audio_voice_router(
        catalog, client_auth=ClientAuthMode.BEARER, client_token=TOKEN,
    ))
    return app


async def request(app, query=b"model=synthetic-voicevox", *, headers=AUTH, method="GET"):
    # Preserve even malformed raw query bytes: HTTP clients may otherwise
    # normalize bad percent escapes before passing them to ASGITransport.
    async def raw_query_app(scope, receive, send):
        await app({**scope, "query_string": query}, receive, send)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=raw_query_app), base_url="http://synthetic",
    ) as client:
        return await client.request(method, "/v1/audio/voices", headers=headers)


def assert_error(response, status, code, field):
    expected = AudioGatewayError(code, field, status_code=status)
    assert response.status_code == status
    assert response.json() == expected.to_dict()
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["voicevox", "model-tts"])
async def test_exact_pinned_voice_mapping_and_public_allowlist(catalog, kind):
    before = deepcopy(catalog.to_dict())
    profile = catalog.get("synthetic-" + kind, operation_schema=SPEECH_OPERATION_SCHEMA)
    response = await request(make_app(catalog), ("model=" + profile.profile_id).encode())
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "object": "list", "model": profile.profile_id,
        "data": [voice.to_dict() for voice in profile.voices],
        "x_astrumweaver": {
            "backend_kind": kind, "capability": "audio.synthesize",
            "operation_schema": SPEECH_OPERATION_SCHEMA,
            "profile_revision": profile.resolved.profile_revision,
            "deployment_revision": profile.resolved.deployment.revision,
            "serving_contract_revision": profile.resolved.contract.revision,
            "semantic_revision": audio_semantic_revision(kind, profile.voices),
        },
    }
    assert [voice["alias"] for voice in response.json()["data"]] == ["sample-a", "sample-z"]
    for voice in response.json()["data"]:
        assert voice["locale"] == "ja"
        assert voice["license_id"] == "synthetic-license-v1"
        assert voice["attribution"] == "Synthetic attribution; not a voice license"
        assert voice["speaker_id"] == "synthetic-speaker-" + kind
    for private in (TOKEN, "synthetic-private-config", "synthetic-provider",
                    "runtime_artifact_sha256", "execution_config_sha256",
                    "validation_evidence_sha256", "http://", "https://"):
        assert private not in response.text
    assert catalog.to_dict() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("query,status,code,field", [
    (b"", 400, "missing_fields", "model"),
    (b"model=", 400, "invalid_request", "model"),
    (b"model", 400, "invalid_request", "query"),
    (b"model=synthetic-model-stt", 400, "unsupported_model", "model"),
    (b"model=nonexistent-private-model", 404, "model_not_found", "model"),
    (b"model=SYNTHETIC-VOICEVOX", 404, "model_not_found", "model"),
    (b"model=sample-a", 404, "model_not_found", "model"),
    (b"model=+synthetic-voicevox", 400, "invalid_request", "model"),
    (b"model=https%3A%2F%2Fprivate.invalid", 400, "invalid_request", "model"),
    (b"model=" + b"x" * 129, 400, "invalid_request", "model"),
    (b"model=%00", 400, "invalid_request", "model"),
    (b"model=%E3%81%82", 400, "invalid_request", "model"),
    (b"model=%FF", 400, "invalid_request", "query"),
    (b"model=%GG", 400, "invalid_request", "query"),
    (b"model=%", 400, "invalid_request", "query"),
    (b"model=\xff", 400, "invalid_request", "query"),
    (b"model=synthetic-voicevox&model=synthetic-model-tts", 400, "duplicate_fields", "model"),
    (b"model=synthetic-voicevox&model=synthetic-voicevox", 400, "duplicate_fields", "model"),
    (b"model=synthetic-voicevox&%6dodel=synthetic-voicevox", 400, "duplicate_fields", "model"),
    (b"model=synthetic-voicevox&model=", 400, "duplicate_fields", "model"),
    (b"model=synthetic-voicevox&voice=sample-a", 400, "unknown_fields", "query"),
    (b"model=synthetic-voicevox&stream=true", 400, "unknown_fields", "query"),
    (b"model=synthetic-voicevox&backend_url=https://private.invalid", 400, "unknown_fields", "query"),
    (b"private-input=secret", 400, "unknown_fields", "query"),
    (b"model=synthetic-voicevox&", 400, "invalid_request", "query"),
    (b"model=" + b"x" * 1024, 413, "request_too_large", "query"),
])
async def test_query_rejection_is_sanitized_without_fallback(catalog, query, status, code, field):
    before = catalog.to_dict()
    response = await request(make_app(catalog), query)
    assert_error(response, status, code, field)
    for private in ("private.invalid", "private-input", "secret", "nonexistent-private-model", TOKEN):
        assert private not in response.text
    assert catalog.to_dict() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [
    {}, {"Authorization": "Bearer wrong-synthetic-token"},
    {"Authorization": "Basic " + TOKEN}, {"Authorization": "Bearer"},
    {"Authorization": "Bearer "}, {"Authorization": "Bearer  " + TOKEN},
    {"Authorization": "Bearer " + TOKEN + " "},
    {"Authorization": "Bearer " + TOKEN + ", Bearer " + TOKEN},
    [("Authorization", "Bearer " + TOKEN), ("Authorization", "Bearer " + TOKEN)],
    [("Authorization", "Bearer wrong"), ("authorization", "Bearer " + TOKEN)],
    [(b"Authorization", b"Bearer nonascii-\xff")],
])
async def test_bearer_only_and_duplicate_header_refusal(catalog, headers):
    app = make_app(catalog)
    for query in (b"model=synthetic-voicevox", b"model=nonexistent", b"unknown=private"):
        response = await request(app, query, headers=headers)
        assert_error(response, 401, "unauthorized", "authorization")
        assert response.headers["www-authenticate"] == "Bearer"
        assert TOKEN not in response.text
        assert "nonascii" not in response.text
        assert "wrong" not in response.text


@pytest.mark.asyncio
async def test_bearer_scheme_is_case_insensitive_and_model_is_exact(catalog):
    response = await request(
        make_app(catalog), b"%6dodel=synthetic-voicevox",
        headers={"Authorization": "bEaReR " + TOKEN},
    )
    assert response.status_code == 200
    assert response.json()["model"] == "synthetic-voicevox"


@pytest.mark.parametrize("mode,token", [
    (ClientAuthMode.NONE, TOKEN), ("none", TOKEN), ("invalid-private-mode", TOKEN),
    (ClientAuthMode.BEARER, None), (ClientAuthMode.BEARER, ""),
    (ClientAuthMode.BEARER, "private token"), (ClientAuthMode.BEARER, "秘密"),
])
def test_factory_refuses_permissive_or_invalid_auth(catalog, mode, token):
    with pytest.raises(ValueError) as error:
        create_audio_voice_router(catalog, client_auth=mode, client_token=token)
    assert str(error.value) in {
        "audio voice catalog requires bearer authentication",
        "audio voice catalog requires a valid bearer token",
    }


@pytest.mark.parametrize("field,value", [("voices", ()), ("backend_kind", "private-unsupported")])
def test_invalid_tts_configuration_cannot_be_mounted(catalog, field, value):
    profile = catalog.get("synthetic-voicevox", operation_schema=SPEECH_OPERATION_SCHEMA)
    # Simulate corrupted configuration bypassing frozen dataclass construction.
    # Ordinary callers cannot create these states via the shared validators.
    object.__setattr__(profile, field, value)
    with pytest.raises(AudioGatewayError) as error:
        make_app(catalog)
    assert error.value.to_dict() == AudioGatewayError(
        "invalid_audio_configuration", "catalog", status_code=500,
    ).to_dict()


@pytest.mark.asyncio
async def test_semantic_change_requires_new_pins_and_response_is_detached(catalog):
    original = catalog.get("synthetic-voicevox", operation_schema=SPEECH_OPERATION_SCHEMA)
    changed = make_profile("voicevox", attribution="Revised synthetic attribution")
    with pytest.raises(AudioGatewayError):
        replace(original, voices=changed.voices)
    old_app = make_app(catalog)
    new_app = make_app(AudioProfileCatalog((changed,)))
    first = await request(old_app)
    second = await request(new_app)
    assert second.status_code == 200
    for key in ("profile_revision", "serving_contract_revision", "semantic_revision"):
        assert first.json()["x_astrumweaver"][key] != second.json()["x_astrumweaver"][key]
    assert second.json()["data"][0]["attribution"] == "Revised synthetic attribution"
    detached = first.json()
    detached["data"][0]["speaker_id"] = "client-side-only"
    assert (await request(old_app)).json() == first.json()


@pytest.mark.asyncio
async def test_opt_in_get_only_and_existing_catalog_contracts_unchanged(catalog):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=FastAPI()), base_url="http://synthetic",
    ) as client:
        assert (await client.get("/v1/audio/voices")).status_code == 404
    app = make_app(catalog)
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        assert (await request(app, method=method)).status_code == 405
    before = catalog.to_dict()
    round_trip = AudioProfileCatalog.from_dict(deepcopy(before))
    assert round_trip == catalog
    assert AudioProfileCatalog(list(reversed(catalog.profiles))) == catalog
    for kind in ("voicevox", "model-tts"):
        profile = catalog.get("synthetic-" + kind, operation_schema=SPEECH_OPERATION_SCHEMA)
        selected = validate_speech_request({
            "model": profile.profile_id, "input": "日本語", "voice": "sample-a",
            "response_format": "wav",
        }, catalog, request_size_bytes=100)
        assert selected.profile is profile
        assert selected.voice == profile.voices[0]
    assert validate_transcription_metadata({
        "model": "synthetic-model-stt", "language": "ja", "response_format": "json",
    }, catalog, metadata_size_bytes=100).profile.backend_kind == "model-stt"
    assert (await request(app)).status_code == 200
    assert catalog.to_dict() == before


@pytest.mark.asyncio
async def test_existing_models_route_keeps_non_audio_no_auth_semantics(catalog):
    deployment = make_profile("model-tts").resolved.deployment
    contract = ServingContract(
        deployment_revision=deployment.revision, capability="llm.chat",
        operation_schema=CHAT_OPERATION_SCHEMA, validation_evidence_sha256=digest("8"),
        semantic_revision=digest("9"), features=frozenset(),
        limits={"input_tokens": 16, "output_tokens": 16, "total_tokens": 32,
                "request_bytes": 1024},
    )
    logical = LogicalServingProfile(
        profile_id="synthetic-chat", deployment_revision=deployment.revision,
        serving_contract_revision=contract.revision, capability="llm.chat",
        operation_schema=CHAT_OPERATION_SCHEMA, required_features=frozenset(), limits={},
    )
    chat_catalog = ChatProfileCatalog((ChatGatewayProfile(
        resolve_profile(logical, contract, deployment), LLAMA_CPP_CHAT_ADAPTER, 1.0,
    ),))

    class NoRepositoryAccess:
        def __getattr__(self, name):
            raise AssertionError("catalog routes must not access a repository")

    app = FastAPI()
    app.include_router(create_chat_router(
        NoRepositoryAccess(), chat_catalog, client_auth=ClientAuthMode.NONE, client_token=None,
    ))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://synthetic",
    ) as client:
        before = await client.get("/v1/models")
        assert before.status_code == 200
        assert before.json() == chat_catalog.models_response()
        assert (await client.get("/v1/audio/voices")).status_code == 404
        app.include_router(create_audio_voice_router(
            catalog, client_auth=ClientAuthMode.BEARER, client_token=TOKEN,
        ))
        after = await client.get("/v1/models")
        assert after.status_code == before.status_code
        assert after.json() == before.json()
        assert [value["id"] for value in after.json()["data"]] == ["synthetic-chat"]
        assert (await client.get("/v1/audio/voices?model=synthetic-voicevox")).status_code == 401
        assert (await client.get(
            "/v1/audio/voices?model=synthetic-voicevox", headers=AUTH,
        )).status_code == 200
