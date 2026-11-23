"""#247 R2: attach-only VOICEVOX ManagedRuntime shell, never engine ownership.

The user installs, starts, stops and maintains the *native* VOICEVOX process.
This runtime owns only TSUMGI's read-only, bounded discovery operations.
It is intentionally NOT imported into RuntimeProvider/Worker/Control
registries, and never exposes a JobExecutor or schedulable READY state.
Voice licensing, operator trust, immutable serving identity and automatic
Worker recovery require separate R3/R4 reviews.
"""

from __future__ import annotations

import asyncio

from ...execution import JobExecutor, ResidencyReport
from ..contracts import ManagedRuntime, RuntimeHealth, RuntimeHealthState
from .voicevox import VOICEVOX_PROVIDER_ID
from .voicevox_discovery import (
    VoicevoxDiscoveryConfig,
    VoicevoxDiscoveryProbe,
    VoicevoxDiscoveryResult,
    VoicevoxDiscoveryStatus,
    VoicevoxObservedCatalog,
)


class ExternalVoicevoxRuntimeError(RuntimeError):
    """Fixed error without raw URLs, private voice text or native diagnostics."""


class ExternalVoicevoxManagedRuntime(ManagedRuntime):
    """Source-only non-owning lifecycle; NEVER advertises runtime readiness.

    start and health may only issue R1 GET /version + GET /speakers probes.
    stop and release revoke observation and cancel TSUMGI-owned probe tasks;
    they never install, launch, signal, restart, configure, update, or stop
    a user-owned VOICEVOX engine.

    The observation is *not* identity attestation, a voice license grant,
    GPU residency proof, or an authority to execute synthesis.
    """

    provider_id = VOICEVOX_PROVIDER_ID

    def __init__(self, *, config: VoicevoxDiscoveryConfig) -> None:
        if type(config) is not VoicevoxDiscoveryConfig:
            raise ValueError("voicevox_external_config_invalid")
        # No public arbitrary probe injection: the reviewed R1 probe
        # propagates cancellation and closes its own HTTP writers.
        self._config = config
        self._probe = VoicevoxDiscoveryProbe(config)
        self._lock = asyncio.Lock()
        self._state = RuntimeHealthState.STOPPED
        self._observation: VoicevoxDiscoveryResult | None = None
        self._detail: str | None = None
        self._generation = 0
        self._pending: asyncio.Task[VoicevoxDiscoveryResult] | None = None
        self._released = False

    @property
    def observation(self) -> VoicevoxDiscoveryResult | None:
        """Observed status only, never a serving-authority or artifact hash."""

        if self._state is RuntimeHealthState.STOPPED or self._released:
            return None
        return self._observation

    def _health(self) -> RuntimeHealth:
        return RuntimeHealth(
            state=self._state,
            ready=False,
            detail=self._detail,
            metadata={
                "runtime_provider": self.provider_id,
                "external_engine_verified": False,
                "admitted": False,
            },
        )

    async def _observe_locked(self, generation: int, *, starting: bool) -> None:
        """One bounded R1 observation, fenced against stop/release races.

        Caller holds _lock, whereas stop/release revoke synchronously without
        waiting for _lock. The canonical R1 probe propagates cancellation.
        """

        result: VoicevoxDiscoveryResult | None = None
        failed = False
        task = asyncio.create_task(self._probe.probe_once())
        self._pending = task
        try:
            result = await task
        except asyncio.CancelledError:
            if self._generation != generation or self._released:
                raise ExternalVoicevoxRuntimeError(
                    "voicevox_external_revoked"
                ) from None
            # Caller-initiated cancellation cannot leave a stale observation.
            self._observation = None
            self._state = (
                RuntimeHealthState.STOPPED if starting else RuntimeHealthState.DEGRADED
            )
            self._detail = None if starting else "voicevox_external_probe_cancelled"
            raise
        except Exception:  # noqa: BLE001 - transport errors are private
            failed = True
        finally:
            if self._pending is task:
                self._pending = None
            # Cancel any unfinished TSUMGI-owned probe task. The canonical
            # R1 implementation propagates cancellation and closes its writer.
            if not task.done():
                task.cancel()

        if self._generation != generation or self._released:
            raise ExternalVoicevoxRuntimeError("voicevox_external_revoked")
        if failed or type(result) is not VoicevoxDiscoveryResult:
            self._observation = None
            self._state = RuntimeHealthState.DEGRADED
            self._detail = "voicevox_external_probe_failed"
            return
        if (
            type(result.status) is not VoicevoxDiscoveryStatus
            or (
                result.status is VoicevoxDiscoveryStatus.COMPATIBLE
                and type(result.snapshot) is not VoicevoxObservedCatalog
            )
            or (
                result.status is not VoicevoxDiscoveryStatus.COMPATIBLE
                and result.snapshot is not None
            )
        ):
            self._observation = None
            self._state = RuntimeHealthState.DEGRADED
            self._detail = "voicevox_external_probe_failed"
            return

        if result.status is VoicevoxDiscoveryStatus.DISABLED:
            # Disabled is a non-operational mode, never a voice capability.
            self._observation = None
            self._state = RuntimeHealthState.STOPPED
            self._detail = None
            return

        # Store only allowlisted fixed reason codes, never a source-supplied
        # diagnostic that could carry an external URL or private text.
        safe_reason = {
            VoicevoxDiscoveryStatus.COMPATIBLE: "observed_compatible",
            VoicevoxDiscoveryStatus.ABSENT: "endpoint_unreachable",
            VoicevoxDiscoveryStatus.INCOMPATIBLE: "incompatible_response",
        }[result.status]
        self._observation = VoicevoxDiscoveryResult(
            status=result.status,
            reason=safe_reason,
            snapshot=result.snapshot,
        )
        self._state = RuntimeHealthState.DEGRADED
        self._detail = {
            VoicevoxDiscoveryStatus.COMPATIBLE: (
                "voicevox_external_observed_unqualified"
            ),
            VoicevoxDiscoveryStatus.ABSENT: "voicevox_external_absent",
            VoicevoxDiscoveryStatus.INCOMPATIBLE: ("voicevox_external_incompatible"),
        }[result.status]

    async def start(self) -> None:
        if self._released:
            raise ExternalVoicevoxRuntimeError("voicevox_external_released")
        if not self._config.enabled:
            return

        generation = self._generation
        async with self._lock:
            if self._released or self._generation != generation:
                raise ExternalVoicevoxRuntimeError("voicevox_external_revoked")
            if self._state is not RuntimeHealthState.STOPPED:
                return
            self._state = RuntimeHealthState.STARTING
            self._detail = None
            await self._observe_locked(generation, starting=True)

    async def health(self) -> RuntimeHealth:
        if (
            self._released
            or not self._config.enabled
            or self._state is RuntimeHealthState.STOPPED
        ):
            return self._health()

        generation = self._generation
        async with self._lock:
            if (
                self._released
                or self._generation != generation
                or self._state is RuntimeHealthState.STOPPED
            ):
                return self._health()
            try:
                await self._observe_locked(generation, starting=False)
            except ExternalVoicevoxRuntimeError:
                # An in-flight observation revoked by stop or release must
                # never publish a late stale result or recover the runtime.
                if self._generation != generation or self._released:
                    return self._health()
                raise
            return self._health()

    def _revoke(self) -> None:
        # Revocation intentionally happens before *any* await in stop/release.
        self._generation += 1
        self._state = RuntimeHealthState.STOPPED
        self._detail = None
        self._observation = None
        if self._pending is not None and not self._pending.done():
            self._pending.cancel()

    async def stop(self) -> None:
        self._revoke()

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._revoke()

    async def residency(self) -> ResidencyReport:
        # The TSUMGI runtime owns NO model or external engine resources.
        # Empty items are not evidence the user-managed engine uses no VRAM.
        return ResidencyReport(
            items=(),
            metadata={"external_engine_residency": "unverified"},
        )

    def executor(self) -> JobExecutor:
        # R3 voice provenance/rights and R4 Worker admission are not built.
        raise ExternalVoicevoxRuntimeError("voicevox_external_not_admitted")
