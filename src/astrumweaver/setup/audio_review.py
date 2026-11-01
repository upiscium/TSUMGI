"""Source-only review of an explicitly selected audio deployment (#180 B4).

This is deliberately NOT a SetupPlan: SetupPlan can be passed to an action
Driver, whereas no audio adapter, model, or host is qualified by this source.
No discovery, filesystem access, networking, service changes or execution.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json

from ..contracts import WorkerSpec
from ..gateway.audio import AudioProfileCatalog
from ..serving import ServingDeploymentDeclaration
from .contracts import SetupHostSnapshot


_ERROR = "audio setup review is invalid"
_AUDIO_CAPABILITIES = frozenset({"audio.synthesize", "audio.transcribe"})
_REVIEW_CHECKS = (
    "adapter_implementation_review",
    "artifact_digest_verification",
    "host_resource_preflight",
    "license_and_attribution_review",
    "operator_activation_approval",
    "provider_real_e2e_acceptance",
    "runtime_package_abi_qualification",
)


class AudioSetupReviewError(ValueError):
    """Fixed, value-redacted review error. An invalid input never grants setup."""

    def __init__(self) -> None:
        super().__init__(_ERROR)


@dataclass(frozen=True, slots=True)
class AudioSetupReview:
    """Deterministic informational result; cannot be applied or grant readiness."""

    enabled: bool
    profile_id: str | None = None
    backend_kind: str | None = None
    provider_id: str | None = None
    capability: str | None = None
    deployment_revision: str | None = None
    profile_revision: str | None = None
    serving_contract_revision: str | None = None
    model_artifact_sha256: str | None = None
    deployment_path: str | None = None
    declared_audio_capabilities: tuple[str, ...] = ()
    declared_gpu_count: int | None = None
    declared_cpu_count: int | None = None
    declared_host_ram_mb: int | None = None
    voice_aliases: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()

    @property
    def can_apply(self) -> bool:
        # No implementation can turn this record into a SetupAction approval.
        return False

    @property
    def status(self) -> str:
        return "review_only_blocked" if self.enabled else "disabled"

    def to_dict(self) -> dict[str, object]:
        """Allowlist-only public-safe output. Never copy raw config or secrets."""
        return {
            "schema_version": "audio-setup-review-v1",
            "status": self.status,
            "enabled": self.enabled,
            "can_apply": False,
            "profile_id": self.profile_id,
            "backend_kind": self.backend_kind,
            "provider_id": self.provider_id,
            "capability": self.capability,
            "deployment_revision": self.deployment_revision,
            "profile_revision": self.profile_revision,
            "serving_contract_revision": self.serving_contract_revision,
            "model_artifact_sha256": self.model_artifact_sha256,
            "deployment_path": self.deployment_path,
            "declared_audio_capabilities": list(self.declared_audio_capabilities),
            "declared_gpu_count": self.declared_gpu_count,
            "declared_cpu_count": self.declared_cpu_count,
            "declared_host_ram_mb": self.declared_host_ram_mb,
            "voice_aliases": list(self.voice_aliases),
            "blockers": list(self.blockers),
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":"),
            ensure_ascii=True, allow_nan=False,
        )

    @property
    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()


def review_audio_setup(
    *,
    enabled: bool,
    profile_id: str | None = None,
    catalog: AudioProfileCatalog | None = None,
    declaration: ServingDeploymentDeclaration | None = None,
    worker: WorkerSpec | None = None,
    snapshot: SetupHostSnapshot | None = None,
) -> AudioSetupReview:
    """Require exact catalog↔serving↔Worker agreement; never select a provider.

    Explicit enabled=False returns a constant no-side-effect disabled review.
    Enabled mode is intentionally not an apply plan or proof of runtime health.
    Provider-specific release/package/host gates remain unresolved here.
    """
    if type(enabled) is not bool:
        raise AudioSetupReviewError()
    if not enabled:
        # Do not inspect/parse any optional provider input in disabled mode.
        return AudioSetupReview(enabled=False)
    if (
        type(profile_id) is not str or not profile_id
        or type(catalog) is not AudioProfileCatalog
        or type(declaration) is not ServingDeploymentDeclaration
        or type(worker) is not WorkerSpec
        or type(snapshot) is not SetupHostSnapshot
    ):
        raise AudioSetupReviewError()

    selected = next(
        (candidate for candidate in catalog.profiles
         if candidate.profile_id == profile_id), None,
    )
    if selected is None:
        # No default, first-match fallback, or runtime-side provider discovery.
        raise AudioSetupReviewError()
    resolved = selected.resolved
    if (
        declaration.deployment != resolved.deployment
        or len(declaration.contracts) != 1
        or declaration.contracts[0] != resolved.contract
    ):
        raise AudioSetupReviewError()
    advertised = frozenset(worker.capabilities) & _AUDIO_CAPABILITIES
    if advertised != {resolved.profile.capability}:
        # Initial review binds one provider/operation to one Worker: no
        # accidental dual-role/silent fallback, non-audio work is untouched.
        raise AudioSetupReviewError()
    if selected.backend_kind not in ("voicevox", "model-tts", "model-stt"):
        raise AudioSetupReviewError()
    blockers = set(_REVIEW_CHECKS)
    if selected.backend_kind == "model-stt":
        # #178 requires real comparative Japanese STT evidence and an explicit
        # later Human choice. A synthetic pinned contract is never that choice.
        blockers.add("stt_model_selection_pending")
    if worker.resources.gpu_count:
        blockers.add("gpu_identity_and_vram_unverified")
    return AudioSetupReview(
        enabled=True,
        profile_id=selected.profile_id,
        backend_kind=selected.backend_kind,
        provider_id=resolved.deployment.provider_id,
        capability=resolved.profile.capability,
        deployment_revision=resolved.deployment.revision,
        profile_revision=resolved.profile_revision,
        serving_contract_revision=resolved.contract.revision,
        model_artifact_sha256=resolved.deployment.model_artifact_sha256,
        deployment_path=snapshot.deployment_path.value,
        declared_audio_capabilities=tuple(sorted(advertised)),
        declared_gpu_count=worker.resources.gpu_count,
        declared_cpu_count=snapshot.runtime_host.cpu_count,
        declared_host_ram_mb=snapshot.runtime_host.host_ram_mb,
        voice_aliases=tuple(voice.alias for voice in selected.voices),
        blockers=tuple(sorted(blockers)),
    )
