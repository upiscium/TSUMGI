"""#247 R4-A: pure, non-authoritative external VOICEVOX generation planner.

The planner emits advisory qualification/withdrawal requests, never Control
fencing acknowledgements, RuntimeInstance epochs, Worker specifications,
serving readiness or a native engine lifecycle operation. An observed voice
and an operator declaration do NOT constitute verified rights or identity.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum

from .voicevox_external_catalog import review_external_voicevox_catalog

_SCHEMA = "voicevox-external-planner-v1"
_MAX_COUNTER = 2**63 - 1
_SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")


class ExternalVoicevoxPlanningError(ValueError):
    """Fixed error, never include user voice text, endpoints or native details."""

    def __init__(self) -> None:
        super().__init__("VOICEVOX external generation plan is invalid")


class ObservationPhase(str, Enum):
    """Planner-only observation states; NONE authorizes runtime admission."""

    DISABLED = "disabled"
    UNAVAILABLE = "unavailable"
    INCOMPATIBLE = "incompatible"
    OBSERVATION_CHANGED = "observation_changed"
    REVIEW_BLOCKED = "review_blocked"
    POLICY_REVOKED = "policy_revoked"
    CANDIDATE_UNQUALIFIED = "candidate_unqualified"


class OperatorPolicyStatus(str, Enum):
    """Never accepts a positive grant; R4-C must verify separate authority."""

    UNVERIFIED = "unverified"
    REVOKED = "revoked"


class PlanRequest(str, Enum):
    """Advisory output only. No side effects are performed by the planner."""

    WITHDRAW_PRIOR = "withdraw_prior_generation_requested"
    QUALIFY_CANDIDATE = "qualification_requested"


_ALLOWED_REASONS = frozenset(
    {"unchanged", "stale_ignored", "candidate_unchanged", "qualification_requested"}
    | {phase.value for phase in ObservationPhase}
)


@dataclass(frozen=True, slots=True, repr=False)
class ExternalVoicevoxPlanState:
    """Safe redacted, immutable snapshot for a *planning*, not Worker, epoch.

    Generation counters are local planner identifiers, not Serving RuntimeInstance
    epochs and not claims of Control cleanup. Retired generations remain marked
    pending until a FUTURE separately verified Control acknowledgement boundary.
    This source slice has NO method to clear that flag.
    """

    last_sequence: int = 0
    generation_counter: int = 0
    phase: ObservationPhase = ObservationPhase.DISABLED
    candidate_signature: str | None = None
    last_retired_generation: int = 0
    retirement_pending: bool = False

    def __post_init__(self) -> None:
        if (
            type(self.last_sequence) is not int
            or not 0 <= self.last_sequence <= _MAX_COUNTER
            or type(self.generation_counter) is not int
            or not 0 <= self.generation_counter <= _MAX_COUNTER
            or type(self.phase) is not ObservationPhase
            or type(self.last_retired_generation) is not int
            or not 0 <= self.last_retired_generation <= self.generation_counter
            or type(self.retirement_pending) is not bool
            or self.retirement_pending != (self.last_retired_generation > 0)
        ):
            raise ExternalVoicevoxPlanningError()
        candidate = self.candidate_signature
        if self.phase is ObservationPhase.CANDIDATE_UNQUALIFIED:
            if (
                self.generation_counter == 0
                or type(candidate) is not str
                or _SHA.fullmatch(candidate) is None
            ):
                raise ExternalVoicevoxPlanningError()
        elif candidate is not None:
            raise ExternalVoicevoxPlanningError()

    @property
    def ready(self) -> bool:
        return False

    @property
    def can_apply(self) -> bool:
        return False

    @property
    def worker_runtime_instance_epoch(self) -> None:
        """No Worker epoch is ever minted by R4-A."""
        return None

    def to_dict(self) -> dict[str, object]:
        # Serialization is a separate public boundary. Frozen dataclasses
        # can be reflectively mutated; never echo unvalidated raw values.
        checked = _validate_state(self)
        return {
            "schema_version": _SCHEMA,
            "last_sequence": checked.last_sequence,
            "generation_counter": checked.generation_counter,
            "phase": checked.phase.value,
            "candidate_signature": checked.candidate_signature,
            "last_retired_generation": checked.last_retired_generation,
            "retirement_pending": checked.retirement_pending,
            "worker_runtime_instance_epoch": None,
            "ready": False,
            "can_apply": False,
        }


@dataclass(frozen=True, slots=True, repr=False)
class ExternalVoicevoxPlanEvent:
    """One coordinator sample. Inputs are evaluated only if opted in and fresh.

    config, observation and voices are intentionally NOT retained in PlanState.
    policy_status denotes unverified vs explicit withdrawal, never 'approved'.
    policy_revision is a *declaration fingerprint*, NOT operator authorization.
    """

    sequence: int
    enabled: bool
    config: object = None
    observation: object = None
    expected_observed_digest: object = None
    voices: object = ()
    policy_status: OperatorPolicyStatus = OperatorPolicyStatus.UNVERIFIED
    policy_revision: object = None

    def __post_init__(self) -> None:
        if (
            type(self.sequence) is not int
            or not 1 <= self.sequence <= _MAX_COUNTER
            or type(self.enabled) is not bool
        ):
            raise ExternalVoicevoxPlanningError()
        # Disabled deliberately does not inspect ANY of the foreign values.
        if not self.enabled:
            return
        if type(self.policy_status) is not OperatorPolicyStatus:
            raise ExternalVoicevoxPlanningError()
        if self.policy_revision is not None and (
            type(self.policy_revision) is not str
            or _SHA.fullmatch(self.policy_revision) is None
        ):
            raise ExternalVoicevoxPlanningError()


@dataclass(frozen=True, slots=True, repr=False)
class ExternalVoicevoxPlanTransition:
    """Redacted action list is a plan, not a real Worker/Control mutation."""

    state: ExternalVoicevoxPlanState
    requests: tuple[PlanRequest, ...] = ()
    reason: str = "unchanged"

    def __post_init__(self) -> None:
        _validate_transition(self)

    @property
    def ready(self) -> bool:
        return False

    @property
    def can_apply(self) -> bool:
        return False

    @property
    def control_reconciliation_required(self) -> bool:
        return _validate_state(self.state).retirement_pending

    def to_dict(self) -> dict[str, object]:
        state, requests, reason = _validate_transition(self)
        return {
            "schema_version": _SCHEMA,
            "state": state.to_dict(),
            "requests": [request.value for request in requests],
            "reason": reason,
            "control_reconciliation_required": state.retirement_pending,
            "ready": False,
            "can_apply": False,
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )


def _validate_state(value: object) -> ExternalVoicevoxPlanState:
    """Revalidate even reflectively mutated frozen dataclass state."""
    if type(value) is not ExternalVoicevoxPlanState:
        raise ExternalVoicevoxPlanningError()
    try:
        return ExternalVoicevoxPlanState(
            last_sequence=value.last_sequence,
            generation_counter=value.generation_counter,
            phase=value.phase,
            candidate_signature=value.candidate_signature,
            last_retired_generation=value.last_retired_generation,
            retirement_pending=value.retirement_pending,
        )
    except Exception:  # noqa: BLE001 - never echo unexpected input
        raise ExternalVoicevoxPlanningError() from None


def _validate_transition(
    value: object,
) -> tuple[ExternalVoicevoxPlanState, tuple[PlanRequest, ...], str]:
    """Validate public construction AND serialization; never echo diagnostics."""
    if type(value) is not ExternalVoicevoxPlanTransition:
        raise ExternalVoicevoxPlanningError()
    try:
        state = _validate_state(value.state)
        requests = value.requests
        reason = value.reason
        if (
            type(requests) is not tuple
            or len(requests) > 2
            or any(type(req) is not PlanRequest for req in requests)
            or requests
            not in (
                (),
                (PlanRequest.WITHDRAW_PRIOR,),
                (PlanRequest.QUALIFY_CANDIDATE,),
                (PlanRequest.WITHDRAW_PRIOR, PlanRequest.QUALIFY_CANDIDATE),
            )
            or type(reason) is not str
            or reason not in _ALLOWED_REASONS
            or (PlanRequest.WITHDRAW_PRIOR in requests and not state.retirement_pending)
            or (
                PlanRequest.QUALIFY_CANDIDATE in requests
                and state.phase is not ObservationPhase.CANDIDATE_UNQUALIFIED
            )
        ):
            raise ExternalVoicevoxPlanningError()
        return state, requests, reason
    except Exception:  # noqa: BLE001 - fixed redacted error only
        raise ExternalVoicevoxPlanningError() from None


def _validated_event_header(
    value: object,
) -> tuple[int, bool]:
    if type(value) is not ExternalVoicevoxPlanEvent:
        raise ExternalVoicevoxPlanningError()
    try:
        sequence, enabled = value.sequence, value.enabled
        if (
            type(sequence) is not int
            or not 1 <= sequence <= _MAX_COUNTER
            or type(enabled) is not bool
        ):
            raise ExternalVoicevoxPlanningError()
    except Exception:  # noqa: BLE001 - no untrusted diagnostics
        raise ExternalVoicevoxPlanningError() from None
    return sequence, enabled


def _blocked_phase(
    event: ExternalVoicevoxPlanEvent,
) -> tuple[ObservationPhase, str | None]:
    """Call the pure, fail-closed R3 codec. Never treat it as admission."""
    if type(event.policy_status) is not OperatorPolicyStatus or (
        event.policy_revision is not None
        and (
            type(event.policy_revision) is not str
            or _SHA.fullmatch(event.policy_revision) is None
        )
    ):
        return ObservationPhase.REVIEW_BLOCKED, None
    if event.policy_status is OperatorPolicyStatus.REVOKED:
        return ObservationPhase.POLICY_REVOKED, None

    try:
        review = review_external_voicevox_catalog(
            enabled=True,
            config=event.config,
            observation=event.observation,
            expected_observed_digest=event.expected_observed_digest,
            voices=event.voices,
        )
    except Exception:  # noqa: BLE001 - malformed operator input, fail closed
        return ObservationPhase.REVIEW_BLOCKED, None

    if review.status == "review_only_blocked":
        # R3 always marks rights/identity unverified and admission unavailable.
        if (
            review.ready is not False
            or review.can_apply is not False
            or review.rights_verified is not False
            or review.external_engine_verified is not False
            or type(review.review_digest) is not str
            or _SHA.fullmatch(review.review_digest) is None
        ):
            return ObservationPhase.REVIEW_BLOCKED, None
        raw = json.dumps(
            {
                "schema": _SCHEMA,
                "review_digest": review.review_digest,
                "operator_policy_revision": event.policy_revision,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
        signature = "sha256:" + hashlib.sha256(raw).hexdigest()
        return ObservationPhase.CANDIDATE_UNQUALIFIED, signature

    if review.status == "observation_unavailable":
        return ObservationPhase.UNAVAILABLE, None
    if review.status in ("observation_changed", "voice_selection_changed"):
        return ObservationPhase.OBSERVATION_CHANGED, None
    if review.status == "invalid_observation":
        return ObservationPhase.INCOMPATIBLE, None
    return ObservationPhase.REVIEW_BLOCKED, None


def plan_external_voicevox_generation(
    previous: ExternalVoicevoxPlanState,
    event: ExternalVoicevoxPlanEvent,
) -> ExternalVoicevoxPlanTransition:
    """Pure planner: stale events ignored, never emit serving authority.

    Every replaced/withdrawn candidate is recorded as pending retirement,
    which cannot be acknowledged/cleared here. A subsequent compatible
    observation creates a DIFFERENT *planner* generation index, never reuses
    or mints a Worker Serving epoch. An actual Control fence, identity/rights
    and logging gate must be satisfied outside this slice before use.
    """
    state = _validate_state(previous)
    sequence, enabled = _validated_event_header(event)
    if sequence <= state.last_sequence:
        # Deliberately do not inspect observation or policy data of stale events.
        return ExternalVoicevoxPlanTransition(state=state, reason="stale_ignored")

    if not enabled:
        phase, signature = ObservationPhase.DISABLED, None
    else:
        phase, signature = _blocked_phase(event)

    old_candidate = state.candidate_signature
    withdraw = old_candidate is not None and old_candidate != signature
    if phase is ObservationPhase.CANDIDATE_UNQUALIFIED and signature == old_candidate:
        next_index = state.generation_counter
        request_qualification = False
        reason = "candidate_unchanged"
    elif phase is ObservationPhase.CANDIDATE_UNQUALIFIED:
        if state.generation_counter >= _MAX_COUNTER:
            raise ExternalVoicevoxPlanningError()
        next_index = state.generation_counter + 1
        request_qualification = True
        reason = "qualification_requested"
    else:
        next_index = state.generation_counter
        request_qualification = False
        reason = phase.value

    retired = state.generation_counter if withdraw else state.last_retired_generation
    if withdraw and state.last_retired_generation > retired:
        retired = state.last_retired_generation
    next_state = ExternalVoicevoxPlanState(
        last_sequence=sequence,
        generation_counter=next_index,
        phase=phase,
        candidate_signature=signature,
        last_retired_generation=retired,
        retirement_pending=retired > 0,
    )
    requests: list[PlanRequest] = []
    if withdraw:
        requests.append(PlanRequest.WITHDRAW_PRIOR)
    if request_qualification:
        requests.append(PlanRequest.QUALIFY_CANDIDATE)

    return ExternalVoicevoxPlanTransition(
        state=next_state,
        requests=tuple(requests),
        reason=reason,
    )
