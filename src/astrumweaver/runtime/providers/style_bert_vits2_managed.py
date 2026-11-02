"""#216 B5c: provider-local, source-only isolated process and framed IPC.

No native Style-Bert-VITS2 runner, model import, registry entry, package or
deployment is furnished. The injected/child protocol alone cannot establish
authentic model loading, license rights, host GPU ownership, or an OS sandbox.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import signal
import stat
import struct
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import ClassVar
from uuid import uuid4

from ...contracts import WorkerSpec
from ...execution import ResidencyReport
from ...serving import ServingDeploymentDeclaration
from ..contracts import (
    ManagedRuntime,
    RuntimeHealth,
    RuntimeHealthState,
    RuntimeHostFacts,
)
from .style_bert_vits2 import (
    STYLE_BERT_VITS2_PROVIDER_ID,
    StyleBertVits2AsyncModelApi,
    StyleBertVits2Executor,
    StyleBertVits2ModelPin,
    StyleBertVits2ProviderConfig,
)

_SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")
_FRAME_LIMIT = 32 * 1024
_FILE_LIMIT = 16 * 1024 * 1024 * 1024
_PROTOCOL_VERSION = 1


class StyleBertRuntimeFailure(RuntimeError):
    """Only fixed error codes; native exceptions/text/paths are not exposed."""


def _bad_config() -> ValueError:
    return ValueError("Style-Bert-VITS2 owned runtime configuration is invalid")


def _path(value: object) -> bool:
    return (
        type(value) is str
        and bool(value)
        and os.path.isabs(value)
        and os.path.normpath(value) == value
        and value != "/"
        and "\x00" not in value
        and len(value.encode("utf-8")) <= 4096
    )


def _sha(value: object) -> bool:
    return type(value) is str and _SHA.fullmatch(value) is not None


@dataclass(frozen=True, slots=True, repr=False)
class StyleBertOwnedRuntimeSpec:
    """Exact fixed process and artifact contract, not evidence of readiness."""

    adapter: StyleBertVits2ProviderConfig
    serving: ServingDeploymentDeclaration
    worker: WorkerSpec
    host: RuntimeHostFacts
    model_directory: str
    interpreter_path: str
    runner_path: str
    runner_sha256: str
    interpreter_sha256: str
    rights_review_sha256: str
    voice_rights_qualified: bool = False
    device: str = "cpu"
    gpu_uuid: str | None = None
    required_single_gpu_vram_mb: int = 0
    required_host_ram_mb: int = 128
    boot_timeout_seconds: float = 3.0
    inference_timeout_seconds: float = 3.0
    stop_timeout_seconds: float = 2.0

    def __post_init__(self) -> None:
        if (
            type(self.adapter) is not StyleBertVits2ProviderConfig
            or type(self.serving) is not ServingDeploymentDeclaration
            or type(self.worker) is not WorkerSpec
            or type(self.host) is not RuntimeHostFacts
            or not all(
                _path(p)
                for p in (
                    self.model_directory,
                    self.interpreter_path,
                    self.runner_path,
                )
            )
            or not all(
                _sha(x)
                for x in (
                    self.runner_sha256,
                    self.interpreter_sha256,
                    self.rights_review_sha256,
                )
            )
            or self.voice_rights_qualified is not True
        ):
            raise _bad_config()
        dep = self.serving.deployment
        model = self.adapter.model
        if (
            dep.provider_id != STYLE_BERT_VITS2_PROVIDER_ID
            or dep.runtime_artifact_sha256 != self.interpreter_sha256
            or dep.adapter_artifact_sha256 != self.runner_sha256
            or dep.model_artifact_sha256 != "sha256:" + model.model_sha256
            or len(self.serving.contracts) != 1
        ):
            raise _bad_config()
        contract = self.serving.contracts[0]
        if (
            contract.capability != "audio.synthesize"
            or contract.deployment_revision != dep.revision
            or contract.operation_schema != "openai-audio-speech-v1"
            or contract.semantic_revision != self.adapter.required_semantic_revision
            or contract.features != frozenset({"ja", "wav"})
            or len({(v.speaker_id, v.style) for v in self.adapter.voices})
            != len(self.adapter.voices)
        ):
            raise _bad_config()
        if (
            type(self.required_host_ram_mb) is not int
            or not 1 <= self.required_host_ram_mb <= self.host.host_ram_mb
        ):
            raise _bad_config()
        if self.device == "cpu":
            if self.gpu_uuid is not None or self.required_single_gpu_vram_mb != 0:
                raise _bad_config()
        elif self.device == "cuda":
            if (
                type(self.gpu_uuid) is not str
                or self.gpu_uuid not in self.worker.gpu_uuids
                or self.worker.resources.gpu_count < 1
                or type(self.required_single_gpu_vram_mb) is not int
                or not 1
                <= self.required_single_gpu_vram_mb
                <= self.worker.resources.max_single_gpu_vram_mb
            ):
                raise _bad_config()
        else:
            raise _bad_config()
        if "audio.synthesize" not in self.worker.capabilities:
            raise _bad_config()
        for value, maximum in (
            (self.boot_timeout_seconds, 120),
            (self.inference_timeout_seconds, 300),
            (self.stop_timeout_seconds, 60),
        ):
            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or not 0 < value <= maximum
            ):
                raise _bad_config()


def _trusted_file_digest(path: str) -> str:
    """Read-only, no-symlink-component point-in-time file verification."""
    if not _path(path):
        raise StyleBertRuntimeFailure("stylebert_untrusted_artifact")
    part_path = "/"
    for part in Path(path).parts[1:]:
        part_path = os.path.join(part_path, part)
        if stat.S_ISLNK(os.lstat(part_path).st_mode):
            raise StyleBertRuntimeFailure("stylebert_untrusted_artifact")
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= _FILE_LIMIT:
            raise StyleBertRuntimeFailure("stylebert_untrusted_artifact")
        sha = hashlib.sha256()
        total = 0
        while chunk := os.read(fd, 1024 * 1024):
            total += len(chunk)
            if total > _FILE_LIMIT:
                raise StyleBertRuntimeFailure("stylebert_untrusted_artifact")
            sha.update(chunk)
        after = os.fstat(fd)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) or total != before.st_size:
            raise StyleBertRuntimeFailure("stylebert_untrusted_artifact")
        return "sha256:" + sha.hexdigest()
    finally:
        os.close(fd)


def _artifact_paths(spec: StyleBertOwnedRuntimeSpec) -> dict[str, str]:
    model = spec.adapter.model
    return {
        "interpreter": spec.interpreter_path,
        "runner": spec.runner_path,
        "model": os.path.join(spec.model_directory, model.model_filename),
        "config": os.path.join(spec.model_directory, "config.json"),
        "styles": os.path.join(spec.model_directory, "style_vectors.npy"),
    }


def _verify_artifacts(spec: StyleBertOwnedRuntimeSpec) -> None:
    paths = _artifact_paths(spec)
    if len(set(paths.values())) != len(paths):
        raise StyleBertRuntimeFailure("stylebert_duplicate_artifact")
    try:
        model_dir = Path(spec.model_directory)
        if not model_dir.is_dir() or model_dir.is_symlink():
            raise StyleBertRuntimeFailure("stylebert_untrusted_artifact")
        weights = [
            x.name
            for x in model_dir.iterdir()
            if x.name.endswith(".safetensors") or x.name.endswith(".onnx")
        ]
        if weights != [spec.adapter.model.model_filename]:
            raise StyleBertRuntimeFailure("stylebert_unreviewed_model_set")
        expected = {
            "interpreter": spec.interpreter_sha256,
            "runner": spec.runner_sha256,
            "model": "sha256:" + spec.adapter.model.model_sha256,
            "config": "sha256:" + spec.adapter.model.config_sha256,
            "styles": "sha256:" + spec.adapter.model.style_vectors_sha256,
        }
        for role, path in paths.items():
            if _trusted_file_digest(path) != expected[role]:
                raise StyleBertRuntimeFailure("stylebert_untrusted_artifact")
    except (OSError, ValueError, TypeError):
        raise StyleBertRuntimeFailure("stylebert_untrusted_artifact") from None


def _file_identities(spec: StyleBertOwnedRuntimeSpec) -> tuple[tuple[int, ...], ...]:
    """Cheap drift detection only; not loaded-model/binary provenance proof."""
    paths = (spec.model_directory, *_artifact_paths(spec).values())
    result = []
    for path in paths:
        current = "/"
        for part in Path(path).parts[1:]:
            current = os.path.join(current, part)
            if stat.S_ISLNK(os.lstat(current).st_mode):
                raise StyleBertRuntimeFailure("stylebert_artifact_drift")
        info = os.stat(path, follow_symlinks=False)
        result.append(
            (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_mode)
        )
    return tuple(result)


def _encoded_frame(payload: dict) -> bytes:
    encoded = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    if not 1 <= len(encoded) <= _FRAME_LIMIT:
        raise StyleBertRuntimeFailure("stylebert_invalid_ipc")
    return struct.pack(">I", len(encoded)) + encoded


async def _read_frame(reader: asyncio.StreamReader) -> dict:
    header = await reader.readexactly(4)
    length = struct.unpack(">I", header)[0]
    if not 1 <= length <= _FRAME_LIMIT:
        raise StyleBertRuntimeFailure("stylebert_invalid_ipc")
    body = await reader.readexactly(length)

    def _unique_pairs(pairs: list[tuple[str, object]]) -> dict:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate IPC field")
            result[key] = value
        return result

    def _forbid_constant(_value: str) -> None:
        raise ValueError("non JSON constant")

    try:
        parsed = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_unique_pairs,
            parse_constant=_forbid_constant,
        )
    except (ValueError, UnicodeError, TypeError, RecursionError):
        raise StyleBertRuntimeFailure("stylebert_invalid_ipc") from None
    if type(parsed) is not dict or len(parsed) > 32:
        raise StyleBertRuntimeFailure("stylebert_invalid_ipc")
    return parsed


class _QualifiedStyleBertExecutor(StyleBertVits2Executor):
    serving_features: ClassVar[Mapping[str, frozenset[str]]] = MappingProxyType(
        {"audio.synthesize": frozenset({"ja", "wav"})}
    )


class StyleBertOwnedManagedRuntime(ManagedRuntime, StyleBertVits2AsyncModelApi):
    """One isolated child with one request at a time and sticky failures."""

    provider_id = STYLE_BERT_VITS2_PROVIDER_ID

    def __init__(self, *, config: StyleBertOwnedRuntimeSpec) -> None:
        if type(config) is not StyleBertOwnedRuntimeSpec:
            raise _bad_config()
        self._config = config
        self._state = RuntimeHealthState.STOPPED
        self._proc: asyncio.subprocess.Process | None = None
        self._epoch: str | None = None
        self._released = False
        self._failed_detail: str | None = None
        self._file_ids: tuple[tuple[int, ...], ...] | None = None
        self._ipc_lock = asyncio.Lock()
        self._executor = _QualifiedStyleBertExecutor(config=config.adapter, api=self)

    def _health(
        self, state: RuntimeHealthState, detail: str | None = None
    ) -> RuntimeHealth:
        return RuntimeHealth(
            state=state,
            ready=state is RuntimeHealthState.READY,
            detail=detail,
            metadata={"runtime_provider": STYLE_BERT_VITS2_PROVIDER_ID},
        )

    def _fail(self, code: str) -> None:
        self._state = RuntimeHealthState.FAILED
        self._failed_detail = code

    async def _send(self, request: dict) -> dict:
        proc = self._proc
        if (
            proc is None
            or proc.returncode is not None
            or proc.stdin is None
            or proc.stdout is None
        ):
            raise StyleBertRuntimeFailure("stylebert_process_lost")
        proc.stdin.write(_encoded_frame(request))
        await proc.stdin.drain()
        response = await _read_frame(proc.stdout)
        if (
            response.get("version") != _PROTOCOL_VERSION
            or response.get("epoch") != self._epoch
            or response.get("request_id") != request.get("request_id")
        ):
            raise StyleBertRuntimeFailure("stylebert_stale_ipc")
        return response

    async def start(self) -> None:
        if (
            self._released
            or self._state is not RuntimeHealthState.STOPPED
            or self._proc is not None
        ):
            raise StyleBertRuntimeFailure("stylebert_start_not_allowed")
        cfg = self._config
        try:
            async with asyncio.timeout(cfg.boot_timeout_seconds):
                await asyncio.to_thread(_verify_artifacts, cfg)
                self._file_ids = await asyncio.to_thread(_file_identities, cfg)
                self._epoch = str(uuid4())
                self._state = RuntimeHealthState.STARTING
                # Fixed argv; no text, raw model paths or secrets in argv/env.
                # The synthetic runner observes model identities via handshake.
                self._proc = await asyncio.create_subprocess_exec(
                    cfg.interpreter_path,
                    "-I",
                    "-u",
                    cfg.runner_path,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    cwd="/",
                    start_new_session=True,
                    close_fds=True,
                    env={
                        "PYTHONUNBUFFERED": "1",
                        "CUDA_VISIBLE_DEVICES": cfg.gpu_uuid
                        if cfg.device == "cuda"
                        else "",
                    },
                )
                proc = self._proc
                request = {
                    "version": _PROTOCOL_VERSION,
                    "op": "bootstrap",
                    "request_id": str(uuid4()),
                    "epoch": self._epoch,
                    "pid": proc.pid,
                    "worker_id": cfg.worker.worker_id,
                    "deployment": cfg.serving.deployment.revision,
                    "device": cfg.device,
                    "gpu_uuid": cfg.gpu_uuid,
                    "model_id": cfg.adapter.model.model_id,
                    # Private, bounded child stdin only. Never argv/env/log/Job.
                    "artifact_paths": {
                        k: v
                        for k, v in _artifact_paths(cfg).items()
                        if k in ("model", "config", "styles")
                    },
                    "model_hashes": {
                        "weight": cfg.adapter.model.model_sha256,
                        "config": cfg.adapter.model.config_sha256,
                        "styles": cfg.adapter.model.style_vectors_sha256,
                    },
                    "runtime_revision": cfg.adapter.model.runtime_revision,
                    "adapter_revision": cfg.adapter.model.adapter_revision,
                    "voices": [
                        {
                            "alias": v.alias,
                            "speaker_id": v.speaker_id,
                            "style_id": v.style_id,
                            "style": v.style,
                        }
                        for v in cfg.adapter.voices
                    ],
                }
                reply = await self._send(request)
                if (
                    reply.get("op") != "ready"
                    or reply.get("pid") != proc.pid
                    or reply.get("worker_id") != cfg.worker.worker_id
                    or reply.get("deployment") != cfg.serving.deployment.revision
                    or reply.get("device") != cfg.device
                    or reply.get("gpu_uuid") != cfg.gpu_uuid
                    or reply.get("model_id") != cfg.adapter.model.model_id
                    or reply.get("model_hashes") != request["model_hashes"]
                    or reply.get("runtime_revision")
                    != cfg.adapter.model.runtime_revision
                    or reply.get("adapter_revision")
                    != cfg.adapter.model.adapter_revision
                    or reply.get("voices") != request["voices"]
                    or reply.get("probe_status") != "synthesized"
                    or type(reply.get("probe_pcm16")) is not bool
                    or reply.get("probe_pcm16") is not True
                    or proc.returncode is not None
                ):
                    raise StyleBertRuntimeFailure("stylebert_bootstrap_mismatch")
                self._state = RuntimeHealthState.READY
        except BaseException as error:
            self._fail("stylebert_bootstrap_failed")
            try:
                await self._stop_owned()
            except Exception:  # noqa: BLE001 - best effort; FAIL latch preserved
                self._failed_detail = "stylebert_cleanup_uncertain"
            if not isinstance(error, Exception):
                raise
            raise StyleBertRuntimeFailure("stylebert_bootstrap_failed") from None

    async def synthesize(
        self,
        *,
        model: StyleBertVits2ModelPin,
        text: str,
        speaker_id: int,
        style: str,
    ) -> bytes:
        cfg = self._config
        if (
            self._state is not RuntimeHealthState.READY
            or self._released
            or type(model) is not StyleBertVits2ModelPin
            or model != cfg.adapter.model
            or type(text) is not str
            or not text.strip()
            or len(text.encode("utf-8")) > cfg.adapter.max_text_bytes
            or type(speaker_id) is not int
            or type(style) is not str
            or (speaker_id, style)
            not in {(v.speaker_id, v.style) for v in cfg.adapter.voices}
        ):
            raise StyleBertRuntimeFailure("stylebert_inference_not_allowed")
        # No unbounded work queue or concurrent IPC streams.
        if self._ipc_lock.locked():
            raise StyleBertRuntimeFailure("stylebert_session_busy")
        async with self._ipc_lock:
            try:
                async with asyncio.timeout(cfg.inference_timeout_seconds):
                    request = {
                        "version": _PROTOCOL_VERSION,
                        "op": "infer",
                        "request_id": str(uuid4()),
                        "epoch": self._epoch,
                        "text": text,
                        "model_id": model.model_id,
                        "speaker_id": speaker_id,
                        "style": style,
                    }
                    reply = await self._send(request)
                    if reply.get("op") != "audio" or reply.get("status") != "ok":
                        raise StyleBertRuntimeFailure("stylebert_invalid_ipc")
                    length = reply.get("wav_length")
                    if (
                        type(length) is not int
                        or not 46 <= length <= cfg.adapter.max_output_bytes
                        or self._proc is None
                        or self._proc.stdout is None
                    ):
                        raise StyleBertRuntimeFailure("stylebert_invalid_ipc")
                    wav = await self._proc.stdout.readexactly(length)
                    if (
                        self._proc.returncode is not None
                        or self._state is not RuntimeHealthState.READY
                    ):
                        raise StyleBertRuntimeFailure("stylebert_process_lost")
                    return (
                        wav  # Existing adapter validates canonical PCM16 before stage.
                    )
            except BaseException as error:
                self._fail("stylebert_inference_failed")
                try:
                    await self._stop_owned()
                except Exception:  # noqa: BLE001 - deny reuse even if reap fails
                    self._failed_detail = "stylebert_cleanup_uncertain"
                if not isinstance(error, Exception):
                    raise
                raise StyleBertRuntimeFailure("stylebert_inference_failed") from None

    async def _stop_owned(self) -> None:
        proc = self._proc
        if proc is None:
            return
        # Exclusive new session/process group. Never signal an unrelated PID.
        if proc.returncode is None:
            try:
                pgid = os.getpgid(proc.pid)
                if pgid != proc.pid:
                    raise StyleBertRuntimeFailure("stylebert_unowned_process_group")
                os.killpg(pgid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                async with asyncio.timeout(self._config.stop_timeout_seconds / 2):
                    await proc.wait()
            except TimeoutError:
                try:
                    if proc.returncode is None and os.getpgid(proc.pid) == proc.pid:
                        os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        try:
            async with asyncio.timeout(self._config.stop_timeout_seconds):
                await proc.wait()
        except TimeoutError:
            self._fail("stylebert_reap_timeout")
            raise StyleBertRuntimeFailure("stylebert_reap_timeout") from None
        if proc.stdin is not None:
            proc.stdin.close()
        if self._proc is proc:
            self._proc = None

    async def health(self) -> RuntimeHealth:
        if self._state is RuntimeHealthState.FAILED:
            return self._health(RuntimeHealthState.FAILED, self._failed_detail)
        if self._state is RuntimeHealthState.STOPPED:
            return self._health(RuntimeHealthState.STOPPED)
        proc = self._proc
        if proc is None or proc.returncode is not None:
            self._fail("stylebert_process_lost")
            return self._health(RuntimeHealthState.FAILED, self._failed_detail)
        if self._state is RuntimeHealthState.READY:
            try:
                if os.getpgid(proc.pid) != proc.pid or not self._epoch:
                    self._fail("stylebert_process_identity_lost")
                elif self._file_ids != _file_identities(self._config):
                    self._fail("stylebert_artifact_drift")
            except (OSError, StyleBertRuntimeFailure):
                self._fail("stylebert_artifact_or_process_lost")
            return self._health(self._state, self._failed_detail)
        return self._health(RuntimeHealthState.STARTING)

    async def stop(self) -> None:
        try:
            await self._stop_owned()
        except Exception:  # noqa: BLE001 - redact subprocess errors
            self._fail("stylebert_cleanup_uncertain")
            raise StyleBertRuntimeFailure("stylebert_cleanup_uncertain") from None
        if self._state is not RuntimeHealthState.FAILED:
            self._state = RuntimeHealthState.STOPPED
        self._epoch = None

    async def release(self) -> None:
        if self._released:
            return
        await self.stop()
        self._released = True

    def executor(self) -> StyleBertVits2Executor:
        if self._released or self._state is not RuntimeHealthState.READY:
            raise StyleBertRuntimeFailure("stylebert_runtime_not_ready")
        return self._executor

    async def residency(self) -> ResidencyReport:
        return await self._executor.residency()
