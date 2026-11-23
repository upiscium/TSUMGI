"""Manifest-only, pre-provisioned Linux x86-64 CPU VOICEVOX provider.

Construction neither installs software nor starts or adopts an engine. The
existing owned runtime, not the two manifest declarations, decides readiness.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sys

from ...contracts import ResourceShape
from ...gateway.audio import SPEECH_OPERATION_SCHEMA, VoiceAlias, audio_semantic_revision
from ...serving import DeploymentIdentity, ServingContract, ServingDeploymentDeclaration
from ..contracts import (
    CompatibilityReason, ExecutionDemand, GPUTopology, ModelDemand, ModelTopology,
    ResidencyPolicy, RuntimeCompatibility, RuntimeCompatibilityContext, RuntimeProviderInfo,
    RuntimeSetupIntent,
)
from .voicevox import VOICEVOX_PROVIDER_ID, VoicevoxAdapterConfig, VoicevoxVoicePin
from .voicevox_http import VoicevoxHttpConfig, VoicevoxLoopbackHttpApi
from .voicevox_linux_process import LinuxVoicevoxOwnedProcessControl, VoicevoxLinuxLaunchSpec
from .voicevox_managed import VoicevoxOwnedManagedRuntime, VoicevoxOwnedRuntimeSpec


ARCHIVE_SHA256 = "sha256:483633de02700cd918731538543a22ce895877aeb09450a990ff38af6e4c26c0"
EXECUTABLE_SHA256 = "sha256:acf74848282c0ff6f0aaf8514427c5f44b0992c26b48448d76b35a97dbe81a25"
MODEL_SHA256 = "sha256:ecd35374d4182cd883cba5040376f7f888cc6ba248b1c2f4cea07cdb34bb1318"
_MODEL = "model/0.vvm"
_VERSION = "0.26.2"
_STYLE = ("ずんだもん", "ノーマル", "streaming_talk")
_PIN = VoicevoxVoicePin("zundamon-normal", "388f246b-8c41-4ac1-8e2d-5d79f3ff56d9",
                        3, "voicevox-zundamon", "VOICEVOX:ずんだもん")
_VOICE = VoiceAlias(_PIN.alias, _PIN.speaker_uuid, str(_PIN.style_id), "ja",
                    _PIN.license_id, _PIN.attribution)
_SEMANTIC = audio_semantic_revision("voicevox", (_VOICE,))
_DEMAND = ExecutionDemand(ModelDemand(_MODEL, "vvm", ModelTopology.DENSE),
                          ResidencyPolicy.CPU_GPU_HYBRID, GPUTopology.NONE)
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_INPUT_BYTES = 8192


def _invalid() -> ValueError:
    return ValueError("VOICEVOX manifest configuration is invalid")


def _absolute_path(value: object) -> bool:
    if (type(value) is not str or not value.startswith("/") or value.startswith("//")
            or value == "/" or value != os.path.normpath(value)
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        return False
    try:
        return len(value.encode("utf-8")) <= 4096
    except UnicodeError:
        return False


def _digest(material: object) -> str:
    data = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _installed_adapter_digest() -> str:
    """Hash the fixed adapter bundle in the installed, trusted TSUMGI package."""
    names = ("voicevox.py", "voicevox_managed.py", "voicevox_http.py",
             "voicevox_linux_process.py", "voicevox_manifest.py")
    try:
        return _digest({name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                        for name in names})
    except OSError:
        raise _invalid() from None


@dataclass(frozen=True, slots=True, kw_only=True, repr=False)
class VoicevoxProviderConfig:
    engine_root: str
    private_home: str
    port: int
    archive_sha256: str
    rights_review_sha256: str
    validation_evidence_sha256: str
    enabled: bool = False

    def __post_init__(self) -> None:
        if (self.enabled is not True
                or not _absolute_path(self.engine_root) or not _absolute_path(self.private_home)
                or type(self.port) is not int or not 1024 <= self.port <= 65535
                or self.archive_sha256 != ARCHIVE_SHA256
                or any(type(v) is not str or _DIGEST.fullmatch(v) is None
                       for v in (self.rights_review_sha256, self.validation_evidence_sha256))):
            raise _invalid()
        root, home = Path(self.engine_root), Path(self.private_home)
        if home.is_relative_to(root) or root.is_relative_to(home):
            raise _invalid()

    def serving_declaration(self) -> ServingDeploymentDeclaration:
        """Independent expected binding. Never copy pins out of serving.manifest."""
        deployment = DeploymentIdentity(
            provider_id=VOICEVOX_PROVIDER_ID, runtime_artifact_sha256=EXECUTABLE_SHA256,
            adapter_artifact_sha256=_installed_adapter_digest(), model_artifact_sha256=MODEL_SHA256,
            # No deployment-local paths, input text, tokens or Worker identifiers.
            execution_config_sha256=_digest({
                "schema": "voicevox-owned-linux-cpu-v1", "archive": self.archive_sha256,
                "version": _VERSION, "port": self.port, "style": _STYLE,
                "rights_review": self.rights_review_sha256,
            }),
            quantization="none",
        )
        return ServingDeploymentDeclaration(deployment, (ServingContract(
            deployment_revision=deployment.revision, capability="audio.synthesize",
            operation_schema=SPEECH_OPERATION_SCHEMA, semantic_revision=_SEMANTIC,
            validation_evidence_sha256=self.validation_evidence_sha256, features={"ja", "wav"},
            limits={"input_bytes": _INPUT_BYTES, "request_bytes": 16384},
        ),))


@dataclass(frozen=True, slots=True)
class VoicevoxProvider:
    config: VoicevoxProviderConfig
    info = RuntimeProviderInfo(VOICEVOX_PROVIDER_ID, "Owned VOICEVOX CPU (pre-provisioned)")

    def compatibility(self, context: RuntimeCompatibilityContext) -> RuntimeCompatibility:
        worker = context.worker
        ok = (self.config.enabled is True and sys.platform == "linux"
              and context.host.architecture == "x86_64" and os.geteuid() != 0
              and worker.capabilities == {"audio.synthesize"} and worker.resources == ResourceShape()
              and not worker.gpu_uuids and not worker.accelerators and context.demand == _DEMAND)
        return RuntimeCompatibility(VOICEVOX_PROVIDER_ID, () if ok else (CompatibilityReason(
            "voicevox_worker_incompatible", "requires the dedicated unprivileged Linux x86-64 CPU speech Worker",
        ),))

    def setup_intent(self, context: RuntimeCompatibilityContext) -> RuntimeSetupIntent:
        raise ValueError("VOICEVOX installation and SetupPlan activation are not supported")

    def create_runtime(self, context: RuntimeCompatibilityContext, setup: RuntimeSetupIntent):
        raise ValueError("VOICEVOX requires a reviewed serving manifest; setup is not supported")

    def create_manifest_runtime(
        self, context: RuntimeCompatibilityContext, serving: ServingDeploymentDeclaration | None,
    ) -> VoicevoxOwnedManagedRuntime:
        cfg = self.config
        if (not self.compatibility(context).compatible or type(serving) is not ServingDeploymentDeclaration
                or serving != cfg.serving_declaration()):
            raise _invalid()
        adapter = VoicevoxAdapterConfig(
            _VERSION, EXECUTABLE_SHA256, serving.deployment.adapter_artifact_sha256, MODEL_SHA256,
            _SEMANTIC, (_PIN,), max_text_bytes=_INPUT_BYTES,
        )
        endpoint = f"http://127.0.0.1:{cfg.port}"
        spec = VoicevoxOwnedRuntimeSpec(
            adapter=adapter, serving=serving, executable_path=str(Path(cfg.engine_root) / "run"),
            model_artifact_path=str(Path(cfg.engine_root) / _MODEL), endpoint=endpoint,
            worker_resource_id=context.worker.worker_id, owner_uid=os.geteuid(),
            approved_voice_aliases=frozenset({_PIN.alias}),
            voice_rights_review_sha256=cfg.rights_review_sha256, startup_timeout_seconds=120,
        )
        process = LinuxVoicevoxOwnedProcessControl(config=spec, launch_spec=VoicevoxLinuxLaunchSpec(
            spec.executable_path, ("--host", "127.0.0.1", "--port", str(cfg.port)),
            private_home=cfg.private_home,
        ))
        return VoicevoxOwnedManagedRuntime(config=spec, process=process, engine=VoicevoxLoopbackHttpApi(
            VoicevoxHttpConfig(endpoint, adapter, strict_style_identity=_STYLE),
        ))
