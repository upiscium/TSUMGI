"""#215 B5b: provider-local, source-only VOICEVOX owned runtime.

This is NOT registered in the shared RuntimeProvider registry. The process and
engine transports MUST be supplied by a separately reviewed operator binding.
Fake transports exercise the contract but do not attest an actual engine,
binary/process identity, listener ownership, native HTTP logging, or voice rights.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import ClassVar, Protocol, runtime_checkable
from urllib.parse import urlsplit
from uuid import uuid4

from ...execution import ResidencyReport
from ...serving import ServingDeploymentDeclaration
from ..contracts import ManagedRuntime, RuntimeHealth, RuntimeHealthState
from .voicevox import (
    VOICEVOX_PROVIDER_ID,
    VoicevoxAdapterConfig,
    VoicevoxEngineApi,
    VoicevoxExecutor,
    _review_speakers,
    _uuid,
)

_SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")
_RESOURCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_VERSION_LIMIT = 256
_SPEAKERS_LIMIT = 256 * 1024
_MAX_ASSET_SIZE = 16 * 1024 * 1024 * 1024


def _invalid() -> ValueError:
    return ValueError("VOICEVOX owned runtime configuration is invalid")


class VoicevoxRuntimeFailure(RuntimeError):
    """Redacted runtime failure: never concatenate process/HTTP exceptions."""


@dataclass(frozen=True, slots=True, repr=False)
class VoicevoxOwnedRuntimeSpec:
    adapter: VoicevoxAdapterConfig
    serving: ServingDeploymentDeclaration
    executable_path: str
    model_artifact_path: str
    endpoint: str
    worker_resource_id: str
    owner_uid: int
    approved_voice_aliases: frozenset[str]
    voice_rights_review_sha256: str
    startup_timeout_seconds: float = 5.0
    probe_timeout_seconds: float = 1.0
    stop_timeout_seconds: float = 2.0

    def __post_init__(self) -> None:
        if (
            type(self.adapter) is not VoicevoxAdapterConfig
            or type(self.serving) is not ServingDeploymentDeclaration
        ):
            raise _invalid()
        dep = self.serving.deployment
        a = self.adapter
        if (
            dep.provider_id != VOICEVOX_PROVIDER_ID
            or dep.runtime_artifact_sha256 != a.engine_artifact_sha256
            or dep.adapter_artifact_sha256 != a.adapter_artifact_sha256
            or dep.model_artifact_sha256 != a.model_artifact_sha256
            or len(self.serving.contracts) != 1
        ):
            raise _invalid()
        contract = self.serving.contracts[0]
        if (
            contract.deployment_revision != dep.revision
            or contract.capability != "audio.synthesize"
            or contract.operation_schema != "openai-audio-speech-v1"
            or contract.semantic_revision != a.semantic_revision
            or contract.features != frozenset({"ja", "wav"})
        ):
            raise _invalid()
        if (
            type(self.approved_voice_aliases) is not frozenset
            or self.approved_voice_aliases != frozenset(v.alias for v in a.voices)
            or len({v.style_id for v in a.voices}) != len(a.voices)
            or type(self.voice_rights_review_sha256) is not str
            or _SHA.fullmatch(self.voice_rights_review_sha256) is None
            or type(self.worker_resource_id) is not str
            or _RESOURCE.fullmatch(self.worker_resource_id) is None
            or type(self.owner_uid) is not int
            or self.owner_uid < 0
        ):
            raise _invalid()
        for path in (self.executable_path, self.model_artifact_path):
            if (
                type(path) is not str
                or not os.path.isabs(path)
                or os.path.normpath(path) != path
                or "\x00" in path
                or path == "/"
            ):
                raise _invalid()
        if type(self.endpoint) is not str:
            raise _invalid()
        try:
            parsed = urlsplit(self.endpoint)
            port = parsed.port
        except ValueError:
            raise _invalid() from None
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or port is None
            or not 1024 <= port < 65536
            or self.endpoint != f"http://127.0.0.1:{port}"
        ):
            raise _invalid()
        for value, maximum in (
            (self.startup_timeout_seconds, 120),
            (self.probe_timeout_seconds, 5),
            (self.stop_timeout_seconds, 60),
        ):
            if (
                type(value) not in (float, int)
                or not math.isfinite(value)
                or not 0 < value <= maximum
            ):
                raise _invalid()


@dataclass(frozen=True, slots=True)
class VoicevoxProcessObservation:
    """Observed by the injected OS-aware controller, not supplied by a client."""

    pid: int
    uid: int
    endpoint_owner_pid: int
    executable_sha256: str
    model_artifact_sha256: str
    endpoint: str
    worker_resource_id: str
    deployment_revision: str
    launch_nonce: str


@runtime_checkable
class VoicevoxOwnedProcessControl(Protocol):
    """Implementations must enforce OS process/port identity and owned reap."""

    async def endpoint_owner(self) -> int | None: ...
    async def inspect(self) -> VoicevoxProcessObservation | None: ...
    async def launch(self, *, config: VoicevoxOwnedRuntimeSpec, nonce: str) -> None: ...
    async def stop_owned(self, *, nonce: str) -> None: ...


@runtime_checkable
class VoicevoxManagedEngineApi(VoicevoxEngineApi, Protocol):
    """One engine transport used for both bounded health and audio execution.

    A real transport MUST cap received bytes before decoding, use fixed
    loopback destination, disable/redact native /audio_query URL logging, and
    bind calls to the process controller's verified endpoint. No real transport
    is implemented or enabled in this source slice.
    """

    async def bounded_json(self, *, path: str, max_bytes: int) -> bytes: ...


def _bounded_json(body: object, max_bytes: int) -> object:
    if type(body) is not bytes or not 1 <= len(body) <= max_bytes:
        raise VoicevoxRuntimeFailure("voicevox_invalid_probe_payload")

    def no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate-key")
            result[key] = value
        return result

    def no_constant(_value: str) -> None:
        raise ValueError("non-json-constant")

    try:
        return json.loads(
            body.decode("utf-8"),
            object_pairs_hook=no_duplicate_keys,
            parse_constant=no_constant,
        )
    except (ValueError, UnicodeError, RecursionError, TypeError):
        raise VoicevoxRuntimeFailure("voicevox_invalid_probe_payload") from None


def _full_catalog_matches(value: object, config: VoicevoxAdapterConfig) -> bool:
    if type(value) is not list or not 1 <= len(value) <= 1024:
        return False
    all_styles: set[int] = set()
    all_uuids: set[str] = set()
    for speaker in value:
        if type(speaker) is not dict:
            return False
        uuid = speaker.get("speaker_uuid")
        styles = speaker.get("styles")
        if (
            not _uuid(uuid)
            or uuid in all_uuids
            or type(styles) is not list
            or not 1 <= len(styles) <= 256
        ):
            return False
        all_uuids.add(uuid)
        for style in styles:
            if type(style) is not dict or type(style.get("id")) is not int:
                return False
            style_id = style["id"]
            if not 0 <= style_id < 2**31 or style_id in all_styles:
                return False
            all_styles.add(style_id)
    return all(_review_speakers(value, pin) for pin in config.voices)


def _digest_regular_file(path: str) -> str:
    """Read-only file hash with no symlink components; advisory until launch."""
    current = "/"
    for part in path.split("/")[1:]:
        current = os.path.join(current, part)
        if stat.S_ISLNK(os.lstat(current).st_mode):
            raise VoicevoxRuntimeFailure("voicevox_untrusted_artifact")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or not 0 < before.st_size <= _MAX_ASSET_SIZE
        ):
            raise VoicevoxRuntimeFailure("voicevox_untrusted_artifact")
        digest = hashlib.sha256()
        total = 0
        while chunk := os.read(fd, 1024 * 1024):
            total += len(chunk)
            if total > _MAX_ASSET_SIZE:
                raise VoicevoxRuntimeFailure("voicevox_untrusted_artifact")
            digest.update(chunk)
        after = os.fstat(fd)
        if (before.st_ino, before.st_dev, before.st_size, before.st_mtime_ns) != (
            after.st_ino,
            after.st_dev,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise VoicevoxRuntimeFailure("voicevox_untrusted_artifact")
        return "sha256:" + digest.hexdigest()
    finally:
        os.close(fd)


class _QualifiedVoicevoxExecutor(VoicevoxExecutor):
    serving_features: ClassVar[Mapping[str, frozenset[str]]] = MappingProxyType(
        {"audio.synthesize": frozenset({"ja", "wav"})}
    )


class VoicevoxOwnedManagedRuntime(ManagedRuntime):
    provider_id = VOICEVOX_PROVIDER_ID

    def __init__(
        self,
        *,
        config: VoicevoxOwnedRuntimeSpec,
        process: VoicevoxOwnedProcessControl,
        engine: VoicevoxManagedEngineApi,
    ) -> None:
        if type(config) is not VoicevoxOwnedRuntimeSpec:
            raise _invalid()
        for method in ("endpoint_owner", "inspect", "launch", "stop_owned"):
            if not inspect.iscoroutinefunction(getattr(process, method, None)):
                raise _invalid()
        for method in (
            "version",
            "speakers",
            "audio_query",
            "synthesis",
            "bounded_json",
        ):
            if not inspect.iscoroutinefunction(getattr(engine, method, None)):
                raise _invalid()
        self._config = config
        self._process = process
        self._engine = engine
        self._executor = _QualifiedVoicevoxExecutor(config=config.adapter, api=engine)
        self._state = RuntimeHealthState.STOPPED
        self._nonce: str | None = None
        self._pid: int | None = None
        self._released = False
        self._lock = asyncio.Lock()
        self._cleanup_uncertain = False

    def _health(
        self, state: RuntimeHealthState, code: str | None = None
    ) -> RuntimeHealth:
        return RuntimeHealth(
            state=state,
            ready=state is RuntimeHealthState.READY,
            detail=code,
            metadata={"runtime_provider": VOICEVOX_PROVIDER_ID},
        )

    def _fail(self, code: str) -> RuntimeHealth:
        self._state = RuntimeHealthState.FAILED
        return self._health(RuntimeHealthState.FAILED, code)

    def _matches(self, obs: object) -> bool:
        cfg = self._config
        return (
            type(obs) is VoicevoxProcessObservation
            and type(obs.pid) is int
            and obs.pid > 0
            and (self._pid is None or obs.pid == self._pid)
            and type(obs.uid) is int
            and obs.uid == cfg.owner_uid
            and type(obs.endpoint_owner_pid) is int
            and obs.endpoint_owner_pid == obs.pid
            and obs.executable_sha256 == cfg.adapter.engine_artifact_sha256
            and obs.model_artifact_sha256 == cfg.adapter.model_artifact_sha256
            and obs.endpoint == cfg.endpoint
            and obs.worker_resource_id == cfg.worker_resource_id
            and obs.deployment_revision == cfg.serving.deployment.revision
            and obs.launch_nonce == self._nonce
        )

    async def _preflight(self) -> None:
        cfg = self._config
        try:
            async with asyncio.timeout(cfg.startup_timeout_seconds):
                for path, digest in (
                    (cfg.executable_path, cfg.adapter.engine_artifact_sha256),
                    (cfg.model_artifact_path, cfg.adapter.model_artifact_sha256),
                ):
                    actual = await asyncio.to_thread(_digest_regular_file, path)
                    if actual != digest:
                        raise VoicevoxRuntimeFailure("voicevox_untrusted_artifact")
        except TimeoutError:
            raise VoicevoxRuntimeFailure("voicevox_preflight_timeout") from None
        except (OSError, RuntimeError, ValueError, TypeError):
            raise VoicevoxRuntimeFailure("voicevox_untrusted_artifact") from None
        try:
            async with asyncio.timeout(cfg.probe_timeout_seconds):
                owner = await self._process.endpoint_owner()
                existing = await self._process.inspect()
            if owner is not None or existing is not None:
                raise VoicevoxRuntimeFailure("voicevox_endpoint_occupied")
        except VoicevoxRuntimeFailure:
            raise
        except Exception:  # noqa: BLE001 - sanitize controller failures
            raise VoicevoxRuntimeFailure("voicevox_preflight_unavailable") from None

    async def start(self) -> None:
        async with self._lock:
            if self._released or self._state is RuntimeHealthState.FAILED:
                raise VoicevoxRuntimeFailure("voicevox_runtime_unavailable")
            if self._state is RuntimeHealthState.READY:
                if (await self.health()).ready:
                    return
                raise VoicevoxRuntimeFailure("voicevox_runtime_unavailable")
            if self._state is not RuntimeHealthState.STOPPED:
                raise VoicevoxRuntimeFailure("voicevox_runtime_unavailable")
            try:
                await self._preflight()
                self._nonce = str(uuid4())
                self._state = RuntimeHealthState.STARTING
                async with asyncio.timeout(self._config.startup_timeout_seconds):
                    await self._process.launch(config=self._config, nonce=self._nonce)
                    deadline = (
                        asyncio.get_running_loop().time()
                        + self._config.startup_timeout_seconds
                    )
                    while True:
                        status = await self.health()
                        if status.state is RuntimeHealthState.READY:
                            return
                        if status.state is RuntimeHealthState.FAILED:
                            raise VoicevoxRuntimeFailure("voicevox_runtime_unavailable")
                        if asyncio.get_running_loop().time() >= deadline:
                            raise VoicevoxRuntimeFailure("voicevox_start_timeout")
                        await asyncio.sleep(0.05)
            except BaseException as error:
                # Cancelled startup also must stop the exact process it launched.
                nonce = self._nonce
                if nonce is not None:
                    try:
                        async with asyncio.timeout(self._config.stop_timeout_seconds):
                            await self._process.stop_owned(nonce=nonce)
                    except Exception:  # noqa: BLE001 - untrusted controller, redacted
                        self._cleanup_uncertain = True
                self._state = RuntimeHealthState.FAILED
                if not isinstance(error, Exception):
                    raise
                raise VoicevoxRuntimeFailure("voicevox_start_failed") from None

    async def health(self) -> RuntimeHealth:
        if self._state in (RuntimeHealthState.STOPPED, RuntimeHealthState.FAILED):
            return self._health(self._state)
        cfg = self._config
        was_ready = self._state is RuntimeHealthState.READY
        try:
            async with asyncio.timeout(cfg.probe_timeout_seconds):
                obs = await self._process.inspect()
                if obs is None:
                    if was_ready:
                        return self._fail("voicevox_owned_process_lost")
                    owner = await self._process.endpoint_owner()
                    if owner is not None:
                        return self._fail("voicevox_foreign_endpoint")
                    return self._health(RuntimeHealthState.STARTING)
                if not self._matches(obs):
                    return self._fail("voicevox_identity_changed")
                if self._pid is None:
                    self._pid = obs.pid
                owner = await self._process.endpoint_owner()
                if type(owner) is not int or owner != obs.pid:
                    return self._fail("voicevox_foreign_endpoint")
                version = _bounded_json(
                    await self._engine.bounded_json(
                        path="/version",
                        max_bytes=_VERSION_LIMIT,
                    ),
                    _VERSION_LIMIT,
                )
                speakers = _bounded_json(
                    await self._engine.bounded_json(
                        path="/speakers",
                        max_bytes=_SPEAKERS_LIMIT,
                    ),
                    _SPEAKERS_LIMIT,
                )
                if type(version) is not str or version != cfg.adapter.engine_version:
                    return self._fail("voicevox_engine_version_changed")
                if not _full_catalog_matches(speakers, cfg.adapter):
                    return self._fail("voicevox_speakers_changed")
                after = await self._process.inspect()
                if not self._matches(after) or after.pid != obs.pid:
                    return self._fail("voicevox_identity_changed")
                owner_after = await self._process.endpoint_owner()
                if type(owner_after) is not int or owner_after != obs.pid:
                    return self._fail("voicevox_foreign_endpoint")
                self._state = RuntimeHealthState.READY
                return self._health(RuntimeHealthState.READY)
        except asyncio.CancelledError:
            raise
        except (TimeoutError, OSError):
            if not was_ready:
                return self._health(RuntimeHealthState.STARTING)
            return self._fail("voicevox_probe_timeout")
        except Exception:  # noqa: BLE001 - untrusted transport detail may be private
            return self._fail("voicevox_probe_failed")

    async def stop(self) -> None:
        async with self._lock:
            nonce = self._nonce
            if nonce is not None:
                try:
                    async with asyncio.timeout(self._config.stop_timeout_seconds):
                        await self._process.stop_owned(nonce=nonce)
                except Exception:  # noqa: BLE001 - sanitize process-controller errors
                    self._state = RuntimeHealthState.FAILED
                    raise VoicevoxRuntimeFailure("voicevox_owned_stop_failed") from None
                self._nonce = None
                self._pid = None
            if self._state is not RuntimeHealthState.FAILED:
                self._state = RuntimeHealthState.STOPPED

    async def release(self) -> None:
        if self._released:
            return
        await self.stop()
        close = getattr(self._engine, "close", None)
        if inspect.iscoroutinefunction(close):
            try:
                async with asyncio.timeout(self._config.stop_timeout_seconds):
                    await close()
            except Exception:  # noqa: BLE001 - sanitize engine transport errors
                raise VoicevoxRuntimeFailure("voicevox_engine_close_failed") from None
        self._released = True

    async def residency(self) -> ResidencyReport:
        return await self._executor.residency()

    def executor(self) -> VoicevoxExecutor:
        if self._released or self._state is not RuntimeHealthState.READY:
            raise VoicevoxRuntimeFailure("voicevox_runtime_not_ready")
        return self._executor
