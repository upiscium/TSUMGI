"""Explicit, pre-provisioned CPU JVNV provider. No install, discovery or fallback."""
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
from . import jvnv_assets as assets
from .style_bert_vits2 import (
    STYLE_BERT_VITS2_PROVIDER_ID, StyleBertVits2ModelPin, StyleBertVits2ProviderConfig,
    StyleBertVits2Voice,
)
from .style_bert_vits2_managed import StyleBertOwnedManagedRuntime, StyleBertOwnedRuntimeSpec

_ID = STYLE_BERT_VITS2_PROVIDER_ID
_MODEL = "jvnv-F1-jp"
_DEMAND = ExecutionDemand(ModelDemand(_MODEL, "safetensors", ModelTopology.DENSE),
                          ResidencyPolicy.CPU_GPU_HYBRID, GPUTopology.NONE)
_VOICE = VoiceAlias("jvnv-f1-neutral", "0", "0", "ja", "CC-BY-SA-4.0",
                    "litagin / JVNV (CC BY-SA 4.0)")
_SEMANTIC = audio_semantic_revision("model-tts", (_VOICE,))
_RUNNER = Path(__file__).with_name("jvnv_child.py")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ENGINE = "d8148f3090ee5038ca7b4e4b327116c64467f952"
_MODEL_REV = "205830ca1d49e666ddfbf2a755f0108e9cade4dd"
_ENCODER_REV = "547b0e8b044fba3f9b84d0ab9f990440bd130c8b"


def _invalid() -> ValueError:
    return ValueError("Style-Bert-VITS2 manifest configuration is invalid")


def _absolute(value: object) -> bool:
    if (type(value) is not str or not value.startswith("/") or value.startswith("//") or value == "/"
            or os.path.normpath(value) != value or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        return False
    try:
        return len(value.encode("utf-8")) <= 4096
    except UnicodeError:
        return False


def _digest(obj: dict) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"),
                                                 ensure_ascii=True).encode()).hexdigest()


def _code_pin(path: Path) -> str:
    try:
        if path.is_symlink() or not path.is_file():
            raise _invalid()
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        raise _invalid() from None


@dataclass(frozen=True, slots=True, kw_only=True, repr=False)
class StyleBertManifestConfig:
    model_directory: str
    interpreter_path: str
    dependency_lock_path: str
    dictionary_directory: str
    dictionary_sha256: str
    private_home: str
    rights_review_sha256: str
    validation_evidence_sha256: str
    enabled: bool = False

    def __post_init__(self) -> None:
        if (self.enabled is not True or not all(_absolute(x) for x in (
                self.model_directory, self.interpreter_path, self.dependency_lock_path, self.private_home,
                self.dictionary_directory))
                or Path(self.model_directory).name != _MODEL
                or Path(self.interpreter_path).parts[-2:] != ("bin", "python")
                or any(type(v) is not str or not _DIGEST.fullmatch(v) for v in
                       (self.rights_review_sha256, self.validation_evidence_sha256, self.dictionary_sha256))):
            raise _invalid()
        home = Path(self.private_home)
        model = Path(self.model_directory)
        roots = (model, model.parent / "bert", Path(self.interpreter_path).parent.parent,
                 Path(self.dependency_lock_path), Path(self.dictionary_directory))
        if any(home.is_relative_to(p) or p.is_relative_to(home) for p in roots):
            raise _invalid()

    def serving_declaration(self) -> ServingDeploymentDeclaration:
        """Expected pins come from the trusted package, not the supplied serving.json."""
        names = ("style_bert_vits2.py", "style_bert_vits2_managed.py",
                 "style_bert_vits2_manifest.py", "jvnv_assets.py")
        dep = DeploymentIdentity(
            provider_id=_ID, runtime_artifact_sha256=assets.INTERPRETER_SHA256,
            adapter_artifact_sha256=_code_pin(_RUNNER), model_artifact_sha256=assets.MODEL_SHA256,
            execution_config_sha256=_digest({
                "schema": "owned-jvnv-f1-jp-cpu-v1", "engine": _ENGINE, "lock": assets.LOCK_SHA256,
                "model_snapshot": _MODEL_REV, "config": assets.CONFIG_SHA256, "styles": assets.STYLES_SHA256,
                "encoder_snapshot": _ENCODER_REV, "ready_probe": "pcm16-wav-v1",
                "dictionary_sha256": self.dictionary_sha256,
                "rights_review": self.rights_review_sha256,
                "adapter_files": {name: _code_pin(_RUNNER.with_name(name)) for name in names},
            }), quantization="fp32",
        )
        return ServingDeploymentDeclaration(dep, (ServingContract(
            deployment_revision=dep.revision, capability="audio.synthesize", operation_schema=SPEECH_OPERATION_SCHEMA,
            validation_evidence_sha256=self.validation_evidence_sha256, semantic_revision=_SEMANTIC,
            features={"ja", "wav"}, limits={"request_bytes": 4096, "input_bytes": 256},
        ),))


@dataclass(frozen=True, slots=True)
class StyleBertManifestProvider:
    config: StyleBertManifestConfig
    info = RuntimeProviderInfo(_ID, "Style-Bert-VITS2 JVNV F1 CPU (pre-provisioned)")

    def compatibility(self, context: RuntimeCompatibilityContext) -> RuntimeCompatibility:
        w = context.worker
        ok = (self.config.enabled is True and sys.platform == "linux" and os.geteuid() != 0
              and context.host.architecture == "x86_64" and context.host.host_ram_mb >= 4096
              and w.capabilities == {"audio.synthesize"} and w.resources == ResourceShape()
              and not w.gpu_uuids and not w.accelerators and context.demand == _DEMAND)
        return RuntimeCompatibility(_ID, () if ok else (CompatibilityReason(
            "jvnv_worker_incompatible", "requires the dedicated unprivileged Linux x86-64 CPU speech Worker",
        ),))

    def setup_intent(self, context: RuntimeCompatibilityContext) -> RuntimeSetupIntent:
        raise ValueError("Style-Bert-VITS2 installation and SetupPlan activation are not supported")

    def create_runtime(self, context: RuntimeCompatibilityContext, setup: RuntimeSetupIntent):
        raise ValueError("Style-Bert-VITS2 requires a reviewed serving manifest")

    def create_manifest_runtime(
        self, context: RuntimeCompatibilityContext, serving: ServingDeploymentDeclaration | None,
    ) -> StyleBertOwnedManagedRuntime:
        cfg = self.config
        if (not self.compatibility(context).compatible or type(serving) is not ServingDeploymentDeclaration
                or serving != cfg.serving_declaration()):
            raise _invalid()
        runner_sha = serving.deployment.adapter_artifact_sha256
        model = StyleBertVits2ModelPin(
            _MODEL, assets.WEIGHT_FILENAME, assets.MODEL_SHA256.removeprefix("sha256:"),
            assets.CONFIG_SHA256.removeprefix("sha256:"), assets.STYLES_SHA256.removeprefix("sha256:"),
            _ENGINE, runner_sha,
        )
        adapter = StyleBertVits2ProviderConfig(
            model, (StyleBertVits2Voice(_VOICE.alias, 0, "0", "Neutral"),), _SEMANTIC,
            max_text_bytes=256, max_output_bytes=4 * 1024**2, max_duration_seconds=30,
            synthesis_timeout_seconds=90,
        )
        return StyleBertOwnedManagedRuntime(config=StyleBertOwnedRuntimeSpec(
            adapter=adapter, serving=serving, worker=context.worker, host=context.host,
            model_directory=cfg.model_directory, interpreter_path=cfg.interpreter_path,
            runner_path=str(_RUNNER), runner_sha256=runner_sha, interpreter_sha256=assets.INTERPRETER_SHA256,
            rights_review_sha256=cfg.rights_review_sha256, voice_rights_qualified=True, required_host_ram_mb=4096,
            jvnv_inputs=assets.JvnvInputs(private_home=cfg.private_home, dependency_lock_path=cfg.dependency_lock_path,
                                         dictionary_directory=cfg.dictionary_directory,
                                         dictionary_sha256=cfg.dictionary_sha256),
            boot_timeout_seconds=120, inference_timeout_seconds=90,
        ))
