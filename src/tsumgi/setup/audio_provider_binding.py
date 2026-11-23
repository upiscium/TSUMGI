"""#180 B6a: pure, non-applicable audio profile-to-provider binding review.

The provider-local B5b/B5c runtime configurations are source-only. No real
engine/client/loaded-model attestation or runnable RuntimeProvider is supplied.
This audit closes cross-layer *declaration* mismatches before any future,
separately approved registry/SetupPlan integration; it grants no setup.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum

from ..contracts import WorkerSpec
from ..gateway.audio import (
    AudioGatewayProfile,
    AudioProfileCatalog,
)
from ..runtime.providers.style_bert_vits2_managed import StyleBertOwnedRuntimeSpec
from ..runtime.providers.voicevox_managed import VoicevoxOwnedRuntimeSpec
from ..serving import ServingDeploymentDeclaration
from .audio_review import AudioSetupReview, AudioSetupReviewError, review_audio_setup
from .contracts import SetupHostSnapshot


class AudioProviderBindingError(ValueError):
    """Fixed, redacted error for mismatched cross-layer declarations."""

    def __init__(self) -> None:
        super().__init__("audio provider binding review is invalid")


@dataclass(frozen=True, slots=True)
class AudioProviderBindingReview:
    """Auditable, non-actionable result. No secrets, paths or runtime object."""

    enabled: bool
    setup_review_digest: str | None = None
    profile_id: str | None = None
    profile_revision: str | None = None
    deployment_revision: str | None = None
    contract_revision: str | None = None
    provider_id: str | None = None
    provider_config_digest: str | None = None
    voice_aliases: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()

    @property
    def can_apply(self) -> bool:
        return False

    @property
    def status(self) -> str:
        return "review_only_blocked" if self.enabled else "disabled"

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "audio-provider-binding-review-v1",
            "enabled": self.enabled,
            "status": self.status,
            "can_apply": False,
            "setup_review_digest": self.setup_review_digest,
            "profile_id": self.profile_id,
            "profile_revision": self.profile_revision,
            "deployment_revision": self.deployment_revision,
            "contract_revision": self.contract_revision,
            "provider_id": self.provider_id,
            "provider_config_digest": self.provider_config_digest,
            "voice_aliases": list(self.voice_aliases),
            "blockers": list(self.blockers),
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )

    @property
    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()


def _voicevox_voice_match(
    profile: AudioGatewayProfile, runtime: VoicevoxOwnedRuntimeSpec
) -> bool:
    catalog_voices = {
        (v.alias, v.speaker_id, v.style_id, v.license_id, v.attribution)
        for v in profile.voices
    }
    runtime_voices = {
        (v.alias, v.speaker_uuid, str(v.style_id), v.license_id, v.attribution)
        for v in runtime.adapter.voices
    }
    return catalog_voices == runtime_voices


def _stylebert_voice_match(
    profile: AudioGatewayProfile, runtime: StyleBertOwnedRuntimeSpec
) -> bool:
    # License and attribution are present in the Gateway but not in the
    # model-local adapter voice struct. Semantic revision pins the Gateway
    # fields; an independent real license review remains mandatory.
    catalog_voices = {(v.alias, v.speaker_id, v.style_id) for v in profile.voices}
    runtime_voices = {
        (v.alias, str(v.speaker_id), v.style_id) for v in runtime.adapter.voices
    }
    return catalog_voices == runtime_voices


def _config_value(value: object, *, depth: int = 0) -> object:
    """Strict canonical snapshot for a fingerprint, never a user-facing dump.

    Includes model-native voice style and private deployment/worker attributes:
    a path, nonce, resource or native style change must change the fingerprint.
    The returned object is immediately serialized to a digest and not stored.
    This is not a secret-hiding HMAC, signed approval or byte attestation.
    """
    if depth > 14:
        raise AudioProviderBindingError()
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _config_value(getattr(value, field.name), depth=depth + 1)
            for field in fields(value)
        }
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        if len(value) > 256 or any(
            type(key) is not str or len(key) > 4096 for key in value
        ):
            raise AudioProviderBindingError()
        return {
            key: _config_value(item, depth=depth + 1)
            for key, item in sorted(value.items())
        }
    if type(value) in (tuple, list):
        if len(value) > 256:
            raise AudioProviderBindingError()
        return [_config_value(v, depth=depth + 1) for v in value]
    if type(value) in (set, frozenset):
        if len(value) > 256:
            raise AudioProviderBindingError()
        encoded = [_config_value(v, depth=depth + 1) for v in value]
        if any(type(v) is not str for v in encoded):
            raise AudioProviderBindingError()
        return sorted(encoded)
    if type(value) is str:
        if len(value) > 4096:
            raise AudioProviderBindingError()
        return value
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise AudioProviderBindingError()


def _runtime_fingerprint(
    runtime: VoicevoxOwnedRuntimeSpec | StyleBertOwnedRuntimeSpec,
) -> str:
    # Domain separator: fingerprints cannot be confused with SetupPlan grants.
    raw = _config_value(
        {
            "schema_version": "audio-provider-runtime-spec-v1",
            "runtime": runtime,
        }
    )
    try:
        encoded = json.dumps(
            raw,
            sort_keys=True,
            ensure_ascii=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise AudioProviderBindingError() from None
    if len(encoded) > 256 * 1024:
        raise AudioProviderBindingError()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def review_audio_provider_binding(
    *,
    enabled: bool,
    profile_id: str | None = None,
    catalog: AudioProfileCatalog | None = None,
    declaration: ServingDeploymentDeclaration | None = None,
    worker: WorkerSpec | None = None,
    snapshot: SetupHostSnapshot | None = None,
    runtime: VoicevoxOwnedRuntimeSpec | StyleBertOwnedRuntimeSpec | None = None,
) -> AudioProviderBindingReview:
    """Exact declared binding for two TTS providers. No I/O, no activation.

    The generic B4 review permits unrelated non-audio Worker capabilities
    because it is informational. Here we require a single-capability Worker:
    provider-local Executors only implement synthesis and an eventual registry
    must never claim additional unimplemented Worker capabilities.
    """
    if type(enabled) is not bool:
        raise AudioProviderBindingError()
    if not enabled:
        # Disabled configuration must not access or validate any candidate
        # runtime, catalog, Worker, manifest, host or filesystem.
        return AudioProviderBindingReview(enabled=False)

    if (
        type(runtime) not in (VoicevoxOwnedRuntimeSpec, StyleBertOwnedRuntimeSpec)
        or type(declaration) is not ServingDeploymentDeclaration
        or type(worker) is not WorkerSpec
        or type(snapshot) is not SetupHostSnapshot
        or type(catalog) is not AudioProfileCatalog
    ):
        raise AudioProviderBindingError()

    try:
        setup: AudioSetupReview = review_audio_setup(
            enabled=True,
            profile_id=profile_id,
            catalog=catalog,
            declaration=declaration,
            worker=worker,
            snapshot=snapshot,
        )
    except (AudioSetupReviewError, TypeError, ValueError):
        raise AudioProviderBindingError() from None

    selected = next(
        (profile for profile in catalog.profiles if profile.profile_id == profile_id),
        None,
    )
    if (
        selected is None
        or setup.capability != "audio.synthesize"
        or worker.capabilities != frozenset({"audio.synthesize"})
        or runtime.serving != declaration
        or selected.resolved.contract.features != frozenset({"ja", "wav"})
        or not selected.voices
    ):
        raise AudioProviderBindingError()

    max_input_bytes: int
    if type(runtime) is VoicevoxOwnedRuntimeSpec:
        if (
            setup.provider_id != "voicevox"
            or selected.backend_kind != "voicevox"
            or runtime.worker_resource_id != worker.worker_id
            or worker.resources.gpu_count != 0
            or not _voicevox_voice_match(selected, runtime)
        ):
            # VOICEVOX GPU ownership cannot be proved by B5b's single
            # worker_resource_id. Do not claim GPU qualification yet.
            raise AudioProviderBindingError()
        max_input_bytes = runtime.adapter.max_text_bytes

    else:
        if (
            setup.provider_id != "style-bert-vits2"
            or selected.backend_kind != "model-tts"
            or runtime.worker != worker
            or runtime.host != snapshot.runtime_host
            or (runtime.device == "cpu" and worker.resources.gpu_count != 0)
            or (runtime.device == "cuda" and worker.resources.gpu_count != 1)
            or not _stylebert_voice_match(selected, runtime)
        ):
            raise AudioProviderBindingError()
        max_input_bytes = runtime.adapter.max_text_bytes

    # Gateway must not accept a speech text payload larger than the selected
    # provider-local runtime can process. No implicit truncation/fallback.
    if selected.resolved.effective_limits["input_bytes"] > max_input_bytes:
        raise AudioProviderBindingError()

    return AudioProviderBindingReview(
        enabled=True,
        setup_review_digest=setup.digest,
        profile_id=selected.profile_id,
        profile_revision=selected.resolved.profile_revision,
        deployment_revision=declaration.deployment.revision,
        contract_revision=declaration.contracts[0].revision,
        provider_id=declaration.deployment.provider_id,
        provider_config_digest=_runtime_fingerprint(runtime),
        voice_aliases=tuple(v.alias for v in selected.voices),
        blockers=tuple(
            sorted(
                set(setup.blockers)
                | {
                    "native_transport_and_process_identity_unqualified",
                    "provider_runtime_registry_not_enabled",
                    "voice_model_and_license_real_acceptance_pending",
                    "execution_config_provenance_unqualified",
                }
            )
        ),
    )
