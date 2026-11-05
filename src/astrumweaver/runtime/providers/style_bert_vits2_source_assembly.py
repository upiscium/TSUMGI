"""#235 B6e: Style-Bert source-only, non-applicable provider-local assembly.

The reviewed declaration and disposable constructor do not grant an installed
runtime, native model-loaded proof, license approval, or Worker admission.
Nothing here registers a provider or creates an executable SetupPlan.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace

from ...contracts import WorkerSpec
from ...gateway.audio import AudioProfileCatalog
from ...serving import ServingDeploymentDeclaration
from ...setup.audio_provider_binding import review_audio_provider_binding
from ...setup.contracts import SetupHostSnapshot
from .style_bert_vits2 import (
    STYLE_BERT_VITS2_PROVIDER_ID,
    StyleBertVits2ModelPin,
    StyleBertVits2ProviderConfig,
    StyleBertVits2Voice,
)
from .style_bert_vits2_managed import (
    StyleBertOwnedManagedRuntime,
    StyleBertOwnedRuntimeSpec,
)


class StyleBertSourceAssemblyError(ValueError):
    """Fixed redacted response; never reflect private config in diagnostics."""

    def __init__(self) -> None:
        super().__init__("Style-Bert source assembly review is invalid")


@dataclass(frozen=True, slots=True, repr=False)
class StyleBertSourceAssemblyIntent:
    """Operator declarations, NOT an authenticated model or apply grant.

    Frozen fields can contain mutable maps and can be reflectively altered.
    The review revalidates their structural contracts and hashes their whole
    binding; the disposable constructor must check the same digest again.
    """

    profile_id: str
    catalog: AudioProfileCatalog
    declaration: ServingDeploymentDeclaration
    worker: WorkerSpec
    snapshot: SetupHostSnapshot
    runtime: StyleBertOwnedRuntimeSpec


@dataclass(frozen=True, slots=True, repr=False)
class StyleBertSourceAssemblyReview:
    """Public-safe informational record, never a RuntimeProvider or SetupPlan."""

    enabled: bool
    binding_digest: str | None = None
    assembly_digest: str | None = None
    profile_id: str | None = None
    provider_id: str | None = None
    blockers: tuple[str, ...] = ()
    _intent: StyleBertSourceAssemblyIntent | None = field(
        default=None, repr=False, compare=False
    )

    @property
    def can_apply(self) -> bool:
        return False

    @property
    def status(self) -> str:
        return "review_only_blocked" if self.enabled else "disabled"

    def to_dict(self) -> dict[str, object]:
        """Never serialize native paths, model names, UUIDs, voices or argv."""
        return {
            "schema_version": "stylebert-source-assembly-v1",
            "enabled": self.enabled,
            "status": self.status,
            "can_apply": False,
            "binding_digest": self.binding_digest,
            "assembly_digest": self.assembly_digest,
            "profile_id": self.profile_id,
            "provider_id": self.provider_id,
            "blockers": list(self.blockers),
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            sort_keys=True,
            ensure_ascii=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    def compose_disposable_source_runtime(self) -> StyleBertOwnedManagedRuntime:
        """Construct STOPPED object only; no model, process, GPU or file I/O."""
        if not self.enabled or self._intent is None:
            raise StyleBertSourceAssemblyError()
        verified = review_stylebert_source_assembly(enabled=True, intent=self._intent)
        if (
            self.assembly_digest is None
            or verified.assembly_digest != self.assembly_digest
            or verified.binding_digest != self.binding_digest
        ):
            raise StyleBertSourceAssemblyError()
        try:
            return StyleBertOwnedManagedRuntime(config=self._intent.runtime)
        except Exception:  # noqa: BLE001 - no private exception text
            raise StyleBertSourceAssemblyError() from None


def review_stylebert_source_assembly(
    *,
    enabled: bool,
    intent: StyleBertSourceAssemblyIntent | None = None,
) -> StyleBertSourceAssemblyReview:
    """Disabled mode inspects no candidate. Enabled mode never authorizes it."""
    if type(enabled) is not bool:
        raise StyleBertSourceAssemblyError()
    if not enabled:
        return StyleBertSourceAssemblyReview(enabled=False)
    try:
        return _review_enabled(intent)
    except Exception:  # noqa: BLE001 - reflective/mutable corruption is untrusted
        # A malformed private filename/Unicode string must not escape as
        # a raw ValueError, UnicodeError or TypeError containing input.
        raise StyleBertSourceAssemblyError() from None


def _review_enabled(
    intent: StyleBertSourceAssemblyIntent | None,
) -> StyleBertSourceAssemblyReview:
    if (
        type(intent) is not StyleBertSourceAssemblyIntent
        or type(intent.catalog) is not AudioProfileCatalog
        or type(intent.declaration) is not ServingDeploymentDeclaration
        or type(intent.worker) is not WorkerSpec
        or type(intent.snapshot) is not SetupHostSnapshot
        or type(intent.profile_id) is not str
        or type(intent.runtime) is not StyleBertOwnedRuntimeSpec
    ):
        raise StyleBertSourceAssemblyError()

    spec = intent.runtime
    worker = intent.worker
    snapshot = intent.snapshot
    if (
        type(spec.adapter) is not StyleBertVits2ProviderConfig
        or type(spec.adapter.model) is not StyleBertVits2ModelPin
        or type(spec.adapter.voices) is not tuple
        or any(type(v) is not StyleBertVits2Voice for v in spec.adapter.voices)
    ):
        raise StyleBertSourceAssemblyError()

    # Rerun pure dataclass validators against reflectively mutated nested
    # values. In particular, recheck filenames, SHA pins, weight/config/style
    # vectors, interpreter/runner SHA, revisions and the declared rights flag.
    model = replace(spec.adapter.model)
    voices = tuple(replace(v) for v in spec.adapter.voices)
    adapter = replace(spec.adapter, model=model, voices=voices)
    checked = replace(spec, adapter=adapter)
    if checked != spec:
        raise StyleBertSourceAssemblyError()

    if (
        spec.serving != intent.declaration
        or spec.worker != worker
        or spec.host != snapshot.runtime_host
        or intent.declaration.deployment.provider_id != STYLE_BERT_VITS2_PROVIDER_ID
        or worker.capabilities != frozenset({"audio.synthesize"})
        or type(worker.resources.gpu_count) is not int
    ):
        raise StyleBertSourceAssemblyError()

    gpu_count = worker.resources.gpu_count
    if spec.device == "cpu":
        if (
            gpu_count != 0
            or worker.gpu_uuids
            or spec.gpu_uuid is not None
            or spec.required_single_gpu_vram_mb != 0
        ):
            raise StyleBertSourceAssemblyError()
    elif spec.device == "cuda":
        if (
            gpu_count != 1
            or type(worker.gpu_uuids) is not tuple
            or worker.gpu_uuids != (spec.gpu_uuid,)
            or type(spec.required_single_gpu_vram_mb) is not int
            or spec.required_single_gpu_vram_mb < 1
            or spec.required_single_gpu_vram_mb
            > worker.resources.max_single_gpu_vram_mb
            or spec.required_single_gpu_vram_mb > worker.resources.total_vram_mb
        ):
            raise StyleBertSourceAssemblyError()
    else:
        raise StyleBertSourceAssemblyError()

    binding = review_audio_provider_binding(
        enabled=True,
        profile_id=intent.profile_id,
        catalog=intent.catalog,
        declaration=intent.declaration,
        worker=worker,
        snapshot=snapshot,
        runtime=spec,
    )
    if (
        binding.enabled is not True
        or binding.can_apply is not False
        or binding.status != "review_only_blocked"
        or binding.provider_id != STYLE_BERT_VITS2_PROVIDER_ID
        or type(binding.provider_config_digest) is not str
        or not binding.provider_config_digest.startswith("sha256:")
    ):
        raise StyleBertSourceAssemblyError()

    # This is an opaque *fingerprint* of the full private runtime spec and
    # exact cross-layer binding; it is not a signature, package proof or grant.
    raw = json.dumps(
        {
            "schema_version": "stylebert-source-assembly-v1",
            "binding_digest": binding.digest,
            "runtime_digest": binding.provider_config_digest,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    blockers = {
        *binding.blockers,
        "source_assembly_is_not_runtime_admission",
        "native_runner_and_loaded_model_unqualified",
        "native_process_ipc_and_dependency_abi_unqualified",
        "native_license_attribution_and_privacy_unqualified",
    }
    if spec.device == "cuda":
        blockers.add("real_gpu_isolation_and_vram_unqualified")
    return StyleBertSourceAssemblyReview(
        enabled=True,
        binding_digest=binding.digest,
        assembly_digest=digest,
        profile_id=binding.profile_id,
        provider_id=STYLE_BERT_VITS2_PROVIDER_ID,
        blockers=tuple(sorted(blockers)),
        _intent=intent,
    )
