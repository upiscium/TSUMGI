"""#247 R4-B1/A: pure, synthetic observation-to-advisory-planner tests.

No HTTP server, socket, real engine, Worker, Control, registry or native audio.
Actual R1 polling lifecycle integration is separately gated to R4-B1/B.
"""

from __future__ import annotations

import asyncio
import math
import os
from dataclasses import asdict, replace

import pytest

from astrumweaver.gateway.audio import VoiceAlias
from astrumweaver.runtime.providers.voicevox_discovery import (
    VoicevoxDiscoveryConfig,
    VoicevoxDiscoveryResult,
    VoicevoxDiscoveryStatus,
    _catalog,
)
from astrumweaver.runtime.providers.voicevox_external_plan import (
    ObservationPhase,
    OperatorPolicyStatus,
    PlanRequest,
)
from astrumweaver.runtime.providers.voicevox_external_reconcile import (
    ExternalVoicevoxReconcileConfig,
    ExternalVoicevoxReconcileError,
    ExternalVoicevoxReconcileState,
    ExternalVoicevoxSampleToken,
    begin_external_voicevox_reconcile_sample,
    complete_external_voicevox_reconcile_sample,
    revoke_external_voicevox_reconcile_run,
    start_external_voicevox_reconcile_run,
)

_UUID = "7ffcb7ce-00ec-4bdc-82cd-45a8889e43ff"
_OTHER = "ab888885-e22d-4fb5-8161-df19a8b97a02"
_POLICY1 = "sha256:" + "a" * 64
_POLICY2 = "sha256:" + "b" * 64
_SECRET = "VOICEVOX PRIVATE /audio_query?text=keep-me-secret"


def _discovery(port=50021):
    return VoicevoxDiscoveryConfig(
        enabled=True, endpoint=f"http://127.0.0.1:{port}", timeout_seconds=1.0
    )


def _observation(*, port=50021, style=8, version="0.15.0", uuid=_UUID):
    return VoicevoxDiscoveryResult(
        status=VoicevoxDiscoveryStatus.COMPATIBLE,
        reason="observed_compatible",
        snapshot=_catalog(
            version,
            [{"speaker_uuid": uuid, "styles": [{"id": style}]}],
            f"http://127.0.0.1:{port}",
        ),
    )


def _voice(*, alias="narrator", style="8", uuid=_UUID, attribution="Synthetic credit"):
    return VoiceAlias(
        alias=alias,
        speaker_id=uuid,
        style_id=style,
        locale="ja",
        license_id="operator-declared-license",
        attribution=attribution,
    )


def _config(
    *,
    pin=None,
    port=50021,
    voices=None,
    status=OperatorPolicyStatus.UNVERIFIED,
    policy_revision=_POLICY1,
):
    observation = _observation(port=port)
    return ExternalVoicevoxReconcileConfig(
        enabled=True,
        discovery=_discovery(port),
        expected_observed_digest=(
            observation.snapshot.observed_digest if pin is None else pin
        ),
        voices=(_voice(),) if voices is None else voices,
        policy_status=status,
        policy_revision=policy_revision,
    )


def _start(config=None):
    state = ExternalVoicevoxReconcileState(config=config or _config())
    return start_external_voicevox_reconcile_run(state)


def _sample(state, *, launch=10.0, observed=None, result=None):
    started, token = begin_external_voicevox_reconcile_sample(
        state, launched_at_monotonic=launch
    )
    assert token is not None
    finished, transition = complete_external_voicevox_reconcile_sample(
        started,
        token=token,
        observation=_observation() if result is None else result,
        observed_at_monotonic=launch + 0.3 if observed is None else observed,
    )
    assert transition is not None
    assert finished.ready is False and finished.can_apply is False
    assert finished.worker_runtime_instance_epoch is None
    assert finished.pending is None
    assert transition.ready is False and transition.can_apply is False
    return finished, transition


def _positive():
    return _sample(_start())


def _safety(state):
    payload = state.to_dict()
    assert payload["ready"] is False
    assert payload["can_apply"] is False
    assert payload["rights_verified"] is False
    assert payload["external_engine_verified"] is False
    assert payload["worker_runtime_instance_epoch"] is None
    assert payload["plan"]["ready"] is False
    assert payload["plan"]["can_apply"] is False
    assert "voices" not in payload and "config" not in payload
    assert _UUID not in state.to_json()
    assert "127.0.0.1" not in state.to_json()
    assert "Synthetic credit" not in state.to_json()


def test_default_disabled_no_foreign_data_read_or_actual_admission():
    class Trap:
        def __getattribute__(self, attr):
            pytest.fail(f"disabled inspected foreign {attr}")

        def __bool__(self) -> bool:
            pytest.fail("disabled inspected foreign truth value")
            return False

    config = ExternalVoicevoxReconcileConfig(
        enabled=False,
        discovery=Trap(),
        expected_observed_digest=Trap(),
        voices=Trap(),
        policy_status=Trap(),
        policy_revision=Trap(),
    )
    state = ExternalVoicevoxReconcileState(config=config)
    assert not state.active
    assert state.run_generation == 0
    assert state.plan.retirement_pending is False
    assert start_external_voicevox_reconcile_run(state) == state
    state2, token = begin_external_voicevox_reconcile_sample(
        state, launched_at_monotonic=math.nan
    )
    assert state2 == state and token is None
    _safety(state)


def test_positive_r1_catalog_goes_only_to_unqualified_r4a_planning():
    state, transition = _positive()
    assert state.plan.phase is ObservationPhase.CANDIDATE_UNQUALIFIED
    assert state.plan.generation_counter == 1
    assert state.plan.candidate_signature.startswith("sha256:")
    assert state.plan.retirement_pending is False
    assert state.last_sequence == 1
    assert transition.requests == (PlanRequest.QUALIFY_CANDIDATE,)
    assert not transition.can_apply and not transition.ready
    _safety(state)


def test_repeated_identical_full_catalog_is_idempotent_for_planning_generation():
    first, _ = _positive()
    second, transition = _sample(first, launch=11.0, observed=11.2)
    assert second.plan.candidate_signature == first.plan.candidate_signature
    assert second.plan.generation_counter == first.plan.generation_counter
    assert transition.requests == ()
    assert transition.reason == "candidate_unchanged"


def test_malformed_body_and_absent_voicevox_withdraw_and_preserve_retirement():
    first, _ = _positive()
    for status in (
        VoicevoxDiscoveryStatus.ABSENT,
        VoicevoxDiscoveryStatus.INCOMPATIBLE,
    ):
        ended, transition = _sample(
            first,
            launch=11.0,
            observed=11.1,
            result=VoicevoxDiscoveryResult(status=status, reason=_SECRET),
        )
        assert transition.requests == (PlanRequest.WITHDRAW_PRIOR,)
        assert ended.plan.candidate_signature is None
        assert ended.plan.retirement_pending is True
        assert ended.plan.last_retired_generation == 1
        assert _SECRET not in ended.to_json()


def test_repeated_negative_observations_do_not_repeat_withdraw_request():
    first, _ = _positive()
    second, _ = _sample(
        first,
        launch=11.0,
        result=VoicevoxDiscoveryResult(VoicevoxDiscoveryStatus.ABSENT, "absent"),
    )
    third, transition = _sample(
        second,
        launch=12.0,
        observed=12.3,
        result=VoicevoxDiscoveryResult(VoicevoxDiscoveryStatus.ABSENT, "absent"),
    )
    assert transition.requests == ()
    assert third.plan.retirement_pending
    assert third.plan.last_retired_generation == 1
    assert third.plan.generation_counter == 1


def test_loss_and_reappearance_mints_fresh_planner_generation_never_worker_epoch():
    first, _ = _positive()
    lost, _ = _sample(
        first,
        launch=11.0,
        result=VoicevoxDiscoveryResult(VoicevoxDiscoveryStatus.ABSENT, "gone"),
    )
    restored, transition = _sample(lost, launch=12.0, observed=12.3)
    assert restored.plan.generation_counter == 2
    assert restored.plan.candidate_signature == first.plan.candidate_signature
    assert restored.plan.last_retired_generation == 1
    assert restored.plan.retirement_pending
    assert transition.requests == (PlanRequest.QUALIFY_CANDIDATE,)
    assert restored.worker_runtime_instance_epoch is None


def test_changed_version_voice_style_or_uuid_without_new_pin_withdraws():
    first, _ = _positive()
    for obs in (
        _observation(version="0.16.0"),
        _observation(style=9),
        _observation(uuid=_OTHER),
    ):
        state, transition = _sample(first, launch=11.0, result=obs)
        assert state.plan.phase is ObservationPhase.OBSERVATION_CHANGED
        assert transition.requests == (PlanRequest.WITHDRAW_PRIOR,)
        assert state.plan.candidate_signature is None
        assert state.plan.retirement_pending


def test_pin_is_operator_independent_not_auto_updated_on_drift():
    first, _ = _positive()
    changed = _observation(version="0.16.0")
    drifted, _ = _sample(first, launch=11.0, result=changed)
    assert (
        drifted.config.expected_observed_digest == first.config.expected_observed_digest
    )
    next_state, next_token = begin_external_voicevox_reconcile_sample(
        drifted, launched_at_monotonic=12.0
    )
    restored, transition = complete_external_voicevox_reconcile_sample(
        next_state, token=next_token, observation=changed, observed_at_monotonic=12.2
    )
    assert restored.plan.phase is ObservationPhase.OBSERVATION_CHANGED
    assert transition.requests == ()
    assert restored.plan.retirement_pending


def test_checked_config_copies_selected_voice_and_operator_policy_revision():
    source = _voice()
    cfg = _config(voices=(source,))
    assert cfg.voices == (source,)
    assert cfg.voices[0] is not source
    assert cfg.revision.startswith("sha256:")
    assert cfg.revision != _POLICY1
    assert cfg.policy_status is OperatorPolicyStatus.UNVERIFIED


@pytest.mark.parametrize(
    "changed",
    [
        {"policy_revision": _POLICY2},
        {"voices": (_voice(attribution="Changed synthetic credit"),)},
        {"voices": (_voice(alias="new-narrator"),)},
        {"voices": (_voice(style="9"),)},
        {"port": 50022},
        {"status": OperatorPolicyStatus.REVOKED},
    ],
)
def test_changing_operator_configuration_synchronously_withdraws_prior(changed):
    first, _ = _positive()
    new_cfg = _config(**changed)
    next_state, transition = revoke_external_voicevox_reconcile_run(
        first, config=new_cfg
    )
    assert transition is not None
    assert transition.requests == (PlanRequest.WITHDRAW_PRIOR,)
    assert next_state.plan.retirement_pending
    assert next_state.plan.last_retired_generation == 1
    assert next_state.plan.candidate_signature is None
    assert not next_state.active
    assert next_state.run_generation == first.run_generation + 1
    assert next_state.last_sequence == first.last_sequence + 1
    assert next_state.config.revision == new_cfg.revision
    _safety(next_state)


def test_source_only_revoke_does_not_start_polling_and_restart_explicit():
    first, _ = _positive()
    stopped, transition = revoke_external_voicevox_reconcile_run(first)
    assert transition.requests == (PlanRequest.WITHDRAW_PRIOR,)
    assert stopped.pending is None and not stopped.active
    assert stopped.plan.retirement_pending
    assert revoke_external_voicevox_reconcile_run(stopped) == (stopped, None)
    restarted = start_external_voicevox_reconcile_run(stopped)
    assert restarted.active and restarted.run_generation > first.run_generation
    assert restarted.plan.retirement_pending
    assert restarted.plan.candidate_signature is None
    new, request = _sample(restarted, launch=11.0, observed=11.3)
    assert new.plan.generation_counter == 2
    assert request.requests == (PlanRequest.QUALIFY_CANDIDATE,)
    assert new.plan.retirement_pending


def test_revoke_during_inflight_response_blocks_old_token_without_inspection():
    first, _ = _positive()
    state, old_token = begin_external_voicevox_reconcile_sample(
        first, launched_at_monotonic=11.0
    )
    revoked, transition = revoke_external_voicevox_reconcile_run(state)
    assert transition.requests == (PlanRequest.WITHDRAW_PRIOR,)
    assert revoked.plan.retirement_pending and revoked.pending is None
    assert revoked.last_sequence > old_token.sequence

    class Poison:
        def __getattribute__(self, name):
            pytest.fail(f"late result inspected {name}")

        def __bool__(self) -> bool:
            pytest.fail("late result truthiness")
            return False

    again, stale = complete_external_voicevox_reconcile_sample(
        revoked,
        token=old_token,
        observation=Poison(),
        observed_at_monotonic=Poison(),
    )
    assert stale is None
    assert again == revoked
    assert again.plan.retirement_pending


def test_old_config_and_run_token_cannot_restore_after_new_start():
    first, _ = _positive()
    pending, old_token = begin_external_voicevox_reconcile_sample(
        first, launched_at_monotonic=11.0
    )
    new_cfg = _config(policy_revision=_POLICY2)
    replaced, transition = revoke_external_voicevox_reconcile_run(
        pending, config=new_cfg
    )
    assert transition.requests == (PlanRequest.WITHDRAW_PRIOR,)
    restarted = start_external_voicevox_reconcile_run(replaced)
    fresh, token = begin_external_voicevox_reconcile_sample(
        restarted, launched_at_monotonic=12.0
    )
    ignored, stale = complete_external_voicevox_reconcile_sample(
        fresh, token=old_token, observation=_observation(), observed_at_monotonic=12.1
    )
    assert stale is None
    assert ignored == fresh
    accepted, new_transition = complete_external_voicevox_reconcile_sample(
        fresh, token=token, observation=_observation(), observed_at_monotonic=12.1
    )
    assert new_transition.requests == (PlanRequest.QUALIFY_CANDIDATE,)
    assert accepted.plan.retirement_pending
    assert accepted.plan.generation_counter == 2


def test_stale_token_duplicate_completion_does_not_churn_or_read_result():
    initial = _start()
    pending, token = begin_external_voicevox_reconcile_sample(
        initial, launched_at_monotonic=1.0
    )
    next_state, transition = complete_external_voicevox_reconcile_sample(
        pending, token=token, observation=_observation(), observed_at_monotonic=1.1
    )
    assert transition.requests == (PlanRequest.QUALIFY_CANDIDATE,)
    duplicate, stale = complete_external_voicevox_reconcile_sample(
        next_state, token=token, observation=object(), observed_at_monotonic=object()
    )
    assert stale is None and duplicate == next_state


def test_policy_revoked_blocks_start_and_reappearing_sample():
    good, _ = _positive()
    revoked_cfg = _config(status=OperatorPolicyStatus.REVOKED)
    revoked, transition = revoke_external_voicevox_reconcile_run(
        good, config=revoked_cfg
    )
    assert transition.requests == (PlanRequest.WITHDRAW_PRIOR,)
    assert revoked.plan.phase is ObservationPhase.POLICY_REVOKED
    assert start_external_voicevox_reconcile_run(revoked) == revoked
    new, token = begin_external_voicevox_reconcile_sample(
        revoked, launched_at_monotonic=2.0
    )
    assert new == revoked and token is None
    assert revoked.plan.retirement_pending


def test_source_only_closed_state_cannot_restart_or_change_config():
    first, _ = _positive()
    closed, transition = revoke_external_voicevox_reconcile_run(first, close=True)
    assert transition.requests == (PlanRequest.WITHDRAW_PRIOR,)
    assert closed.closed and not closed.active
    assert closed.plan.retirement_pending
    assert revoke_external_voicevox_reconcile_run(closed) == (closed, None)
    with pytest.raises(ExternalVoicevoxReconcileError):
        start_external_voicevox_reconcile_run(closed)
    with pytest.raises(ExternalVoicevoxReconcileError):
        revoke_external_voicevox_reconcile_run(
            closed, config=_config(policy_revision=_POLICY2)
        )


def test_malformed_completion_clock_is_negative_and_retirement_preserved():
    first, _ = _positive()
    for received_at in (-1, math.nan, math.inf, 10**1000, "private"):
        state, token = begin_external_voicevox_reconcile_sample(
            first, launched_at_monotonic=11.0
        )
        next_state, transition = complete_external_voicevox_reconcile_sample(
            state,
            token=token,
            observation=_observation(),
            observed_at_monotonic=received_at,
        )
        assert transition.requests == (PlanRequest.WITHDRAW_PRIOR,)
        assert next_state.plan.phase is ObservationPhase.REVIEW_BLOCKED
        assert next_state.plan.retirement_pending
        assert next_state.pending is None


def test_completion_clock_preceding_launch_fails_closed():
    first, _ = _positive()
    state, token = begin_external_voicevox_reconcile_sample(
        first, launched_at_monotonic=11.0
    )
    new, transition = complete_external_voicevox_reconcile_sample(
        state, token=token, observation=_observation(), observed_at_monotonic=10.0
    )
    assert transition.requests == (PlanRequest.WITHDRAW_PRIOR,)
    assert new.plan.candidate_signature is None


@pytest.mark.parametrize(
    "invalid_launch",
    [-1, math.nan, math.inf, True, False, "2", None, 10**1000],
)
def test_invalid_launch_clock_is_redacted_error(invalid_launch):
    active = _start()
    with pytest.raises(ExternalVoicevoxReconcileError):
        begin_external_voicevox_reconcile_sample(
            active, launched_at_monotonic=invalid_launch
        )


def test_launch_clock_cannot_move_backwards():
    active = _start()
    after, token = begin_external_voicevox_reconcile_sample(
        active, launched_at_monotonic=10.0
    )
    after, _ = complete_external_voicevox_reconcile_sample(
        after, token=token, observation=_observation(), observed_at_monotonic=10.1
    )
    with pytest.raises(ExternalVoicevoxReconcileError):
        begin_external_voicevox_reconcile_sample(after, launched_at_monotonic=9.0)


def test_double_begin_raises_and_only_one_pending_observation():
    active = _start()
    pending, first_token = begin_external_voicevox_reconcile_sample(
        active, launched_at_monotonic=1.0
    )
    with pytest.raises(ExternalVoicevoxReconcileError):
        begin_external_voicevox_reconcile_sample(pending, launched_at_monotonic=2.0)
    assert pending.pending == first_token
    assert pending.plan.last_sequence == 0
    assert pending.last_sequence == 1


def test_tokens_reject_forged_run_revision_and_overflow():
    active = _start()
    pending, token = begin_external_voicevox_reconcile_sample(
        active, launched_at_monotonic=1.0
    )
    for bogus in (
        replace(token, run_generation=99),
        replace(token, sequence=99),
        replace(token, config_revision="sha256:" + "f" * 64),
    ):
        _, ignored = complete_external_voicevox_reconcile_sample(
            pending, token=bogus, observation=_observation(), observed_at_monotonic=1.2
        )
        assert ignored is None
    for invalid in (
        {"run_generation": True},
        {"sequence": 2**63},
        {"sequence": "private"},
        {"config_revision": "private"},
        {"launched_at_monotonic": math.nan},
    ):
        with pytest.raises(ExternalVoicevoxReconcileError):
            ExternalVoicevoxSampleToken(**{**asdict(token), **invalid})


def test_configuration_requires_independent_pin_unverified_policy_and_voices():
    with pytest.raises(ExternalVoicevoxReconcileError):
        ExternalVoicevoxReconcileConfig(enabled=True, discovery=_discovery())
    with pytest.raises(ExternalVoicevoxReconcileError):
        ExternalVoicevoxReconcileConfig(
            enabled=True,
            discovery=_discovery(),
            expected_observed_digest="sha256:" + "a" * 64,
            voices=(_voice(),),
            policy_revision="nope",
        )
    with pytest.raises(ExternalVoicevoxReconcileError):
        _config(status="APPROVED")
    with pytest.raises(ExternalVoicevoxReconcileError):
        _config(voices=())


def test_duplicate_voices_are_rejected_not_deduplicated():
    with pytest.raises(ExternalVoicevoxReconcileError):
        _config(voices=(_voice(), _voice()))
    with pytest.raises(ExternalVoicevoxReconcileError):
        _config(voices=(_voice(), _voice(alias="x")))


def test_configuration_mutation_is_rechecked_at_use_and_serialization():
    config = _config()
    state = _start(config)
    # The state copies the caller's original configuration. Mutate the
    # actual stored copy to exercise its public validation boundary.
    object.__setattr__(state.config, "policy_revision", _POLICY2)
    with pytest.raises(ExternalVoicevoxReconcileError):
        start_external_voicevox_reconcile_run(state)
    with pytest.raises(ExternalVoicevoxReconcileError):
        state.to_json()


def test_reflective_voice_mutation_cannot_silently_rebind_without_revision():
    config = _config()
    state = _start(config)
    object.__setattr__(state.config.voices[0], "attribution", _SECRET)
    with pytest.raises(ExternalVoicevoxReconcileError) as caught:
        begin_external_voicevox_reconcile_sample(state, launched_at_monotonic=1.0)
    assert _SECRET not in str(caught.value)
    with pytest.raises(ExternalVoicevoxReconcileError):
        state.to_dict()


def test_public_state_serialization_rejects_forged_private_values():
    original = _start()
    assert original.to_dict()["config_revision"].startswith("sha256:")
    for name, value in (
        ("last_sequence", _SECRET),
        ("run_generation", _SECRET),
        ("active", _SECRET),
        ("plan", _SECRET),
    ):
        state = replace(original)
        object.__setattr__(state, name, value)
        with pytest.raises(ExternalVoicevoxReconcileError) as error:
            state.to_dict()
        assert _SECRET not in str(error.value)
    assert _SECRET not in repr(original)
    _safety(original)


def test_reflective_pending_token_mutation_rejected_before_observation():
    initial = _start()
    pending, token = begin_external_voicevox_reconcile_sample(
        initial, launched_at_monotonic=1.0
    )
    object.__setattr__(pending.pending, "config_revision", _SECRET)
    with pytest.raises(ExternalVoicevoxReconcileError):
        complete_external_voicevox_reconcile_sample(
            pending, token=token, observation=object(), observed_at_monotonic=1.1
        )


def test_deterministic_replay_same_state_event_has_same_redacted_transition():
    active = _start()
    pending, token = begin_external_voicevox_reconcile_sample(
        active, launched_at_monotonic=1.0
    )
    a, transition_a = complete_external_voicevox_reconcile_sample(
        pending, token=token, observation=_observation(), observed_at_monotonic=1.1
    )
    b, transition_b = complete_external_voicevox_reconcile_sample(
        pending, token=token, observation=_observation(), observed_at_monotonic=1.1
    )
    assert a.to_json() == b.to_json()
    assert transition_a.to_json() == transition_b.to_json()


def test_no_network_process_or_async_task_owned_by_pure_reconciler(monkeypatch):
    def deny(*_args, **_kwargs):
        pytest.fail("pure R4-B1/A attempted an external side effect")

    monkeypatch.setattr(asyncio, "create_task", deny)
    monkeypatch.setattr(asyncio, "open_connection", deny)
    monkeypatch.setattr(os, "kill", deny)
    active = _start()
    observed, _ = _sample(active)
    revoked, _ = revoke_external_voicevox_reconcile_run(observed)
    assert not revoked.ready and not revoked.can_apply


def test_reconfiguration_always_never_auto_updates_expected_observed_digest():
    initial = _config()
    changed = _config(policy_revision=_POLICY2)
    assert changed.expected_observed_digest == initial.expected_observed_digest
    initial_state = _start(initial)
    updated, _ = revoke_external_voicevox_reconcile_run(initial_state, config=changed)
    assert updated.config.expected_observed_digest == initial.expected_observed_digest
    assert updated.config.revision != initial.revision
    assert not updated.active


def test_planning_sequence_overflow_fails_closed():
    config = _config()
    state = ExternalVoicevoxReconcileState(
        config=config,
        plan=replace(_start(config).plan, last_sequence=2**63 - 1),
        run_generation=1,
        last_sequence=2**63 - 1,
        active=True,
    )
    with pytest.raises(ExternalVoicevoxReconcileError):
        begin_external_voicevox_reconcile_sample(state, launched_at_monotonic=2.0)
    with pytest.raises(ExternalVoicevoxReconcileError):
        revoke_external_voicevox_reconcile_run(state)


def test_private_complete_observation_envelope_has_no_public_raw_repr():
    from astrumweaver.runtime.providers.voicevox_external_reconcile import (
        _VoicevoxR1ObservationEnvelope,
    )

    state = _start()
    state, token = begin_external_voicevox_reconcile_sample(
        state, launched_at_monotonic=10.0
    )
    private = _VoicevoxR1ObservationEnvelope(
        token=token,
        observed_at_monotonic=10.1,
        observation=VoicevoxDiscoveryResult(
            VoicevoxDiscoveryStatus.INCOMPATIBLE, _SECRET
        ),
    )
    assert _SECRET not in repr(private)
    assert _UUID not in repr(private)
    assert "127.0.0.1" not in repr(private)


def test_requalification_uses_separately_reviewed_new_pin_and_voice_selection():
    first, _ = _positive()
    changed = _observation(style=9)
    drifted, transition = _sample(first, launch=11.0, result=changed)
    assert transition.requests == (PlanRequest.WITHDRAW_PRIOR,)
    assert drifted.plan.retirement_pending
    # New R1 digest is accepted ONLY through explicit new operator config,
    # with the actual new voice style selection, not by observation auto-pin.
    new_cfg = _config(
        pin=changed.snapshot.observed_digest,
        voices=(_voice(style="9"),),
        policy_revision=_POLICY2,
    )
    replacement, revoked = revoke_external_voicevox_reconcile_run(
        drifted, config=new_cfg
    )
    assert revoked.requests == ()
    assert (
        replacement.config.expected_observed_digest == changed.snapshot.observed_digest
    )
    resumed = start_external_voicevox_reconcile_run(replacement)
    updated, request = _sample(resumed, launch=12.0, observed=12.2, result=changed)
    assert request.requests == (PlanRequest.QUALIFY_CANDIDATE,)
    assert updated.plan.generation_counter == 2
    assert updated.plan.last_retired_generation == 1
    assert updated.plan.retirement_pending
    assert updated.worker_runtime_instance_epoch is None


def test_malformed_reflectively_modified_r1_result_withdraws_and_redacts():
    first, _ = _positive()
    bad = _observation()
    object.__setattr__(bad.snapshot, "observed_digest", _SECRET)
    state, transition = _sample(first, launch=11.0, result=bad)
    assert transition.requests == (PlanRequest.WITHDRAW_PRIOR,)
    assert state.plan.candidate_signature is None
    assert state.plan.retirement_pending
    assert _SECRET not in state.to_json()


def test_recovery_does_not_rewrite_operator_rights_or_native_identity():
    config = _config()
    first, _ = _sample(_start(config))
    second, second_transition = _sample(first, launch=11.0, observed=11.2)
    assert second_transition.requests == ()
    assert second.config.revision == config.revision
    assert second.config.policy_status is OperatorPolicyStatus.UNVERIFIED
    assert second.config.expected_observed_digest == config.expected_observed_digest
    snapshot = second.to_dict()
    assert snapshot["external_engine_verified"] is False
    assert snapshot["rights_verified"] is False
    assert snapshot["worker_runtime_instance_epoch"] is None


def test_same_config_revision_revoke_without_active_cannot_issue_duplicate():
    state, _ = _positive()
    revoked, first = revoke_external_voicevox_reconcile_run(state)
    assert first.requests == (PlanRequest.WITHDRAW_PRIOR,)
    duplicate, none = revoke_external_voicevox_reconcile_run(revoked, config=_config())
    assert duplicate == revoked
    assert none is None


def test_reconfiguration_after_inflight_probe_does_not_inspect_stale_result():
    good, _ = _positive()
    pending, token = begin_external_voicevox_reconcile_sample(
        good, launched_at_monotonic=11.0
    )
    replacement = _config(
        voices=(_voice(attribution="Edited synthetic public credit"),),
        policy_revision=_POLICY2,
    )
    replaced, revoked = revoke_external_voicevox_reconcile_run(
        pending, config=replacement
    )
    assert revoked.requests == (PlanRequest.WITHDRAW_PRIOR,)

    class Trap:
        def __getattribute__(self, key):
            pytest.fail(f"stale result attribute inspected: {key}")

    unchanged, outcome = complete_external_voicevox_reconcile_sample(
        replaced, token=token, observation=Trap(), observed_at_monotonic=Trap()
    )
    assert outcome is None
    assert unchanged == replaced
    assert unchanged.plan.retirement_pending


def test_reconciler_state_does_not_claim_cleanup_after_process_crash():
    # Even a new advisory configuration whose local sequence starts at zero
    # cannot attest Control cleanup or supply a real Worker epoch.
    new_state = _start()
    assert new_state.plan.retirement_pending is False
    assert new_state.worker_runtime_instance_epoch is None
    assert new_state.can_apply is False
    assert new_state.ready is False
    assert new_state.to_dict()["external_engine_verified"] is False


def test_no_raw_secret_in_config_revision_or_token_repr():
    cfg = _config(
        voices=(_voice(attribution="Operator statement " + _SECRET),),
        policy_revision=_POLICY2,
    )
    state = _start(cfg)
    pending, token = begin_external_voicevox_reconcile_sample(
        state, launched_at_monotonic=1.0
    )
    assert _SECRET not in repr(cfg)
    assert _SECRET not in repr(pending)
    assert _SECRET not in repr(token)
    assert _SECRET not in state.to_json()
    assert _SECRET not in pending.to_json()
    assert len(cfg.revision) == 71
