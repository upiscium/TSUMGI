"""Explicit pre-provisioned Parakeet JA manifest; never installs a runtime."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

from ...gateway.audio import TRANSCRIPTION_OPERATION_SCHEMA, audio_semantic_revision
from ...serving import DeploymentIdentity, ServingContract, ServingDeploymentDeclaration
from ..contracts import (
    CompatibilityReason, ExecutionDemand, GPUTopology, ModelDemand, ModelTopology,
    ResidencyPolicy, RuntimeCompatibility, RuntimeCompatibilityContext, RuntimeProviderInfo,
)
from .parakeet_environment import validate_environment

PROVIDER_ID = "parakeet-ja"
MODEL_REF = "nvidia/parakeet-tdt_ctc-0.6b-ja"
MODEL_REVISION = "44edb27eea9317daf89333e75eb830db4b1cc298"
MODEL_SHA256 = "sha256:a2ed2aab9c82cce4e9b699dca669184d7525c7b5e1eb8c3f7b789421bc3637e8"
INTERPRETER_SHA256 = "sha256:e3cc9523fbfbd83a0fbc51fa568868e77e06388480297a7ece177ce491bbe1b9"
LOCK_SHA256 = "sha256:a25119ce239944b4eedb6bd87f27ea99f5d865f993b64c64ddf520c5335751be"
# Conservative declared capacity of the qualified trial GPU, NOT a measured
# minimum, free-VRAM reservation, or a qualification of other CUDA hardware.
DEMAND = ExecutionDemand(
    ModelDemand(MODEL_REF, "nemo", ModelTopology.DENSE),
    ResidencyPolicy.VRAM_ONLY, GPUTopology.SINGLE_GPU, min_gpu_count=1,
    min_total_vram_mb=11264, min_single_gpu_vram_mb=11264,
)
_SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")
_GPU = re.compile(r"GPU-[A-Fa-f0-9]{8}(?:-[A-Fa-f0-9]{4}){3}-[A-Fa-f0-9]{12}\Z")
_ERROR = "Parakeet manifest configuration is invalid"
_CODE = ("parakeet.py", "parakeet_native.py", "parakeet_child.py",
         "parakeet_process.py", "parakeet_manifest.py", "parakeet_managed.py",
         "parakeet_environment.py")


def _invalid() -> ValueError:
    return ValueError(_ERROR)


def _absolute(value: object) -> bool:
    if (type(value) is not str or not value.startswith("/") or value.startswith("//")
            or value == "/" or os.path.normpath(value) != value
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        return False
    try:
        return len(value.encode("utf-8")) <= 4096
    except UnicodeError:
        return False


def _trusted(path: Path) -> os.stat_result:
    if str(path.resolve(strict=True)) != str(path):
        raise _invalid()
    for parent in path.parents:
        info = parent.lstat()
        sticky = (str(parent) in ("/tmp", "/var/tmp") and info.st_uid == 0
                  and info.st_mode & stat.S_ISVTX)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.geteuid())
                or (info.st_mode & 0o022 and not sticky)):
            raise _invalid()
    info = path.lstat()
    if info.st_uid not in (0, os.geteuid()) or info.st_mode & 0o022:
        raise _invalid()
    return info


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)


def _file_digest(path: Path, maximum: int) -> str:
    before = _trusted(path)
    if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= maximum:
        raise _invalid()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    digest, count = hashlib.sha256(), 0
    with os.fdopen(fd, "rb") as stream:
        if _identity(os.fstat(fd)) != _identity(before):
            raise _invalid()
        while count < before.st_size:
            block = stream.read(min(1024**2, before.st_size - count))
            if not block:
                raise _invalid()
            count += len(block)
            digest.update(block)
        if stream.read(1) or _identity(os.fstat(fd)) != _identity(before):
            raise _invalid()
    if _identity(_trusted(path)) != _identity(before):
        raise _invalid()
    return "sha256:" + digest.hexdigest()


def _digest(value: dict) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode()).hexdigest()


@dataclass(frozen=True, slots=True, kw_only=True, repr=False)
class ParakeetManifestConfig:
    model_path: str
    interpreter_path: str
    base_python_root: str
    dependency_lock_path: str
    private_home: str
    gpu_uuid: str
    rights_review_sha256: str
    validation_evidence_sha256: str
    enabled: bool = False

    def __post_init__(self) -> None:
        if (self.enabled is not True
                or not all(_absolute(v) for v in (
                    self.model_path, self.interpreter_path, self.base_python_root,
                    self.dependency_lock_path, self.private_home))
                or Path(self.model_path).suffix != ".nemo"
                or Path(self.interpreter_path).parts[-2:] != ("bin", "python")
                or type(self.gpu_uuid) is not str or not _GPU.fullmatch(self.gpu_uuid)
                or any(type(v) is not str or not _SHA.fullmatch(v) for v in (
                    self.rights_review_sha256, self.validation_evidence_sha256))):
            raise _invalid()
        home = Path(self.private_home)
        roots = (Path(self.model_path), Path(self.dependency_lock_path),
                 Path(self.interpreter_path).parent.parent, Path(self.base_python_root))
        if (len({self.model_path, self.interpreter_path, self.dependency_lock_path}) != 3
                or any(home.is_relative_to(p) or p.is_relative_to(home) for p in roots)):
            raise _invalid()

    def serving_declaration(self) -> ServingDeploymentDeclaration:
        try:
            code = {name: _file_digest(Path(__file__).with_name(name), 1024**2) for name in _CODE}
        except (OSError, ValueError, RuntimeError):
            raise _invalid() from None
        dep = DeploymentIdentity(
            provider_id=PROVIDER_ID, runtime_artifact_sha256=INTERPRETER_SHA256,
            adapter_artifact_sha256=_digest(code), model_artifact_sha256=MODEL_SHA256,
            execution_config_sha256=_digest({
                "schema": "owned-parakeet-ja-v1", "model_revision": MODEL_REVISION,
                "lock": LOCK_SHA256, "python": "3.11.14", "nemo": "2.4.0",
                "torch": "2.7.1+cu126", "torchaudio": "2.7.1+cu126",
                "decoding": "TDT greedy_batch FP32", "language": "ja",
                "sample_rate": 16000, "channels": 1, "max_seconds": 30,
                "rights_review": self.rights_review_sha256,
            }), quantization="fp32-tdt-greedy_batch",
        )
        return ServingDeploymentDeclaration(dep, (ServingContract(
            deployment_revision=dep.revision, capability="audio.transcribe",
            operation_schema=TRANSCRIPTION_OPERATION_SCHEMA,
            semantic_revision=audio_semantic_revision("model-stt"),
            validation_evidence_sha256=self.validation_evidence_sha256,
            features={"ja", "json"},
            limits={"file_bytes": 1024**2, "metadata_bytes": 4096, "request_bytes": 2 * 1024**2},
        ),))

    def identities(self) -> tuple:
        paths = (self.model_path, self.interpreter_path, self.dependency_lock_path,
                 str(Path(self.interpreter_path).parent.parent / "pyvenv.cfg"),
                 str(Path(self.base_python_root) / "bin/python3.11"),
                 *(str(Path(__file__).with_name(name)) for name in _CODE))
        return tuple(_identity(_trusted(Path(p))) for p in paths)

    def preflight(self) -> None:
        """Read-only bounded byte checks; no imports of the native environment."""
        try:
            validate_environment(Path(self.interpreter_path), Path(self.base_python_root))
            for path, maximum, expected in (
                (self.model_path, 4 * 1024**3, MODEL_SHA256),
                (self.interpreter_path, 64 * 1024**2, INTERPRETER_SHA256),
                (str(Path(self.base_python_root) / "bin/python3.11"), 64 * 1024**2, INTERPRETER_SHA256),
                (self.dependency_lock_path, 8 * 1024**2, LOCK_SHA256),
            ):
                if _file_digest(Path(path), maximum) != expected:
                    raise _invalid()
            if not os.access(self.interpreter_path, os.X_OK):
                raise _invalid()
            home = Path(self.private_home)
            info = _trusted(home)
            if (not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700
                    or info.st_uid != os.geteuid() or any(home.iterdir())):
                raise _invalid()
        except (OSError, ValueError, RuntimeError):
            raise _invalid() from None


@dataclass(frozen=True, slots=True)
class ParakeetManifestProvider:
    config: ParakeetManifestConfig
    info = RuntimeProviderInfo(PROVIDER_ID, "Owned Parakeet Japanese STT (pre-provisioned)")

    def compatibility(self, context: RuntimeCompatibilityContext) -> RuntimeCompatibility:
        w = context.worker
        ok = (self.config.enabled is True and sys.platform == "linux" and os.geteuid() != 0
              and context.host.architecture == "x86_64" and context.demand == DEMAND
              and w.capabilities == {"audio.transcribe"} and w.resources.gpu_count == 1
              and tuple(w.gpu_uuids) == (self.config.gpu_uuid,)
              and w.resources.total_vram_mb >= DEMAND.min_total_vram_mb
              and w.resources.max_single_gpu_vram_mb >= DEMAND.min_single_gpu_vram_mb)
        return RuntimeCompatibility(PROVIDER_ID, () if ok else (CompatibilityReason(
            "parakeet_worker_incompatible",
            "requires the dedicated unprivileged x86-64 single-GPU transcription Worker",
        ),))

    def setup_intent(self, context):
        raise ValueError("Parakeet installation and SetupPlan activation are not supported")

    def create_runtime(self, context, setup):
        raise ValueError("Parakeet requires a reviewed serving manifest")

    def create_manifest_runtime(self, context, serving):
        if (not self.compatibility(context).compatible
                or type(serving) is not ServingDeploymentDeclaration
                or serving != self.config.serving_declaration()):
            raise _invalid()
        from .parakeet_managed import ParakeetManagedRuntime
        return ParakeetManagedRuntime(config=self.config)
