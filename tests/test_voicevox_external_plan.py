"""#247 R4-A: source-only pure generation-planning regression tests."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace

import pytest

from astrumweaver.gateway.audio import VoiceAlias
from astrumweaver.runtime.providers.voicevox_discovery import (
    VoicevoxDiscoveryConfig,
    VoicevoxDiscoveryResult,
    VoicevoxDiscoveryStatus,
    _catalog,
)
from astrumweaver.runtime.providers.voicevox_external_plan import (
    ExternalVoicevoxPlanEvent,
    ExternalVoicevoxPlanningError,
    ExternalVoicevoxPlanState,
    ObservationPhase,
    OperatorPolicyStatus,
    PlanRequest,
    plan_external_voicevox_generation,
)

_UUID_A = "7ffcb7ce-00ec-4bdc-82cd-45a8889e43ff"
_UUID_B = "388f246b-8c41-4ac1-8e2d-5d79f3ff56d9"
_POLICY_V1 = "sha256:" + "a" * 64
_POLICY_V2 = "sha256:" + "b" * 64
_ABSENT = VoicevoxDiscoveryResult(
    VoicevoxDiscoveryStatus.ABSENT, "endpoint_unreachable"
)
_INCOMPATIBLE = VoicevoxDiscoveryResult(
    VoicevoxDiscoveryStatus.INCOMPATIBLE, "incompatible_response"
)
_UNSET = object()


def _config(port: int = 50021):
    return VoicevoxDiscoveryConfig(enabled=True, endpoint=f"http://127.0.0.1:{port}")


def _observation(*, version="0.15.0", port=50021, second=False, style=8):
    speakers = [
        {"speaker_uuid": _UUID_A, "styles": [{"id": style}]},
    ]
    if second:
        speakers.append({"speaker_uuid": _UUID_B, "styles": [{"id": 17}]})
    return VoicevoxDiscoveryResult(
        status=VoicevoxDiscoveryStatus.COMPATIBLE,
        reason="observed_compatible",
        snapshot=_catalog(version, speakers, f"http://127.0.0.1:{port}"),
    )


def _voice(
    *,
    alias="narrator",
    speaker_id=_UUID_A,
    style_id="8",
    license_id="operator-rights-declared",
    attribution="Credit supplied by operator",
):
    return VoiceAlias(
        alias=alias,
        speaker_id=speaker_id,
        style_id=style_id,
        locale="ja",
        license_id=license_id,
        attribution=attribution,
    )


def _event(
    sequence: int,
    *,
    enabled=True,
    observation=_UNSET,
    config=None,
    voices=_UNSET,
    digest=_UNSET,
    policy=OperatorPolicyStatus.UNVERIFIED,
    policy_revision=_POLICY_V1,
):
    observed = _observation() if observation is _UNSET else observation
    pin = (
        observed.snapshot.observed_digest
        if digest is _UNSET
        and isinstance(observed, VoicevoxDiscoveryResult)
        and observed.snapshot is not None
        else digest
        if digest is not _UNSET
        else None
    )
    return ExternalVoicevoxPlanEvent(
        sequence=sequence,
        enabled=enabled,
        observation=observed,
        config=_config() if config is None else config,
        voices=(_voice(),) if voices is _UNSET else voices,
        expected_observed_digest=pin,
        policy_status=policy,
        policy_revision=policy_revision,
    )


def _plan(state, event):
    result = plan_external_voicevox_generation(state, event)
    assert result.ready is False and result.can_apply is False
    assert result.state.ready is False and result.state.can_apply is False
    assert result.state.worker_runtime_instance_epoch is None
    encoded = result.to_dict()
    assert encoded["ready"] is False and encoded["can_apply"] is False
    assert encoded["state"]["worker_runtime_instance_epoch"] is None
    assert encoded["control_reconciliation_required"] is result.state.retirement_pending
    return result


def _candidate(seq=1, state=None, **kwargs):
    return _plan(state or ExternalVoicevoxPlanState(), _event(seq, **kwargs))


def test_default_plan_has_no_worker_or_control_authority():
    state = ExternalVoicevoxPlanState()
    assert state.phase is ObservationPhase.DISABLED
    assert state.generation_counter == 0
    assert state.candidate_signature is None
    assert state.last_retired_generation == 0
    assert state.retirement_pending is False
    assert state.to_dict()["ready"] is False
    assert state.to_dict()["can_apply"] is False
    assert state.worker_runtime_instance_epoch is None


def test_compatible_observation_mints_only_a_planner_generation():
    transition = _candidate()
    assert transition.state.phase is ObservationPhase.CANDIDATE_UNQUALIFIED
    assert transition.state.generation_counter == 1
    assert transition.state.last_sequence == 1
    assert transition.state.candidate_signature.startswith("sha256:")
    assert transition.requests == (PlanRequest.QUALIFY_CANDIDATE,)
    assert transition.reason == "qualification_requested"
    assert transition.state.last_retired_generation == 0
    assert not transition.control_reconciliation_required
    assert "qualification_requested" in transition.to_json()
    assert "audio.synthesize" not in transition.to_json()


def test_repeated_same_catalog_and_same_operator_declaration_is_idempotent():
    first = _candidate()
    second = _candidate(2, first.state)
    assert second.requests == ()
    assert second.reason == "candidate_unchanged"
    assert second.state.generation_counter == 1
    assert second.state.candidate_signature == first.state.candidate_signature
    assert second.state.last_sequence == 2
    assert second.to_json() == _candidate(2, first.state).to_json()


def test_compatible_same_sequence_is_ignored_without_examining_input():
    class Trap:
        def __getattribute__(self, key):
            pytest.fail(f"stale event examined foreign {key}")

    first = _candidate()
    stale = ExternalVoicevoxPlanEvent(sequence=1, enabled=True)
    object.__setattr__(stale, "observation", Trap())
    object.__setattr__(stale, "config", Trap())
    object.__setattr__(stale, "voices", Trap())
    object.__setattr__(stale, "policy_status", Trap())
    result = _plan(first.state, stale)
    assert result.state == first.state
    assert result.requests == ()
    assert result.reason == "stale_ignored"


def test_out_of_order_late_success_cannot_restore_withdrawn_candidate():
    first = _candidate()
    absent = _plan(first.state, _event(3, observation=_ABSENT))
    assert absent.state.phase is ObservationPhase.UNAVAILABLE
    assert absent.requests == (PlanRequest.WITHDRAW_PRIOR,)
    late = _plan(absent.state, _event(2))
    assert late.reason == "stale_ignored"
    assert late.state == absent.state
    assert late.requests == ()
    assert late.state.candidate_signature is None


def test_no_engine_boot_and_later_appearance_are_observation_only():
    missing = _plan(ExternalVoicevoxPlanState(), _event(1, observation=_ABSENT))
    assert missing.state.phase is ObservationPhase.UNAVAILABLE
    assert missing.state.generation_counter == 0
    assert missing.requests == ()
    available = _plan(missing.state, _event(2))
    assert available.state.phase is ObservationPhase.CANDIDATE_UNQUALIFIED
    assert available.requests == (PlanRequest.QUALIFY_CANDIDATE,)
    assert available.state.generation_counter == 1
    assert available.state.worker_runtime_instance_epoch is None


@pytest.mark.parametrize(
    "unavailable,phase",
    [
        (_ABSENT, ObservationPhase.UNAVAILABLE),
        (_INCOMPATIBLE, ObservationPhase.UNAVAILABLE),
        (
            VoicevoxDiscoveryResult(VoicevoxDiscoveryStatus.DISABLED, "disabled"),
            ObservationPhase.UNAVAILABLE,
        ),
        (VoicevoxDiscoveryResult("fake", "secret"), ObservationPhase.INCOMPATIBLE),
        (None, ObservationPhase.REVIEW_BLOCKED),
    ],
)
def test_foreign_unavailable_and_invalid_observation_never_qualify(unavailable, phase):
    result = _candidate(observation=unavailable)
    assert result.state.phase is phase
    assert result.state.candidate_signature is None
    assert result.requests == ()
    assert result.state.generation_counter == 0


def test_disabled_initialization_never_inspects_any_foreign_value():
    class Trap:
        def __getattribute__(self, key):
            pytest.fail(f"disabled inspected {key}")

        def __eq__(self, other):
            pytest.fail("disabled evaluated foreign equality")

        def __bool__(self) -> bool:
            pytest.fail("disabled evaluated foreign truth value")
            return False

    event = ExternalVoicevoxPlanEvent(
        sequence=1,
        enabled=False,
        config=Trap(),
        observation=Trap(),
        expected_observed_digest=Trap(),
        voices=Trap(),
        policy_status=Trap(),
        policy_revision=Trap(),
    )
    result = _plan(ExternalVoicevoxPlanState(), event)
    assert result.state.phase is ObservationPhase.DISABLED
    assert result.requests == ()
    assert result.state.last_sequence == 1


def test_disabling_active_candidate_withdraws_and_never_forgets_fence():
    first = _candidate()
    stopped = _plan(first.state, _event(2, enabled=False))
    assert stopped.state.phase is ObservationPhase.DISABLED
    assert stopped.state.candidate_signature is None
    assert stopped.state.last_retired_generation == 1
    assert stopped.state.retirement_pending is True
    assert stopped.control_reconciliation_required
    assert stopped.requests == (PlanRequest.WITHDRAW_PRIOR,)
    again = _plan(stopped.state, _event(3, enabled=False))
    assert again.requests == ()
    assert again.state.last_retired_generation == 1
    assert again.control_reconciliation_required


def test_loss_then_reappearance_uses_new_planning_generation_and_retains_old_fence():
    first = _candidate()
    unavailable = _plan(first.state, _event(2, observation=_ABSENT))
    reappeared = _plan(unavailable.state, _event(3))
    assert reappeared.state.phase is ObservationPhase.CANDIDATE_UNQUALIFIED
    assert reappeared.state.generation_counter == 2
    assert reappeared.state.candidate_signature == first.state.candidate_signature
    assert reappeared.state.last_retired_generation == 1
    assert reappeared.control_reconciliation_required
    assert reappeared.requests == (PlanRequest.QUALIFY_CANDIDATE,)
    # These are source-only planning counters, never real RuntimeInstance epochs.
    assert reappeared.state.worker_runtime_instance_epoch is None


def test_version_drift_with_old_pin_withdraws_instead_of_qualifying():
    first = _candidate()
    version = _observation(version="0.16.0")
    old_digest = _observation().snapshot.observed_digest
    drift = _plan(
        first.state,
        _event(2, observation=version, digest=old_digest),
    )
    assert drift.state.phase is ObservationPhase.OBSERVATION_CHANGED
    assert drift.requests == (PlanRequest.WITHDRAW_PRIOR,)
    assert drift.control_reconciliation_required
    assert drift.state.candidate_signature is None
    accepted_new_pin = _plan(drift.state, _event(3, observation=version))
    assert accepted_new_pin.state.generation_counter == 2
    assert accepted_new_pin.requests == (PlanRequest.QUALIFY_CANDIDATE,)
    assert accepted_new_pin.control_reconciliation_required


def test_endpoint_drift_invalidates_even_matching_raw_digest_on_other_config():
    first = _candidate()
    remote = _observation(port=51021)
    result = _plan(
        first.state,
        _event(2, observation=remote),
    )
    assert result.state.phase is ObservationPhase.INCOMPATIBLE
    assert result.requests == (PlanRequest.WITHDRAW_PRIOR,)
    assert result.state.candidate_signature is None


def test_observation_style_change_rotates_only_after_requalification():
    first = _candidate()
    changed = _observation(style=9)
    old_pin = _observation().snapshot.observed_digest
    drift = _plan(first.state, _event(2, observation=changed, digest=old_pin))
    assert drift.state.phase is ObservationPhase.OBSERVATION_CHANGED
    assert drift.requests == (PlanRequest.WITHDRAW_PRIOR,)
    new_selection = _plan(
        drift.state,
        _event(3, observation=changed, voices=(_voice(style_id="9"),)),
    )
    assert new_selection.state.generation_counter == 2
    assert new_selection.requests == (PlanRequest.QUALIFY_CANDIDATE,)


@pytest.mark.parametrize(
    "changed_voice",
    [
        _voice(alias="alternate"),
        _voice(license_id="operator-terms-revision-2"),
        _voice(attribution="Another public credit statement"),
    ],
)
def test_changed_alias_license_or_credit_forces_prior_generation_withdrawal(
    changed_voice,
):
    first = _candidate()
    changed = _plan(first.state, _event(2, voices=(changed_voice,)))
    assert changed.state.candidate_signature != first.state.candidate_signature
    assert changed.state.generation_counter == 2
    assert changed.requests == (
        PlanRequest.WITHDRAW_PRIOR,
        PlanRequest.QUALIFY_CANDIDATE,
    )
    assert changed.control_reconciliation_required
    assert changed.state.last_retired_generation == 1
    assert changed.state.worker_runtime_instance_epoch is None


def test_changing_operator_policy_declaration_fingerprint_rotates_candidate():
    first = _candidate()
    result = _plan(first.state, _event(2, policy_revision=_POLICY_V2))
    assert result.state.generation_counter == 2
    assert result.requests == (
        PlanRequest.WITHDRAW_PRIOR,
        PlanRequest.QUALIFY_CANDIDATE,
    )
    assert result.control_reconciliation_required
    assert result.state.candidate_signature != first.state.candidate_signature


def test_operator_trust_rights_or_native_log_policy_revocation_withdraws():
    first = _candidate()
    revoked = _plan(
        first.state,
        _event(2, policy=OperatorPolicyStatus.REVOKED),
    )
    assert revoked.state.phase is ObservationPhase.POLICY_REVOKED
    assert revoked.state.candidate_signature is None
    assert revoked.requests == (PlanRequest.WITHDRAW_PRIOR,)
    assert revoked.control_reconciliation_required
    # Merely changing policy back to UNVERIFIED can only REQUEST qualification.
    rerun = _plan(revoked.state, _event(3))
    assert rerun.state.generation_counter == 2
    assert rerun.requests == (PlanRequest.QUALIFY_CANDIDATE,)
    assert rerun.control_reconciliation_required
    assert not rerun.ready and not rerun.can_apply


def test_repeated_policy_revocation_never_repeats_withdrawal_side_effect_request():
    first = _candidate()
    first_revoke = _plan(first.state, _event(2, policy=OperatorPolicyStatus.REVOKED))
    second_revoke = _plan(
        first_revoke.state, _event(3, policy=OperatorPolicyStatus.REVOKED)
    )
    assert second_revoke.requests == ()
    assert second_revoke.control_reconciliation_required
    assert second_revoke.state.generation_counter == 1


def test_two_successive_rotations_leave_monotonic_retirement_fence():
    first = _candidate()
    second = _plan(first.state, _event(2, policy_revision=_POLICY_V2))
    third = _plan(
        second.state,
        _event(3, voices=(_voice(alias="alternate"),), policy_revision=_POLICY_V2),
    )
    assert second.state.last_retired_generation == 1
    assert third.state.last_retired_generation == 2
    assert third.state.generation_counter == 3
    assert third.control_reconciliation_required
    assert third.requests == (PlanRequest.WITHDRAW_PRIOR, PlanRequest.QUALIFY_CANDIDATE)


def test_unknown_operator_rights_authority_is_never_accepted_as_approval():
    first = _candidate()
    evt = _event(2)
    object.__setattr__(evt, "policy_status", "APPROVED")
    blocked = _plan(first.state, evt)
    assert blocked.state.phase is ObservationPhase.REVIEW_BLOCKED
    assert blocked.requests == (PlanRequest.WITHDRAW_PRIOR,)
    assert blocked.control_reconciliation_required


@pytest.mark.parametrize(
    "bad_revision",
    [True, False, "", "secret", "sha256:" + "A" * 64, 10**1000],
)
def test_invalid_operator_policy_revision_fails_closed_in_input(bad_revision):
    with pytest.raises(ExternalVoicevoxPlanningError) as error:
        _event(1, policy_revision=bad_revision)
    assert str(error.value) == "VOICEVOX external generation plan is invalid"


@pytest.mark.parametrize("sequence", [0, -1, True, False, "1", None, 2**63])
def test_invalid_planning_sequence_is_rejected(sequence):
    with pytest.raises(ExternalVoicevoxPlanningError):
        _event(sequence)


@pytest.mark.parametrize("enabled", [1, 0, "yes", None, object()])
def test_planner_enabled_flag_requires_exact_boolean(enabled):
    with pytest.raises(ExternalVoicevoxPlanningError):
        _event(1, enabled=enabled)


def test_invalid_or_mutated_plan_state_fails_closed_and_redacts_diagnostics():
    with pytest.raises(ExternalVoicevoxPlanningError):
        ExternalVoicevoxPlanState(generation_counter=True)
    with pytest.raises(ExternalVoicevoxPlanningError):
        ExternalVoicevoxPlanState(
            phase=ObservationPhase.CANDIDATE_UNQUALIFIED,
            candidate_signature=None,
        )
    with pytest.raises(ExternalVoicevoxPlanningError):
        ExternalVoicevoxPlanState(
            last_retired_generation=1,
            generation_counter=0,
            retirement_pending=True,
        )
    first = _candidate()
    mutated = replace(first.state)
    object.__setattr__(mutated, "candidate_signature", "raw-secret-token")
    with pytest.raises(ExternalVoicevoxPlanningError) as error:
        _plan(mutated, _event(2))
    assert "raw-secret-token" not in str(error.value)


def test_reflectively_mutated_event_header_fails_closed():
    event = _event(1)
    object.__setattr__(event, "sequence", "secret-user-data")
    with pytest.raises(ExternalVoicevoxPlanningError) as error:
        _plan(ExternalVoicevoxPlanState(), event)
    assert "secret-user-data" not in str(error.value)


def test_missing_operator_voice_or_pin_cannot_generate_candidate():
    no_voice = _candidate(voices=())
    assert no_voice.state.phase is ObservationPhase.REVIEW_BLOCKED
    assert no_voice.requests == ()
    no_pin = _candidate(digest=None)
    assert no_pin.state.phase is ObservationPhase.REVIEW_BLOCKED
    assert no_pin.requests == ()
    no_config = _candidate(config=object())
    assert no_config.state.phase is ObservationPhase.REVIEW_BLOCKED
    assert no_config.requests == ()


def test_unobserved_voice_with_updated_pin_withdraws_and_never_registers():
    first = _candidate()
    changed = _plan(first.state, _event(2, voices=(_voice(style_id="9"),)))
    assert changed.state.phase is ObservationPhase.OBSERVATION_CHANGED
    assert changed.requests == (PlanRequest.WITHDRAW_PRIOR,)
    assert changed.state.worker_runtime_instance_epoch is None


def test_poisoned_snapshot_and_private_exception_text_are_redacted():
    first = _candidate()
    bad = _observation()
    object.__setattr__(bad.snapshot, "observed_digest", "secret-user-url")
    result = _plan(
        first.state,
        _event(2, observation=bad, digest="sha256:" + "a" * 64),
    )
    assert result.state.phase is ObservationPhase.INCOMPATIBLE
    assert result.requests == (PlanRequest.WITHDRAW_PRIOR,)
    assert "secret" not in result.to_json()
    assert "secret" not in repr(result)


def test_operator_alias_and_native_endpoint_never_appear_in_plan_json_or_repr():
    changed = _voice(
        alias="secret-private-voice",
        license_id="license-private-billing-text",
        attribution="private user voice license terms",
    )
    result = _candidate(voices=(changed,))
    encoded = result.to_json()
    for secret in (
        "secret-private-voice",
        "license-private-billing-text",
        "private user voice license terms",
        "127.0.0.1",
        _UUID_A,
    ):
        assert secret not in encoded
        assert secret not in repr(result)
        assert secret not in repr(result.state)
    assert json.loads(encoded)["ready"] is False


@pytest.mark.asyncio
async def test_reducer_never_opens_socket_spawns_process_or_touches_external_engine(
    monkeypatch,
):
    def deny(*args, **kwargs):
        pytest.fail("pure R4-A planner attempted external action")

    async def deny_async(*args, **kwargs):
        deny()

    monkeypatch.setattr(asyncio, "open_connection", deny_async)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", deny_async)
    monkeypatch.setattr(os, "kill", deny)
    first = _candidate()
    second = _plan(first.state, _event(2, observation=_ABSENT))
    third = _plan(second.state, _event(3))
    assert not first.ready and not second.ready and not third.ready
    assert third.state.last_retired_generation == 1


def test_absence_of_real_worker_epochs_even_after_planner_reshuffles():
    state = ExternalVoicevoxPlanState()
    events = [
        _event(1),
        _event(2, voices=(_voice(alias="second-alias"),)),
        _event(3, observation=_ABSENT),
        _event(4),
        _event(5, policy=OperatorPolicyStatus.REVOKED),
        _event(6, enabled=False),
    ]
    for event in events:
        outcome = _plan(state, event)
        state = outcome.state
        assert outcome.state.worker_runtime_instance_epoch is None
        assert not outcome.can_apply
    assert state.phase is ObservationPhase.DISABLED
    assert state.generation_counter == 3
    assert state.last_retired_generation == 3
    assert state.retirement_pending


def test_no_replay_of_stale_disabled_event_into_newer_candidate():
    disabled = _plan(ExternalVoicevoxPlanState(), _event(1, enabled=False))
    qualified = _plan(disabled.state, _event(3))
    stale = _plan(qualified.state, _event(2, enabled=False))
    assert stale.state == qualified.state
    assert stale.requests == ()
    assert stale.state.generation_counter == 1
    assert not stale.can_apply


def test_generation_counter_overflow_never_wraps_or_reuses_epoch():
    state = ExternalVoicevoxPlanState(
        last_sequence=3,
        generation_counter=2**63 - 1,
        phase=ObservationPhase.CANDIDATE_UNQUALIFIED,
        candidate_signature="sha256:" + "c" * 64,
        last_retired_generation=2**63 - 2,
        retirement_pending=True,
    )
    with pytest.raises(ExternalVoicevoxPlanningError):
        _plan(state, _event(4))


def test_rejected_stale_sequence_does_not_clear_pending_control_reconciliation():
    first = _candidate()
    revoked = _plan(first.state, _event(10, policy=OperatorPolicyStatus.REVOKED))
    ignored = _plan(revoked.state, _event(9))
    assert ignored.state == revoked.state
    assert ignored.control_reconciliation_required is True
    assert ignored.requests == ()


def test_reflectively_mutated_state_serialization_rejects_private_text():
    first = _candidate()
    state = first.state
    object.__setattr__(state, "candidate_signature", "secret://private-token")
    with pytest.raises(ExternalVoicevoxPlanningError) as caught:
        state.to_dict()
    assert "secret" not in str(caught.value)
    with pytest.raises(ExternalVoicevoxPlanningError) as caught:
        first.to_dict()
    assert "secret" not in str(caught.value)
    with pytest.raises(ExternalVoicevoxPlanningError) as caught:
        first.to_json()
    assert "secret" not in str(caught.value)


def test_transition_constructor_rejects_private_reason_and_requests():
    from astrumweaver.runtime.providers.voicevox_external_plan import (
        ExternalVoicevoxPlanTransition,
    )

    state = _candidate().state
    with pytest.raises(ExternalVoicevoxPlanningError) as caught:
        ExternalVoicevoxPlanTransition(
            state=state, reason="https://private.example/secret"
        )
    assert "private" not in str(caught.value)
    with pytest.raises(ExternalVoicevoxPlanningError):
        ExternalVoicevoxPlanTransition(
            state=state, requests=("unverified_free_form_action",)
        )
    with pytest.raises(ExternalVoicevoxPlanningError):
        ExternalVoicevoxPlanTransition(
            state=state, requests=(PlanRequest.WITHDRAW_PRIOR,)
        )


def test_reflective_transition_mutation_rejected_on_serialization():
    first = _candidate()
    object.__setattr__(first, "reason", "private-caller-text")
    with pytest.raises(ExternalVoicevoxPlanningError) as caught:
        first.to_json()
    assert "private" not in str(caught.value)
    with pytest.raises(ExternalVoicevoxPlanningError) as caught:
        first.to_dict()
    assert "private" not in str(caught.value)
    second = _candidate()
    object.__setattr__(second, "requests", ("private-voice-url",))
    with pytest.raises(ExternalVoicevoxPlanningError):
        second.to_dict()


def test_reflective_retirement_state_mutation_rejected_by_public_property():
    first = _candidate()
    object.__setattr__(first.state, "retirement_pending", "private-secret")
    with pytest.raises(ExternalVoicevoxPlanningError):
        _ = first.control_reconciliation_required
    with pytest.raises(ExternalVoicevoxPlanningError):
        _ = first.to_json()


def test_direct_transition_can_only_emit_fixed_valid_redacted_codes():
    from astrumweaver.runtime.providers.voicevox_external_plan import (
        ExternalVoicevoxPlanTransition,
    )

    first = _candidate()
    result = ExternalVoicevoxPlanTransition(
        state=first.state,
        requests=(PlanRequest.QUALIFY_CANDIDATE,),
        reason="qualification_requested",
    )
    assert result.to_json() == first.to_json()
    assert result.ready is False and result.can_apply is False
