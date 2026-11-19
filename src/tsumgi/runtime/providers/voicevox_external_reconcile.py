"""#247 R4-B1/A: pure R1 result → R4-A advisory plan reconciliation.

No network, scheduler, Worker/Control, native engine, admission or audio I/O.
Tokens are local sequencing evidence, not credentials. A digest is an unkeyed
observation fingerprint, not native process, voice-rights or log-policy proof.
R4-B1/B will separately own the bounded polling task lifecycle and call these
pure functions at launch, completion, reconfiguration and stop boundaries.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field

from .voicevox_discovery import VoicevoxDiscoveryConfig, VoicevoxDiscoveryResult
from .voicevox_external_catalog import _operator_voices
from .voicevox_external_plan import (
    ExternalVoicevoxPlanEvent,
    ExternalVoicevoxPlanState,
    ExternalVoicevoxPlanTransition,
    OperatorPolicyStatus,
    plan_external_voicevox_generation,
)
from .voicevox_external_plan import (
    _validate_state as _validate_plan_state,
)

_SCHEMA = "voicevox-external-reconcile-v1"
_MAX_COUNTER = 2**63 - 1
_MAX_CLOCK = 1e15
_SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")


class ExternalVoicevoxReconcileError(ValueError):
    """Fixed redacted error. No URLs, voices or private native diagnostics."""

    def __init__(self) -> None:
        super().__init__("VOICEVOX external observation reconciliation is invalid")


def _digest(value: object) -> bool:
    return type(value) is str and _SHA.fullmatch(value) is not None


def _clock(value: object) -> bool:
    return (
        type(value) in (int, float)
        and 0 <= value <= _MAX_CLOCK
        and math.isfinite(value)
    )


def _revision(payload: dict[str, object]) -> str:
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
    )


_DISABLED_REVISION = _revision({"schema": _SCHEMA, "enabled": False})


@dataclass(frozen=True, slots=True, repr=False)
class ExternalVoicevoxReconcileConfig:
    """Canonicalized operator declarations, never rights/identity approval.

    Enabled requires an independently selected R1 digest pin, literal local
    endpoint, selected VoiceAlias declarations and unverified policy revision.
    A new observation NEVER automatically updates the expected digest pin.
    Disabled never reads untrusted foreign fields and holds no declarations.
    """

    enabled: bool = False
    discovery: object = field(default_factory=VoicevoxDiscoveryConfig, repr=False)
    expected_observed_digest: object = field(default=None, repr=False)
    voices: object = field(default=(), repr=False)
    policy_status: object = OperatorPolicyStatus.UNVERIFIED
    policy_revision: object = field(default=None, repr=False)
    revision: str = field(default="", init=False)

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ExternalVoicevoxReconcileError()
        if not self.enabled:
            object.__setattr__(self, "discovery", VoicevoxDiscoveryConfig())
            object.__setattr__(self, "expected_observed_digest", None)
            object.__setattr__(self, "voices", ())
            object.__setattr__(self, "policy_status", OperatorPolicyStatus.UNVERIFIED)
            object.__setattr__(self, "policy_revision", None)
            object.__setattr__(self, "revision", _DISABLED_REVISION)
            return

        try:
            raw = self.discovery
            if type(raw) is not VoicevoxDiscoveryConfig or not raw.enabled:
                raise ExternalVoicevoxReconcileError()
            discovery = VoicevoxDiscoveryConfig(
                enabled=True,
                endpoint=raw.endpoint,
                timeout_seconds=raw.timeout_seconds,
            )
            if (
                not _digest(self.expected_observed_digest)
                or not _digest(self.policy_revision)
                or type(self.policy_status) is not OperatorPolicyStatus
            ):
                raise ExternalVoicevoxReconcileError()
            voices = _operator_voices(self.voices)
            payload: dict[str, object] = {
                "schema": _SCHEMA,
                "enabled": True,
                "endpoint": discovery.endpoint,
                "timeout_seconds": discovery.timeout_seconds,
                "expected_observed_digest": self.expected_observed_digest,
                "operator_policy_status": self.policy_status.value,
                "operator_policy_revision": self.policy_revision,
                "operator_declared_voices": [v.to_dict() for v in voices],
            }
            revision = _revision(payload)
            object.__setattr__(self, "discovery", discovery)
            object.__setattr__(self, "voices", voices)
            object.__setattr__(self, "revision", revision)
        except Exception:  # noqa: BLE001 - no raw private config leaks
            raise ExternalVoicevoxReconcileError() from None


def _checked_config(
    config: object,
) -> ExternalVoicevoxReconcileConfig:
    if type(config) is not ExternalVoicevoxReconcileConfig:
        raise ExternalVoicevoxReconcileError()
    try:
        verified = ExternalVoicevoxReconcileConfig(
            enabled=config.enabled,
            discovery=config.discovery,
            expected_observed_digest=config.expected_observed_digest,
            voices=config.voices,
            policy_status=config.policy_status,
            policy_revision=config.policy_revision,
        )
        if type(config.revision) is not str or config.revision != verified.revision:
            raise ExternalVoicevoxReconcileError()
        return verified
    except Exception:  # noqa: BLE001 - protect reflective mutation
        raise ExternalVoicevoxReconcileError() from None


@dataclass(frozen=True, slots=True, repr=False)
class ExternalVoicevoxSampleToken:
    """Private one-flight handoff: launch epoch/sequence/revision/clock.

    Not a signature, lease, Worker epoch or proof of external-engine freshness.
    """

    run_generation: int
    sequence: int
    config_revision: str
    launched_at_monotonic: float

    def __post_init__(self) -> None:
        if (
            type(self.run_generation) is not int
            or not 1 <= self.run_generation <= _MAX_COUNTER
            or type(self.sequence) is not int
            or not 1 <= self.sequence <= _MAX_COUNTER
            or not _digest(self.config_revision)
            or not _clock(self.launched_at_monotonic)
        ):
            raise ExternalVoicevoxReconcileError()


def _checked_token(value: object) -> ExternalVoicevoxSampleToken:
    if type(value) is not ExternalVoicevoxSampleToken:
        raise ExternalVoicevoxReconcileError()
    try:
        return ExternalVoicevoxSampleToken(
            run_generation=value.run_generation,
            sequence=value.sequence,
            config_revision=value.config_revision,
            launched_at_monotonic=value.launched_at_monotonic,
        )
    except Exception:  # noqa: BLE001 - fixed error only
        raise ExternalVoicevoxReconcileError() from None


@dataclass(frozen=True, slots=True, repr=False)
class _VoicevoxR1ObservationEnvelope:
    """Private complete R1 handoff, not the public redacted polling snapshot.

    Never serialize or log this record: the untrusted observation contains
    speaker UUID/style data. Completion is accepted only after token fencing.
    """

    token: ExternalVoicevoxSampleToken = field(repr=False)
    observed_at_monotonic: object = field(repr=False)
    observation: object = field(repr=False)

    def __post_init__(self) -> None:
        _checked_token(self.token)
        # Do not inspect foreign response/clock here: stale samples are
        # discarded before this private envelope is ever constructed.


@dataclass(frozen=True, slots=True, repr=False)
class ExternalVoicevoxReconcileState:
    """Only local advisory planner state; no durable Control cleanup proof."""

    config: ExternalVoicevoxReconcileConfig = field(
        default_factory=ExternalVoicevoxReconcileConfig, repr=False
    )
    plan: ExternalVoicevoxPlanState = field(
        default_factory=ExternalVoicevoxPlanState, repr=False
    )
    run_generation: int = 0
    last_sequence: int = 0
    last_launched_at_monotonic: float | None = None
    active: bool = False
    closed: bool = False
    pending: ExternalVoicevoxSampleToken | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        try:
            config = _checked_config(self.config)
            plan = _validate_plan_state(self.plan)
            if (
                type(self.run_generation) is not int
                or not 0 <= self.run_generation <= _MAX_COUNTER
                or type(self.last_sequence) is not int
                or not plan.last_sequence <= self.last_sequence <= _MAX_COUNTER
                or type(self.active) is not bool
                or type(self.closed) is not bool
                or (self.active and (self.closed or not config.enabled))
                or (self.active and self.run_generation == 0)
                or (self.closed and self.pending is not None)
                or (
                    self.last_launched_at_monotonic is not None
                    and not _clock(self.last_launched_at_monotonic)
                )
                or (
                    self.last_sequence == 0
                    and self.last_launched_at_monotonic is not None
                )
            ):
                raise ExternalVoicevoxReconcileError()
            token = self.pending
            if token is not None:
                token = _checked_token(token)
                if (
                    not self.active
                    or token.run_generation != self.run_generation
                    or token.sequence != self.last_sequence
                    or token.config_revision != config.revision
                    or token.launched_at_monotonic != self.last_launched_at_monotonic
                    or token.sequence <= plan.last_sequence
                ):
                    raise ExternalVoicevoxReconcileError()
        except Exception:  # noqa: BLE001 - fixed error on foreign mutation
            raise ExternalVoicevoxReconcileError() from None

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
        state = _checked_state(self)
        return {
            "schema_version": _SCHEMA,
            "config_revision": state.config.revision,
            "run_generation": state.run_generation,
            "last_sequence": state.last_sequence,
            "active": state.active,
            "closed": state.closed,
            "probe_pending": state.pending is not None,
            "plan": state.plan.to_dict(),
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


def _checked_state(value: object) -> ExternalVoicevoxReconcileState:
    if type(value) is not ExternalVoicevoxReconcileState:
        raise ExternalVoicevoxReconcileError()
    try:
        return ExternalVoicevoxReconcileState(
            config=_checked_config(value.config),
            plan=_validate_plan_state(value.plan),
            run_generation=value.run_generation,
            last_sequence=value.last_sequence,
            last_launched_at_monotonic=value.last_launched_at_monotonic,
            active=value.active,
            closed=value.closed,
            pending=(
                _checked_token(value.pending) if value.pending is not None else None
            ),
        )
    except Exception:  # noqa: BLE001 - never echo private data
        raise ExternalVoicevoxReconcileError() from None


def start_external_voicevox_reconcile_run(
    previous: ExternalVoicevoxReconcileState,
) -> ExternalVoicevoxReconcileState:
    """Pure explicit start of a *planning run*. Never starts native VOICEVOX."""
    state = _checked_state(previous)
    if state.closed:
        raise ExternalVoicevoxReconcileError()
    if (
        not state.config.enabled
        or state.config.policy_status is OperatorPolicyStatus.REVOKED
    ):
        return state
    if state.active:
        return state
    if state.pending is not None or state.run_generation >= _MAX_COUNTER:
        raise ExternalVoicevoxReconcileError()
    return ExternalVoicevoxReconcileState(
        config=state.config,
        plan=state.plan,
        run_generation=state.run_generation + 1,
        last_sequence=state.last_sequence,
        last_launched_at_monotonic=state.last_launched_at_monotonic,
        active=True,
    )


def begin_external_voicevox_reconcile_sample(
    previous: ExternalVoicevoxReconcileState,
    *,
    launched_at_monotonic: float,
) -> tuple[ExternalVoicevoxReconcileState, ExternalVoicevoxSampleToken | None]:
    """Allocate strictly monotonic sample sequence at launch, not completion."""
    state = _checked_state(previous)
    if not state.active:
        return state, None
    if state.pending is not None or state.last_sequence >= _MAX_COUNTER:
        raise ExternalVoicevoxReconcileError()
    if not _clock(launched_at_monotonic) or (
        state.last_launched_at_monotonic is not None
        and launched_at_monotonic < state.last_launched_at_monotonic
    ):
        raise ExternalVoicevoxReconcileError()
    token = ExternalVoicevoxSampleToken(
        run_generation=state.run_generation,
        sequence=state.last_sequence + 1,
        config_revision=state.config.revision,
        launched_at_monotonic=float(launched_at_monotonic),
    )
    return (
        ExternalVoicevoxReconcileState(
            config=state.config,
            plan=state.plan,
            run_generation=state.run_generation,
            last_sequence=token.sequence,
            last_launched_at_monotonic=token.launched_at_monotonic,
            active=True,
            pending=token,
        ),
        token,
    )


def complete_external_voicevox_reconcile_sample(
    previous: ExternalVoicevoxReconcileState,
    *,
    token: ExternalVoicevoxSampleToken,
    observation: object,
    observed_at_monotonic: float,
) -> tuple[ExternalVoicevoxReconcileState, ExternalVoicevoxPlanTransition | None]:
    """Ignore stale tokens before reading observation; recheck R3 at use point.

    The completion clock is a bounded monotonic metadata field only. It does
    not prove that a native process/rights/Control lease remains trustworthy.
    """
    state = _checked_state(previous)
    valid_token = _checked_token(token)
    if (
        not state.active
        or state.pending is None
        or valid_token != state.pending
        or valid_token.config_revision != state.config.revision
        or valid_token.run_generation != state.run_generation
        or valid_token.sequence <= state.plan.last_sequence
    ):
        # An old compatible result cannot revive a retired planner generation.
        # Do not inspect observation or even the clock on a stale completion.
        return state, None

    # A private complete R1 handoff is constructed ONLY after all old-token
    # guards have passed; this never travels through the public poll snapshot.
    envelope = _VoicevoxR1ObservationEnvelope(
        token=valid_token,
        observed_at_monotonic=observed_at_monotonic,
        observation=observation,
    )
    # A malformed/inconsistent completion clock is treated as a negative
    # observation, not a positive sample or an uncaught stale READY state.
    compatible_time = (
        _clock(envelope.observed_at_monotonic)
        and envelope.observed_at_monotonic >= envelope.token.launched_at_monotonic
    )
    effective_observation = (
        envelope.observation
        if compatible_time and type(envelope.observation) is VoicevoxDiscoveryResult
        else None
    )
    config = state.config
    event = ExternalVoicevoxPlanEvent(
        sequence=valid_token.sequence,
        enabled=True,
        config=config.discovery,
        observation=effective_observation,
        expected_observed_digest=config.expected_observed_digest,
        voices=config.voices,
        policy_status=config.policy_status,
        policy_revision=config.policy_revision,
    )
    try:
        transition = plan_external_voicevox_generation(state.plan, event)
        next_state = ExternalVoicevoxReconcileState(
            config=config,
            plan=transition.state,
            run_generation=state.run_generation,
            last_sequence=state.last_sequence,
            last_launched_at_monotonic=state.last_launched_at_monotonic,
            active=True,
        )
    except Exception:  # noqa: BLE001 - fail closed and sanitize malformed result
        return _revoke_checked_state(state, config, terminal=False)
    return next_state, transition


def _revoke_checked_state(
    state: ExternalVoicevoxReconcileState,
    config: ExternalVoicevoxReconcileConfig,
    *,
    terminal: bool,
) -> tuple[ExternalVoicevoxReconcileState, ExternalVoicevoxPlanTransition]:
    if state.last_sequence >= _MAX_COUNTER or state.run_generation >= _MAX_COUNTER:
        raise ExternalVoicevoxReconcileError()
    next_sequence = state.last_sequence + 1
    negative = ExternalVoicevoxPlanEvent(
        sequence=next_sequence,
        enabled=(
            config.enabled and config.policy_status is OperatorPolicyStatus.REVOKED
        ),
        policy_status=config.policy_status,
    )
    try:
        transition = plan_external_voicevox_generation(state.plan, negative)
        next_state = ExternalVoicevoxReconcileState(
            config=config,
            plan=transition.state,
            run_generation=state.run_generation + 1,
            last_sequence=next_sequence,
            last_launched_at_monotonic=state.last_launched_at_monotonic,
            active=False,
            closed=terminal,
        )
        return next_state, transition
    except Exception:  # noqa: BLE001 - no native/private diagnostics
        raise ExternalVoicevoxReconcileError() from None


def revoke_external_voicevox_reconcile_run(
    previous: ExternalVoicevoxReconcileState,
    *,
    config: ExternalVoicevoxReconcileConfig | None = None,
    close: bool = False,
) -> tuple[ExternalVoicevoxReconcileState, ExternalVoicevoxPlanTransition | None]:
    """Synchronous source-only withdrawal BEFORE any real cancel/join await.

    A later R4-B1/B caller must perform the actual owned-poller cancellation;
    this function touches neither HTTP nor Worker/Control, ever.
    """
    state = _checked_state(previous)
    if type(close) is not bool:
        raise ExternalVoicevoxReconcileError()
    desired = state.config if config is None else _checked_config(config)
    if state.closed:
        if desired.revision != state.config.revision:
            raise ExternalVoicevoxReconcileError()
        return state, None

    change = desired.revision != state.config.revision
    if (
        not change
        and not close
        and not state.active
        and state.pending is None
        and state.plan.candidate_signature is None
    ):
        return state, None
    return _revoke_checked_state(state, desired, terminal=close)
