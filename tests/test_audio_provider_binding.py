"""#180 B6a: exact gateway→serving→provider→Worker non-applicable binding."""

import json
from dataclasses import replace

import pytest

from astrumweaver.contracts import ResourceShape, WorkerSpec
from astrumweaver.gateway.audio import (
    AudioGatewayProfile,
    AudioProfileCatalog,
    VoiceAlias,
    audio_semantic_revision,
)
from astrumweaver.runtime.contracts import RuntimeHostFacts
from astrumweaver.runtime.providers.style_bert_vits2 import (
    StyleBertVits2ModelPin,
    StyleBertVits2ProviderConfig,
    StyleBertVits2Voice,
)
from astrumweaver.runtime.providers.style_bert_vits2_managed import (
    StyleBertOwnedRuntimeSpec,
)
from astrumweaver.runtime.providers.voicevox import (
    VoicevoxAdapterConfig,
    VoicevoxVoicePin,
)
from astrumweaver.runtime.providers.voicevox_managed import (
    VoicevoxOwnedRuntimeSpec,
)
from astrumweaver.serving import (
    DeploymentIdentity,
    LogicalServingProfile,
    ServingContract,
    ServingDeploymentDeclaration,
    resolve_profile,
)
from astrumweaver.setup import (
    AudioProviderBindingError,
    AudioProviderBindingReview,
    SetupPlan,
    review_audio_provider_binding,
)
from astrumweaver.setup.contracts import DeploymentPath, SetupHostSnapshot

UUID = "c3b5b742-0000-4000-a000-000000000001"


def digest(c: str) -> str:
    return "sha256:" + c * 64


def values(kind="voicevox", *, gpu=False, path=DeploymentPath.NIXOS):
    assert kind in ("voicevox", "style-bert-vits2")
    worker = WorkerSpec(
        worker_id="private-worker-0",
        worker_class="synthetic-test",
        capabilities=frozenset({"audio.synthesize"}),
        resources=(
            ResourceShape(gpu_count=1, total_vram_mb=8192, max_single_gpu_vram_mb=8192)
            if gpu
            else ResourceShape()
        ),
        gpu_uuids=("GPU-PRIVATE-0",) if gpu else (),
        labels={"private": "worker-private-tag"},
    )
    host = RuntimeHostFacts(
        cpu_count=8,
        host_ram_mb=16384,
        labels={"private": "host-private-tag"},
    )
    snapshot = SetupHostSnapshot(
        runtime_host=host,
        deployment_path=path,
        os_id="nixos" if path is DeploymentPath.NIXOS else "ubuntu",
        metadata={"secret": "token-private"},
    )
    if kind == "voicevox":
        assert not gpu
        gateway_voice = VoiceAlias(
            alias="reviewed",
            speaker_id=UUID,
            style_id="3",
            locale="ja",
            license_id="test-license",
            attribution="Synthetic attribution",
        )
        pin = VoicevoxVoicePin(
            alias="reviewed",
            speaker_uuid=UUID,
            style_id=3,
            license_id="test-license",
            attribution="Synthetic attribution",
        )
        semantic = audio_semantic_revision("voicevox", (gateway_voice,))
        adapter = VoicevoxAdapterConfig(
            engine_version="mock-v1",
            engine_artifact_sha256=digest("a"),
            adapter_artifact_sha256=digest("b"),
            model_artifact_sha256=digest("c"),
            semantic_revision=semantic,
            voices=(pin,),
            max_text_bytes=64,
            max_query_bytes=4096,
            max_output_bytes=8192,
        )
        dep = DeploymentIdentity(
            provider_id="voicevox",
            runtime_artifact_sha256=digest("a"),
            adapter_artifact_sha256=digest("b"),
            model_artifact_sha256=digest("c"),
            execution_config_sha256=digest("e"),
            quantization="synthetic",
        )
        backend_kind = "voicevox"
    else:
        gateway_voice = VoiceAlias(
            alias="reviewed",
            speaker_id="1",
            style_id="calm",
            locale="ja",
            license_id="synthetic-rights",
            attribution="Synthetic attribution",
        )
        semantic = audio_semantic_revision("model-tts", (gateway_voice,))
        model_pin = StyleBertVits2ModelPin(
            model_id="synthetic",
            model_filename="model.safetensors",
            model_sha256="c" * 64,
            config_sha256="d" * 64,
            style_vectors_sha256="e" * 64,
            runtime_revision="f" * 40,
            adapter_revision="a" * 40,
        )
        adapter = StyleBertVits2ProviderConfig(
            model=model_pin,
            voices=(
                StyleBertVits2Voice(
                    alias="reviewed",
                    speaker_id=1,
                    style_id="calm",
                    style="Neutral",
                ),
            ),
            required_semantic_revision=semantic,
            max_text_bytes=64,
        )
        dep = DeploymentIdentity(
            provider_id="style-bert-vits2",
            runtime_artifact_sha256=digest("a"),
            adapter_artifact_sha256=digest("b"),
            model_artifact_sha256=digest("c"),
            execution_config_sha256=digest("e"),
            quantization="synthetic",
        )
        backend_kind = "model-tts"

    contract = ServingContract(
        deployment_revision=dep.revision,
        capability="audio.synthesize",
        operation_schema="openai-audio-speech-v1",
        validation_evidence_sha256=digest("7"),
        semantic_revision=semantic,
        features=frozenset({"ja", "wav"}),
        limits={"input_bytes": 12, "request_bytes": 1024},
    )
    profile = LogicalServingProfile(
        profile_id="tts-reviewed-a",
        deployment_revision=dep.revision,
        serving_contract_revision=contract.revision,
        capability="audio.synthesize",
        operation_schema="openai-audio-speech-v1",
        required_features=frozenset({"ja", "wav"}),
    )
    gateway = AudioGatewayProfile(
        resolve_profile(profile, contract, dep),
        backend_kind,
        (gateway_voice,),
    )
    serving = ServingDeploymentDeclaration(
        deployment=dep,
        contracts=(contract,),
    )
    if kind == "voicevox":
        runtime = VoicevoxOwnedRuntimeSpec(
            adapter=adapter,
            serving=serving,
            executable_path="/opt/private/voicevox-engine",
            model_artifact_path="/opt/private/model.dat",
            endpoint="http://127.0.0.1:51000",
            worker_resource_id=worker.worker_id,
            owner_uid=1000,
            approved_voice_aliases=frozenset({"reviewed"}),
            voice_rights_review_sha256=digest("8"),
        )
    else:
        runtime = StyleBertOwnedRuntimeSpec(
            adapter=adapter,
            serving=serving,
            worker=worker,
            host=host,
            model_directory="/opt/private/models",
            interpreter_path="/opt/private/python3",
            runner_path="/opt/private/synthetic-runner.py",
            runner_sha256=digest("b"),
            interpreter_sha256=digest("a"),
            rights_review_sha256=digest("8"),
            voice_rights_qualified=True,
            device="cuda" if gpu else "cpu",
            gpu_uuid="GPU-PRIVATE-0" if gpu else None,
            required_single_gpu_vram_mb=2048 if gpu else 0,
        )
    return {
        "enabled": True,
        "profile_id": gateway.profile_id,
        "catalog": AudioProfileCatalog((gateway,)),
        "declaration": serving,
        "worker": worker,
        "snapshot": snapshot,
        "runtime": runtime,
    }


@pytest.mark.parametrize(
    "kind,gpu",
    [
        ("voicevox", False),
        ("style-bert-vits2", False),
        ("style-bert-vits2", True),
    ],
)
@pytest.mark.parametrize("path", [DeploymentPath.NIXOS, DeploymentPath.SYSTEMD])
def test_exact_provider_binding_is_auditable_and_never_applicable(kind, gpu, path):
    params = values(kind, gpu=gpu, path=path)
    review = review_audio_provider_binding(**params)
    assert type(review) is AudioProviderBindingReview
    assert review.enabled and review.status == "review_only_blocked"
    assert not review.can_apply and not isinstance(review, SetupPlan)
    assert review.profile_id == "tts-reviewed-a"
    assert review.provider_id == params["declaration"].deployment.provider_id
    assert review.voice_aliases == ("reviewed",)
    assert review.deployment_revision == params["declaration"].deployment.revision
    assert "provider_runtime_registry_not_enabled" in review.blockers
    assert "native_transport_and_process_identity_unqualified" in review.blockers
    assert "voice_model_and_license_real_acceptance_pending" in review.blockers
    assert review.digest == review_audio_provider_binding(**params).digest
    assert json.loads(review.to_json()) == review.to_dict()
    assert review.to_dict()["can_apply"] is False
    assert review.setup_review_digest.startswith("sha256:")
    assert review.provider_config_digest.startswith("sha256:")
    assert "execution_config_provenance_unqualified" in review.blockers
    for private in (
        "private-worker-0",
        "GPU-PRIVATE-0",
        "/opt/private",
        "worker-private-tag",
        "host-private-tag",
        "token-private",
        "Synthetic attribution",
        "test-license",
    ):
        assert private not in review.to_json()
    with pytest.raises((TypeError, KeyError, ValueError)):
        SetupPlan.from_dict(review.to_dict())


def test_explicit_disable_returns_constant_without_touching_inputs():
    class CannotInspect:
        def __getattribute__(self, key):
            pytest.fail("disabled audio provider binding inspected an input")

    optional = {
        name: CannotInspect()
        for name in (
            "profile_id",
            "catalog",
            "declaration",
            "worker",
            "snapshot",
            "runtime",
        )
    }
    first = review_audio_provider_binding(enabled=False, **optional)
    assert first == review_audio_provider_binding(enabled=False)
    assert not first.can_apply and first.status == "disabled"
    assert first.voice_aliases == () and first.blockers == ()
    with pytest.raises(AudioProviderBindingError, match="binding review is invalid"):
        review_audio_provider_binding(enabled=1)


@pytest.mark.parametrize("kind", ["voicevox", "style-bert-vits2"])
def test_incorrect_voice_or_speaker_mapping_rejected(kind):
    p = values(kind)
    rt = p["runtime"]
    if kind == "voicevox":
        voice = replace(
            rt.adapter.voices[0], speaker_uuid="c3b5b742-0000-4000-a000-000000000002"
        )
    else:
        voice = replace(rt.adapter.voices[0], speaker_id=2)
    p["runtime"] = replace(
        rt,
        adapter=replace(rt.adapter, voices=(voice,)),
    )
    with pytest.raises(AudioProviderBindingError):
        review_audio_provider_binding(**p)


def test_voicevox_license_and_attribution_alias_mismatch_rejected():
    for field, value in (
        ("license_id", "other-license"),
        ("attribution", "Other attribution"),
    ):
        p = values()
        rt = p["runtime"]
        voice = replace(rt.adapter.voices[0], **{field: value})
        p["runtime"] = replace(rt, adapter=replace(rt.adapter, voices=(voice,)))
        with pytest.raises(AudioProviderBindingError):
            review_audio_provider_binding(**p)


@pytest.mark.parametrize("kind", ["voicevox", "style-bert-vits2"])
def test_runtime_wrong_worker_identity_or_serving_revision_rejected(kind):
    p = values(kind)
    worker = p["worker"]
    p["worker"] = replace(worker, worker_id="different-worker")
    with pytest.raises(AudioProviderBindingError):
        review_audio_provider_binding(**p)
    p = values(kind)
    p["declaration"] = values("style-bert-vits2" if kind == "voicevox" else "voicevox")[
        "declaration"
    ]
    with pytest.raises(AudioProviderBindingError):
        review_audio_provider_binding(**p)


@pytest.mark.parametrize("kind", ["voicevox", "style-bert-vits2"])
def test_mixed_audio_non_audio_capabilities_fail_prior_to_registry(kind):
    p = values(kind)
    p["worker"] = replace(
        p["worker"], capabilities=frozenset({"audio.synthesize", "llm.chat"})
    )
    with pytest.raises(AudioProviderBindingError):
        review_audio_provider_binding(**p)


@pytest.mark.parametrize("kind", ["voicevox", "style-bert-vits2"])
def test_gateway_input_limit_cannot_exceed_selected_executor(kind):
    p = values(kind)
    rt = p["runtime"]
    p["runtime"] = replace(rt, adapter=replace(rt.adapter, max_text_bytes=6))
    with pytest.raises(AudioProviderBindingError):
        review_audio_provider_binding(**p)


def test_stylebert_host_and_gpu_binding_must_match_snapshot():
    p = values("style-bert-vits2", gpu=True)
    p["snapshot"] = replace(
        p["snapshot"], runtime_host=RuntimeHostFacts(cpu_count=8, host_ram_mb=32768)
    )
    with pytest.raises(AudioProviderBindingError):
        review_audio_provider_binding(**p)


def test_model_stt_remains_unselected_and_not_admitted():
    from test_audio_setup_review import args as b4_args

    p = b4_args("model-stt")
    p["runtime"] = values()["runtime"]
    with pytest.raises(AudioProviderBindingError):
        review_audio_provider_binding(**p)


def test_review_never_touches_host_process_network_or_disk(monkeypatch):
    import builtins
    import os
    import socket
    import subprocess

    p = values("style-bert-vits2", gpu=True)

    def forbidden(*args, **kwargs):
        pytest.fail("source-only provider binding performed I/O")

    with monkeypatch.context() as m:
        m.setattr(builtins, "open", forbidden)
        m.setattr(os, "system", forbidden)
        m.setattr(socket, "create_connection", forbidden)
        m.setattr(subprocess, "run", forbidden)
        result = review_audio_provider_binding(**p)
        assert result.enabled and not result.can_apply


@pytest.mark.parametrize("kind", ["voicevox", "style-bert-vits2"])
def test_provider_private_runtime_configuration_changes_review_digest(kind):
    p = values(kind)
    previous = review_audio_provider_binding(**p)
    rt = p["runtime"]
    if kind == "voicevox":
        p["runtime"] = replace(rt, endpoint="http://127.0.0.1:52000")
    else:
        p["runtime"] = replace(rt, runner_path="/opt/private/runner-v2.py")
    current = review_audio_provider_binding(**p)
    assert previous.setup_review_digest == current.setup_review_digest
    assert previous.profile_revision == current.profile_revision
    assert previous.provider_config_digest != current.provider_config_digest
    assert previous.digest != current.digest


def test_stylebert_native_style_string_change_requires_new_review():
    p = values("style-bert-vits2")
    old = review_audio_provider_binding(**p)
    rt = p["runtime"]
    pin = replace(rt.adapter.voices[0], style="Angry")
    p["runtime"] = replace(rt, adapter=replace(rt.adapter, voices=(pin,)))
    new = review_audio_provider_binding(**p)
    assert new.profile_revision == old.profile_revision
    assert new.provider_config_digest != old.provider_config_digest
    assert new.digest != old.digest


def test_oversized_private_worker_snapshot_is_not_serialized_or_exposed():
    p = values("style-bert-vits2")
    oversized = replace(p["worker"], labels={"private": "secret" * 8192})
    p["worker"] = oversized
    p["runtime"] = replace(p["runtime"], worker=oversized)
    with pytest.raises(
        AudioProviderBindingError,
        match="^audio provider binding review is invalid$",
    ):
        review_audio_provider_binding(**p)


@pytest.mark.parametrize("kind", ["voicevox", "style-bert-vits2"])
def test_audio_providers_cannot_be_started_via_existing_runtime_registry(kind):
    from astrumweaver.runtime import (
        ExecutionDemand,
        GPUTopology,
        ModelDemand,
        ModelTopology,
        ResidencyPolicy,
        RuntimeDeploymentSpec,
        provider_from_deployment,
    )

    p = values(kind)
    provider_id = p["declaration"].deployment.provider_id
    manifest = RuntimeDeploymentSpec(
        provider_id=provider_id,
        provider_config={},
        demand=ExecutionDemand(
            model=ModelDemand(
                model_ref="synthetic-no-live-model",
                model_format="unqualified-audio",
                topology=ModelTopology.DENSE,
            ),
            residency_policy=ResidencyPolicy.PREFER_VRAM,
            gpu_topology=GPUTopology.NONE,
        ),
    )
    # Explicitly assert the source-only B6a review does not install a provider
    # in the executable Worker registry by side effect of importing setup.
    with pytest.raises(
        ValueError, match=f"^unsupported runtime provider: {provider_id}$"
    ):
        provider_from_deployment(manifest)
