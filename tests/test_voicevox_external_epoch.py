"""#247 R4-C1: isolated deterministic qualification and fake epoch-CAS tests.

No Control RPC, PostgreSQL, Worker process, registry, native VOICEVOX, HTTP,
audio artifacts, operator approval, deployment or external network I/O.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace

import pytest

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
    AudioEpochRecord,
    ExternalAudioAuthorityError,
    ExternalAudioQualification,
    review_external_audio_qualification,
)
from astrumweaver.runtime.providers.voicevox_external_fake_epoch import (
    SyntheticAudioEpochConflict,
    SyntheticAudioEpochLedger,
)
from astrumweaver.runtime.providers.voicevox_external_plan import (
    OperatorPolicyStatus,
)
from astrumweaver.runtime.providers.voicevox_external_reconcile import (
    ExternalVoicevoxReconcileConfig,
)

_UUID = "7ffcb7ce-00ec-4bdc-82cd-45a8889e43ff"
_E1 = "00000000-0000-4000-8000-000000000001"
_E2 = "00000000-0000-4000-8000-000000000002"
_E3 = "00000000-0000-4000-8000-000000000003"
_R1 = "10000000-0000-4000-8000-000000000001"
_R2 = "10000000-0000-4000-8000-000000000002"
_R3 = "10000000-0000-4000-8000-000000000003"
_SECRET = "RAW PRIVATE: /audio_query?text=do-not-log-me"
_WORKER = "audio-cpu-a"


def _sha(n: int) -> str:
    return "sha256:" + f"{n % 16:x}" * 64


def _source(
    *,
    credit: str = "Synthetic credit",
    license_id: str = "synthetic-license",
    style: int = 8,
    port: int = 50021,
    policy: OperatorPolicyStatus = OperatorPolicyStatus.UNVERIFIED,
    policy_revision: str | None = None,
) -> ExternalVoicevoxReconcileConfig:
    endpoint = f"http://127.0.0.1:{port}"
    snapshot = _catalog(
        "0.15.0",
        [{"speaker_uuid": _UUID, "styles": [{"id": 8}]}],
        endpoint,
    )
    return ExternalVoicevoxReconcileConfig(
        enabled=True,
        discovery=VoicevoxDiscoveryConfig(
            enabled=True, endpoint=endpoint, timeout_seconds=1.0
        ),
        expected_observed_digest=snapshot.observed_digest,
        voices=(
            VoiceAlias(
                alias="narrator",
                speaker_id=_UUID,
                style_id=str(style),
                locale="ja",
                license_id=license_id,
                attribution=credit,
            ),
        ),
        policy_status=policy,
        policy_revision=_sha(11) if policy_revision is None else policy_revision,
    )


def _qual(
    *, worker: str = _WORKER, source=None, trust_rev: str | None = None
) -> ExternalAudioQualification:
    return ExternalAudioQualification(
        worker_id=worker,
        source=_source() if source is None else source,
        worker_spec_revision=_sha(1),
        namespace_revision=_sha(2),
        deployment_revision=_sha(3),
        serving_contract_revision=_sha(4),
        voice_semantic_revision=_sha(5),
        trust_evidence_revision=_sha(6) if trust_rev is None else trust_rev,
        rights_evidence_revision=_sha(7),
        privacy_evidence_revision=_sha(8),
    )


def _identity(
    *,
    epoch: str = _E1,
    intent: int = 1,
    qual: ExternalAudioQualification | None = None,
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


def _fence(
    *,
    epoch: str = _E1,
    expected_record: int = 1,
    expected_intent: int = 1,
    revoke_intent: int = 2,
    request_id: str = _R1,
) -> AudioEpochFenceIntent:
    return AudioEpochFenceIntent(
        worker_id=_WORKER,
        epoch=epoch,
        expected_record_revision=expected_record,
        expected_intent_revision=expected_intent,
        revoke_intent_revision=revoke_intent,
        request_id=request_id,
    )


def _cleanup(
    *,
    epoch: str = _E1,
    expected_record: int = 2,
    fenced_intent: int = 2,
    request_id: str = _R2,
) -> AudioEpochCleanupIntent:
    return AudioEpochCleanupIntent(
        worker_id=_WORKER,
        epoch=epoch,
        expected_record_revision=expected_record,
        expected_fenced_intent_revision=fenced_intent,
        request_id=request_id,
    )


def _ledger(
    *,
    phase: AudioEpochPhase = AudioEpochPhase.PREPARED,
    attempts: int = 0,
) -> SyntheticAudioEpochLedger:
    store = SyntheticAudioEpochLedger()
    store.seed_epoch_for_test(
        _identity(), qualification=_qual(), phase=phase, unresolved_attempts=attempts
    )
    return store


def _safe(data: object) -> None:
    text = json.dumps(data, sort_keys=True)
    for secret in (
        _SECRET,
        _UUID,
        "Synthetic credit",
        "synthetic-license",
        "127.0.0.1",
        "/audio_query?",
        "PRIVATE",
    ):
        assert secret not in text
    assert '"ready": false' in text
    assert '"can_apply": false' in text
    assert '"rights_verified": false' in text
    assert '"external_engine_verified": false' in text


def test_disabled_qualification_does_not_touch_foreign_data():
    class Trap:
        def __getattribute__(self, name):
            pytest.fail("disabled mode inspected foreign data")

        def __bool__(self):
            pytest.fail("disabled mode coerced foreign data")
            return False

    result = review_external_audio_qualification(enabled=False, declaration=Trap())
    assert result["status"] == "disabled"
    _safe(result)
    with pytest.raises(ExternalAudioAuthorityError):
        review_external_audio_qualification(enabled=1)


def test_valid_declaration_remains_unverified_and_private():
    q = _qual()
    r = review_external_audio_qualification(enabled=True, declaration=q)
    assert r["disposition"] == "unverified"
    assert r["qualification_revision"] == q.qualification_revision
    assert r["source_revision"] == q.source.revision
    assert q.ready is False and q.can_apply is False
    assert q.rights_verified is False and q.external_engine_verified is False
    assert q.source.expected_observed_digest == _source().expected_observed_digest
    assert q.source.policy_status is OperatorPolicyStatus.UNVERIFIED
    assert (
        "worker_runtime_instance_epoch" in r
        and r["worker_runtime_instance_epoch"] is None
    )
    _safe(r)
    _safe(json.loads(q.to_json()))


def test_revocation_cannot_promote_operator_declaration():
    q = _qual(source=_source(policy=OperatorPolicyStatus.REVOKED))
    assert q.disposition.value == "revoked"
    assert q.to_dict()["disposition"] == "revoked"
    _safe(q.to_dict())


@pytest.mark.parametrize(
    "change",
    [
        {"credit": "Other synthetic credit"},
        {"license_id": "another-license"},
        {"style": 9},
        {"port": 50022},
        {"policy_revision": _sha(12)},
        {"policy": OperatorPolicyStatus.REVOKED},
    ],
)
def test_private_voice_and_policy_changes_force_new_qualification_revision(change):
    q = _qual()
    changed = _qual(source=_source(**change))
    assert changed.qualification_revision != q.qualification_revision
    _safe(changed.to_dict())


@pytest.mark.parametrize(
    "field",
    [
        "worker_spec_revision",
        "namespace_revision",
        "deployment_revision",
        "serving_contract_revision",
        "voice_semantic_revision",
        "trust_evidence_revision",
        "rights_evidence_revision",
        "privacy_evidence_revision",
    ],
)
def test_qualification_binds_every_semantic_digest(field):
    q = _qual()
    assert (
        replace(q, **{field: _sha(13)}).qualification_revision
        != q.qualification_revision
    )


def test_disabled_reconcile_source_is_not_a_qualification():
    with pytest.raises(ExternalAudioAuthorityError):
        _qual(source=ExternalVoicevoxReconcileConfig())
    with pytest.raises(ExternalAudioAuthorityError):
        review_external_audio_qualification(enabled=True, declaration=object())


@pytest.mark.parametrize("bad", [True, 8, _SECRET, "sha256:" + "G" * 64, ""])
def test_invalid_qualification_revision_redacted(bad):
    with pytest.raises(ExternalAudioAuthorityError) as caught:
        replace(_qual(), trust_evidence_revision=bad)
    assert _SECRET not in str(caught.value)


def test_reflectively_mutated_declaration_rechecked_before_public_serialization():
    q = _qual()
    object.__setattr__(q, "qualification_revision", _SECRET)
    with pytest.raises(ExternalAudioAuthorityError) as exc:
        q.to_dict()
    assert _SECRET not in str(exc.value)


def test_reflectively_mutated_private_source_never_bypasses_revalidation():
    q = _qual()
    object.__setattr__(q.source, "policy_revision", _SECRET)
    with pytest.raises(ExternalAudioAuthorityError):
        q.to_dict()


def test_original_source_mutation_does_not_change_copied_qualification():
    raw = _source()
    q = _qual(source=raw)
    original = q.qualification_revision
    object.__setattr__(raw, "policy_revision", _SECRET)
    assert q.qualification_revision == original
    assert q.to_dict()["qualification_revision"] == original


@pytest.mark.parametrize(
    "bad_epoch",
    [
        "",
        "00000000-0000-0000-0000-000000000000",
        _SECRET,
        "AAAAAAAA-0000-4000-8000-000000000001",
        True,
    ],
)
def test_epoch_uuid_requires_nonzero_canonical_lowercase(bad_epoch):
    with pytest.raises(ExternalAudioAuthorityError):
        _identity(epoch=bad_epoch)


@pytest.mark.parametrize("intent", [0, -1, True, 2**63, _SECRET])
def test_identity_rejects_malformed_intent_counters(intent):
    with pytest.raises(ExternalAudioAuthorityError):
        _identity(intent=intent)


def test_epoch_record_constructor_guards_fence_fields_and_freshness():
    ident = _identity()
    with pytest.raises(ExternalAudioAuthorityError):
        AudioEpochRecord(
            identity=ident,
            phase=AudioEpochPhase.FENCED,
            record_revision=2,
            fresh_until_clock=10.0,
        )
    with pytest.raises(ExternalAudioAuthorityError):
        AudioEpochRecord(
            identity=ident,
            phase=AudioEpochPhase.PREPARED,
            record_revision=1,
            fresh_until_clock=float("nan"),
        )
    with pytest.raises(ExternalAudioAuthorityError):
        AudioEpochRecord(
            identity=ident,
            phase=AudioEpochPhase.PREPARED,
            record_revision=True,
            fresh_until_clock=10.0,
        )


def test_fake_ledger_starts_blocked_and_never_claims():
    fake = SyntheticAudioEpochLedger()
    assert fake.ready is False and fake.can_apply is False
    assert fake.control_authoritative is False
    assert fake.can_claim() is False
    assert fake.can_positive_write() is False
    assert fake.snapshot_for_test()["epochs"] == []
    _safe(fake.snapshot_for_test())


@pytest.mark.parametrize("phase", [AudioEpochPhase.PREPARED, AudioEpochPhase.ADMITTED])
def test_fake_seed_is_not_a_real_worker_admission_even_if_named_admitted(phase):
    fake = _ledger(phase=phase)
    record = fake.readback_for_test(_WORKER, _E1)
    assert record is not None and record.phase is phase
    assert record.ready is False and record.can_apply is False
    assert fake.can_claim(_WORKER, _E1) is False
    assert fake.can_positive_write(_WORKER, _E1) is False
    _safe(record.to_dict())
    _safe(fake.snapshot_for_test())


def test_fake_seed_checks_exact_qualification_binding():
    store = SyntheticAudioEpochLedger()
    q = _qual()
    wrong = _qual(source=_source(credit="different credit"))
    with pytest.raises(ExternalAudioAuthorityError):
        store.seed_epoch_for_test(_identity(qual=q), qualification=wrong)
    assert store.snapshot_for_test()["epochs"] == []
    revoked = _qual(source=_source(policy=OperatorPolicyStatus.REVOKED))
    with pytest.raises(ExternalAudioAuthorityError):
        store.seed_epoch_for_test(_identity(qual=revoked), qualification=revoked)


def test_fence_from_prepared_is_atomic_redacted_and_not_cleanup():
    store = _ledger()
    receipt = store.fence(_fence())
    assert receipt.phase is AudioEpochPhase.FENCED
    assert receipt.record_revision == 2
    assert receipt.fenced_intent_revision == 2
    assert receipt.fence_request_id == _R1
    assert store.readback_for_test(_WORKER, _E1) == receipt
    assert store.can_claim() is False
    _safe(receipt.to_dict())


def test_fence_from_synthetic_admitted_never_claims_or_promotes():
    store = _ledger(phase=AudioEpochPhase.ADMITTED)
    fenced = store.fence(_fence())
    assert fenced.phase is AudioEpochPhase.FENCED
    assert store.can_claim() is False
    assert store.can_positive_write() is False


def test_duplicate_fence_request_is_idempotent_even_after_cleanup():
    store = _ledger()
    req = _fence()
    first = store.fence(req)
    assert store.fence(req) == first
    clean = store.cleanup_complete(_cleanup())
    assert clean.phase is AudioEpochPhase.CLEANUP_COMPLETE
    assert store.fence(req) == first  # old receipt, not state rollback
    assert (
        store.readback_for_test(_WORKER, _E1).phase is AudioEpochPhase.CLEANUP_COMPLETE
    )


def test_stale_or_wrong_fence_cas_does_not_mutate_store():
    store = _ledger()
    baseline = store.snapshot_for_test()
    for bad in (
        _fence(expected_record=2),
        _fence(expected_intent=2, revoke_intent=3),
        _fence(epoch=_E2),
    ):
        with pytest.raises(SyntheticAudioEpochConflict):
            store.fence(bad)
        assert store.snapshot_for_test() == baseline


def test_idempotency_key_cannot_be_reused_for_different_operation_or_intent():
    store = _ledger()
    store.fence(_fence())
    with pytest.raises(SyntheticAudioEpochConflict):
        store.fence(_fence(request_id=_R1, revoke_intent=3))
    with pytest.raises(SyntheticAudioEpochConflict):
        store.cleanup_complete(_cleanup(request_id=_R1))


def test_new_request_id_cannot_repeat_fence_of_same_epoch():
    store = _ledger()
    store.fence(_fence())
    with pytest.raises(SyntheticAudioEpochConflict):
        store.fence(_fence(request_id=_R2))
    assert store.readback_for_test(_WORKER, _E1).record_revision == 2


def test_cleanup_is_distinct_from_fence_and_requires_no_outstanding_attempt():
    store = _ledger(attempts=2)
    store.fence(_fence())
    with pytest.raises(SyntheticAudioEpochConflict):
        store.cleanup_complete(_cleanup())
    store.resolve_attempt_for_test(worker_id=_WORKER, epoch=_E1)
    with pytest.raises(SyntheticAudioEpochConflict):
        store.cleanup_complete(_cleanup())
    store.resolve_attempt_for_test(worker_id=_WORKER, epoch=_E1)
    final = store.cleanup_complete(_cleanup())
    assert final.record_revision == 3
    assert final.phase is AudioEpochPhase.CLEANUP_COMPLETE
    assert store.cleanup_complete(_cleanup()) == final
    _safe(final.to_dict())


def test_cleanup_requires_exact_fenced_revision_and_intent():
    store = _ledger()
    store.fence(_fence())
    with pytest.raises(SyntheticAudioEpochConflict):
        store.cleanup_complete(_cleanup(expected_record=1))
    with pytest.raises(SyntheticAudioEpochConflict):
        store.cleanup_complete(_cleanup(fenced_intent=3))
    assert store.readback_for_test(_WORKER, _E1).phase is AudioEpochPhase.FENCED


def test_cleanup_cannot_precede_fence():
    store = _ledger()
    with pytest.raises(SyntheticAudioEpochConflict):
        store.cleanup_complete(_cleanup())
    with pytest.raises(SyntheticAudioEpochConflict):
        store.resolve_attempt_for_test(worker_id=_WORKER, epoch=_E1)


def test_new_epoch_requires_fenced_and_cleaned_previous_epoch():
    store = _ledger()
    next_qual = _qual()
    with pytest.raises(SyntheticAudioEpochConflict):
        store.seed_epoch_for_test(
            _identity(epoch=_E2, intent=2, qual=next_qual),
            qualification=next_qual,
        )
    store.fence(_fence())
    with pytest.raises(SyntheticAudioEpochConflict):
        store.seed_epoch_for_test(
            _identity(epoch=_E2, intent=3, qual=next_qual),
            qualification=next_qual,
        )
    store.cleanup_complete(_cleanup())
    new_record = store.seed_epoch_for_test(
        _identity(epoch=_E2, intent=3, qual=next_qual),
        qualification=next_qual,
    )
    assert new_record.phase is AudioEpochPhase.PREPARED
    assert (
        store.readback_for_test(_WORKER, _E1).phase is AudioEpochPhase.CLEANUP_COMPLETE
    )
    assert store.can_claim() is False


def test_epoch_uuid_reuse_is_rejected_after_cleanup_and_across_workers():
    store = _ledger()
    store.fence(_fence())
    store.cleanup_complete(_cleanup())
    q_other = _qual(worker="other-audio-cpu")
    with pytest.raises(SyntheticAudioEpochConflict):
        store.seed_epoch_for_test(
            _identity(epoch=_E1, qual=q_other), qualification=q_other
        )


def test_floor_revocation_before_epoch_creation_rejects_stale_seed():
    store = SyntheticAudioEpochLedger()
    store.record_intent_floor_for_test(
        worker_id=_WORKER, expected_intent_revision=0, new_intent_revision=2
    )
    with pytest.raises(SyntheticAudioEpochConflict):
        store.seed_epoch_for_test(_identity(intent=1), qualification=_qual())
    seeded = store.seed_epoch_for_test(
        _identity(epoch=_E3, intent=3), qualification=_qual()
    )
    assert seeded.identity.intent_revision == 3


def test_intent_floor_cas_fail_closed():
    store = SyntheticAudioEpochLedger()
    store.record_intent_floor_for_test(
        worker_id=_WORKER, expected_intent_revision=0, new_intent_revision=10
    )
    with pytest.raises(SyntheticAudioEpochConflict):
        store.record_intent_floor_for_test(
            worker_id=_WORKER, expected_intent_revision=0, new_intent_revision=11
        )


def test_overflow_and_bool_in_fake_fixture_fail_closed():
    store = SyntheticAudioEpochLedger()
    with pytest.raises(ExternalAudioAuthorityError):
        store.seed_epoch_for_test(
            _identity(), qualification=_qual(), unresolved_attempts=True
        )
    with pytest.raises(ExternalAudioAuthorityError):
        store.seed_epoch_for_test(
            _identity(), qualification=_qual(), fresh_until_clock=float("inf")
        )
    with pytest.raises(ExternalAudioAuthorityError):
        store.record_intent_floor_for_test(
            worker_id=_WORKER, expected_intent_revision=0, new_intent_revision=2**63
        )


def test_reflective_epoch_record_mutation_is_revalidated():
    store = _ledger()
    r = store.readback_for_test(_WORKER, _E1)
    object.__setattr__(r, "phase", _SECRET)
    with pytest.raises(ExternalAudioAuthorityError) as exc:
        r.to_dict()
    assert _SECRET not in str(exc.value)
    assert store.readback_for_test(_WORKER, _E1).phase is AudioEpochPhase.PREPARED


@pytest.mark.asyncio
async def test_authority_modules_do_not_open_network_or_start_native_process(
    monkeypatch,
):
    async def fail_open(*_args, **_kwargs):
        pytest.fail("source-only authority attempted socket I/O")

    def fail_native(*_args, **_kwargs):
        pytest.fail("source-only authority attempted native process operation")

    monkeypatch.setattr(asyncio, "open_connection", fail_open)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_native)
    monkeypatch.setattr(os, "kill", fail_native)
    store = _ledger()
    store.fence(_fence())
    store.cleanup_complete(_cleanup())
    assert store.can_claim(_WORKER, _E1) is False
    assert (
        review_external_audio_qualification(enabled=True, declaration=_qual())["ready"]
        is False
    )


def test_intent_floor_cannot_strand_existing_epoch_before_fence_or_cleanup():
    """R4-C1 review P3: existing E must transition through exact fence."""
    store = _ledger()
    with pytest.raises(SyntheticAudioEpochConflict):
        store.record_intent_floor_for_test(
            worker_id=_WORKER, expected_intent_revision=1, new_intent_revision=2
        )
    assert store.readback_for_test(_WORKER, _E1).phase is AudioEpochPhase.PREPARED
    fenced = store.fence(_fence())
    assert fenced.phase is AudioEpochPhase.FENCED
    with pytest.raises(SyntheticAudioEpochConflict):
        store.record_intent_floor_for_test(
            worker_id=_WORKER, expected_intent_revision=2, new_intent_revision=3
        )
    cleaned = store.cleanup_complete(_cleanup())
    assert cleaned.phase is AudioEpochPhase.CLEANUP_COMPLETE
    store.record_intent_floor_for_test(
        worker_id=_WORKER, expected_intent_revision=2, new_intent_revision=3
    )
    q = _qual()
    new_record = store.seed_epoch_for_test(
        _identity(epoch=_E2, intent=4, qual=q), qualification=q
    )
    assert new_record.phase is AudioEpochPhase.PREPARED
    assert (
        store.readback_for_test(_WORKER, _E1).phase is AudioEpochPhase.CLEANUP_COMPLETE
    )
    assert store.can_claim(_WORKER, _E2) is False
