"""Generic AstrumWeaver Worker execution loop."""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import inspect
import json
import math
import logging
import time
import subprocess
from pathlib import Path
from collections.abc import Mapping
from typing import Any, cast

from ..contracts import WorkerSpec
from ..control.models import WorkerState
from ..control.repository import OFFLINE_REASON_KEY, RECOVERABLE_OFFLINE_REASON
from ..control.serde import worker_spec_from_dict
from ..transport import JOB_EVENTS_EXTENSION, PROTOCOL_VERSION, SERVING_EXTENSION
from ..execution import (
    ArtifactRef,
    AudioJobExecutor,
    JobEvent,
    JobEventSink,
    JobExecutionError,
    JobExecutor,
    JobRequest,
    JobResult,
    StreamingJobExecutor,
)
from ..serving import (
    ServingDeploymentDeclaration,
    ServingJobBinding,
    WorkerServingAdvertisement,
    worker_serving_matches,
)
from ..gpu_mapping import (
    discover_gpu_mapping,
    load_reviewed_gpu_map,
    verify_isolated_gpu_access,
)
from .client import ClaimedJob, ControlClient, ControlTransportError
from .supervision import RuntimeHealthSupervisor, RuntimeUnavailable


_LOG = logging.getLogger(__name__)
_AUDIO_CAPABILITIES = frozenset({"audio.synthesize", "audio.transcribe"})
_AUDIO_MAX_BYTES = 16 * 1024 * 1024
_AUDIO_TEXT_MAX_BYTES = 8192
_AUDIO_EXECUTOR_ERROR = "audio executor failed"
_AUDIO_RESULT_ERROR = "audio executor returned an unsupported result"
_AUDIO_MEDIA_ERROR = "audio media operation rejected"


def require_audio_executor(executor: JobExecutor, advertised: frozenset[str]) -> None:
    if not advertised & _AUDIO_CAPABILITIES:
        return
    fn = getattr(executor, "execute_audio", None)
    if not callable(fn) or not (
        inspect.iscoroutinefunction(fn)
        or inspect.iscoroutinefunction(getattr(fn, "__call__", None))
    ):
        raise RuntimeError("audio capabilities require async execute_audio")


def _audio_json(value: Mapping[str, Any]) -> dict[str, Any]:
    """Snapshot bounded JSON before serialization, rejecting binary and NaN."""
    remaining = 256

    def copy(item: Any, depth: int) -> Any:
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > 4:
            raise ValueError(_AUDIO_RESULT_ERROR)
        if item is None or type(item) is bool:
            return item
        if type(item) is str:
            if len(item.encode("utf-8")) > 8192:
                raise ValueError(_AUDIO_RESULT_ERROR)
            return item
        if type(item) is int and -(2**63) <= item < 2**63:
            return item
        if type(item) is float and math.isfinite(item):
            return item
        if isinstance(item, Mapping) and len(item) <= 64:
            if any(type(key) is not str or len(key.encode("utf-8")) > 128 for key in item):
                raise ValueError(_AUDIO_RESULT_ERROR)
            return {key: copy(child, depth + 1) for key, child in item.items()}
        if type(item) is list and len(item) <= 64:
            return [copy(child, depth + 1) for child in item]
        raise ValueError(_AUDIO_RESULT_ERROR)

    snapshot = copy(value, 0)
    if len(json.dumps(snapshot, ensure_ascii=False, allow_nan=False).encode("utf-8")) > 8192:
        raise ValueError(_AUDIO_RESULT_ERROR)
    return snapshot


class _AudioJobMediaSession:
    """Private one-shot authority holder. Python adapters are trusted, not sandboxed."""

    __slots__ = ("__runtime", "__claimed", "__closed", "__used", "__error", "__staged", "__read")

    def __init__(self, runtime: "WorkerRuntime", claimed: ClaimedJob) -> None:
        self.__runtime = runtime
        self.__claimed = claimed
        self.__closed = False
        self.__used = False
        self.__error: Exception | None = None
        self.__staged: ArtifactRef | None = None
        self.__read = False

    def close(self) -> None:
        self.__closed = True

    def _check(self) -> None:
        if self.__closed:
            raise ControlTransportError(409, _AUDIO_MEDIA_ERROR)
        self.__runtime._require_audio_claim(self.__claimed)

    def _begin(self, capability: str, max_bytes: int) -> None:
        self._check()
        used, self.__used = self.__used, True
        if (used or self.__claimed.request.capability != capability
                or type(max_bytes) is not int or not 1 <= max_bytes <= _AUDIO_MAX_BYTES):
            raise JobExecutionError("audio_media_rejected", _AUDIO_MEDIA_ERROR, retryable=False)

    def raise_if_failed(self) -> None:
        if self.__error is not None:
            if isinstance(self.__error, ControlTransportError):
                raise ControlTransportError(self.__error.status_code, _AUDIO_MEDIA_ERROR) from None
            raise JobExecutionError(
                "audio_media_rejected", _AUDIO_MEDIA_ERROR, retryable=False,
            ) from None
        self._check()

    async def read_input(self, *, max_bytes: int) -> bytes:
        try:
            self._begin("audio.transcribe", max_bytes)
            # No _control_lock over media HTTP I/O: the execution owner's
            # independent heartbeat continues to renew this exact claim.
            data = await self.__runtime.client.download_audio_input(
                self.__runtime.spec.worker_id, self.__claimed, max_bytes=max_bytes,
            )
            self._check()
            self.__read = True
            return data
        except asyncio.CancelledError:
            self.__error = ControlTransportError(0, _AUDIO_MEDIA_ERROR)
            raise
        except Exception as exc:
            self.__error = self.__error or exc
            raise

    async def stage_output(self, wav_bytes: bytes, *, max_bytes: int) -> ArtifactRef:
        try:
            self._begin("audio.synthesize", max_bytes)
            reference = await self.__runtime.client.stage_audio_output(
                self.__runtime.spec.worker_id, self.__claimed, wav_bytes, max_bytes=max_bytes,
            )
            self._check()
            self.__staged = reference
            return reference
        except asyncio.CancelledError:
            self.__error = ControlTransportError(0, _AUDIO_MEDIA_ERROR)
            raise
        except Exception as exc:
            # Sticky even if an adapter catches the error. In particular an
            # uncertain POST must never turn into a completion or a retry.
            self.__error = self.__error or exc
            raise

    def result(self, result: Any) -> JobResult:
        self.raise_if_failed()
        if type(result) is not JobResult or result.outputs:
            raise ValueError(_AUDIO_RESULT_ERROR)
        if self.__claimed.request.capability == "audio.synthesize":
            if (self.__staged is None or result.artifacts != (self.__staged,)
                    or result.text is not None):
                raise ValueError(_AUDIO_RESULT_ERROR)
        elif (not self.__read or result.artifacts or type(result.text) is not str
                or len(result.text.encode("utf-8")) > _AUDIO_TEXT_MAX_BYTES):
            raise ValueError(_AUDIO_RESULT_ERROR)
        metadata = _audio_json(result.metadata)
        metrics = _audio_json(result.metrics)
        if any(type(value) not in (int, float) for value in metrics.values()):
            raise ValueError(_AUDIO_RESULT_ERROR)
        return JobResult(artifacts=result.artifacts, text=result.text, metadata=metadata, metrics=metrics)


class _ExecutorAudioMedia:
    """Expose only binary operations, not session lifecycle/result validation."""

    __slots__ = ("__session",)

    def __init__(self, session: _AudioJobMediaSession) -> None:
        self.__session = session

    async def read_input(self, *, max_bytes: int) -> bytes:
        return await self.__session.read_input(max_bytes=max_bytes)

    async def stage_output(self, wav_bytes: bytes, *, max_bytes: int) -> ArtifactRef:
        return await self.__session.stage_output(wav_bytes, max_bytes=max_bytes)


def discover_nvidia_gpu_uuids(command: str = "nvidia-smi") -> tuple[str, ...]:
    try:
        completed = subprocess.run(
            [command, "--query-gpu=uuid", "--format=csv,noheader"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("nvidia-smi preflight failed") from exc
    uuids = tuple(sorted(line.strip() for line in completed.stdout.splitlines() if line.strip()))
    if not uuids:
        raise RuntimeError("no NVIDIA GPUs observed")
    if len(set(uuids)) != len(uuids):
        raise RuntimeError("observed duplicate GPU UUIDs")
    return uuids


def require_exact_gpu_set(
    expected: tuple[str, ...],
    *,
    command: str = "nvidia-smi",
) -> None:
    expected_sorted = tuple(sorted(expected))
    if not expected_sorted:
        return
    if len(set(expected_sorted)) != len(expected_sorted):
        raise RuntimeError("configured GPU UUIDs contain duplicates")
    observed = discover_nvidia_gpu_uuids(command)
    if observed != expected_sorted:
        raise RuntimeError(
            f"GPU UUID set mismatch (expected_count={len(expected_sorted)} observed_count={len(observed)})"
        )


def require_isolated_gpu_access(
    expected: tuple[str, ...],
    *,
    reviewed_map_path: str,
    command: str = "nvidia-smi",
    cuda_visible_devices: str | None,
) -> None:
    if not expected:
        return
    map_path = Path(reviewed_map_path)
    if not map_path.is_absolute():
        raise RuntimeError("isolated GPU device map path must be absolute")
    mapping = discover_gpu_mapping(nvidia_smi=command)
    reviewed = load_reviewed_gpu_map(map_path)
    verify_isolated_gpu_access(
        mapping,
        expected,
        reviewed,
        worker_gpu_order=expected,
        cuda_visible_devices=cuda_visible_devices,
    )


def executor_capabilities(executor: JobExecutor) -> frozenset[str]:
    raw = getattr(executor, "capabilities", None)
    if raw is None:
        raise TypeError("configured executor must declare capabilities")
    if isinstance(raw, str):
        values = (raw,)
    else:
        values = tuple(raw)
    capabilities = frozenset(str(value).strip() for value in values)
    if not capabilities or any(not value for value in capabilities):
        raise ValueError("executor capabilities must be non-empty non-blank strings")
    return capabilities


def require_executor_capabilities(
    executor: JobExecutor,
    advertised: frozenset[str],
) -> None:
    supported = executor_capabilities(executor)
    missing = advertised - supported
    if missing:
        raise RuntimeError(
            "executor does not support advertised capabilities: "
            + ", ".join(sorted(missing))
        )


def require_executor_serving_features(
    executor: JobExecutor,
    declaration: ServingDeploymentDeclaration,
) -> None:
    raw = getattr(executor, "serving_features", {})
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise TypeError("executor serving_features must be a mapping")

    for contract in declaration.contracts:
        values = raw.get(contract.capability, ())
        if isinstance(values, str):
            values = (values,)
        try:
            supported = frozenset(str(value).strip() for value in values)
        except TypeError as exc:
            raise TypeError(
                "executor serving feature values must be iterable"
            ) from exc
        if any(not value for value in supported):
            raise ValueError(
                "executor serving features must be non-blank strings"
            )
        missing = contract.features - supported
        if missing:
            raise RuntimeError(
                "executor does not support advertised serving features for "
                f"{contract.capability}: " + ", ".join(sorted(missing))
            )


def load_executor(specifier: str, config: Mapping[str, Any] | None = None) -> JobExecutor:
    module_name, separator, attribute_name = specifier.partition(":")
    if not separator or not module_name or not attribute_name:
        raise ValueError("executor must use module:attribute syntax")
    module = importlib.import_module(module_name)
    target = getattr(module, attribute_name)
    if isinstance(target, JobExecutor):
        executor = target
    elif callable(target):
        executor = target(dict(config or {}))
        if inspect.isawaitable(executor):
            raise TypeError("executor factory must be synchronous")
    else:
        raise TypeError("executor target must be a JobExecutor or factory")
    if not isinstance(executor, JobExecutor):
        raise TypeError("executor factory did not return JobExecutor")
    return executor


class _WorkerJobEventSink:
    def __init__(self, runtime: "WorkerRuntime", claimed: ClaimedJob) -> None:
        self._runtime = runtime
        self._claimed = claimed

    async def emit(self, event: JobEvent) -> None:
        if not isinstance(event, JobEvent):
            raise TypeError("streaming executor must emit JobEvent values")
        await self._runtime._publish_job_event(self._claimed, event)


class WorkerRuntime:
    """One execution owner with a persistent, job-independent heartbeat deadline.

    Control RPCs are serialized with drain requests. No heartbeat task can
    outlive an attempt or race a terminal write using an obsolete lease token.
    """

    def __init__(
        self,
        *,
        spec: WorkerSpec,
        max_concurrency: int,
        executor: JobExecutor,
        client: ControlClient,
        poll_interval_seconds: float = 1.0,
        heartbeat_interval_seconds: float = 5.0,
        recovery_retry_interval_seconds: float = 30.0,
        runtime_supervisor: RuntimeHealthSupervisor | None = None,
        serving: WorkerServingAdvertisement | None = None,
    ) -> None:
        if max_concurrency != 1:
            raise ValueError("v1 WorkerRuntime currently requires max_concurrency=1")
        require_audio_executor(executor, spec.capabilities)
        if any(
            not math.isfinite(value) or value <= 0
            for value in (
                poll_interval_seconds, heartbeat_interval_seconds,
                recovery_retry_interval_seconds,
            )
        ):
            raise ValueError("worker intervals must be finite and positive")
        self.spec = spec
        self.max_concurrency = max_concurrency
        self.executor = executor
        self.client = client
        self.runtime_supervisor = runtime_supervisor
        if serving is not None and not isinstance(serving, WorkerServingAdvertisement):
            raise TypeError("serving must be WorkerServingAdvertisement")
        if spec.capabilities & _AUDIO_CAPABILITIES and serving is None:
            raise ValueError("audio Worker requires a current serving advertisement")
        self.serving = serving
        self.poll_interval_seconds = poll_interval_seconds
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.recovery_retry_interval_seconds = recovery_retry_interval_seconds
        self._recovery_eligible = False
        self._next_recovery_at = 0.0
        self._stop = asyncio.Event()
        self._draining = False
        self._registered = False
        self._control_state: WorkerState | None = None
        self._control_valid_until = 0.0
        self._next_heartbeat_at = 0.0
        self._control_lock = asyncio.Lock()
        self._active: ClaimedJob | None = None

    @property
    def control_available(self) -> bool:
        return time.monotonic() < self._control_valid_until

    @property
    def control_state(self) -> str | None:
        return None if self._control_state is None else self._control_state.value

    @property
    def ready(self) -> bool:
        return (
            not self._stop.is_set()
            and self._registered
            and not self.draining
            and self.control_available
            and self._control_state is WorkerState.ONLINE
            and self.runtime_available
        )

    @property
    def runtime_available(self) -> bool:
        return self.runtime_supervisor is None or self.runtime_supervisor.available

    @property
    def runtime_state(self) -> str:
        return "unmanaged" if self.runtime_supervisor is None else self.runtime_supervisor.state

    @property
    def runtime_failure(self) -> str | None:
        return None if self.runtime_supervisor is None else self.runtime_supervisor.failure_reason

    def _require_runtime(self) -> None:
        if not self.runtime_available:
            raise RuntimeUnavailable("RuntimeProvider health is unconfirmed")

    @property
    def registered(self) -> bool:
        return self._registered

    @property
    def draining(self) -> bool:
        return (
            self._draining or self._control_state is WorkerState.DRAINING
            or (self.runtime_supervisor is not None and self.runtime_supervisor.failed.is_set())
        )

    @property
    def active_job_id(self) -> str | None:
        return None if self._active is None else self._active.request.job_id

    def request_drain(self) -> None:
        # A signal can set this while an RPC is in flight. A late ONLINE
        # response must never clear local drain/stop intent.
        self._draining = True

    def request_stop(self) -> None:
        self.request_drain()
        self._stop.set()

    def _control_failed(self, exc: ControlTransportError) -> None:
        if self._control_valid_until:
            _LOG.warning("Control acknowledgement lost (status=%s); claims paused", exc.status_code)
        self._control_valid_until = 0.0
        if exc.status_code in (401, 403, 404):
            self._registered = False
        # No immediate busy-loop retry, including after a failed claim/write.
        self._next_heartbeat_at = time.monotonic() + self.heartbeat_interval_seconds

    def _acknowledge_control(self, record: Any, *, sent_at: float) -> None:
        try:
            if not isinstance(record, dict) or record.get("protocol_version") != PROTOCOL_VERSION:
                raise ValueError("invalid Worker response")
            state = WorkerState(record["state"])
            if worker_spec_from_dict(record["spec"]) != self.spec:
                raise ValueError("Worker identity changed")
            if record["max_concurrency"] != self.max_concurrency:
                raise ValueError("Worker capacity changed")
            raw_serving = record.get("serving")
            acknowledged_serving = (
                None
                if raw_serving is None
                else WorkerServingAdvertisement.from_dict(raw_serving)
            )
            if acknowledged_serving is not None and SERVING_EXTENSION not in set(
                record.get("extensions") or ()
            ):
                raise ValueError("Worker acknowledgement lacks serving extension")
            if acknowledged_serving != self.serving:
                raise ValueError("Worker serving identity changed")
            metadata = record.get("metadata") or {}
            if not isinstance(metadata, dict):
                raise ValueError("Worker metadata is invalid")
            can_recover = (
                state is WorkerState.OFFLINE
                and metadata.get(OFFLINE_REASON_KEY) == RECOVERABLE_OFFLINE_REASON
            )
        except (KeyError, TypeError, ValueError) as exc:
            error = ControlTransportError(502, "invalid Worker acknowledgement")
            self._control_failed(error)
            raise error from exc
        if state is not self._control_state:
            _LOG.info("Acknowledged Control Worker state: %s", state.value)
        self._registered = True
        self._control_state = state
        if can_recover and not self._recovery_eligible:
            self._next_recovery_at = (
                time.monotonic() + self.recovery_retry_interval_seconds
            )
        self._recovery_eligible = can_recover
        # Count from request start, not response arrival: a delayed reply
        # cannot grant an unbounded extension of local readiness.
        self._control_valid_until = (
            sent_at + self.heartbeat_interval_seconds + self.client.timeout_seconds
        )

    async def register(self) -> None:
        async with self._control_lock:
            sent_at = time.monotonic()
            try:
                if isinstance(self.executor, StreamingJobExecutor):
                    await self.client.require_extension(JOB_EVENTS_EXTENSION)
                record = await self.client.register(
                    spec=self.spec,
                    max_concurrency=self.max_concurrency,
                    metadata={"runtime": "astrumweaver-worker"},
                    serving=self.serving,
                )
                self._acknowledge_control(record, sent_at=sent_at)
            except ControlTransportError as exc:
                self._control_failed(exc)
                raise
            self._next_heartbeat_at = time.monotonic() + self.heartbeat_interval_seconds
            if self._draining:
                await self._heartbeat_locked()

    async def _heartbeat_locked(self) -> None:
        """Called only by the execution owner or drain, under _control_lock."""
        active = self._active
        sent_at = time.monotonic()
        # Heartbeat is never allowed to reacquire OFFLINE resources. Control
        # rejects an ONLINE/DRAINING promotion from OFFLINE, including races.
        state = (
            WorkerState.DRAINING
            if (self._draining or (
                self.runtime_supervisor is not None and self.runtime_supervisor.failed.is_set()
            )) and self.control_available
            and self._control_state is not WorkerState.OFFLINE
            else None
        )
        try:
            record = await self.client.heartbeat(
                self.spec.worker_id,
                active_job_id=None if active is None else active.request.job_id,
                lease_token=None if active is None else active.lease_token,
                state=state,
                runtime_instance_epoch=(
                    None
                    if self.serving is None
                    else self.serving.runtime_instance.epoch
                ),
            )
            self._acknowledge_control(record, sent_at=sent_at)
        except ControlTransportError as exc:
            self._control_failed(exc)
            raise
        finally:
            self._next_heartbeat_at = time.monotonic() + self.heartbeat_interval_seconds

    async def _heartbeat_if_due(self) -> None:
        async with self._control_lock:
            if time.monotonic() >= self._next_heartbeat_at:
                await self._heartbeat_locked()

    async def _reconcile_offline_if_due(self) -> None:
        """Only an idle TTL-expired Worker may explicitly reclaim resources.

        A stateless Heartbeat MUST NOT re-admit the Worker. The Control
        repository makes the final atomic decision under GPU ownership locks,
        unchanged serving epoch, and zero active durable leases.
        """
        async with self._control_lock:
            if not (
                self._recovery_eligible
                and self._registered
                and self._control_state is WorkerState.OFFLINE
                and self.control_available
                and self.runtime_available
                and not self.draining
                and not self._stop.is_set()
                and self._active is None
                and time.monotonic() >= self._next_recovery_at
            ):
                return
            self._next_recovery_at = (
                time.monotonic() + self.recovery_retry_interval_seconds
            )
            sent_at = time.monotonic()
            try:
                if isinstance(self.executor, StreamingJobExecutor):
                    await self.client.require_extension(JOB_EVENTS_EXTENSION)
                record = await self.client.register(
                    spec=self.spec, max_concurrency=self.max_concurrency,
                    metadata={"runtime": "astrumweaver-worker"},
                    serving=self.serving, recovery=True,
                )
                self._acknowledge_control(record, sent_at=sent_at)
                _LOG.info("Explicit Control Worker TTL reconciliation accepted")
            except ControlTransportError as exc:
                if exc.status_code == 409:
                    # An authoritative conflict is not a network loss:
                    # remain OFFLINE, retry no faster than the cooldown.
                    _LOG.warning("Explicit Worker recovery blocked by Control conflict")
                else:
                    self._control_failed(exc)
                # Keep running for heartbeats but never claim without Online.

    async def _wait_for_poll(self) -> None:
        delay = min(
            self.poll_interval_seconds,
            max(0.0, self._next_heartbeat_at - time.monotonic()),
        )
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=delay)

    async def run_forever(self) -> None:
        try:
            if self._stop.is_set():
                return
            if self.runtime_supervisor is not None:
                await self.runtime_supervisor.start()
                if not self.runtime_available:
                    # Stay not-ready without an automatic service restart loop.
                    await self._stop.wait()
                    return
            await self.register()
            while not self._stop.is_set():
                try:
                    # Also runs during local/remote drain and OFFLINE pause.
                    # Completion and claim never reset this deadline.
                    await self._heartbeat_if_due()
                    await self._reconcile_offline_if_due()
                    if not self.ready:
                        await self._wait_for_poll()
                        continue
                    async with self._control_lock:
                        if not self.ready:
                            continue
                        # A signal-driven drain RPC might have held the lock.
                        if time.monotonic() >= self._next_heartbeat_at:
                            await self._heartbeat_locked()
                        if not self.ready:
                            continue
                        claimed = await self.client.claim(
                            self.spec.worker_id,
                            runtime_instance_epoch=(
                                None
                                if self.serving is None
                                else self.serving.runtime_instance.epoch
                            ),
                        )
                        self._active = claimed
                    if claimed is None:
                        await self._wait_for_poll()
                        continue
                    try:
                        # A claim may have been admitted before a failed probe
                        # while its response was in flight. Do not execute it.
                        self._require_runtime()
                        await self._execute_claim(claimed)
                    finally:
                        self._active = None
                except RuntimeUnavailable:
                    # Do not manufacture a failure for an unconfirmed attempt.
                    # Server lease expiry/recovery remains authoritative.
                    await self._wait_for_poll()
                except ControlTransportError as exc:
                    # Stay live but not ready. Retry a stateless heartbeat.
                    # Only the separate TTL-marked, ownership-validated
                    # reconciliation may ever reacquire OFFLINE resources.
                    self._control_failed(exc)
                    await self._wait_for_poll()
        finally:
            self.request_stop()
            self._control_valid_until = 0.0
            if self.runtime_supervisor is not None:
                await self.runtime_supervisor.close()
                # Do not release an idle GPU's Control reservation before its
                # owned process is stopped. Failed cleanup keeps ownership
                # fail-closed; daemon fallback/service-manager cleanup follows.
                await self.runtime_supervisor.shutdown_owned()
            async with self._control_lock:
                if self._registered:
                    with contextlib.suppress(ControlTransportError):
                        await self.client.set_state(
                            self.spec.worker_id,
                            WorkerState.OFFLINE,
                            runtime_instance_epoch=(
                                None
                                if self.serving is None
                                else self.serving.runtime_instance.epoch
                            ),
                        )
                self._registered = False

    async def drain(self) -> None:
        self.request_drain()
        async with self._control_lock:
            if self._registered and not self._stop.is_set():
                # Do not use set_state(DRAINING), which can reacquire OFFLINE
                # resources. Heartbeat's guarded transition cannot do so.
                await self._heartbeat_locked()

    def _validate_claim_serving(self, claimed: ClaimedJob) -> None:
        metadata = claimed.request.metadata
        raw_binding = metadata.get("serving")
        claimed_values = (
            metadata.get("claimed_deployment_revision"),
            metadata.get("claimed_serving_contract_revision"),
            metadata.get("claimed_runtime_instance_epoch"),
        )
        if raw_binding is None:
            if claimed.request.capability in _AUDIO_CAPABILITIES:
                raise RuntimeError("audio claim requires a current serving binding")
            if any(value is not None for value in claimed_values):
                raise RuntimeError("legacy claim carried partial serving identity")
            return
        binding = ServingJobBinding.from_dict(raw_binding)
        if (claimed.request.capability in _AUDIO_CAPABILITIES
                and binding.capability != claimed.request.capability):
            raise RuntimeError("audio claim serving capability does not match")
        if self.serving is None or not worker_serving_matches(self.serving, binding):
            raise RuntimeError("claimed serving binding does not match local deployment")
        if claimed_values != (
            self.serving.deployment_revision,
            binding.serving_contract_revision,
            self.serving.runtime_instance.epoch,
        ):
            raise RuntimeError("claimed serving attempt identity does not match local runtime")

    def _executor_request(self, claimed: ClaimedJob) -> JobRequest:
        """Attach locally revalidated serving semantics for workload adapters."""
        raw_binding = claimed.request.metadata.get("serving")
        if raw_binding is None:
            return claimed.request
        if self.serving is None:
            raise RuntimeError("serving claim has no local serving advertisement")
        binding = ServingJobBinding.from_dict(raw_binding)
        contract = self.serving.contract_by_revision(
            binding.serving_contract_revision
        )
        if contract is None:
            raise RuntimeError("claimed serving contract is unavailable locally")
        metadata = dict(claimed.request.metadata)
        if claimed.request.capability in _AUDIO_CAPABILITIES:
            # Claims carry an epoch for Worker-side fencing. It is never an
            # executor credential, even though generic legacy adapters retain
            # their historical metadata. Audio uses an explicit allowlist.
            metadata = {
                key: metadata[key] for key in ("attempt", "deadline_at", "serving")
                if key in metadata
            }
        metadata["serving_semantic_revision"] = contract.semantic_revision
        return JobRequest(
            job_id=claimed.request.job_id,
            capability=claimed.request.capability,
            payload=claimed.request.payload,
            metadata=metadata,
        )

    async def _publish_job_event(
        self,
        claimed: ClaimedJob,
        event: JobEvent,
    ) -> None:
        if self._active is not claimed:
            raise RuntimeError("Job event belongs to an inactive local attempt")
        async with self._control_lock:
            if self._active is not claimed:
                raise RuntimeError("Job event belongs to an inactive local attempt")
            self._require_runtime()
            if time.monotonic() >= self._next_heartbeat_at:
                await self._heartbeat_locked()
            if not self.control_available:
                raise ControlTransportError(0, "Worker authority is unconfirmed")
            try:
                await self.client.publish_event(
                    self.spec.worker_id,
                    claimed.request.job_id,
                    claimed.lease_token,
                    kind=event.kind,
                    payload=event.payload,
                    runtime_instance_epoch=(
                        None
                        if self.serving is None
                        else self.serving.runtime_instance.epoch
                    ),
                )
            except ControlTransportError as exc:
                if exc.status_code == 429:
                    raise JobExecutionError(
                        "event_buffer_full",
                        "Control rejected the bounded Job event buffer",
                        retryable=False,
                    ) from exc
                self._control_failed(exc)
                raise

    def _require_audio_claim(self, claimed: ClaimedJob) -> None:
        if (self._active is not claimed or self._stop.is_set()
                or not self._registered or not self.control_available
                or self._control_state not in (WorkerState.ONLINE, WorkerState.DRAINING)
                or not self.runtime_available):
            raise ControlTransportError(409, _AUDIO_MEDIA_ERROR)
        try:
            self._validate_claim_serving(claimed)
        except (KeyError, TypeError, ValueError, RuntimeError):
            raise ControlTransportError(409, _AUDIO_MEDIA_ERROR) from None

    async def _execute_audio(self, claimed: ClaimedJob, request: JobRequest) -> JobResult:
        self._require_audio_claim(claimed)
        session = _AudioJobMediaSession(self, claimed)
        try:
            try:
                result = await cast(AudioJobExecutor, self.executor).execute_audio(
                    request, _ExecutorAudioMedia(session),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                # A media transport error takes precedence over an adapter's
                # replacement exception, including a swallowed uncertain POST.
                session.raise_if_failed()
                raise JobExecutionError(
                    "audio_executor_failed", _AUDIO_EXECUTOR_ERROR, retryable=False,
                ) from None
            session.raise_if_failed()
            try:
                return session.result(result)
            except ControlTransportError:
                raise
            except Exception:
                raise JobExecutionError(
                    "audio_result_invalid", _AUDIO_RESULT_ERROR, retryable=False,
                ) from None
        finally:
            session.close()

    async def _execute_claim(self, claimed: ClaimedJob) -> None:
        # The private direct-entry test path also installs the active attempt;
        # normal callers install it under the claim lock before reaching here.
        if self._active is not None and self._active is not claimed:
            raise RuntimeError("Worker already owns a different local attempt")
        self._require_runtime()
        self._validate_claim_serving(claimed)
        executor_request = self._executor_request(claimed)
        self._active = claimed
        is_audio = claimed.request.capability in _AUDIO_CAPABILITIES
        runtime_failure = (
            asyncio.create_task(self.runtime_supervisor.failed.wait(), name="astrumweaver-runtime-failure")
            if self.runtime_supervisor is not None else None
        )
        if is_audio:
            execution_call = self._execute_audio(claimed, executor_request)
        elif isinstance(self.executor, StreamingJobExecutor):
            execution_call = self.executor.execute_stream(
                executor_request,
                _WorkerJobEventSink(self, claimed),
            )
        else:
            execution_call = self.executor.execute(executor_request)
        execution = asyncio.create_task(
            execution_call,
            name="astrumweaver-job-execution",
        )
        try:
            while not execution.done():
                self._require_runtime()
                await self._heartbeat_if_due()
                self._require_runtime()
                await asyncio.wait(
                    {execution} if runtime_failure is None else {execution, runtime_failure},
                    timeout=max(0.0, self._next_heartbeat_at - time.monotonic()),
                    return_when=asyncio.FIRST_COMPLETED,
                )

            self._require_runtime()
            error: dict[str, str] | None = None
            retryable = False
            try:
                result = await execution
            except asyncio.CancelledError:
                raise
            except ControlTransportError as exc:
                if is_audio:
                    # Media upload/download acknowledgement may be uncertain.
                    # Abandon this attempt for the existing Control lease fence;
                    # never misreport durable failure or resubmit an output.
                    raise
                if self.runtime_supervisor is not None:
                    await self.runtime_supervisor.check()
                    self._require_runtime()
                # Preserve the historical generic executor-error behavior.
                error = {"type": type(exc).__name__, "message": str(exc)}
                retryable = True
            except JobExecutionError as exc:
                if self.runtime_supervisor is not None:
                    await self.runtime_supervisor.check()
                    self._require_runtime()
                error = {
                    "type": type(exc).__name__,
                    "code": exc.code,
                    "message": str(exc),
                }
                retryable = exc.retryable
            except Exception as exc:
                if self.runtime_supervisor is not None:
                    # Detect a dead runtime before consuming another queued
                    # job. Job validation errors on a healthy runtime retain
                    # the ordinary fenced failure path.
                    await self.runtime_supervisor.check()
                    self._require_runtime()
                error = (
                    {"type": "AudioExecutorError", "message": _AUDIO_EXECUTOR_ERROR}
                    if is_audio else {"type": type(exc).__name__, "message": str(exc)}
                )
                retryable = not is_audio
            else:
                if not isinstance(result, JobResult):
                    error = {
                        "type": "ExecutorProtocolError",
                        "message": "executor returned a non-JobResult value",
                    }

            async with self._control_lock:
                # Renew first if due, even when a short execution completed
                # immediately. No terminal write races a lease heartbeat.
                if time.monotonic() >= self._next_heartbeat_at:
                    await self._heartbeat_locked()
                self._require_runtime()
                if not self.control_available:
                    raise ControlTransportError(0, "Worker authority is unconfirmed")
                if is_audio:
                    self._require_audio_claim(claimed)
                if error is not None:
                    await self.client.fail(
                        self.spec.worker_id, claimed.request.job_id,
                        claimed.lease_token, error=error, retryable=retryable,
                        runtime_instance_epoch=(
                            None if self.serving is None
                            else self.serving.runtime_instance.epoch
                        ),
                    )
                else:
                    await self.client.complete(
                        self.spec.worker_id, claimed.request.job_id,
                        claimed.lease_token, result,
                        runtime_instance_epoch=(
                            None if self.serving is None
                            else self.serving.runtime_instance.epoch
                        ),
                    )
                # Do not let a waiting drain heartbeat reuse this terminal lease.
                self._active = None
        except ControlTransportError as exc:
            self._control_failed(exc)
            if exc.status_code == 409:
                async with self._control_lock:
                    status = await self.client.inspect_job(
                        self.spec.worker_id, claimed.request.job_id
                    )
                if status.get("status") == "cancelled":
                    return
            # Unknown/lost lease: abandon local work without reporting a new
            # failure. Durable recovery/fencing decides this attempt's fate.
            raise
        finally:
            try:
                if not execution.done():
                    try:
                        await self.executor.cancel(claimed.request.job_id)
                    finally:
                        execution.cancel()
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await execution
            finally:
                if runtime_failure is not None:
                    runtime_failure.cancel()
                    await asyncio.gather(runtime_failure, return_exceptions=True)
                # Retrieve a completed task's exception even if health failed
                # before the normal result path. No unobserved task leaks.
                if execution.done() and not execution.cancelled():
                    execution.exception()
                self._active = None
