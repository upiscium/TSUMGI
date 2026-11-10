"""#247 R4-C1: pure, non-authoritative external audio qualification/epoch types.

These records are not Control registrations, serving readiness, trust approval,
proof of license/engine identity, or usable Worker epochs. No network, DB,
engine, registry, or host operations occur in this module. R4-C2/R4-D remain
separately gated.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field, replace
from enum import Enum
from uuid import UUID

from .voicevox_external_plan import OperatorPolicyStatus
from .voicevox_external_reconcile import (
    ExternalVoicevoxReconcileConfig,
    _checked_config,
)

_SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")
_WORKER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_MAX_COUNTER = 2**63 - 1
_MAX_CLOCK = 1e15


class ExternalAudioAuthorityError(ValueError):
    """Never expose supplied endpoint, attribution, licensing, or native text."""

    def __init__(self) -> None:
        super().__init__("VOICEVOX external audio authority declaration is invalid")


def _sha(value: object) -> bool:
    return type(value) is str and _SHA.fullmatch(value) is not None


def _worker(value: object) -> bool:
    return type(value) is str and _WORKER.fullmatch(value) is not None


def _counter(value: object, *, minimum: int = 0) -> bool:
    return type(value) is int and minimum <= value <= _MAX_COUNTER


def _clock(value: object) -> bool:
    return (
        type(value) in (int, float)
        and 0 <= value <= _MAX_CLOCK
        and math.isfinite(value)
    )


def _uuid(value: object) -> bool:
    if type(value) is not str:
        return False
    try:
        decoded = UUID(value)
        return decoded.int != 0 and str(decoded) == value
    except (TypeError, ValueError, AttributeError):
        return False


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


class QualificationDisposition(str, Enum):
    """There is deliberately no VERIFIED/APPROVED production state in C1."""

    UNVERIFIED = "unverified"
    REVOKED = "revoked"


class AudioEpochPhase(str, Enum):
    """Future Control states represented only in a synthetic local fixture."""

    PREPARED = "prepared"
    ADMITTED = "admitted"  # fake fixture only; never Worker admission
    FENCED = "fenced"
    CLEANUP_COMPLETE = "cleanup_complete"


@dataclass(frozen=True, slots=True, repr=False)
class ExternalAudioQualification:
    """Canonical operator declaration, NOT reviewed trust, identity or rights.

    The private R4-B1/A config embeds a full exact voice/style/rights declaration.
    We hold a defensive clone and hash its full semantic revision, including
    endpoint/license/attribution, while serializing only safe digests.
    """

    worker_id: str
    source: ExternalVoicevoxReconcileConfig = field(repr=False)
    worker_spec_revision: str
    namespace_revision: str
    deployment_revision: str
    serving_contract_revision: str
    voice_semantic_revision: str
    trust_evidence_revision: str
    rights_evidence_revision: str
    privacy_evidence_revision: str
    qualification_revision: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        try:
            source = _checked_config(self.source)
            if (
                not _worker(self.worker_id)
                or not source.enabled
                or type(source.policy_status) is not OperatorPolicyStatus
                or not all(
                    _sha(value)
                    for value in (
                        self.worker_spec_revision,
                        self.namespace_revision,
                        self.deployment_revision,
                        self.serving_contract_revision,
                        self.voice_semantic_revision,
                        self.trust_evidence_revision,
                        self.rights_evidence_revision,
                        self.privacy_evidence_revision,
                    )
                )
            ):
                raise ExternalAudioAuthorityError()
            fingerprint = _revision(
                {
                    "schema": "voicevox-external-qualification-declaration-v1",
                    "worker_id": self.worker_id,
                    "source_revision": source.revision,
                    "worker_spec_revision": self.worker_spec_revision,
                    "namespace_revision": self.namespace_revision,
                    "deployment_revision": self.deployment_revision,
                    "serving_contract_revision": self.serving_contract_revision,
                    "voice_semantic_revision": self.voice_semantic_revision,
                    "trust_evidence_revision": self.trust_evidence_revision,
                    "rights_evidence_revision": self.rights_evidence_revision,
                    "privacy_evidence_revision": self.privacy_evidence_revision,
                }
            )
            object.__setattr__(self, "source", source)
            object.__setattr__(self, "qualification_revision", fingerprint)
        except Exception:  # noqa: BLE001 - never leak declaration/private source
            raise ExternalAudioAuthorityError() from None

    @property
    def disposition(self) -> QualificationDisposition:
        checked = _checked_qualification(self)
        if checked.source.policy_status is OperatorPolicyStatus.REVOKED:
            return QualificationDisposition.REVOKED
        return QualificationDisposition.UNVERIFIED

    @property
    def ready(self) -> bool:
        return False

    @property
    def can_apply(self) -> bool:
        return False

    @property
    def external_engine_verified(self) -> bool:
        return False

    @property
    def rights_verified(self) -> bool:
        return False

    def to_dict(self) -> dict[str, object]:
        checked = _checked_qualification(self)
        return {
            "schema_version": "voicevox-external-qualification-declaration-v1",
            "worker_id": checked.worker_id,
            "source_revision": checked.source.revision,
            "qualification_revision": checked.qualification_revision,
            "disposition": (
                "revoked"
                if checked.source.policy_status is OperatorPolicyStatus.REVOKED
                else "unverified"
            ),
            "ready": False,
            "can_apply": False,
            "external_engine_verified": False,
            "rights_verified": False,
            "worker_runtime_instance_epoch": None,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


def _checked_qualification(value: object) -> ExternalAudioQualification:
    if type(value) is not ExternalAudioQualification:
        raise ExternalAudioAuthorityError()
    try:
        verified = ExternalAudioQualification(
            worker_id=value.worker_id,
            source=value.source,
            worker_spec_revision=value.worker_spec_revision,
            namespace_revision=value.namespace_revision,
            deployment_revision=value.deployment_revision,
            serving_contract_revision=value.serving_contract_revision,
            voice_semantic_revision=value.voice_semantic_revision,
            trust_evidence_revision=value.trust_evidence_revision,
            rights_evidence_revision=value.rights_evidence_revision,
            privacy_evidence_revision=value.privacy_evidence_revision,
        )
        if value.qualification_revision != verified.qualification_revision:
            raise ExternalAudioAuthorityError()
        return verified
    except Exception:  # noqa: BLE001 - fixed error on malicious reflective edits
        raise ExternalAudioAuthorityError() from None


def review_external_audio_qualification(
    *, enabled: bool = False, declaration: object = None
) -> dict[str, object]:
    """No trust attestation. Disabled ignores all foreign declarations."""
    if type(enabled) is not bool:
        raise ExternalAudioAuthorityError()
    if not enabled:
        return {
            "status": "disabled",
            "ready": False,
            "can_apply": False,
            "external_engine_verified": False,
            "rights_verified": False,
        }
    checked = _checked_qualification(declaration)
    return checked.to_dict()


@dataclass(frozen=True, slots=True, repr=False)
class AudioEpochIdentity:
    """Exact fake ledger key/bindings; UUID is not a production authority."""

    worker_id: str
    epoch: str
    qualification_revision: str
    worker_spec_revision: str
    deployment_revision: str
    serving_contract_revision: str
    voice_semantic_revision: str
    intent_revision: int

    def __post_init__(self) -> None:
        if (
            not _worker(self.worker_id)
            or not _uuid(self.epoch)
            or not _counter(self.intent_revision, minimum=1)
            or not all(
                _sha(v)
                for v in (
                    self.qualification_revision,
                    self.worker_spec_revision,
                    self.deployment_revision,
                    self.serving_contract_revision,
                    self.voice_semantic_revision,
                )
            )
        ):
            raise ExternalAudioAuthorityError()


def _checked_identity(value: object) -> AudioEpochIdentity:
    if type(value) is not AudioEpochIdentity:
        raise ExternalAudioAuthorityError()
    try:
        return AudioEpochIdentity(
            worker_id=value.worker_id,
            epoch=value.epoch,
            qualification_revision=value.qualification_revision,
            worker_spec_revision=value.worker_spec_revision,
            deployment_revision=value.deployment_revision,
            serving_contract_revision=value.serving_contract_revision,
            voice_semantic_revision=value.voice_semantic_revision,
            intent_revision=value.intent_revision,
        )
    except Exception:  # noqa: BLE001
        raise ExternalAudioAuthorityError() from None


@dataclass(frozen=True, slots=True, repr=False)
class AudioEpochRecord:
    """One immutable fake epoch record; no real Control/Worker insertion."""

    identity: AudioEpochIdentity = field(repr=False)
    phase: AudioEpochPhase
    record_revision: int
    fresh_until_clock: float
    fenced_intent_revision: int = 0
    fence_request_id: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        _checked_identity(self.identity)
        if (
            type(self.phase) is not AudioEpochPhase
            or not _counter(self.record_revision, minimum=1)
            or not _clock(self.fresh_until_clock)
            or not _counter(self.fenced_intent_revision)
        ):
            raise ExternalAudioAuthorityError()
        if self.phase in (AudioEpochPhase.PREPARED, AudioEpochPhase.ADMITTED):
            if self.fenced_intent_revision != 0 or self.fence_request_id is not None:
                raise ExternalAudioAuthorityError()
        elif self.fenced_intent_revision <= self.identity.intent_revision or not _uuid(
            self.fence_request_id
        ):
            raise ExternalAudioAuthorityError()

    @property
    def ready(self) -> bool:
        return False

    @property
    def can_apply(self) -> bool:
        return False

    def to_dict(self) -> dict[str, object]:
        x = _checked_epoch_record(self)
        return {
            "schema_version": "voicevox-synthetic-audio-epoch-v1",
            "worker_id": x.identity.worker_id,
            "phase": x.phase.value,
            "record_revision": x.record_revision,
            "intent_revision": (
                x.fenced_intent_revision
                if x.fenced_intent_revision
                else x.identity.intent_revision
            ),
            "qualification_revision": x.identity.qualification_revision,
            "ready": False,
            "can_apply": False,
            "control_authoritative": False,
            "rights_verified": False,
            "external_engine_verified": False,
            "worker_runtime_instance_epoch": None,
        }


def _checked_epoch_record(value: object) -> AudioEpochRecord:
    if type(value) is not AudioEpochRecord:
        raise ExternalAudioAuthorityError()
    try:
        return AudioEpochRecord(
            identity=_checked_identity(value.identity),
            phase=value.phase,
            record_revision=value.record_revision,
            fresh_until_clock=value.fresh_until_clock,
            fenced_intent_revision=value.fenced_intent_revision,
            fence_request_id=value.fence_request_id,
        )
    except Exception:  # noqa: BLE001
        raise ExternalAudioAuthorityError() from None


@dataclass(frozen=True, slots=True, repr=False)
class AudioEpochFenceIntent:
    """CAS withdrawal intent for a disposable fake; not an HTTP request."""

    worker_id: str
    epoch: str
    expected_record_revision: int
    expected_intent_revision: int
    revoke_intent_revision: int
    request_id: str

    def __post_init__(self) -> None:
        if (
            not _worker(self.worker_id)
            or not _uuid(self.epoch)
            or not _counter(self.expected_record_revision, minimum=1)
            or not _counter(self.expected_intent_revision, minimum=1)
            or not _counter(self.revoke_intent_revision, minimum=2)
            or self.revoke_intent_revision <= self.expected_intent_revision
            or not _uuid(self.request_id)
        ):
            raise ExternalAudioAuthorityError()


def _checked_fence_intent(value: object) -> AudioEpochFenceIntent:
    if type(value) is not AudioEpochFenceIntent:
        raise ExternalAudioAuthorityError()
    try:
        return AudioEpochFenceIntent(
            worker_id=value.worker_id,
            epoch=value.epoch,
            expected_record_revision=value.expected_record_revision,
            expected_intent_revision=value.expected_intent_revision,
            revoke_intent_revision=value.revoke_intent_revision,
            request_id=value.request_id,
        )
    except Exception:  # noqa: BLE001
        raise ExternalAudioAuthorityError() from None


@dataclass(frozen=True, slots=True, repr=False)
class AudioEpochCleanupIntent:
    """Privileged fake-only cleanup, requiring matching fence revision."""

    worker_id: str
    epoch: str
    expected_record_revision: int
    expected_fenced_intent_revision: int
    request_id: str

    def __post_init__(self) -> None:
        if (
            not _worker(self.worker_id)
            or not _uuid(self.epoch)
            or not _counter(self.expected_record_revision, minimum=1)
            or not _counter(self.expected_fenced_intent_revision, minimum=2)
            or not _uuid(self.request_id)
        ):
            raise ExternalAudioAuthorityError()


def _checked_cleanup_intent(value: object) -> AudioEpochCleanupIntent:
    if type(value) is not AudioEpochCleanupIntent:
        raise ExternalAudioAuthorityError()
    try:
        return AudioEpochCleanupIntent(
            worker_id=value.worker_id,
            epoch=value.epoch,
            expected_record_revision=value.expected_record_revision,
            expected_fenced_intent_revision=value.expected_fenced_intent_revision,
            request_id=value.request_id,
        )
    except Exception:  # noqa: BLE001
        raise ExternalAudioAuthorityError() from None


def _advance(
    record: AudioEpochRecord,
    *,
    phase: AudioEpochPhase,
    record_revision: int,
    fenced_intent_revision: int,
    request_id: str,
) -> AudioEpochRecord:
    """Only a pure copy. Real Control transaction guarantees are not supplied."""
    return replace(
        _checked_epoch_record(record),
        phase=phase,
        record_revision=record_revision,
        fenced_intent_revision=fenced_intent_revision,
        fence_request_id=request_id,
    )
