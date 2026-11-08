"""#247 R4-B0: bounded opt-in observation-only polling of a user-managed VOICEVOX.

This code owns only its own asyncio task and canonical R1 read-only HTTP
probe. It never starts/stops/configures a native engine, nor advertises a
Worker, grants rights, invokes R4-A planning or creates serving authority.
The opt-in candidate must be exact worker-namespace loopback; no LAN scan.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from dataclasses import dataclass, field
from enum import Enum

from .voicevox_discovery import (
    VoicevoxDiscoveryConfig,
    VoicevoxDiscoveryProbe,
    VoicevoxDiscoveryResult,
    VoicevoxDiscoveryStatus,
)
from .voicevox_external_catalog import _verified_shape

_MAX_SEQ = 2**63 - 1
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SCHEMA = "voicevox-external-polling-snapshot-v1"


class ExternalVoicevoxPollingError(RuntimeError):
    """Never expose raw endpoint, native body, exception, or voice metadata."""

    def __init__(self) -> None:
        super().__init__("VOICEVOX external polling is unavailable")


def _invalid() -> ValueError:
    return ValueError("VOICEVOX external polling config is invalid")


class VoicevoxPollStatus(str, Enum):
    """Observation only: compatible_observed is NOT ready/verified/admitted."""

    DISABLED = "disabled"
    IDLE = "idle"
    PROBING = "probing"
    ABSENT = "absent"
    INCOMPATIBLE = "incompatible"
    COMPATIBLE_OBSERVED = "compatible_observed"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True, repr=False)
class VoicevoxExternalPollingConfig:
    """One bounded loopback endpoint, disabled by default.

    Disabled has no obligation to inspect any malformed foreign settings.
    Valid enabled declarations are re-created at the coordinator boundary.
    """

    enabled: bool = False
    discovery: object = field(default_factory=VoicevoxDiscoveryConfig, repr=False)
    interval_seconds: float = 5.0
    max_backoff_seconds: float = 30.0

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise _invalid()
        if not self.enabled:
            return
        try:
            if (
                type(self.discovery) is not VoicevoxDiscoveryConfig
                or not self.discovery.enabled
            ):
                raise _invalid()
            canonical = VoicevoxDiscoveryConfig(
                enabled=True,
                endpoint=self.discovery.endpoint,
                timeout_seconds=self.discovery.timeout_seconds,
            )
            if (
                type(self.interval_seconds) not in (int, float)
                or not 1 <= self.interval_seconds <= 60
                or not math.isfinite(self.interval_seconds)
                or type(self.max_backoff_seconds) not in (int, float)
                or not self.interval_seconds <= self.max_backoff_seconds <= 60
                or not math.isfinite(self.max_backoff_seconds)
            ):
                raise _invalid()
            object.__setattr__(self, "discovery", canonical)
        except Exception:  # noqa: BLE001 - never echo arbitrary endpoint
            raise _invalid() from None


@dataclass(frozen=True, slots=True, repr=False)
class VoicevoxExternalPollSnapshot:
    """Sanitized point-in-time observation, not freshness/serving authority."""

    status: VoicevoxPollStatus
    running: bool
    sequence: int
    completed_probes: int
    consecutive_failures: int
    observed_digest: str | None = None

    def __post_init__(self) -> None:
        if (
            type(self.status) is not VoicevoxPollStatus
            or type(self.running) is not bool
            or type(self.sequence) is not int
            or not 0 <= self.sequence <= _MAX_SEQ
            or type(self.completed_probes) is not int
            or not 0 <= self.completed_probes <= self.sequence
            or type(self.consecutive_failures) is not int
            or not 0 <= self.consecutive_failures <= self.completed_probes
            or (
                self.status is VoicevoxPollStatus.COMPATIBLE_OBSERVED
                and (
                    not self.running
                    or self.consecutive_failures != 0
                    or type(self.observed_digest) is not str
                    or _SHA256.fullmatch(self.observed_digest) is None
                )
            )
            or (
                self.status is not VoicevoxPollStatus.COMPATIBLE_OBSERVED
                and self.observed_digest is not None
            )
            or (
                self.status
                in (
                    VoicevoxPollStatus.DISABLED,
                    VoicevoxPollStatus.IDLE,
                    VoicevoxPollStatus.STOPPED,
                    VoicevoxPollStatus.FAILED,
                )
                and self.running
            )
            or (
                self.status
                in (
                    VoicevoxPollStatus.PROBING,
                    VoicevoxPollStatus.ABSENT,
                    VoicevoxPollStatus.INCOMPATIBLE,
                )
                and not self.running
            )
        ):
            raise ExternalVoicevoxPollingError()

    @property
    def ready(self) -> bool:
        return False

    @property
    def can_apply(self) -> bool:
        return False

    @property
    def worker_runtime_instance_epoch(self) -> None:
        return None

    def to_dict(self) -> dict[str, object]:
        # Frozen dataclasses can be reflectively modified. Revalidate them
        # again at this public serialization boundary before emitting fields.
        try:
            checked = VoicevoxExternalPollSnapshot(
                status=self.status,
                running=self.running,
                sequence=self.sequence,
                completed_probes=self.completed_probes,
                consecutive_failures=self.consecutive_failures,
                observed_digest=self.observed_digest,
            )
        except Exception:  # noqa: BLE001 - fixed error, no raw data
            raise ExternalVoicevoxPollingError() from None
        return {
            "schema_version": _SCHEMA,
            "status": checked.status.value,
            "running": checked.running,
            "sequence": checked.sequence,
            "completed_probes": checked.completed_probes,
            "consecutive_failures": checked.consecutive_failures,
            "observed_digest": checked.observed_digest,
            "ready": False,
            "can_apply": False,
            "rights_verified": False,
            "external_engine_verified": False,
            "worker_runtime_instance_epoch": None,
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )


class ExternalVoicevoxPollingCoordinator:
    """One opt-in R1 polling task, single flight, no process ownership.

    start() is idempotent for a running task, but a stopped task must be
    joined with stop() before restart. request_stop() revokes observations
    synchronously before cancellation; it does not shut down VOICEVOX.
    close() is terminal; stop() permits a deliberate new coordinator run.
    """

    def __init__(self, *, config: VoicevoxExternalPollingConfig) -> None:
        if type(config) is not VoicevoxExternalPollingConfig:
            raise _invalid()
        try:
            if config.enabled:
                self._config = VoicevoxExternalPollingConfig(
                    enabled=True,
                    discovery=config.discovery,
                    interval_seconds=config.interval_seconds,
                    max_backoff_seconds=config.max_backoff_seconds,
                )
                self._probe = VoicevoxDiscoveryProbe(self._config.discovery)
                self._status = VoicevoxPollStatus.IDLE
            else:
                # Never touch disabled discovery, interval, or policy values.
                self._config = VoicevoxExternalPollingConfig(enabled=False)
                self._probe = None
                self._status = VoicevoxPollStatus.DISABLED
        except Exception:  # noqa: BLE001 - redact invalid configuration
            raise _invalid() from None
        self._task: asyncio.Task[None] | None = None
        self._generation = 0
        self._sequence = 0
        self._completed = 0
        self._failures = 0
        self._digest: str | None = None
        self._stop_requested = False
        self._closed = False

    @property
    def ready(self) -> bool:
        return False

    @property
    def can_apply(self) -> bool:
        return False

    def snapshot(self) -> VoicevoxExternalPollSnapshot:
        running = (
            self._config.enabled
            and not self._stop_requested
            and not self._closed
            and self._task is not None
            and not self._task.done()
        )
        # A finished task cannot leave a positive snapshot, even if an
        # unforeseen exit bypasses normal finally handling.
        status = (
            self._status
            if running
            else (
                VoicevoxPollStatus.STOPPED
                if self._closed or self._stop_requested
                else VoicevoxPollStatus.DISABLED
                if not self._config.enabled
                else VoicevoxPollStatus.FAILED
                if self._task is not None
                else VoicevoxPollStatus.IDLE
            )
        )
        return VoicevoxExternalPollSnapshot(
            status=status,
            running=bool(running),
            sequence=self._sequence,
            completed_probes=self._completed,
            consecutive_failures=self._failures,
            observed_digest=(
                self._digest
                if running and status is VoicevoxPollStatus.COMPATIBLE_OBSERVED
                else None
            ),
        )

    async def start(self) -> None:
        if self._closed:
            raise ExternalVoicevoxPollingError()
        if not self._config.enabled:
            return
        if self._task is not None:
            if self._stop_requested or self._task.done():
                raise ExternalVoicevoxPollingError()
            return

        self._generation += 1
        self._stop_requested = False
        self._digest = None
        self._failures = 0
        self._status = VoicevoxPollStatus.PROBING
        token = self._generation
        self._task = asyncio.create_task(
            self._run(token), name="tsumgi-external-voicevox-r4b0-poll"
        )

    def request_stop(self) -> None:
        """Revoke any observation synchronously, then cancel TSUMGI task."""
        if self._closed or self._stop_requested:
            return
        self._stop_requested = True
        self._generation += 1
        if self._sequence < _MAX_SEQ:
            self._sequence += 1  # invalidates in-flight sample sequence
        self._status = VoicevoxPollStatus.STOPPED
        self._digest = None
        if self._task is not None and not self._task.done():
            self._task.cancel()

    async def _join_owned_task(self) -> None:
        task = self._task
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
            if self._task is task and task.done():
                self._task = None

    async def stop(self) -> None:
        self.request_stop()
        await self._join_owned_task()

    async def close(self) -> None:
        if not self._closed:
            self.request_stop()
            self._closed = True
            self._status = VoicevoxPollStatus.STOPPED
            self._digest = None
        await self._join_owned_task()

    async def _sleep_interval(self, seconds: float) -> None:
        """Private test-only timing seam; production uses monotonic asyncio."""
        await asyncio.sleep(seconds)

    async def _run(self, token: int) -> None:
        try:
            while (
                not self._closed
                and not self._stop_requested
                and token == self._generation
            ):
                if self._sequence >= _MAX_SEQ:
                    break
                self._sequence += 1  # allocated at probe START, not completion
                sequence = self._sequence
                self._digest = None
                self._status = VoicevoxPollStatus.PROBING
                try:
                    # Only canonical internally constructed R1 probe can run.
                    result = await self._probe.probe_once()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - redact native diagnostics
                    result = None

                # A stop/reconfiguration request invalidates token + sequence
                # synchronously, even if private test fakes swallow cancellation.
                if (
                    self._closed
                    or self._stop_requested
                    or token != self._generation
                    or sequence != self._sequence
                ):
                    break

                status = VoicevoxPollStatus.INCOMPATIBLE
                digest: str | None = None
                if type(result) is VoicevoxDiscoveryResult:
                    if result.status is VoicevoxDiscoveryStatus.ABSENT:
                        status = VoicevoxPollStatus.ABSENT
                    elif result.status is VoicevoxDiscoveryStatus.INCOMPATIBLE:
                        status = VoicevoxPollStatus.INCOMPATIBLE
                    elif (
                        result.status is VoicevoxDiscoveryStatus.COMPATIBLE
                        and _verified_shape(
                            result.snapshot, self._config.discovery.endpoint
                        )
                        is not None
                    ):
                        status = VoicevoxPollStatus.COMPATIBLE_OBSERVED
                        digest = result.snapshot.observed_digest

                self._status = status
                self._digest = digest
                self._completed = min(self._completed + 1, _MAX_SEQ)
                if status is VoicevoxPollStatus.COMPATIBLE_OBSERVED:
                    self._failures = 0
                    delay = self._config.interval_seconds
                else:
                    self._failures = min(self._failures + 1, _MAX_SEQ)
                    # Bound the exponent and the eventual retry delay.
                    delay = min(
                        self._config.interval_seconds * 2 ** min(self._failures - 1, 6),
                        self._config.max_backoff_seconds,
                    )
                await self._sleep_interval(delay)
        except asyncio.CancelledError:
            # Cancellation is a TSUMGI-owned task event, NOT a native signal.
            raise
        except Exception:  # noqa: BLE001 - raw diagnostics must be redacted
            self._status = VoicevoxPollStatus.FAILED
            self._digest = None
        finally:
            if token == self._generation and not self._stop_requested:
                # Unexpected exit/cancellation must withdraw observed state.
                self._status = VoicevoxPollStatus.FAILED
                self._digest = None
