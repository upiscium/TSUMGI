"""#180 B4 pure review only: no SetupPlan, model, host or network actions."""

from dataclasses import FrozenInstanceError, replace
import json

import pytest

from astrumweaver import ResourceShape, WorkerSpec
from astrumweaver.runtime import RuntimeHostFacts
from astrumweaver.setup import SetupPlan
from astrumweaver.setup.audio_review import (
    AudioSetupReview,
    AudioSetupReviewError,
    review_audio_setup,
)
from astrumweaver.setup.contracts import DeploymentPath, SetupHostSnapshot
from astrumweaver.serving import ServingDeploymentDeclaration
from astrumweaver.gateway.audio import AudioProfileCatalog
from test_audio_gateway_contracts import profile


def args(kind="voicevox", *, gpu=False, deployment=DeploymentPath.NIXOS):
    selected = profile(kind)
    capability = selected.resolved.profile.capability
    worker = WorkerSpec(
        worker_id="synthetic-worker-private-identifier",
        worker_class="synthetic-worker-class",
        resources=ResourceShape(
            gpu_count=1 if gpu else 0,
            total_vram_mb=8192 if gpu else 0,
            max_single_gpu_vram_mb=8192 if gpu else 0,
        ),
        gpu_uuids=("GPU-SYNTHETIC-PRIVATE",) if gpu else (),
        capabilities=frozenset({capability}),
        labels={"private-token": "private-should-never-leak"},
    )
    snapshot = SetupHostSnapshot(
        runtime_host=RuntimeHostFacts(
            cpu_count=4, host_ram_mb=16384,
            labels={"private-note": "private-should-never-leak"},
        ),
        deployment_path=deployment,
        os_id="nixos" if deployment is DeploymentPath.NIXOS else "ubuntu",
        metadata={"private-url": "https://private.internal/model-token"},
    )
    return dict(
        enabled=True,
        profile_id=selected.profile_id,
        catalog=AudioProfileCatalog((selected,)),
        declaration=ServingDeploymentDeclaration(
            deployment=selected.resolved.deployment,
            contracts=(selected.resolved.contract,),
        ),
        worker=worker,
        snapshot=snapshot,
    )


@pytest.mark.parametrize("kind", ["voicevox", "model-tts", "model-stt"])
@pytest.mark.parametrize("deployment", [DeploymentPath.NIXOS, DeploymentPath.SYSTEMD])
def test_explicit_single_audio_profile_review_never_authorizes_activation(kind, deployment):
    input_args = args(kind, deployment=deployment)
    review = review_audio_setup(**input_args)
    assert type(review) is AudioSetupReview
    assert review.enabled is True
    assert review.status == "review_only_blocked"
    assert review.can_apply is False
    assert review.deployment_path == deployment.value
    assert review.declared_host_ram_mb == 16384
    assert review.provider_id == input_args["declaration"].deployment.provider_id
    assert review.serving_contract_revision == input_args["declaration"].contracts[0].revision
    assert review.profile_revision == input_args["catalog"].profiles[0].resolved.profile_revision
    assert review.capability in input_args["worker"].capabilities
    assert "adapter_implementation_review" in review.blockers
    assert "artifact_digest_verification" in review.blockers
    assert "operator_activation_approval" in review.blockers
    assert "license_and_attribution_review" in review.blockers
    assert ("stt_model_selection_pending" in review.blockers) is (kind == "model-stt")
    assert review.digest.startswith("sha256:") and len(review.digest) == 71
    assert review.digest == review_audio_setup(**input_args).digest
    assert json.loads(review.to_json()) == review.to_dict()
    assert not any(word in review.to_json() for word in (
        "private-should-never-leak", "private.internal", "GPU-SYNTHETIC-PRIVATE",
        "synthetic-worker-private-identifier", "private-token",
    ))
    assert not isinstance(review, SetupPlan)
    with pytest.raises((KeyError, TypeError, ValueError)):
        SetupPlan.from_dict(review.to_dict())
    with pytest.raises(FrozenInstanceError):
        review.enabled = False


def test_explicit_disabled_skips_all_inspection_and_is_deterministic():
    class MustNotRead:
        def __getattribute__(self, name):
            pytest.fail("disabled audio inspected provider input")
    got = review_audio_setup(
        enabled=False,
        profile_id=MustNotRead(), catalog=MustNotRead(),
        declaration=MustNotRead(), worker=MustNotRead(), snapshot=MustNotRead(),
    )
    assert got == review_audio_setup(enabled=False)
    assert got.status == "disabled"
    assert not got.can_apply and not got.blockers
    assert got.to_dict()["provider_id"] is None


@pytest.mark.parametrize("mutation", [
    lambda p: p.update(profile_id=None),
    lambda p: p.update(profile_id=""),
    lambda p: p.update(profile_id="other-model"),
    lambda p: p.update(catalog=None),
    lambda p: p.update(declaration=None),
    lambda p: p.update(worker=None),
    lambda p: p.update(snapshot=None),
    lambda p: p.update(enabled=1),
    lambda p: p.update(catalog=AudioProfileCatalog((profile("model-tts"),))),
    lambda p: p.update(declaration=ServingDeploymentDeclaration(
        deployment=profile("model-tts").resolved.deployment,
        contracts=(profile("model-tts").resolved.contract,),
    )),
    lambda p: p.update(worker=replace(
        p["worker"], capabilities=frozenset({"debug.echo"}),
    )),
    lambda p: p.update(worker=replace(
        p["worker"], capabilities=frozenset({"audio.synthesize", "audio.transcribe"}),
    )),
])
def test_missing_mismatched_or_ambiguous_selection_fails_closed_without_leak(mutation):
    p = args()
    mutation(p)
    with pytest.raises(AudioSetupReviewError, match="^audio setup review is invalid$") as caught:
        review_audio_setup(**p)
    assert "private" not in str(caught.value)
    assert "model-tts" not in str(caught.value)


def test_gpu_is_claimed_not_discovered_and_never_ready():
    p = args(gpu=True)
    review = review_audio_setup(**p)
    assert review.declared_gpu_count == 1
    assert "gpu_identity_and_vram_unverified" in review.blockers
    assert review.status == "review_only_blocked" and not review.can_apply
    # WorkerSpec itself rejects contradictory declared GPU identity before
    # this source-only review sees any candidate, without probing the host.
    with pytest.raises(ValueError, match="gpu_uuids count"):
        replace(p["worker"], gpu_uuids=())


def test_digest_bound_to_exact_profile_identity_without_raw_provider_config():
    p = args("voicevox")
    original = review_audio_setup(**p)
    assert original.voice_aliases == ("sample-a",)
    assert original.model_artifact_sha256 == profile("voicevox").resolved.deployment.model_artifact_sha256
    different = args("model-tts")
    other = review_audio_setup(**different)
    assert original.digest != other.digest
    assert original.provider_id != other.provider_id
    changed_path = review_audio_setup(**{**p, "snapshot": args(deployment=DeploymentPath.SYSTEMD)["snapshot"]})
    assert original.digest != changed_path.digest
    other_snapshot = replace(
        p["snapshot"],
        runtime_host=RuntimeHostFacts(cpu_count=4, host_ram_mb=32768),
    )
    assert original.digest != review_audio_setup(
        **{**p, "snapshot": other_snapshot}
    ).digest


def test_enabled_audio_review_never_opens_files_executes_commands_or_connects(monkeypatch):
    import builtins
    import os
    import socket
    import subprocess

    reviewed = args("voicevox")
    def forbidden(*_args, **_kwargs):
        pytest.fail("review-only API attempted filesystem, process or network access")

    # All input value construction is complete before the no-I/O boundary.
    with monkeypatch.context() as m:
        m.setattr(builtins, "open", forbidden)
        m.setattr(subprocess, "run", forbidden)
        m.setattr(os, "system", forbidden)
        m.setattr(socket, "create_connection", forbidden)
        result = review_audio_setup(**reviewed)
        assert result.enabled and not result.can_apply
        assert "adapter_implementation_review" in result.blockers


def test_existing_non_audio_worker_capabilities_are_preserved_but_not_qualified():
    p = args("voicevox")
    worker = p["worker"]
    p["worker"] = replace(
        worker, capabilities=frozenset({"audio.synthesize", "llm.chat", "text.embed"}),
    )
    reviewed = review_audio_setup(**p)
    assert reviewed.declared_audio_capabilities == ("audio.synthesize",)
    assert reviewed.can_apply is False
    assert p["worker"].capabilities == frozenset(
        {"audio.synthesize", "llm.chat", "text.embed"}
    )


def test_review_digest_changes_when_same_profile_tightens_limits():
    before = args("voicevox")
    original = review_audio_setup(**before)
    changed_profile = profile("voicevox", profile_limits={"input_bytes": 6})
    changed = review_audio_setup(**{
        **before,
        "catalog": AudioProfileCatalog((changed_profile,)),
    })
    assert changed.profile_id == original.profile_id
    assert changed.provider_id == original.provider_id
    assert changed.deployment_revision == original.deployment_revision
    assert changed.serving_contract_revision == original.serving_contract_revision
    assert changed.profile_revision != original.profile_revision
    assert changed.digest != original.digest
    assert changed.status == original.status == "review_only_blocked"
    assert changed.can_apply is False
