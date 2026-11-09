"""#247 R4-B1/B: one owned R1 poller feeding a non-authoritative R4-A plan.

No native process ownership, Worker/Control/Registry authority, serving admission,
license attestation or audio endpoints. All observations remain untrusted.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

from .voicevox_external_plan import ExternalVoicevoxPlanTransition, OperatorPolicyStatus
from .voicevox_external_polling import (
    ExternalVoicevoxPollingCoordinator,
    VoicevoxExternalPollingConfig,
)
from .voicevox_external_reconcile import (
    ExternalVoicevoxReconcileConfig,
    ExternalVoicevoxReconcileState,
    ExternalVoicevoxSampleToken,
    _checked_config,
    begin_external_voicevox_reconcile_sample,
    complete_external_voicevox_reconcile_sample,
    revoke_external_voicevox_reconcile_run,
    start_external_voicevox_reconcile_run,
)


class ExternalVoicevoxObservationError(RuntimeError):
    """Fixed error; neither external HTTP nor operator metadata is echoed."""

    def __init__(self) -> None:
        super().__init__("VOICEVOX external observation coordination is unavailable")


def _canonical_pair(
    reconcile: object, polling: object
) -> tuple[ExternalVoicevoxReconcileConfig, VoicevoxExternalPollingConfig]:
    """Defensive operator-data admission, never trust frozen object identity."""
    try:
        cfg = _checked_config(reconcile)
        if not cfg.enabled:
            # Disabled does not inspect any malformed foreign polling settings.
            return cfg, VoicevoxExternalPollingConfig(enabled=False)
        if type(polling) is not VoicevoxExternalPollingConfig or not polling.enabled:
            raise ExternalVoicevoxObservationError()
        p = VoicevoxExternalPollingConfig(
            enabled=True,
            discovery=polling.discovery,
            interval_seconds=polling.interval_seconds,
            max_backoff_seconds=polling.max_backoff_seconds,
        )
        if (
            p.discovery.endpoint != cfg.discovery.endpoint
            or p.discovery.timeout_seconds != cfg.discovery.timeout_seconds
        ):
            raise ExternalVoicevoxObservationError()
        return cfg, p
    except Exception:  # noqa: BLE001 - do not leak operator declarations
        raise ExternalVoicevoxObservationError() from None


@dataclass(frozen=True, slots=True, repr=False)
class _Pending:
    poller: _PrivateReconcilePoller
    generation: int
    sequence: int
    token: ExternalVoicevoxSampleToken


class _PrivateReconcilePoller(ExternalVoicevoxPollingCoordinator):
    """Internal subclass only: no arbitrary public callback/probe injection."""

    def __init__(
        self,
        owner: ExternalVoicevoxObservationCoordinator,
        *,
        config: VoicevoxExternalPollingConfig,
    ) -> None:
        super().__init__(config=config)
        self._owner = owner

    def _on_private_sample_start(
        self, generation: int, sequence: int, launched_at: float
    ) -> None:
        self._owner._sample_started(self, generation, sequence, launched_at)

    def _on_private_sample_complete(
        self, generation: int, sequence: int, result: object, observed_at: float
    ) -> None:
        self._owner._sample_completed(self, generation, sequence, result, observed_at)

    def _on_private_terminal(self, generation: int) -> None:
        self._owner._terminated(self, generation)


class ExternalVoicevoxObservationCoordinator:
    """Serialized R4-B0/R4-B1A composition; all public state is advisory.

    request_stop(), request_close(), request_reconfigure() are synchronous:
    former planner candidacy is withdrawn before any cancellation/join await.
    No new poller is installed until the old owned task has been joined.
    This coordinator is NOT connected to Worker or Control.
    """

    def __init__(
        self,
        *,
        config: ExternalVoicevoxReconcileConfig | None = None,
        polling: VoicevoxExternalPollingConfig | None = None,
    ) -> None:
        cfg, poll = _canonical_pair(
            ExternalVoicevoxReconcileConfig() if config is None else config,
            VoicevoxExternalPollingConfig() if polling is None else polling,
        )
        self._state = ExternalVoicevoxReconcileState(config=cfg)
        self._poller = _PrivateReconcilePoller(self, config=poll)
        self._pending: _Pending | None = None
        self._transition: ExternalVoicevoxPlanTransition | None = None
        self._update_pending: VoicevoxExternalPollingConfig | None = None
        self._lock = asyncio.Lock()
        self._intent = 0
        self._closed = False
        self._fault = False

    @property
    def ready(self) -> bool:
        return False

    @property
    def can_apply(self) -> bool:
        return False

    @property
    def worker_runtime_instance_epoch(self) -> None:
        return None

    def _revoke(
        self,
        *,
        config: ExternalVoicevoxReconcileConfig | None = None,
        close: bool = False,
    ) -> None:
        self._pending = None
        try:
            self._state, transition = revoke_external_voicevox_reconcile_run(
                self._state, config=config, close=close
            )
            if transition is not None:
                self._transition = transition
        except Exception:  # noqa: BLE001 - counter/foreign state fail closed
            self._fault = True

    def _sample_started(
        self,
        poller: _PrivateReconcilePoller,
        generation: int,
        sequence: int,
        launched_at: float,
    ) -> None:
        if (
            self._closed
            or self._fault
            or self._update_pending is not None
            or poller is not self._poller
            or self._pending is not None
        ):
            raise ExternalVoicevoxObservationError()
        try:
            state, token = begin_external_voicevox_reconcile_sample(
                self._state, launched_at_monotonic=launched_at
            )
            if token is None:
                raise ExternalVoicevoxObservationError()
            self._state = state
            self._pending = _Pending(poller, generation, sequence, token)
        except Exception:  # noqa: BLE001 - never allow a stale positive on failure
            self._revoke()
            raise ExternalVoicevoxObservationError() from None

    def _sample_completed(
        self,
        poller: _PrivateReconcilePoller,
        generation: int,
        sequence: int,
        result: object,
        observed_at: float,
    ) -> None:
        pending = self._pending
        if (
            self._closed
            or self._fault
            or self._update_pending is not None
            or poller is not self._poller
            or pending is None
            or pending.poller is not poller
            or pending.generation != generation
            or pending.sequence != sequence
        ):
            raise ExternalVoicevoxObservationError()
        try:
            state, transition = complete_external_voicevox_reconcile_sample(
                self._state,
                token=pending.token,
                observation=result,
                observed_at_monotonic=observed_at,
            )
            if transition is None:
                raise ExternalVoicevoxObservationError()
            self._state = state
            self._transition = transition
            self._pending = None
        except Exception:  # noqa: BLE001 - redacted fail-closed terminal
            self._revoke()
            raise ExternalVoicevoxObservationError() from None

    def _terminated(self, poller: _PrivateReconcilePoller, generation: int) -> None:
        if poller is self._poller and generation == poller._generation:
            self._revoke()

    def _request_stop(self) -> None:
        self._intent += 1
        self._revoke()
        self._poller.request_stop()

    def request_stop(self) -> None:
        """Immediately withdraw eligibility, but preserve uninstalled new config.

        A cancelled reconfiguration must never make old polling settings usable
        against a newer operator declaration. stop() can safely finish the
        pending installation without restarting any probe.
        """
        if not self._closed:
            self._request_stop()

    async def stop(self) -> None:
        self.request_stop()
        stop_intent = self._intent
        async with self._lock:
            await self._poller.stop()
            if (
                not self._closed
                and not self._fault
                and stop_intent == self._intent
                and self._update_pending is not None
            ):
                self._poller = _PrivateReconcilePoller(
                    self, config=self._update_pending
                )
                self._update_pending = None

    def request_close(self) -> None:
        """Terminal synchronous withdrawal before joining owned work."""
        if self._closed:
            return
        self._closed = True
        self._intent += 1
        self._update_pending = None
        self._revoke(close=True)
        self._poller.request_stop()

    async def close(self) -> None:
        self.request_close()
        async with self._lock:
            await self._poller.close()

    async def _start_under_lock(self) -> None:
        if self._closed or self._fault or self._update_pending is not None:
            raise ExternalVoicevoxObservationError()
        if not self._state.config.enabled or (
            self._state.config.policy_status is OperatorPolicyStatus.REVOKED
        ):
            return
        # Recovery after a cancelled reconfiguration must not restart an old
        # poller with an obsolete endpoint/timeout or mismatched operator pin.
        _canonical_pair(self._state.config, self._poller._config)
        old_task = self._poller._task
        if old_task is not None:
            if (
                self._state.active
                and not self._poller._stop_requested
                and not old_task.done()
            ):
                return
            raise ExternalVoicevoxObservationError()
        try:
            self._state = start_external_voicevox_reconcile_run(self._state)
            await self._poller.start()
        except Exception:  # noqa: BLE001 - no raw diagnostic leakage
            self._revoke()
            self._poller.request_stop()
            raise ExternalVoicevoxObservationError() from None

    async def start(self) -> None:
        async with self._lock:
            await self._start_under_lock()

    def request_reconfigure(
        self,
        *,
        config: ExternalVoicevoxReconcileConfig,
        polling: VoicevoxExternalPollingConfig | None = None,
    ) -> int:
        """New operator intent: revoke old candidate synchronously."""
        if self._closed or self._fault:
            raise ExternalVoicevoxObservationError()
        cfg, polled = _canonical_pair(
            config, VoicevoxExternalPollingConfig() if polling is None else polling
        )
        self._intent += 1
        self._revoke(config=cfg)
        self._update_pending = polled
        self._poller.request_stop()
        return self._intent

    async def _apply_reconfiguration(self, intent: int) -> None:
        async with self._lock:
            old = self._poller
            await old.stop()
            if self._closed or intent != self._intent or self._fault:
                return
            pending = self._update_pending
            if pending is None:
                return
            self._poller = _PrivateReconcilePoller(self, config=pending)
            self._update_pending = None
            await self._start_under_lock()

    async def reconfigure(
        self,
        *,
        config: ExternalVoicevoxReconcileConfig,
        polling: VoicevoxExternalPollingConfig | None = None,
    ) -> None:
        """Update under a new intent; never run an old and new poller together."""
        intent = self.request_reconfigure(config=config, polling=polling)
        await self._apply_reconfiguration(intent)

    def snapshot(self) -> dict[str, object]:
        """Bounded redacted view; no rights/identity/control/serving authority."""
        try:
            if (
                self._poller._task is not None
                and self._poller._task.done()
                and self._state.active
            ):
                self._revoke()
            if self._fault:
                raise ExternalVoicevoxObservationError()
            return {
                "schema_version": "voicevox-external-observation-v1",
                "polling": self._poller.snapshot().to_dict(),
                "reconciliation": self._state.to_dict(),
                "last_transition": (
                    None if self._transition is None else self._transition.to_dict()
                ),
                "ready": False,
                "can_apply": False,
                "rights_verified": False,
                "external_engine_verified": False,
                "worker_runtime_instance_epoch": None,
            }
        except Exception:  # noqa: BLE001 - do not serialize mutated private data
            self._fault = True
            return {
                "schema_version": "voicevox-external-observation-v1",
                "status": "failed",
                "ready": False,
                "can_apply": False,
                "rights_verified": False,
                "external_engine_verified": False,
                "worker_runtime_instance_epoch": None,
            }

    def to_json(self) -> str:
        return json.dumps(
            self.snapshot(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
