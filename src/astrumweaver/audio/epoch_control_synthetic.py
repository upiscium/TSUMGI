"""#247 R4-C2-A: disposable source-only prototype of Control audio epoch authority.

This module is NOT part of ControlRepository, PostgresControlRepository, the
Worker protocol, the serving registry, or any deployed HTTP route. All actors,
Worker snapshots, state and the clock below are synthetic, in-process values.
No actual job claim or positive write can be authorized by this prototype.

R4-C2-B must separately implement persistence and transactional row locking;
R4-C2-C must separately connect authenticated Worker and Job effects. R4-D
must separately review engine trust, license/credit and logging privacy.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from threading import RLock

from ..runtime.providers.voicevox_external_epoch import (
    AudioEpochCleanupIntent,
    AudioEpochFenceIntent,
    AudioEpochIdentity,
    AudioEpochPhase,
    AudioEpochRecord,
    ExternalAudioQualification,
    QualificationDisposition,
    _checked_cleanup_intent,
    _checked_epoch_record,
    _checked_identity,
    _checked_qualification,
    _clock,
    _counter,
    _uuid,
    _worker,
)

_MAX_COUNTER = 2**63 - 1


class SyntheticControlEpochConflict(ValueError):
    """Fixed, non-oracular error; no private source, Worker or credentials."""

    def __init__(self) -> None:
        super().__init__("synthetic audio epoch operation rejected")


class SyntheticPrincipalKind(StrEnum):
    WORKER = "worker"
    OPERATOR = "operator"


@dataclass(frozen=True, slots=True, repr=False)
class SyntheticPrincipal:
    """Test assertion, NOT authenticated identity or a security principal."""

    kind: SyntheticPrincipalKind
    worker_id: str

    def __post_init__(self) -> None:
        if type(self.kind) is not SyntheticPrincipalKind or not _worker(self.worker_id):
            raise SyntheticControlEpochConflict()


def _principal(value: object, worker_id: str, *, operator_only: bool = False) -> None:
    try:
        if (
            type(value) is not SyntheticPrincipal
            or not _worker(worker_id)
            or type(value.kind) is not SyntheticPrincipalKind
            or value.worker_id != worker_id
            or (operator_only and value.kind is not SyntheticPrincipalKind.OPERATOR)
        ):
            raise SyntheticControlEpochConflict()
    except Exception:  # noqa: BLE001 - fail closed on reflected foreign values
        raise SyntheticControlEpochConflict() from None


@dataclass(frozen=True, slots=True, repr=False)
class SyntheticWorkerWitness:
    """Fake bound Worker/health snapshot, NOT runtime readiness attestation."""

    worker_id: str
    runtime_instance_epoch: str
    worker_spec_revision: str
    deployment_revision: str
    serving_contract_revision: str
    voice_semantic_revision: str
    online: bool
    supervisor_ready: bool
    fresh_until_clock: float

    def __post_init__(self) -> None:
        try:
            from ..runtime.providers.voicevox_external_epoch import _sha

            if (
                not _worker(self.worker_id)
                or not _uuid(self.runtime_instance_epoch)
                or not all(
                    _sha(v)
                    for v in (
                        self.worker_spec_revision,
                        self.deployment_revision,
                        self.serving_contract_revision,
                        self.voice_semantic_revision,
                    )
                )
                or type(self.online) is not bool
                or type(self.supervisor_ready) is not bool
                or not _clock(self.fresh_until_clock)
            ):
                raise SyntheticControlEpochConflict()
        except Exception:  # noqa: BLE001 - fail closed on reflected foreign values
            raise SyntheticControlEpochConflict() from None


def _witness(value: object, identity: AudioEpochIdentity) -> SyntheticWorkerWitness:
    if type(value) is not SyntheticWorkerWitness:
        raise SyntheticControlEpochConflict()
    try:
        cloned = SyntheticWorkerWitness(
            worker_id=value.worker_id,
            runtime_instance_epoch=value.runtime_instance_epoch,
            worker_spec_revision=value.worker_spec_revision,
            deployment_revision=value.deployment_revision,
            serving_contract_revision=value.serving_contract_revision,
            voice_semantic_revision=value.voice_semantic_revision,
            online=value.online,
            supervisor_ready=value.supervisor_ready,
            fresh_until_clock=value.fresh_until_clock,
        )
        if (
            cloned.worker_id != identity.worker_id
            or cloned.runtime_instance_epoch != identity.epoch
            or cloned.worker_spec_revision != identity.worker_spec_revision
            or cloned.deployment_revision != identity.deployment_revision
            or cloned.serving_contract_revision != identity.serving_contract_revision
            or cloned.voice_semantic_revision != identity.voice_semantic_revision
        ):
            raise SyntheticControlEpochConflict()
        return cloned
    except Exception:  # noqa: BLE001 - fail closed on reflected foreign values
        raise SyntheticControlEpochConflict() from None


def _bound_qualification(
    identity: AudioEpochIdentity, qualification: object
) -> ExternalAudioQualification:
    try:
        verified = _checked_qualification(qualification)
        if (
            verified.disposition is not QualificationDisposition.UNVERIFIED
            or identity.worker_id != verified.worker_id
            or identity.qualification_revision != verified.qualification_revision
            or identity.worker_spec_revision != verified.worker_spec_revision
            or identity.deployment_revision != verified.deployment_revision
            or identity.serving_contract_revision != verified.serving_contract_revision
            or identity.voice_semantic_revision != verified.voice_semantic_revision
        ):
            raise SyntheticControlEpochConflict()
        return verified
    except Exception:  # noqa: BLE001 - fail closed on reflected foreign values
        raise SyntheticControlEpochConflict() from None


@dataclass(frozen=True, slots=True, repr=False)
class SyntheticEpochReceipt:
    """Snapshot of a fake commit. Never a durable or transferable ACK."""

    operation: str
    record: AudioEpochRecord = field(repr=False)
    request_id: str = field(repr=False)

    @property
    def control_authoritative(self) -> bool:
        return False

    @property
    def ready(self) -> bool:
        return False

    def to_dict(self) -> dict[str, object]:
        if (
            type(self.operation) is not str
            or self.operation not in ("prepare", "activate", "fence", "cleanup")
            or not _uuid(self.request_id)
        ):
            raise SyntheticControlEpochConflict()
        value = _record(self.record)
        return {
            "schema_version": "synthetic-control-audio-epoch-receipt-v1",
            "operation": self.operation,
            "phase": value.phase.value,
            "record_revision": value.record_revision,
            "synthetic_only": True,
            "control_authoritative": False,
            "ready": False,
            "can_apply": False,
            "rights_verified": False,
            "external_engine_verified": False,
            "worker_runtime_instance_epoch": None,
        }


def _record(value: object) -> AudioEpochRecord:
    try:
        return _checked_epoch_record(value)
    except Exception:  # noqa: BLE001 - fail closed on reflected foreign values
        raise SyntheticControlEpochConflict() from None


class SyntheticAudioControlEpochAdapter:
    """One-lock deterministic Control-epoch model for disposable tests only.

    PREPARED and ADMITTED are *simulation phases*; can_claim and positive writes
    are hard-denied unconditionally. All state is volatile. No import or call
    is made to the real Control repository, DB, HTTP transport or Worker.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._records: dict[tuple[str, str], AudioEpochRecord] = {}
        self._qualifications: dict[tuple[str, str], ExternalAudioQualification] = {}
        self._intent_floor: dict[str, int] = {}
        self._used_epochs: set[str] = set()
        self._requests: dict[str, tuple[tuple[object, ...], SyntheticEpochReceipt]] = {}
        self._outstanding: dict[tuple[str, str], int] = {}

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
        return False

    def can_positive_write(self, *_args: object, **_kwargs: object) -> bool:
        return False

    @staticmethod
    def _request(request_id: object) -> str:
        if not _uuid(request_id):
            raise SyntheticControlEpochConflict()
        return request_id

    def _replay(
        self, request_id: str, signature: tuple[object, ...]
    ) -> SyntheticEpochReceipt | None:
        result = self._requests.get(request_id)
        if result is None:
            return None
        if result[0] != signature:
            raise SyntheticControlEpochConflict()
        receipt = result[1]
        return SyntheticEpochReceipt(
            receipt.operation, _record(receipt.record), receipt.request_id
        )

    def _commit(
        self,
        request_id: str,
        signature: tuple[object, ...],
        record: AudioEpochRecord,
        operation: str,
    ) -> SyntheticEpochReceipt:
        receipt = SyntheticEpochReceipt(operation, _record(record), request_id)
        self._requests[request_id] = (signature, receipt)
        return SyntheticEpochReceipt(operation, _record(record), request_id)

    def prepare_for_test(
        self,
        *,
        actor: SyntheticPrincipal,
        identity: AudioEpochIdentity,
        qualification: ExternalAudioQualification,
        request_id: str,
        expected_intent_revision: int,
        now_clock: float,
        fresh_until_clock: float,
    ) -> SyntheticEpochReceipt:
        identity = _checked_identity(identity)
        _principal(actor, identity.worker_id)
        qualification = _bound_qualification(identity, qualification)
        request_id = self._request(request_id)
        if (
            not _counter(expected_intent_revision)
            or not _clock(now_clock)
            or not _clock(fresh_until_clock)
            or fresh_until_clock <= now_clock
        ):
            raise SyntheticControlEpochConflict()
        signature = (
            "prepare",
            actor.kind,
            actor.worker_id,
            identity,
            qualification.qualification_revision,
            request_id,
            expected_intent_revision,
            now_clock,
            fresh_until_clock,
        )
        with self._lock:
            replay = self._replay(request_id, signature)
            if replay is not None:
                return replay
            key = (identity.worker_id, identity.epoch)
            current = self._intent_floor.get(identity.worker_id, 0)
            if (
                expected_intent_revision != current
                or identity.intent_revision != current + 1
                or identity.epoch in self._used_epochs
                or key in self._records
                or any(
                    rec.identity.worker_id == identity.worker_id
                    and rec.phase is not AudioEpochPhase.CLEANUP_COMPLETE
                    for rec in self._records.values()
                )
            ):
                raise SyntheticControlEpochConflict()
            record = AudioEpochRecord(
                identity=identity,
                phase=AudioEpochPhase.PREPARED,
                record_revision=1,
                fresh_until_clock=fresh_until_clock,
            )
            self._used_epochs.add(identity.epoch)
            self._intent_floor[identity.worker_id] = identity.intent_revision
            self._records[key] = record
            self._qualifications[key] = qualification
            self._outstanding[key] = 0
            return self._commit(request_id, signature, record, "prepare")

    def activate_for_test(
        self,
        *,
        actor: SyntheticPrincipal,
        identity: AudioEpochIdentity,
        witness: SyntheticWorkerWitness,
        request_id: str,
        expected_record_revision: int,
        expected_intent_revision: int,
        now_clock: float,
        synthetic_gate: bool = False,
    ) -> SyntheticEpochReceipt:
        identity = _checked_identity(identity)
        _principal(actor, identity.worker_id)
        witness = _witness(witness, identity)
        request_id = self._request(request_id)
        if (
            not _counter(expected_record_revision, minimum=1)
            or not _counter(expected_intent_revision, minimum=1)
            or not _clock(now_clock)
            or type(synthetic_gate) is not bool
        ):
            raise SyntheticControlEpochConflict()
        signature = (
            "activate",
            actor.kind,
            actor.worker_id,
            identity,
            witness,
            request_id,
            expected_record_revision,
            expected_intent_revision,
            now_clock,
            synthetic_gate,
        )
        with self._lock:
            replay = self._replay(request_id, signature)
            if replay is not None:
                return replay
            key = (identity.worker_id, identity.epoch)
            rec = self._records.get(key)
            if (
                rec is None
                or rec.identity != identity
                or rec.phase is not AudioEpochPhase.PREPARED
                or rec.record_revision != expected_record_revision
                or self._intent_floor.get(identity.worker_id, 0)
                != expected_intent_revision
                or identity.intent_revision != expected_intent_revision
                or not synthetic_gate
                or not witness.online
                or not witness.supervisor_ready
                or now_clock >= min(rec.fresh_until_clock, witness.fresh_until_clock)
                or self._qualifications[key].disposition
                is not QualificationDisposition.UNVERIFIED
                or any(
                    other.identity.worker_id == identity.worker_id
                    and other.identity.epoch != identity.epoch
                    and other.phase is not AudioEpochPhase.CLEANUP_COMPLETE
                    for other in self._records.values()
                )
            ):
                raise SyntheticControlEpochConflict()
            if not _counter(rec.record_revision + 1, minimum=1):
                raise SyntheticControlEpochConflict()
            admitted = replace(
                rec,
                phase=AudioEpochPhase.ADMITTED,
                record_revision=rec.record_revision + 1,
            )
            self._records[key] = admitted
            return self._commit(request_id, signature, admitted, "activate")

    def fence_for_test(
        self, *, actor: SyntheticPrincipal, request: AudioEpochFenceIntent
    ) -> SyntheticEpochReceipt:
        try:
            from ..runtime.providers.voicevox_external_epoch import (
                _checked_fence_intent,
            )

            req = _checked_fence_intent(request)
            _principal(actor, req.worker_id)
        except Exception:  # noqa: BLE001 - fail closed on reflected foreign values
            raise SyntheticControlEpochConflict() from None
        signature = ("fence", actor.kind, actor.worker_id, req)
        with self._lock:
            replay = self._replay(req.request_id, signature)
            if replay is not None:
                return replay
            key = (req.worker_id, req.epoch)
            rec = self._records.get(key)
            if (
                rec is None
                or rec.phase not in (AudioEpochPhase.PREPARED, AudioEpochPhase.ADMITTED)
                or rec.record_revision != req.expected_record_revision
                or self._intent_floor.get(req.worker_id, 0)
                != req.expected_intent_revision
                or rec.identity.intent_revision != req.expected_intent_revision
                or not _counter(rec.record_revision + 1, minimum=1)
            ):
                raise SyntheticControlEpochConflict()
            fenced = replace(
                rec,
                phase=AudioEpochPhase.FENCED,
                record_revision=rec.record_revision + 1,
                fenced_intent_revision=req.revoke_intent_revision,
                fence_request_id=req.request_id,
            )
            self._records[key] = fenced
            self._intent_floor[req.worker_id] = req.revoke_intent_revision
            return self._commit(req.request_id, signature, fenced, "fence")

    def set_outstanding_for_test(
        self,
        *,
        actor: SyntheticPrincipal,
        worker_id: str,
        epoch: str,
        expected_count: int,
        new_count: int,
    ) -> None:
        """One fake accounting CAS; never an actual attempt or lease mutation."""
        _principal(actor, worker_id, operator_only=True)
        if not _uuid(epoch) or not _counter(expected_count) or not _counter(new_count):
            raise SyntheticControlEpochConflict()
        with self._lock:
            key = (worker_id, epoch)
            rec = self._records.get(key)
            if (
                rec is None
                or self._outstanding.get(key) != expected_count
                or rec.phase is AudioEpochPhase.CLEANUP_COMPLETE
                or (
                    new_count > expected_count
                    and rec.phase is not AudioEpochPhase.ADMITTED
                )
            ):
                raise SyntheticControlEpochConflict()
            self._outstanding[key] = new_count

    def cleanup_for_test(
        self,
        *,
        actor: SyntheticPrincipal,
        request: AudioEpochCleanupIntent,
        outputs_revoked_for_test: bool = False,
    ) -> SyntheticEpochReceipt:
        try:
            req = _checked_cleanup_intent(request)
            _principal(actor, req.worker_id, operator_only=True)
        except Exception:  # noqa: BLE001 - fail closed on reflected foreign values
            raise SyntheticControlEpochConflict() from None
        if type(outputs_revoked_for_test) is not bool:
            raise SyntheticControlEpochConflict()
        signature = (
            "cleanup",
            actor.kind,
            actor.worker_id,
            req,
            outputs_revoked_for_test,
        )
        with self._lock:
            replay = self._replay(req.request_id, signature)
            if replay is not None:
                return replay
            key = (req.worker_id, req.epoch)
            rec = self._records.get(key)
            if (
                rec is None
                or rec.phase is not AudioEpochPhase.FENCED
                or rec.record_revision != req.expected_record_revision
                or rec.fenced_intent_revision != req.expected_fenced_intent_revision
                or self._intent_floor.get(req.worker_id, 0)
                != req.expected_fenced_intent_revision
                or self._outstanding.get(key, 0) != 0
                or not outputs_revoked_for_test
                or not _counter(rec.record_revision + 1, minimum=1)
            ):
                raise SyntheticControlEpochConflict()
            cleaned = replace(
                rec,
                phase=AudioEpochPhase.CLEANUP_COMPLETE,
                record_revision=rec.record_revision + 1,
            )
            self._records[key] = cleaned
            return self._commit(req.request_id, signature, cleaned, "cleanup")

    def readback_for_test(
        self, *, actor: SyntheticPrincipal, worker_id: str, epoch: str
    ) -> AudioEpochRecord | None:
        _principal(actor, worker_id)
        if not _uuid(epoch):
            raise SyntheticControlEpochConflict()
        with self._lock:
            rec = self._records.get((worker_id, epoch))
            return None if rec is None else _record(rec)

    def receipt_for_test(
        self,
        *,
        actor: SyntheticPrincipal,
        worker_id: str,
        epoch: str,
        request_id: str,
    ) -> SyntheticEpochReceipt | None:
        _principal(actor, worker_id)
        request_id = self._request(request_id)
        if not _uuid(epoch):
            raise SyntheticControlEpochConflict()
        with self._lock:
            entry = self._requests.get(request_id)
            if entry is None:
                return None
            _signature, receipt = entry
            rec = _record(receipt.record)
            if rec.identity.worker_id != worker_id or rec.identity.epoch != epoch:
                raise SyntheticControlEpochConflict()
            return SyntheticEpochReceipt(receipt.operation, rec, receipt.request_id)

    def report_for_test(self) -> dict[str, object]:
        """Redacted audit view, no epoch UUID/endpoint/voice/secret material."""
        with self._lock:
            return {
                "schema_version": "synthetic-control-audio-adapter-v1",
                "synthetic_only": True,
                "epochs": [
                    _record(rec).to_dict() for _, rec in sorted(self._records.items())
                ],
                "ready": False,
                "can_apply": False,
                "control_authoritative": False,
                "rights_verified": False,
                "external_engine_verified": False,
            }
