"""#235 B6e: Style-Bert source-only assembly and disposable child acceptance."""

from __future__ import annotations

import asyncio
import os
import socket
from dataclasses import replace
from pathlib import Path

import pytest
from test_audio_provider_binding import values
from test_style_bert_vits2_managed_runtime import make_spec
from test_style_bert_vits2_provider import FakeMedia, job

from astrumweaver.contracts import ResourceShape
from astrumweaver.gateway.audio import (
    AudioGatewayProfile,
    AudioProfileCatalog,
    VoiceAlias,
    audio_semantic_revision,
)
from astrumweaver.runtime.contracts import RuntimeHealthState, RuntimeHostFacts
from astrumweaver.runtime.deployment import _PROVIDER_TYPES
from astrumweaver.runtime.providers.style_bert_vits2_managed import (
    StyleBertRuntimeFailure,
)
from astrumweaver.runtime.providers.style_bert_vits2_source_assembly import (
    StyleBertSourceAssemblyError,
    StyleBertSourceAssemblyIntent,
    StyleBertSourceAssemblyReview,
    review_stylebert_source_assembly,
)
from astrumweaver.serving import (
    LogicalServingProfile,
    ServingDeploymentDeclaration,
    resolve_profile,
)
from astrumweaver.setup import SetupPlan
from astrumweaver.setup.contracts import DeploymentPath, SetupHostSnapshot


def static_intent(*, gpu=False) -> StyleBertSourceAssemblyIntent:
    cfg = values("style-bert-vits2", gpu=gpu)
    return StyleBertSourceAssemblyIntent(
        profile_id=cfg["profile_id"],
        catalog=cfg["catalog"],
        declaration=cfg["declaration"],
        worker=cfg["worker"],
        snapshot=cfg["snapshot"],
        runtime=cfg["runtime"],
    )


def test_disabled_candidate_not_inspected_and_no_io(monkeypatch):
    class Poison:
        def __getattribute__(self, name):
            raise AssertionError("disabled candidate accessed")

    def forbidden(*_a, **_kw):
        raise AssertionError("disabled review performed I/O")

    with monkeypatch.context() as patch:
        patch.setattr("builtins.open", forbidden)
        patch.setattr(os, "open", forbidden)
        patch.setattr(socket, "socket", forbidden)
        patch.setattr(asyncio, "create_subprocess_exec", forbidden)
        review = review_stylebert_source_assembly(enabled=False, intent=Poison())
    assert type(review) is StyleBertSourceAssemblyReview
    assert review.status == "disabled" and not review.enabled
    assert review.to_dict()["can_apply"] is False
    assert review.assembly_digest is None and review.binding_digest is None
    with pytest.raises(StyleBertSourceAssemblyError):
        review.compose_disposable_source_runtime()
    with pytest.raises(StyleBertSourceAssemblyError):
        review_stylebert_source_assembly(enabled=0, intent=Poison())


@pytest.mark.parametrize("gpu", [False, True])
def test_static_review_preserves_blockers_and_public_redaction(gpu):
    intent = static_intent(gpu=gpu)
    review = review_stylebert_source_assembly(enabled=True, intent=intent)
    same = review_stylebert_source_assembly(enabled=True, intent=intent)
    assert review.to_json() == same.to_json()
    assert review.assembly_digest == same.assembly_digest
    assert review.enabled and review.status == "review_only_blocked"
    assert not review.can_apply and not isinstance(review, SetupPlan)
    assert review.provider_id == "style-bert-vits2"
    assert review.binding_digest.startswith("sha256:")
    assert review.assembly_digest.startswith("sha256:")
    assert "provider_runtime_registry_not_enabled" in review.blockers
    assert "native_runner_and_loaded_model_unqualified" in review.blockers
    assert "native_license_attribution_and_privacy_unqualified" in review.blockers
    assert ("real_gpu_isolation_and_vram_unqualified" in review.blockers) is gpu
    assert "style-bert-vits2" not in _PROVIDER_TYPES
    assert "voicevox" not in _PROVIDER_TYPES
    for private in (
        "/opt/private",
        "model.safetensors",
        "config.json",
        "style_vectors.npy",
        "GPU-PRIVATE-0",
        "token-private",
        "synthetic-rights",
        "Synthetic attribution",
    ):
        assert private not in review.to_json()
        assert private not in repr(review) and private not in repr(intent)


def test_assembly_is_inert_and_cannot_be_accessed_as_ready(monkeypatch):
    intent = static_intent()
    review = review_stylebert_source_assembly(enabled=True, intent=intent)

    def forbidden(*_a, **_kw):
        raise AssertionError("disposable source constructor performed I/O")

    with monkeypatch.context() as patch:
        patch.setattr("builtins.open", forbidden)
        patch.setattr(os, "open", forbidden)
        patch.setattr(os, "stat", forbidden)
        patch.setattr(Path, "read_bytes", forbidden)
        patch.setattr(socket, "socket", forbidden)
        patch.setattr(asyncio, "create_subprocess_exec", forbidden)
        runtime = review.compose_disposable_source_runtime()
        assert runtime._proc is None
        assert runtime._state is RuntimeHealthState.STOPPED
        assert runtime.provider_id == "style-bert-vits2"
        with pytest.raises(StyleBertRuntimeFailure):
            runtime.executor()
    assert not review.can_apply


@pytest.mark.parametrize(
    "mutation",
    [
        "wrong_profile",
        "wrong_deployment",
        "wrong_worker",
        "wrong_host",
        "wrong_capabilities",
        "cpu_has_gpu",
        "gpu_uuid_mismatch",
        "gpu_vram_exceeded",
        "gpu_has_multiple",
        "model_backend_substitution",
    ],
)
def test_mismatched_declaration_never_composes(mutation):
    intent = static_intent(gpu=mutation.startswith("gpu_"))
    if mutation == "wrong_profile":
        intent = replace(intent, profile_id="unapproved-profile")
    elif mutation == "wrong_deployment":
        changed_contract = replace(
            intent.declaration.contracts[0],
            validation_evidence_sha256="sha256:" + "9" * 64,
        )
        intent = replace(
            intent,
            declaration=ServingDeploymentDeclaration(
                deployment=intent.declaration.deployment,
                contracts=(changed_contract,),
            ),
        )
    elif mutation == "wrong_worker":
        intent = replace(intent, worker=replace(intent.worker, worker_id="other"))
    elif mutation == "wrong_host":
        intent = replace(
            intent,
            snapshot=replace(
                intent.snapshot,
                runtime_host=RuntimeHostFacts(cpu_count=2, host_ram_mb=32768),
            ),
        )
    elif mutation == "wrong_capabilities":
        intent = replace(
            intent,
            worker=replace(
                intent.worker, capabilities=frozenset({"audio.synthesize", "chat"})
            ),
        )
    elif mutation == "cpu_has_gpu":
        intent = replace(
            intent,
            worker=replace(
                intent.worker,
                resources=ResourceShape(
                    gpu_count=1,
                    total_vram_mb=8192,
                    max_single_gpu_vram_mb=8192,
                ),
                gpu_uuids=("GPU-OTHER",),
            ),
        )
    elif mutation == "gpu_uuid_mismatch":
        intent = replace(
            intent, worker=replace(intent.worker, gpu_uuids=("GPU-OTHER",))
        )
    elif mutation == "gpu_vram_exceeded":
        intent = replace(
            intent,
            worker=replace(
                intent.worker,
                resources=ResourceShape(
                    gpu_count=1, total_vram_mb=1024, max_single_gpu_vram_mb=1024
                ),
            ),
        )
    elif mutation == "gpu_has_multiple":
        intent = replace(
            intent,
            worker=replace(
                intent.worker,
                resources=ResourceShape(
                    gpu_count=2,
                    total_vram_mb=16000,
                    max_single_gpu_vram_mb=8000,
                ),
                gpu_uuids=("GPU-PRIVATE-0", "GPU-OTHER"),
            ),
        )
    elif mutation == "model_backend_substitution":
        v = values("voicevox")
        intent = replace(intent, catalog=v["catalog"], profile_id=v["profile_id"])
    else:
        raise AssertionError(mutation)
    with pytest.raises(StyleBertSourceAssemblyError) as err:
        review_stylebert_source_assembly(enabled=True, intent=intent)
    assert str(err.value) == "Style-Bert source assembly review is invalid"


@pytest.mark.parametrize(
    ("path", "field", "value"),
    [
        (("runtime",), "model_directory", "/opt/private/../SECRET"),
        (("runtime",), "interpreter_path", "SECRET_relative_python"),
        (("runtime",), "runner_sha256", "PRIVATE_BAD_SHA"),
        (("runtime",), "voice_rights_qualified", False),
        (("runtime",), "rights_review_sha256", "SECRET_RIGHTS"),
        (("runtime",), "inference_timeout_seconds", float("nan")),
        (("runtime", "adapter", "model"), "model_filename", "../SECRET.safetensors"),
        (("runtime", "adapter", "model"), "model_sha256", "PRIVATE_MODEL_HASH"),
        (("runtime", "adapter", "model"), "config_sha256", "PRIVATE_CONFIG_HASH"),
        (
            ("runtime", "adapter", "model"),
            "style_vectors_sha256",
            "PRIVATE_STYLES_HASH",
        ),
        (("runtime", "adapter", "model"), "runtime_revision", "PRIVATE_RUNTIME_REV"),
        (("runtime", "adapter", "model"), "adapter_revision", "PRIVATE_ADAPTER_REV"),
        (("runtime", "adapter", "voices", 0), "style", "\ud800"),
        (("runtime", "worker"), "resources", None),
        (("runtime",), "device", "PRIVATE_UNKNOWN_DEVICE"),
    ],
)
def test_reflected_corrupt_config_never_leaks_private_errors(path, field, value):
    intent = static_intent()
    review = review_stylebert_source_assembly(enabled=True, intent=intent)
    target = intent
    for node in path:
        target = target[node] if type(node) is int else getattr(target, node)
    object.__setattr__(target, field, value)
    for action in (
        lambda: review_stylebert_source_assembly(enabled=True, intent=intent),
        review.compose_disposable_source_runtime,
    ):
        with pytest.raises(StyleBertSourceAssemblyError) as err:
            action()
        assert str(err.value) == "Style-Bert source assembly review is invalid"
        assert "PRIVATE" not in str(err.value) and "SECRET" not in str(err.value)


def test_coherent_post_review_drift_changes_digest_and_is_rejected():
    intent = static_intent()
    review = review_stylebert_source_assembly(enabled=True, intent=intent)
    object.__setattr__(intent.runtime, "model_directory", "/opt/private/alternative")
    changed = review_stylebert_source_assembly(enabled=True, intent=intent)
    assert changed.assembly_digest != review.assembly_digest
    with pytest.raises(StyleBertSourceAssemblyError):
        review.compose_disposable_source_runtime()


def synthetic_intent(tmp_path: Path, *, fault="none") -> StyleBertSourceAssemblyIntent:
    spec = make_spec(tmp_path, fault=fault)
    native = spec.adapter.voices[0]
    voice = VoiceAlias(
        alias=native.alias,
        speaker_id=str(native.speaker_id),
        style_id=native.style_id,
        locale="ja",
        license_id="synthetic-rights",
        attribution="Synthetic voice; no native model",
    )
    semantic = audio_semantic_revision("model-tts", (voice,))
    contract = replace(
        spec.serving.contracts[0],
        semantic_revision=semantic,
        limits={"input_bytes": 12, "request_bytes": 1024},
    )
    serving = ServingDeploymentDeclaration(
        deployment=spec.serving.deployment, contracts=(contract,)
    )
    spec = replace(
        spec,
        adapter=replace(spec.adapter, required_semantic_revision=semantic),
        serving=serving,
    )
    logical = LogicalServingProfile(
        profile_id="synthetic-b6e-review",
        deployment_revision=serving.deployment.revision,
        serving_contract_revision=contract.revision,
        capability="audio.synthesize",
        operation_schema="openai-audio-speech-v1",
        required_features=frozenset({"ja", "wav"}),
    )
    catalog = AudioProfileCatalog(
        (
            AudioGatewayProfile(
                resolve_profile(logical, contract, serving.deployment),
                "model-tts",
                (voice,),
            ),
        )
    )
    snapshot = SetupHostSnapshot(
        runtime_host=spec.host,
        deployment_path=DeploymentPath.NIXOS,
        os_id="nixos",
    )
    return StyleBertSourceAssemblyIntent(
        profile_id=logical.profile_id,
        catalog=catalog,
        declaration=serving,
        worker=spec.worker,
        snapshot=snapshot,
        runtime=spec,
    )


@pytest.mark.asyncio
async def test_disposable_exact_synthetic_runner_wav_and_model_drift(tmp_path):
    intent = synthetic_intent(tmp_path)
    review = review_stylebert_source_assembly(enabled=True, intent=intent)
    assert not review.can_apply and "style-bert-vits2" not in _PROVIDER_TYPES
    runtime = review.compose_disposable_source_runtime()
    media = FakeMedia()
    child = None
    try:
        assert (await runtime.health()).state is RuntimeHealthState.STOPPED
        await runtime.start()
        assert (await runtime.health()).ready
        child = runtime._proc
        result = await runtime.executor().execute_audio(
            job(
                metadata={
                    "serving_semantic_revision": intent.runtime.adapter.required_semantic_revision
                }
            ),
            media,
        )
        assert len(result.artifacts) == 1 and len(media.calls) == 1
        assert result.text is None and not result.outputs
        assert "散歩" not in str(result)
        # Only the fake model/config/style vectors are touched.
        (Path(intent.runtime.model_directory) / "style_vectors.npy").write_bytes(
            b"corrupted-synthetic-style-vectors"
        )
        health = await runtime.health()
        assert not health.ready and health.state is RuntimeHealthState.FAILED
        with pytest.raises(StyleBertRuntimeFailure):
            runtime.executor()
    finally:
        await runtime.release()
    assert child is not None and child.returncode is not None
    assert runtime._proc is None
    assert "style-bert-vits2" not in _PROVIDER_TYPES


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["wrong_epoch", "wrong_model", "unqualified_probe"])
async def test_invalid_synthetic_bootstrap_never_becomes_ready(fault, tmp_path):
    intent = synthetic_intent(tmp_path, fault=fault)
    review = review_stylebert_source_assembly(enabled=True, intent=intent)
    runtime = review.compose_disposable_source_runtime()
    with pytest.raises(StyleBertRuntimeFailure):
        await runtime.start()
    assert runtime._proc is None
    assert not (await runtime.health()).ready
    with pytest.raises(StyleBertRuntimeFailure):
        runtime.executor()


@pytest.mark.asyncio
async def test_cancelled_synthetic_inference_reaps_child_without_staging(tmp_path):
    intent = synthetic_intent(tmp_path, fault="infer_delay")
    runtime = review_stylebert_source_assembly(
        enabled=True, intent=intent
    ).compose_disposable_source_runtime()
    await runtime.start()
    child = runtime._proc
    media = FakeMedia()
    request = job(
        metadata={
            "serving_semantic_revision": intent.runtime.adapter.required_semantic_revision
        }
    )
    task = asyncio.create_task(runtime.executor().execute_audio(request, media))
    await asyncio.sleep(0.04)
    await runtime.executor().cancel(request.job_id)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert child.returncode is not None
    assert runtime._proc is None and not media.calls
    assert not (await runtime.health()).ready
