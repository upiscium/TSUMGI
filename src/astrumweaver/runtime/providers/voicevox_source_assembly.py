"""#231 B6d: reviewed VOICEVOX source-only assembly, never provider admission.

This module is deliberately absent from runtime.deployment._PROVIDER_TYPES,
shared RuntimeProvider exports, Control, Worker daemon, and SetupPlan.
Reviewing declarations and constructing an inert ManagedRuntime object are
not proof of real VOICEVOX, model loading, process provenance or voice rights.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from ...contracts import WorkerSpec
from ...gateway.audio import AudioProfileCatalog
from ...serving import ServingDeploymentDeclaration
from ...setup.audio_provider_binding import (
    AudioProviderBindingError,
    review_audio_provider_binding,
)
from ...setup.contracts import SetupHostSnapshot
from .voicevox import VOICEVOX_PROVIDER_ID
from .voicevox_http import VoicevoxHttpConfig, VoicevoxLoopbackHttpApi
from .voicevox_linux_process import (
    LinuxVoicevoxOwnedProcessControl,
    VoicevoxLinuxLaunchSpec,
)
from .voicevox_managed import VoicevoxOwnedManagedRuntime, VoicevoxOwnedRuntimeSpec


class VoicevoxSourceAssemblyError(ValueError):
    """Redacted, fixed text independent of private filenames and voice data."""

    def __init__(self) -> None:
        super().__init__("VOICEVOX source assembly review is invalid")


@dataclass(frozen=True, slots=True, repr=False)
class VoicevoxSourceAssemblyIntent:
    """Exact operator-authored declarations; NO verified executable authority.

    Each component must be frozen by its own constructor. Some embedded
    mappings are still mutable: revalidation and the stored review digest
    detect any content drift before composing a source-only runtime.
    """

    profile_id: str
    catalog: AudioProfileCatalog
    declaration: ServingDeploymentDeclaration
    worker: WorkerSpec
    snapshot: SetupHostSnapshot
    runtime: VoicevoxOwnedRuntimeSpec
    launch: VoicevoxLinuxLaunchSpec
    http: VoicevoxHttpConfig


@dataclass(frozen=True, slots=True, repr=False)
class VoicevoxSourceAssemblyReview:
    """Informational report, not a SetupPlan, grant, or RuntimeProvider."""

    enabled: bool
    binding_digest: str | None = None
    assembly_digest: str | None = None
    profile_id: str | None = None
    provider_id: str | None = None
    blockers: tuple[str, ...] = ()
    _intent: VoicevoxSourceAssemblyIntent | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    @property
    def can_apply(self) -> bool:
        return False

    @property
    def status(self) -> str:
        return "review_only_blocked" if self.enabled else "disabled"

    def to_dict(self) -> dict[str, object]:
        """Allowlist fields only; no executable, argv, native URL or secrets."""
        return {
            "schema_version": "voicevox-source-assembly-v1",
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
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )

    def compose_disposable_source_runtime(self) -> VoicevoxOwnedManagedRuntime:
        """Construct without start, bind, socket, filesystem or Control access.

        This is for disposable source acceptance ONLY. It never grants Worker
        admission and is not present in the executable RuntimeProvider registry.
        Re-check every binding and hash before returning even an inert object.
        """
        if not self.enabled or self._intent is None:
            raise VoicevoxSourceAssemblyError()
        reviewed = review_voicevox_source_assembly(
            enabled=True,
            intent=self._intent,
        )
        if (
            reviewed.assembly_digest is None
            or reviewed.assembly_digest != self.assembly_digest
            or reviewed.binding_digest != self.binding_digest
        ):
            raise VoicevoxSourceAssemblyError()
        try:
            intent = self._intent
            controller = LinuxVoicevoxOwnedProcessControl(
                launch_spec=intent.launch,
                config=intent.runtime,
            )
            transport = VoicevoxLoopbackHttpApi(intent.http)
            return VoicevoxOwnedManagedRuntime(
                config=intent.runtime,
                process=controller,
                engine=transport,
            )
        except Exception:  # noqa: BLE001 - redact arbitrary process/transport errors
            # The caller cannot inspect or log the underlying native paths.
            raise VoicevoxSourceAssemblyError() from None


def _bound_launch_args(args: tuple[str, ...], port: int) -> bool:
    """Conservative source-only port/host token convention.

    Native VOICEVOX CLI flags still require later operator qualification.
    Requiring an explicit target port here blocks a silent fixed-argv/config
    mismatch but does not claim that the native binary honors its arguments.
    """
    target = str(port)
    if target not in args and f"--port={port}" not in args:
        return False
    for index, arg in enumerate(args):
        if arg in ("--port", "-p"):
            if index + 1 >= len(args) or args[index + 1] != target:
                return False
        elif arg.startswith("--port=") and arg != f"--port={port}":
            return False
        elif arg in ("--host", "--listen"):
            if index + 1 >= len(args) or args[index + 1] != "127.0.0.1":
                return False
        elif (
            arg.startswith(("--host=", "--listen="))
            and arg.split("=", 1)[1] != "127.0.0.1"
        ) or arg in ("0.0.0.0", "::", "::1"):
            return False
    return True


def review_voicevox_source_assembly(
    *,
    enabled: bool,
    intent: VoicevoxSourceAssemblyIntent | None = None,
) -> VoicevoxSourceAssemblyReview:
    """Side-effect-free exact config review. Disabled inspects NOTHING.

    A positive review ALWAYS leaves real/native/package/licensing/Registry
    gates blocked. Actual process/file/port checks are deferred to start().
    """
    if type(enabled) is not bool:
        raise VoicevoxSourceAssemblyError()
    if not enabled:
        return VoicevoxSourceAssemblyReview(enabled=False)
    if (
        type(intent) is not VoicevoxSourceAssemblyIntent
        or type(intent.runtime) is not VoicevoxOwnedRuntimeSpec
        or type(intent.launch) is not VoicevoxLinuxLaunchSpec
        or type(intent.http) is not VoicevoxHttpConfig
        or type(intent.declaration) is not ServingDeploymentDeclaration
        or type(intent.worker) is not WorkerSpec
        or type(intent.snapshot) is not SetupHostSnapshot
        or type(intent.catalog) is not AudioProfileCatalog
        or type(intent.profile_id) is not str
    ):
        raise VoicevoxSourceAssemblyError()

    runtime = intent.runtime
    launch = intent.launch
    http = intent.http
    port = urlsplit(runtime.endpoint).port  # Already canonical localhost URL.
    if (
        runtime.serving != intent.declaration
        or runtime.worker_resource_id != intent.worker.worker_id
        or intent.worker.resources.gpu_count != 0
        or runtime.serving.deployment.provider_id != VOICEVOX_PROVIDER_ID
        or launch.executable_path != runtime.executable_path
        or http.endpoint != runtime.endpoint
        or http.adapter != runtime.adapter
        or launch.stop_timeout_seconds > runtime.stop_timeout_seconds
        or http.request_timeout_seconds > runtime.adapter.synthesis_timeout_seconds
        or port is None
        or not _bound_launch_args(launch.fixed_args, port)
    ):
        raise VoicevoxSourceAssemblyError()

    try:
        binding = review_audio_provider_binding(
            enabled=True,
            profile_id=intent.profile_id,
            catalog=intent.catalog,
            declaration=intent.declaration,
            worker=intent.worker,
            snapshot=intent.snapshot,
            runtime=runtime,
        )
        if (
            not binding.enabled
            or binding.provider_id != VOICEVOX_PROVIDER_ID
            or binding.can_apply
            or binding.status != "review_only_blocked"
        ):
            raise VoicevoxSourceAssemblyError()
        # The digest contains private static provenance, not native file
        # contents. It is deliberately neither an HMAC nor an authorization.
        encoded = json.dumps(
            {
                "schema_version": "voicevox-source-assembly-v1",
                "binding_digest": binding.digest,
                "launch": {
                    "executable_path": launch.executable_path,
                    "args": launch.fixed_args,
                    "auxiliary_path": launch.auxiliary_path,
                    "auxiliary_sha256": launch.auxiliary_sha256,
                    "stop_timeout_seconds": launch.stop_timeout_seconds,
                },
                "transport": {
                    "endpoint": http.endpoint,
                    "request_timeout_seconds": http.request_timeout_seconds,
                },
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
        if len(encoded) > 256 * 1024:
            raise VoicevoxSourceAssemblyError()
        digest = "sha256:" + hashlib.sha256(encoded).hexdigest()
        return VoicevoxSourceAssemblyReview(
            enabled=True,
            binding_digest=binding.digest,
            assembly_digest=digest,
            profile_id=binding.profile_id,
            provider_id=VOICEVOX_PROVIDER_ID,
            blockers=tuple(
                sorted(
                    set(binding.blockers)
                    | {
                        "source_assembly_is_not_runtime_admission",
                        "native_execution_and_model_load_unqualified",
                        "native_access_log_and_license_review_pending",
                    }
                )
            ),
            _intent=intent,
        )
    except (
        AudioProviderBindingError,
        TypeError,
        ValueError,
        UnicodeError,
        RecursionError,
    ):
        raise VoicevoxSourceAssemblyError() from None
