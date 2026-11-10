"""#247 R4-C1: disposable in-memory fake Control epoch ledger for synthetic tests.

This is deliberately NOT the real Control repository, a PostgreSQL substitute,
a Worker health provider, a serving registry, or a production admission gate.
Only explicit private synthetic fixtures can seed PREPARED/ADMITTED records.
All real prepare/activate/claim/readiness methods are absent or denied.
"""

from __future__ import annotations

from dataclasses import replace
from threading import RLock

from .voicevox_external_epoch import (
    AudioEpochCleanupIntent,
    AudioEpochFenceIntent,
    AudioEpochIdentity,
    AudioEpochPhase,
    AudioEpochRecord,
    ExternalAudioAuthorityError,
    ExternalAudioQualification,
    QualificationDisposition,
    _checked_cleanup_intent,
    _checked_epoch_record,
    _checked_fence_intent,
    _checked_identity,
    _checked_qualification,
    _counter,
    _worker,
)


class SyntheticAudioEpochConflict(ExternalAudioAuthorityError):
    """Fixed redacted synthetic CAS/idempotency conflict."""

    def __init__(self) -> None:
        ValueError.__init__(self, "VOICEVOX synthetic epoch transition conflicts")


class SyntheticAudioEpochLedger:
    """Single-process deterministic fake for CAS, revocation and cleanup.

    State is intentionally non-durable. It NEVER signs, authenticates or admits
    any audio Worker, despite using future state labels as synthetic fixtures.
    Real per-epoch durable locks/claims and job-lease checks belong to R4-C2.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._records: dict[tuple[str, str], AudioEpochRecord] = {}
        self._intent_revision: dict[str, int] = {}
        self._used_epochs: set[str] = set()
        self._idempotency: dict[str, tuple[object, AudioEpochRecord]] = {}
        self._pending_attempts: dict[tuple[str, str], int] = {}

    @property
    def ready(self) -> bool:
        return False

    @property
    def can_apply(self) -> bool:
        return False

    @property
    def control_authoritative(self) -> bool:
        return False

    def can_claim(self, *_args: object, **_kwargs: object) -> bool:
        """Never allow actual claim from an in-memory simulation."""
        return False

    def can_positive_write(self, *_args: object, **_kwargs: object) -> bool:
        return False

    def record_intent_floor_for_test(
        self,
        *,
        worker_id: str,
        expected_intent_revision: int,
        new_intent_revision: int,
    ) -> None:
        """Represent a local withdrawal before a fake epoch exists.

        A stale synthetic prepare/seed with an older intent can never follow.
        No production reviewer may accept this volatile floor as durable proof.
        """
        if (
            not _worker(worker_id)
            or not _counter(expected_intent_revision)
            or not _counter(new_intent_revision, minimum=1)
            or new_intent_revision <= expected_intent_revision
        ):
            raise ExternalAudioAuthorityError()
        with self._lock:
            if self._intent_revision.get(
                worker_id, 0
            ) != expected_intent_revision or any(
                record.identity.worker_id == worker_id
                and record.phase is not AudioEpochPhase.CLEANUP_COMPLETE
                for record in self._records.values()
            ):
                # This helper is only for a pre-epoch withdrawal. Once E
                # exists, the exact E must be fenced before advancing intent.
                raise SyntheticAudioEpochConflict()
            self._intent_revision[worker_id] = new_intent_revision

    def seed_epoch_for_test(
        self,
        identity: AudioEpochIdentity,
        *,
        qualification: ExternalAudioQualification,
        phase: AudioEpochPhase = AudioEpochPhase.PREPARED,
        fresh_until_clock: float = 10.0,
        unresolved_attempts: int = 0,
    ) -> AudioEpochRecord:
        """Fixture injection only. It cannot register or qualify a Worker.

        Simulates an existing Control record so we can test withdrawal without
        implementing or invoking prepare/activate Control operations.
        """
        checked = _checked_identity(identity)
        reviewed = _checked_qualification(qualification)
        if (
            reviewed.disposition is not QualificationDisposition.UNVERIFIED
            or checked.worker_id != reviewed.worker_id
            or checked.qualification_revision != reviewed.qualification_revision
            or checked.worker_spec_revision != reviewed.worker_spec_revision
            or checked.deployment_revision != reviewed.deployment_revision
            or checked.serving_contract_revision != reviewed.serving_contract_revision
            or checked.voice_semantic_revision != reviewed.voice_semantic_revision
            or type(phase) is not AudioEpochPhase
            or phase not in (AudioEpochPhase.PREPARED, AudioEpochPhase.ADMITTED)
            or not _counter(unresolved_attempts)
        ):
            raise ExternalAudioAuthorityError()
        candidate = AudioEpochRecord(
            identity=checked,
            phase=phase,
            record_revision=1,
            fresh_until_clock=fresh_until_clock,
        )
        key = (checked.worker_id, checked.epoch)
        with self._lock:
            if (
                checked.epoch in self._used_epochs
                or key in self._records
                or checked.intent_revision
                != self._intent_revision.get(checked.worker_id, 0) + 1
                or any(
                    record.identity.worker_id == checked.worker_id
                    and record.phase is not AudioEpochPhase.CLEANUP_COMPLETE
                    for record in self._records.values()
                )
            ):
                raise SyntheticAudioEpochConflict()
            self._used_epochs.add(checked.epoch)
            self._records[key] = candidate
            self._intent_revision[checked.worker_id] = checked.intent_revision
            self._pending_attempts[key] = unresolved_attempts
        return _checked_epoch_record(candidate)

    def readback_for_test(self, worker_id: str, epoch: str) -> AudioEpochRecord | None:
        """Return a defensive immutable snapshot, not a Control ACK."""
        if not _worker(worker_id):
            raise ExternalAudioAuthorityError()
        # Validate epoch without looking up arbitrary untrusted keys.
        from .voicevox_external_epoch import _uuid

        if not _uuid(epoch):
            raise ExternalAudioAuthorityError()
        with self._lock:
            current = self._records.get((worker_id, epoch))
            return None if current is None else _checked_epoch_record(current)

    def fence(self, request: AudioEpochFenceIntent) -> AudioEpochRecord:
        """One linearized fake CAS; a fence reply is not cleanup or permission."""
        checked = _checked_fence_intent(request)
        key = (checked.worker_id, checked.epoch)
        with self._lock:
            stored = self._idempotency.get(checked.request_id)
            if stored is not None:
                if stored[0] != checked:
                    raise SyntheticAudioEpochConflict()
                return _checked_epoch_record(stored[1])
            current = self._records.get(key)
            if (
                current is None
                or current.phase
                not in (AudioEpochPhase.PREPARED, AudioEpochPhase.ADMITTED)
                or current.record_revision != checked.expected_record_revision
                or self._intent_revision.get(checked.worker_id, 0)
                != checked.expected_intent_revision
                or current.identity.intent_revision != checked.expected_intent_revision
                or not _counter(current.record_revision + 1, minimum=1)
            ):
                raise SyntheticAudioEpochConflict()
            next_record = replace(
                current,
                phase=AudioEpochPhase.FENCED,
                record_revision=current.record_revision + 1,
                fenced_intent_revision=checked.revoke_intent_revision,
                fence_request_id=checked.request_id,
            )
            self._records[key] = next_record
            self._intent_revision[checked.worker_id] = checked.revoke_intent_revision
            self._idempotency[checked.request_id] = (checked, next_record)
            return _checked_epoch_record(next_record)

    def resolve_attempt_for_test(self, *, worker_id: str, epoch: str) -> None:
        """Simulate exactly one old attempt's *negative* terminal accounting."""
        current = self.readback_for_test(worker_id, epoch)
        if current is None:
            raise SyntheticAudioEpochConflict()
        key = (worker_id, epoch)
        with self._lock:
            if (
                self._records[key].phase is not AudioEpochPhase.FENCED
                or self._pending_attempts.get(key, 0) <= 0
            ):
                raise SyntheticAudioEpochConflict()
            self._pending_attempts[key] -= 1

    def cleanup_complete(self, request: AudioEpochCleanupIntent) -> AudioEpochRecord:
        """Fake final accounting. Never asserts physical deletion/native stop."""
        checked = _checked_cleanup_intent(request)
        key = (checked.worker_id, checked.epoch)
        with self._lock:
            stored = self._idempotency.get(checked.request_id)
            if stored is not None:
                if stored[0] != checked:
                    raise SyntheticAudioEpochConflict()
                return _checked_epoch_record(stored[1])
            current = self._records.get(key)
            if (
                current is None
                or current.phase is not AudioEpochPhase.FENCED
                or current.record_revision != checked.expected_record_revision
                or current.fenced_intent_revision
                != checked.expected_fenced_intent_revision
                or self._intent_revision.get(checked.worker_id, 0)
                != checked.expected_fenced_intent_revision
                or self._pending_attempts.get(key, 0) != 0
                or not _counter(current.record_revision + 1, minimum=1)
            ):
                raise SyntheticAudioEpochConflict()
            next_record = replace(
                current,
                phase=AudioEpochPhase.CLEANUP_COMPLETE,
                record_revision=current.record_revision + 1,
            )
            self._records[key] = next_record
            self._idempotency[checked.request_id] = (checked, next_record)
            return _checked_epoch_record(next_record)

    def snapshot_for_test(self) -> dict[str, object]:
        """Redacted fixture summary, no real epoch/qualification authority."""
        with self._lock:
            return {
                "schema_version": "voicevox-synthetic-epoch-ledger-v1",
                "synthetic_only": True,
                "epochs": [r.to_dict() for _, r in sorted(self._records.items())],
                "ready": False,
                "can_apply": False,
                "control_authoritative": False,
                "rights_verified": False,
                "external_engine_verified": False,
            }
