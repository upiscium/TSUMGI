"""Explicit pre-provisioned Faster-Whisper JA manifest; never installs a runtime.

Mirrors parakeet_manifest.py (kept untouched). Selected model: the third-party
CTranslate2 conversion deepdml/faster-whisper-large-v3-turbo-ct2 (MIT) of
OpenAI Whisper large-v3-turbo, float32 on a GTX 1080 Ti.
"""
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
from . import faster_whisper_native as pins
from .faster_whisper_environment import validate_environment
from .faster_whisper_process import check_home_parent

PROVIDER_ID = "faster-whisper-ja"
MODEL_REF = "deepdml/faster-whisper-large-v3-turbo-ct2"
MODEL_REVISION = pins.MODEL_REVISION
# Mutable only so tests can substitute tiny fixtures; production code never does.
FILE_SHA256 = pins.FILE_SHA256
# Declared capacity of the selected GTX 1080 Ti (11 GiB), NOT a measured
# minimum (device-level peak was about 4.2 GiB), a free-VRAM reservation, or a
# qualification of other CUDA hardware.
DEMAND = ExecutionDemand(
    ModelDemand(MODEL_REF, "ctranslate2", ModelTopology.DENSE),
    ResidencyPolicy.VRAM_ONLY, GPUTopology.SINGLE_GPU, min_gpu_count=1,
    min_total_vram_mb=11264, min_single_gpu_vram_mb=11264,
)
_SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")
_GPU = re.compile(r"GPU-[A-Fa-f0-9]{8}(?:-[A-Fa-f0-9]{4}){3}-[A-Fa-f0-9]{12}\Z")
_ERROR = "Faster-Whisper manifest configuration is invalid"
_CODE = ("faster_whisper.py", "faster_whisper_native.py", "faster_whisper_child.py",
         "faster_whisper_process.py", "faster_whisper_manifest.py", "faster_whisper_managed.py",
         "faster_whisper_environment.py")


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


def _model_files(model_dir: str) -> tuple[Path, ...]:
    directory = Path(model_dir)
    _trusted(directory)
    if not stat.S_ISDIR(directory.lstat().st_mode) or set(os.listdir(directory)) != set(FILE_SHA256):
        raise _invalid()  # exactly the pinned files: no extra, missing or renamed entry
    return tuple(directory / name for name in sorted(FILE_SHA256))


@dataclass(frozen=True, slots=True, kw_only=True, repr=False)
class FasterWhisperManifestConfig:
    model_dir: str
    interpreter_path: str
    base_python_root: str
    dependency_lock_path: str
    private_home: str
    gpu_uuid: str
    interpreter_sha256: str
    dependency_lock_sha256: str
    rights_review_sha256: str
    validation_evidence_sha256: str
    enabled: bool = False

    def __post_init__(self) -> None:
        if (self.enabled is not True
                or not all(_absolute(v) for v in (
                    self.model_dir, self.interpreter_path, self.base_python_root,
                    self.dependency_lock_path, self.private_home))
                or Path(self.interpreter_path).parts[-2:] != ("bin", "python")
                or type(self.gpu_uuid) is not str or not _GPU.fullmatch(self.gpu_uuid)
                or any(type(v) is not str or not _SHA.fullmatch(v) for v in (
                    self.interpreter_sha256, self.dependency_lock_sha256,
                    self.rights_review_sha256, self.validation_evidence_sha256))):
            raise _invalid()
        home = Path(self.private_home)
        roots = (Path(self.model_dir), Path(self.dependency_lock_path),
                 Path(self.interpreter_path).parent.parent, Path(self.base_python_root))
        if (len({self.model_dir, self.interpreter_path, self.dependency_lock_path}) != 3
                or any(home.is_relative_to(p) or p.is_relative_to(home) for p in roots)):
            raise _invalid()

    def serving_declaration(self) -> ServingDeploymentDeclaration:
        try:
            code = {name: _file_digest(Path(__file__).with_name(name), 1024**2) for name in _CODE}
        except (OSError, ValueError, RuntimeError):
            raise _invalid() from None
        dep = DeploymentIdentity(
            provider_id=PROVIDER_ID, runtime_artifact_sha256=self.interpreter_sha256,
            adapter_artifact_sha256=_digest(code),
            model_artifact_sha256="sha256:" + FILE_SHA256["model.bin"],
            tokenizer_artifact_sha256="sha256:" + FILE_SHA256["tokenizer.json"],
            execution_config_sha256=_digest({
                "schema": "owned-faster-whisper-ja-v1", "model_ref": MODEL_REF,
                "model_revision": MODEL_REVISION, "files": dict(sorted(FILE_SHA256.items())),
                "lock": self.dependency_lock_sha256, "python": ".".join(map(str, pins.PYTHON)),
                "temperature": "faster-whisper 1.2.1 default fallback schedule",
                "packages": dict(sorted(pins.VERSIONS.items())),
                "decoding": dict(sorted(pins.DECODING.items())),
                "compute_type": pins.COMPUTE_TYPE, "device": "cuda:0",
                "language": "ja", "sample_rate": 16000, "channels": 1, "max_seconds": 30,
                "rights_review": self.rights_review_sha256,
            }), quantization="float32-ctranslate2-beam5",
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
        paths = (self.model_dir, *map(str, _model_files(self.model_dir)),
                 self.interpreter_path, self.dependency_lock_path,
                 str(Path(self.interpreter_path).parent.parent / "pyvenv.cfg"),
                 str(Path(self.base_python_root) / "bin/python3.11"),
                 *(str(Path(__file__).with_name(name)) for name in _CODE))
        return tuple(_identity(_trusted(Path(p))) for p in paths)

    def preflight(self) -> None:
        """Read-only bounded byte checks; no imports of the native environment."""
        try:
            validate_environment(Path(self.interpreter_path), Path(self.base_python_root))
            for path in _model_files(self.model_dir):
                limit = pins.MAX_FILE_BYTES.get(path.name, pins.MAX_SMALL_FILE_BYTES)
                if _file_digest(path, limit) != "sha256:" + FILE_SHA256[path.name]:
                    raise _invalid()
            for path, maximum, expected in (
                (self.interpreter_path, 64 * 1024**2, self.interpreter_sha256),
                (str(Path(self.base_python_root) / "bin/python3.11"), 64 * 1024**2, self.interpreter_sha256),
                (self.dependency_lock_path, 8 * 1024**2, self.dependency_lock_sha256),
            ):
                if _file_digest(Path(path), maximum) != expected:
                    raise _invalid()
            if not os.access(self.interpreter_path, os.X_OK):
                raise _invalid()
            # The home is a dedicated parent: only stale run-<hex> directories
            # of an earlier instance may exist; each start uses a fresh one.
            _trusted(Path(self.private_home))
            check_home_parent(Path(self.private_home))
        except (OSError, ValueError, RuntimeError):
            raise _invalid() from None


@dataclass(frozen=True, slots=True)
class FasterWhisperManifestProvider:
    config: FasterWhisperManifestConfig
    info = RuntimeProviderInfo(PROVIDER_ID, "Owned Faster-Whisper Japanese STT (pre-provisioned)")

    def compatibility(self, context: RuntimeCompatibilityContext) -> RuntimeCompatibility:
        w = context.worker
        ok = (self.config.enabled is True and sys.platform == "linux" and os.geteuid() != 0
              and context.host.architecture == "x86_64" and context.demand == DEMAND
              and w.capabilities == {"audio.transcribe"} and w.resources.gpu_count == 1
              and tuple(w.gpu_uuids) == (self.config.gpu_uuid,)
              and w.resources.total_vram_mb >= DEMAND.min_total_vram_mb
              and w.resources.max_single_gpu_vram_mb >= DEMAND.min_single_gpu_vram_mb)
        return RuntimeCompatibility(PROVIDER_ID, () if ok else (CompatibilityReason(
            "faster_whisper_worker_incompatible",
            "requires the dedicated unprivileged x86-64 single-GPU transcription Worker",
        ),))

    def setup_intent(self, context):
        raise ValueError("Faster-Whisper installation and SetupPlan activation are not supported")

    def create_runtime(self, context, setup):
        raise ValueError("Faster-Whisper requires a reviewed serving manifest")

    def create_manifest_runtime(self, context, serving):
        if (not self.compatibility(context).compatible
                or type(serving) is not ServingDeploymentDeclaration
                or serving != self.config.serving_declaration()):
            raise _invalid()
        from .faster_whisper_managed import FasterWhisperManagedRuntime
        return FasterWhisperManagedRuntime(config=self.config)
