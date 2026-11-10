"""#247 R4-C2-A: deterministic pure Control epoch prototype tests.

All Worker's state, Control actor, clock, Job attempts and evidence are *fake*.
No real HTTP, DB, scheduler, GPU, VOICEVOX, Worker, rights or audio data.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from astrumweaver.audio.epoch_control_synthetic import (
    SyntheticAudioControlEpochAdapter,
    SyntheticControlEpochConflict,
    SyntheticEpochReceipt,
    SyntheticPrincipal,
    SyntheticPrincipalKind,
    SyntheticWorkerWitness,
)
from astrumweaver.gateway.audio import VoiceAlias
from astrumweaver.runtime.providers.voicevox_discovery import (
    VoicevoxDiscoveryConfig,
    _catalog,
)
from astrumweaver.runtime.providers.voicevox_external_epoch import (
    AudioEpochCleanupIntent,
    AudioEpochFenceIntent,
    AudioEpochIdentity,
    AudioEpochPhase,
    ExternalAudioQualification,
)
from astrumweaver.runtime.providers.voicevox_external_plan import (
    OperatorPolicyStatus,
)
from astrumweaver.runtime.providers.voicevox_external_reconcile import (
    ExternalVoicevoxReconcileConfig,
)

_E1 = "00000000-0000-4000-8000-000000000101"
_E2 = "00000000-0000-4000-8000-000000000102"
_E3 = "00000000-0000-4000-8000-000000000103"
_R1 = "10000000-0000-4000-8000-000000000101"
_R2 = "10000000-0000-4000-8000-000000000102"
_R3 = "10000000-0000-4000-8000-000000000103"
_R4 = "10000000-0000-4000-8000-000000000104"
_R5 = "10000000-0000-4000-8000-000000000105"
_VOICE_UUID = "7ffcb7ce-00ec-4bdc-82cd-45a8889e43ff"
_SECRET = "private-input-/audio_query?text=confidential"
_WORKER = "audio-cpu-a"


def _sha(n: int) -> str:
    return "sha256:" + f"{n % 16:x}" * 64


def _qual(
    *,
    worker_id: str = _WORKER,
    policy: OperatorPolicyStatus = OperatorPolicyStatus.UNVERIFIED,
    credit: str = "synthetic-credit",
) -> ExternalAudioQualification:
    endpoint = "http://127.0.0.1:50021"
    catalog = _catalog(
        "0.15.0",
        [{"speaker_uuid": _VOICE_UUID, "styles": [{"id": 8}]}],
        endpoint,
    )
    config = ExternalVoicevoxReconcileConfig(
        enabled=True,
        discovery=VoicevoxDiscoveryConfig(enabled=True, endpoint=endpoint),
        expected_observed_digest=catalog.observed_digest,
        voices=(
            VoiceAlias(
                alias="synthetic-narrator",
                speaker_id=_VOICE_UUID,
                style_id="8",
                locale="ja",
                license_id="fictional-license",
                attribution=credit,
            ),
        ),
        policy_status=policy,
        policy_revision=_sha(9),
    )
    return ExternalAudioQualification(
        worker_id=worker_id,
        source=config,
        worker_spec_revision=_sha(1),
        namespace_revision=_sha(2),
        deployment_revision=_sha(3),
        serving_contract_revision=_sha(4),
        voice_semantic_revision=_sha(5),
        trust_evidence_revision=_sha(6),
        rights_evidence_revision=_sha(7),
        privacy_evidence_revision=_sha(8),
    )


def _identity(
    qual: ExternalAudioQualification | None = None,
    *,
    epoch: str = _E1,
    intent: int = 1,
) -> AudioEpochIdentity:
    q = _qual() if qual is None else qual
    return AudioEpochIdentity(
        worker_id=q.worker_id,
        epoch=epoch,
        qualification_revision=q.qualification_revision,
        worker_spec_revision=q.worker_spec_revision,
        deployment_revision=q.deployment_revision,
        serving_contract_revision=q.serving_contract_revision,
        voice_semantic_revision=q.voice_semantic_revision,
        intent_revision=intent,
    )


def _worker_actor(wid: str = _WORKER) -> SyntheticPrincipal:
    return SyntheticPrincipal(SyntheticPrincipalKind.WORKER, wid)


def _operator(wid: str = _WORKER) -> SyntheticPrincipal:
    return SyntheticPrincipal(SyntheticPrincipalKind.OPERATOR, wid)


def _witness(
    ident: AudioEpochIdentity | None = None, **override
) -> SyntheticWorkerWitness:
    x = _identity() if ident is None else ident
    data = {
        "worker_id": x.worker_id,
        "runtime_instance_epoch": x.epoch,
        "worker_spec_revision": x.worker_spec_revision,
        "deployment_revision": x.deployment_revision,
        "serving_contract_revision": x.serving_contract_revision,
        "voice_semantic_revision": x.voice_semantic_revision,
        "online": True,
        "supervisor_ready": True,
        "fresh_until_clock": 15.0,
    }
    data.update(override)
    return SyntheticWorkerWitness(**data)


def _prepare(
    adapter: SyntheticAudioControlEpochAdapter,
    *,
    q: ExternalAudioQualification | None = None,
    epoch: str = _E1,
    intent: int = 1,
    request_id: str = _R1,
    actor: SyntheticPrincipal | None = None,
    expected_floor: int = 0,
    now: float = 1.0,
    fresh_until: float = 12.0,
) -> SyntheticEpochReceipt:
    qual = _qual() if q is None else q
    return adapter.prepare_for_test(
        actor=_worker_actor(qual.worker_id) if actor is None else actor,
        identity=_identity(qual, epoch=epoch, intent=intent),
        qualification=qual,
        request_id=request_id,
        expected_intent_revision=expected_floor,
        now_clock=now,
        fresh_until_clock=fresh_until,
    )


def _activate(
    adapter: SyntheticAudioControlEpochAdapter,
    *,
    ident: AudioEpochIdentity | None = None,
    actor: SyntheticPrincipal | None = None,
    witness: SyntheticWorkerWitness | None = None,
    request_id: str = _R2,
    expected_rev: int = 1,
    expected_intent: int = 1,
    now: float = 2.0,
    test_gate: bool = True,
) -> SyntheticEpochReceipt:
    identity = _identity() if ident is None else ident
    return adapter.activate_for_test(
        actor=_worker_actor(identity.worker_id) if actor is None else actor,
        identity=identity,
        witness=_witness(identity) if witness is None else witness,
        request_id=request_id,
        expected_record_revision=expected_rev,
        expected_intent_revision=expected_intent,
        now_clock=now,
        synthetic_gate=test_gate,
    )


def _fence(
    adapter: SyntheticAudioControlEpochAdapter,
    *,
    epoch: str = _E1,
    expected_rev: int = 2,
    expected_intent: int = 1,
    revoke_intent: int = 2,
    request_id: str = _R3,
    actor: SyntheticPrincipal | None = None,
) -> SyntheticEpochReceipt:
    req = AudioEpochFenceIntent(
        worker_id=_WORKER,
        epoch=epoch,
        expected_record_revision=expected_rev,
        expected_intent_revision=expected_intent,
        revoke_intent_revision=revoke_intent,
        request_id=request_id,
    )
    return adapter.fence_for_test(
        actor=_worker_actor() if actor is None else actor, request=req
    )


def _cleanup(
    adapter: SyntheticAudioControlEpochAdapter,
    *,
    epoch: str = _E1,
    expected_rev: int = 3,
    expected_intent: int = 2,
    request_id: str = _R4,
    actor: SyntheticPrincipal | None = None,
    outputs_revoked: bool = True,
) -> SyntheticEpochReceipt:
    req = AudioEpochCleanupIntent(
        worker_id=_WORKER,
        epoch=epoch,
        expected_record_revision=expected_rev,
        expected_fenced_intent_revision=expected_intent,
        request_id=request_id,
    )
    return adapter.cleanup_for_test(
        actor=_operator() if actor is None else actor,
        request=req,
        outputs_revoked_for_test=outputs_revoked,
    )


def _prepared() -> SyntheticAudioControlEpochAdapter:
    a = SyntheticAudioControlEpochAdapter()
    _prepare(a)
    return a


def _admitted() -> SyntheticAudioControlEpochAdapter:
    a = _prepared()
    _activate(a)
    return a


def _redacted(value) -> None:
    text = json.dumps(value, sort_keys=True)
    for secret in (
        _VOICE_UUID,
        "127.0.0.1",
        "synthetic-credit",
        "fictional-license",
        _SECRET,
        "/audio_query?text=",
        _E1,
        _E2,
        _E3,
        "Bearer",
    ):
        assert secret not in text
    assert '"ready": false' in text
    assert '"can_apply": false' in text
    assert '"rights_verified": false' in text
    assert '"external_engine_verified": false' in text


def test_default_denies_all_real_authority_and_has_no_epochs():
    a = SyntheticAudioControlEpochAdapter()
    assert a.ready is False
    assert a.can_apply is False
    assert a.control_authoritative is False
    assert a.can_claim() is False
    assert a.can_positive_write() is False
    assert a.report_for_test()["epochs"] == []
    _redacted(a.report_for_test())


def test_prototype_can_prepare_and_admit_synthetic_state_but_no_real_claim():
    a = SyntheticAudioControlEpochAdapter()
    prepared = _prepare(a)
    assert prepared.operation == "prepare"
    assert prepared.record.phase is AudioEpochPhase.PREPARED
    assert prepared.record.record_revision == 1
    assert (
        a.readback_for_test(actor=_worker_actor(), worker_id=_WORKER, epoch=_E1)
        == prepared.record
    )
    _redacted(prepared.to_dict())
    _redacted(a.report_for_test())
    admitted = _activate(a)
    assert admitted.record.phase is AudioEpochPhase.ADMITTED
    assert admitted.record.record_revision == 2
    assert admitted.control_authoritative is False
    assert admitted.ready is False
    assert a.can_claim(_WORKER, _E1) is False
    assert a.can_positive_write(_WORKER, _E1) is False
    _redacted(admitted.to_dict())
    _redacted(a.report_for_test())


def test_fake_activation_gate_is_disabled_by_default():
    a = _prepared()
    with pytest.raises(SyntheticControlEpochConflict):
        _activate(a, test_gate=False)
    assert (
        a.readback_for_test(actor=_worker_actor(), worker_id=_WORKER, epoch=_E1).phase
        is AudioEpochPhase.PREPARED
    )


@pytest.mark.parametrize(
    "flags",
    [
        {"online": False},
        {"supervisor_ready": False},
        {"fresh_until_clock": 2.0},
        {"fresh_until_clock": 0.0},
    ],
)
def test_fake_activation_rejects_health_or_expiry(flags):
    a = _prepared()
    with pytest.raises(SyntheticControlEpochConflict):
        _activate(a, witness=_witness(**flags))
    assert a.can_claim() is False


@pytest.mark.parametrize(
    "wrong",
    [
        {"worker_id": "other-worker"},
        {"runtime_instance_epoch": _E2},
        {"worker_spec_revision": _sha(7)},
        {"deployment_revision": _sha(8)},
        {"serving_contract_revision": _sha(7)},
        {"voice_semantic_revision": _sha(8)},
    ],
)
def test_worker_snapshot_identity_mismatch_is_denied(wrong):
    a = _prepared()
    with pytest.raises(SyntheticControlEpochConflict):
        _activate(a, witness=_witness(**wrong))
    assert (
        a.readback_for_test(actor=_operator(), worker_id=_WORKER, epoch=_E1).phase
        is AudioEpochPhase.PREPARED
    )


def test_prepare_rejects_foreign_qualification_and_revoked_policy():
    a = SyntheticAudioControlEpochAdapter()
    q = _qual()
    with pytest.raises(SyntheticControlEpochConflict):
        a.prepare_for_test(
            actor=_worker_actor(),
            identity=_identity(q),
            qualification=_qual(credit="other-credit"),
            request_id=_R1,
            expected_intent_revision=0,
            now_clock=1.0,
            fresh_until_clock=12.0,
        )
    assert a.report_for_test()["epochs"] == []
    revoked = _qual(policy=OperatorPolicyStatus.REVOKED)
    with pytest.raises(SyntheticControlEpochConflict):
        _prepare(a, q=revoked)


def test_principal_scope_is_explicit_but_never_real_authentication():
    a = SyntheticAudioControlEpochAdapter()
    with pytest.raises(SyntheticControlEpochConflict):
        _prepare(a, actor=_worker_actor("other-worker"))
    _prepare(a)
    with pytest.raises(SyntheticControlEpochConflict):
        _activate(a, actor=_worker_actor("other-worker"))
    with pytest.raises(SyntheticControlEpochConflict):
        a.readback_for_test(
            actor=_operator("other-worker"), worker_id=_WORKER, epoch=_E1
        )
    assert a.can_claim() is False


def test_operator_scope_required_to_cleanup_and_exact_attempt_count():
    a = _admitted()
    a.set_outstanding_for_test(
        actor=_operator(),
        worker_id=_WORKER,
        epoch=_E1,
        expected_count=0,
        new_count=2,
    )
    _fence(a)
    with pytest.raises(SyntheticControlEpochConflict):
        _cleanup(a, actor=_worker_actor())
    with pytest.raises(SyntheticControlEpochConflict):
        _cleanup(a, outputs_revoked=False)
    # Cannot increase old work after fence.
    with pytest.raises(SyntheticControlEpochConflict):
        a.set_outstanding_for_test(
            actor=_operator(),
            worker_id=_WORKER,
            epoch=_E1,
            expected_count=2,
            new_count=3,
        )
    with pytest.raises(SyntheticControlEpochConflict):
        _cleanup(a)
    a.set_outstanding_for_test(
        actor=_operator(),
        worker_id=_WORKER,
        epoch=_E1,
        expected_count=2,
        new_count=1,
    )
    with pytest.raises(SyntheticControlEpochConflict):
        _cleanup(a)
    a.set_outstanding_for_test(
        actor=_operator(),
        worker_id=_WORKER,
        epoch=_E1,
        expected_count=1,
        new_count=0,
    )
    receipt = _cleanup(a)
    assert receipt.record.phase is AudioEpochPhase.CLEANUP_COMPLETE
    assert receipt.record.record_revision == 4
    _redacted(receipt.to_dict())


def test_fence_ack_is_separate_from_cleanup_ack_and_old_retry_is_snapshot_only():
    a = _admitted()
    prepared = a.receipt_for_test(
        actor=_worker_actor(), worker_id=_WORKER, epoch=_E1, request_id=_R1
    )
    activated = a.receipt_for_test(
        actor=_worker_actor(), worker_id=_WORKER, epoch=_E1, request_id=_R2
    )
    assert prepared is not None and prepared.record.phase is AudioEpochPhase.PREPARED
    assert activated is not None and activated.record.phase is AudioEpochPhase.ADMITTED
    fenced = _fence(a)
    assert fenced.record.phase is AudioEpochPhase.FENCED
    assert a.can_positive_write() is False
    cleaned = _cleanup(a)
    assert cleaned.record.phase is AudioEpochPhase.CLEANUP_COMPLETE
    assert (
        a.readback_for_test(actor=_operator(), worker_id=_WORKER, epoch=_E1).phase
        is AudioEpochPhase.CLEANUP_COMPLETE
    )
    assert _fence(a) == fenced
    assert _cleanup(a) == cleaned
    assert _activate(a) == activated
    assert (
        a.readback_for_test(actor=_operator(), worker_id=_WORKER, epoch=_E1).phase
        is AudioEpochPhase.CLEANUP_COMPLETE
    )


def test_wrong_request_id_fails_even_for_same_commit_target():
    a = _admitted()
    _fence(a)
    with pytest.raises(SyntheticControlEpochConflict):
        _fence(a, request_id=_R5)
    with pytest.raises(SyntheticControlEpochConflict):
        _fence(a, request_id=_R2)
    with pytest.raises(SyntheticControlEpochConflict):
        _cleanup(a, request_id=_R3)
    assert (
        a.readback_for_test(actor=_operator(), worker_id=_WORKER, epoch=_E1).phase
        is AudioEpochPhase.FENCED
    )


def test_reused_request_id_with_changed_payload_fails_without_state_change():
    a = _prepared()
    baseline = a.report_for_test()
    with pytest.raises(SyntheticControlEpochConflict):
        _prepare(a, fresh_until=14.0)
    assert a.report_for_test() == baseline
    _activate(a)
    with pytest.raises(SyntheticControlEpochConflict):
        _activate(a, now=3.0)
    assert (
        a.readback_for_test(actor=_worker_actor(), worker_id=_WORKER, epoch=_E1).phase
        is AudioEpochPhase.ADMITTED
    )


@pytest.mark.parametrize("bad", [True, 1.5, -1, 2**63])
def test_revision_counters_fail_closed(bad):
    a = SyntheticAudioControlEpochAdapter()
    with pytest.raises(SyntheticControlEpochConflict):
        _prepare(a, expected_floor=bad)
    assert not a.report_for_test()["epochs"]


@pytest.mark.parametrize(
    "now,until", [(2.0, 2.0), (9.0, 2.0), (float("nan"), 10.0), (1.0, float("inf"))]
)
def test_prepare_clock_must_be_strictly_bounded_and_fresh(now, until):
    a = SyntheticAudioControlEpochAdapter()
    with pytest.raises(SyntheticControlEpochConflict):
        _prepare(a, now=now, fresh_until=until)


def test_activation_at_control_deadline_after_wait_denies():
    a = _prepared()
    with pytest.raises(SyntheticControlEpochConflict):
        _activate(a, now=12.0)
    with pytest.raises(SyntheticControlEpochConflict):
        _activate(a, now=15.0)
    assert (
        a.readback_for_test(actor=_worker_actor(), worker_id=_WORKER, epoch=_E1).phase
        is AudioEpochPhase.PREPARED
    )


def test_new_epoch_cannot_prepare_before_old_cleanup_and_uuid_never_reused():
    a = _admitted()
    q2 = _qual()
    with pytest.raises(SyntheticControlEpochConflict):
        _prepare(a, q=q2, epoch=_E2, intent=2, expected_floor=1, request_id=_R5)
    _fence(a)
    with pytest.raises(SyntheticControlEpochConflict):
        _prepare(a, q=q2, epoch=_E2, intent=3, expected_floor=2, request_id=_R5)
    _cleanup(a)
    prepared_e2 = _prepare(
        a, q=q2, epoch=_E2, intent=3, expected_floor=2, request_id=_R5
    )
    assert prepared_e2.record.phase is AudioEpochPhase.PREPARED
    assert (
        a.readback_for_test(actor=_operator(), worker_id=_WORKER, epoch=_E1).phase
        is AudioEpochPhase.CLEANUP_COMPLETE
    )
    with pytest.raises(SyntheticControlEpochConflict):
        _prepare(
            a,
            q=_qual(worker_id="second-worker"),
            epoch=_E1,
            intent=1,
            expected_floor=0,
            request_id="10000000-0000-4000-8000-000000000106",
        )


def test_prepare_fence_from_prepared_and_then_cleanup():
    a = _prepared()
    fenced = _fence(a, expected_rev=1)
    assert fenced.record.record_revision == 2
    assert fenced.record.phase is AudioEpochPhase.FENCED
    clean = _cleanup(a, expected_rev=2)
    assert clean.record.record_revision == 3
    assert a.can_claim() is False


def test_cannot_cleanup_before_fence_or_without_explicit_output_revocation():
    a = _prepared()
    with pytest.raises(SyntheticControlEpochConflict):
        _cleanup(a, expected_rev=1)
    _fence(a, expected_rev=1)
    with pytest.raises(SyntheticControlEpochConflict):
        _cleanup(a, expected_rev=2, outputs_revoked=False)
    assert (
        a.readback_for_test(actor=_operator(), worker_id=_WORKER, epoch=_E1).phase
        is AudioEpochPhase.FENCED
    )


def test_ambiguous_ack_exact_readback_then_idempotent_replay():
    a = _prepared()
    first = _activate(a)
    # Treat response as lost after commit; read the precise request receipt.
    recovered = a.receipt_for_test(
        actor=_worker_actor(), worker_id=_WORKER, epoch=_E1, request_id=_R2
    )
    assert recovered == first
    assert _activate(a) == first
    assert (
        a.readback_for_test(
            actor=_worker_actor(), worker_id=_WORKER, epoch=_E1
        ).record_revision
        == 2
    )
    assert a.can_claim() is False


def test_duplicate_worker_identity_after_old_cleanup_needs_new_epoch():
    a = _admitted()
    _fence(a)
    _cleanup(a)
    with pytest.raises(SyntheticControlEpochConflict):
        _prepare(a, epoch=_E1, intent=3, request_id=_R5, expected_floor=2)
    fresh = _prepare(a, epoch=_E3, intent=3, request_id=_R5, expected_floor=2)
    assert fresh.record.identity.epoch == _E3


def test_late_fence_cannot_restore_old_admission():
    a = _admitted()
    old_activate = a.receipt_for_test(
        actor=_worker_actor(), worker_id=_WORKER, epoch=_E1, request_id=_R2
    )
    _fence(a)
    assert _activate(a) == old_activate
    assert (
        a.readback_for_test(actor=_worker_actor(), worker_id=_WORKER, epoch=_E1).phase
        is AudioEpochPhase.FENCED
    )
    assert a.can_claim() is False


def test_outstanding_count_is_exact_cas_not_trusted_cleanup_source():
    a = _admitted()
    with pytest.raises(SyntheticControlEpochConflict):
        a.set_outstanding_for_test(
            actor=_worker_actor(),
            worker_id=_WORKER,
            epoch=_E1,
            expected_count=0,
            new_count=1,
        )
    a.set_outstanding_for_test(
        actor=_operator(), worker_id=_WORKER, epoch=_E1, expected_count=0, new_count=1
    )
    with pytest.raises(SyntheticControlEpochConflict):
        a.set_outstanding_for_test(
            actor=_operator(),
            worker_id=_WORKER,
            epoch=_E1,
            expected_count=0,
            new_count=2,
        )
    _fence(a)
    with pytest.raises(SyntheticControlEpochConflict):
        _cleanup(a)
    a.set_outstanding_for_test(
        actor=_operator(), worker_id=_WORKER, epoch=_E1, expected_count=1, new_count=0
    )
    _cleanup(a)


def test_receipts_are_scoped_and_missing_request_does_not_prove_no_commit():
    a = _prepared()
    assert (
        a.receipt_for_test(
            actor=_worker_actor(), worker_id=_WORKER, epoch=_E1, request_id=_R5
        )
        is None
    )
    with pytest.raises(SyntheticControlEpochConflict):
        a.receipt_for_test(
            actor=_worker_actor("another-worker"),
            worker_id=_WORKER,
            epoch=_E1,
            request_id=_R1,
        )
    with pytest.raises(SyntheticControlEpochConflict):
        a.receipt_for_test(
            actor=_worker_actor(), worker_id=_WORKER, epoch=_E2, request_id=_R1
        )
    assert a.can_claim() is False


def test_readback_defensive_copy_after_reflective_mutation():
    a = _prepared()
    returned = a.readback_for_test(actor=_worker_actor(), worker_id=_WORKER, epoch=_E1)
    object.__setattr__(returned, "phase", "hacked")
    assert (
        a.readback_for_test(actor=_worker_actor(), worker_id=_WORKER, epoch=_E1).phase
        is AudioEpochPhase.PREPARED
    )
    assert a.can_positive_write() is False


def test_reflective_actor_and_witness_mutations_denied():
    a = _prepared()
    actor = _worker_actor()
    object.__setattr__(actor, "worker_id", _SECRET)
    with pytest.raises(SyntheticControlEpochConflict) as error:
        _activate(a, actor=actor)
    assert _SECRET not in str(error.value)
    witness = _witness()
    object.__setattr__(witness, "voice_semantic_revision", _SECRET)
    with pytest.raises(SyntheticControlEpochConflict) as error:
        _activate(a, witness=witness)
    assert _SECRET not in str(error.value)


def test_mutated_receipt_cannot_leak_epoch_or_original_source():
    a = _prepared()
    receipt = _activate(a)
    _redacted(receipt.to_dict())
    object.__setattr__(receipt, "operation", _SECRET)
    with pytest.raises(SyntheticControlEpochConflict) as err:
        receipt.to_dict()
    assert _SECRET not in str(err.value)


def test_fake_snapshot_is_always_redacted_even_after_cleanup():
    a = _admitted()
    _fence(a)
    _cleanup(a)
    _redacted(a.report_for_test())
    report = a.report_for_test()
    assert report["control_authoritative"] is False
    assert report["epochs"][0]["phase"] == AudioEpochPhase.CLEANUP_COMPLETE.value
    assert report["epochs"][0]["ready"] is False


@pytest.mark.asyncio
async def test_c2a_source_does_not_create_network_or_native_process(monkeypatch):
    async def no_connect(*_args, **_kwargs):
        pytest.fail("source-only adapter used network")

    def no_native(*_args, **_kwargs):
        pytest.fail("source-only adapter used native process")

    monkeypatch.setattr(asyncio, "open_connection", no_connect)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", no_connect)
    monkeypatch.setattr(os, "kill", no_native)
    a = _admitted()
    _fence(a)
    _cleanup(a)
    assert a.can_claim(_WORKER, _E1) is False
    assert a.can_positive_write(_WORKER, _E1) is False


def test_prepared_and_fenced_epochs_cannot_acquire_simulated_running_attempts():
    a = _prepared()
    for phase in ("prepared", "fenced"):
        with pytest.raises(SyntheticControlEpochConflict):
            a.set_outstanding_for_test(
                actor=_operator(),
                worker_id=_WORKER,
                epoch=_E1,
                expected_count=0,
                new_count=1,
            )
        if phase == "prepared":
            _fence(a, expected_rev=1)
    # No positive attempt or claim can be manufactured by either path.
    assert a.can_claim(_WORKER, _E1) is False
    assert a.can_positive_write(_WORKER, _E1) is False
    _cleanup(a, expected_rev=2)


@pytest.mark.parametrize("order", ["fence-first", "activate-first"])
def test_activate_fence_order_has_no_post_fence_revival(order):
    a = _prepared()
    if order == "fence-first":
        fenced = _fence(a, expected_rev=1)
        with pytest.raises(SyntheticControlEpochConflict):
            _activate(a)
        _cleanup(a, expected_rev=2)
    else:
        admitted = _activate(a)
        assert admitted.record.phase is AudioEpochPhase.ADMITTED
        fenced = _fence(a, expected_rev=2)
        with pytest.raises(SyntheticControlEpochConflict):
            _activate(a, request_id=_R5)
        _cleanup(a, expected_rev=3)
    assert fenced.record.phase is AudioEpochPhase.FENCED
    # Its original record/receipt is retained without mutating current state.
    assert _fence(a, expected_rev=fenced.record.record_revision - 1) == fenced
    assert (
        a.readback_for_test(actor=_worker_actor(), worker_id=_WORKER, epoch=_E1).phase
        is AudioEpochPhase.CLEANUP_COMPLETE
    )
    assert a.can_claim(_WORKER, _E1) is False


def test_no_worker_provenance_or_qualification_is_granted_by_simulated_scope():
    a = _prepared()
    # Construction of the fake operator is not authentication by Control.
    fake_operator = _operator()
    assert fake_operator.worker_id == _WORKER
    assert a.control_authoritative is False
    _activate(a, actor=fake_operator)
    assert a.ready is False
    assert a.can_apply is False
    assert a.can_positive_write() is False
    assert a.report_for_test()["rights_verified"] is False


def test_unknown_worker_readback_absence_is_not_a_commit_receipt():
    a = SyntheticAudioControlEpochAdapter()
    assert (
        a.readback_for_test(actor=_worker_actor(), worker_id=_WORKER, epoch=_E1) is None
    )
    assert (
        a.receipt_for_test(
            actor=_worker_actor(), worker_id=_WORKER, epoch=_E1, request_id=_R1
        )
        is None
    )
    assert a.control_authoritative is False


def test_prepared_expired_freshness_cannot_be_resurrected_by_new_request():
    a = _prepared()
    with pytest.raises(SyntheticControlEpochConflict):
        _activate(a, now=12.01, request_id=_R5)
    with pytest.raises(SyntheticControlEpochConflict):
        _activate(a, now=12.00)
    assert (
        a.readback_for_test(actor=_operator(), worker_id=_WORKER, epoch=_E1).phase
        is AudioEpochPhase.PREPARED
    )
    # Already prepared records are still retractable (no claim or timeout grant).
    _fence(a, expected_rev=1)
    _cleanup(a, expected_rev=2)
