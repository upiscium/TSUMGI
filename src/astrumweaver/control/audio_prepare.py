"""Default-off, non-authorizing preparation records for audio runtime epochs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import re
from uuid import UUID


_SHA = re.compile(r"[0-9a-f]{64}\Z")
_WORKER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_QUAL_FIELDS = (
    "deployment_revision", "serving_contract_revision", "worker_spec_revision",
    "voice_semantic_revision", "namespace_revision", "trust_evidence_revision",
    "rights_evidence_revision", "privacy_evidence_revision", "review_authority_revision",
)
_QUAL_KEYS = {"schema_version", "worker_id", *_QUAL_FIELDS, "reviewed",
              "issued_at", "expires_at", "qualification_revision"}
_MAX_INT = 2**63 - 1


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False)


def _digest(value: object) -> bool:
    return type(value) is str and _SHA.fullmatch(value) is not None


def _timestamp(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("invalid audio epoch preparation")
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_time(value: object) -> datetime:
    if type(value) is not str or not value.endswith("Z"):
        raise ValueError("invalid audio epoch preparation")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise ValueError("invalid audio epoch preparation") from None
    if _iso(parsed) != value:
        raise ValueError("invalid audio epoch preparation")
    return parsed.astimezone(UTC)


def worker_spec_revision(worker) -> str:
    from .serde import worker_spec_to_dict

    return hashlib.sha256(_canonical({
        "spec": worker_spec_to_dict(worker.spec),
        "max_concurrency": worker.max_concurrency,
    }).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class AudioPreparationQualification:
    worker_id: str
    deployment_revision: str
    serving_contract_revision: str
    worker_spec_revision: str
    voice_semantic_revision: str
    namespace_revision: str
    trust_evidence_revision: str
    rights_evidence_revision: str
    privacy_evidence_revision: str
    review_authority_revision: str
    reviewed: bool
    issued_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        if type(self.worker_id) is not str or _WORKER.fullmatch(self.worker_id) is None:
            raise ValueError("invalid audio epoch preparation")
        if any(not _digest(getattr(self, name)) for name in _QUAL_FIELDS):
            raise ValueError("invalid audio epoch preparation")
        if type(self.reviewed) is not bool:
            raise ValueError("invalid audio epoch preparation")
        issued, expires = _timestamp(self.issued_at), _timestamp(self.expires_at)
        if expires <= issued:
            raise ValueError("invalid audio epoch preparation")
        object.__setattr__(self, "issued_at", issued)
        object.__setattr__(self, "expires_at", expires)

    def _payload(self) -> dict[str, object]:
        return {
            "schema_version": "audio-preparation-qualification-v1",
            "worker_id": self.worker_id,
            **{name: getattr(self, name) for name in _QUAL_FIELDS},
            "reviewed": self.reviewed,
            "issued_at": _iso(self.issued_at),
            "expires_at": _iso(self.expires_at),
        }

    @property
    def qualification_revision(self) -> str:
        return hashlib.sha256(_canonical(self._payload()).encode()).hexdigest()

    def to_dict(self) -> dict[str, object]:
        return {**self._payload(), "qualification_revision": self.qualification_revision}

    @classmethod
    def from_dict(cls, value: object) -> "AudioPreparationQualification":
        if type(value) is not dict or set(value) != _QUAL_KEYS:
            raise ValueError("invalid audio epoch preparation")
        if value.get("schema_version") != "audio-preparation-qualification-v1":
            raise ValueError("invalid audio epoch preparation")
        item = dict(value)
        revision = item.pop("qualification_revision")
        item.pop("schema_version")
        item["issued_at"] = _parse_time(item["issued_at"])
        item["expires_at"] = _parse_time(item["expires_at"])
        record = cls(**item)
        if type(revision) is not str or revision != record.qualification_revision:
            raise ValueError("invalid audio epoch preparation")
        return record


@dataclass(frozen=True, slots=True)
class AudioEpochPreparation:
    worker_id: str
    runtime_instance_epoch: str
    qualification_revision: str
    deployment_revision: str
    serving_contract_revision: str
    prepare_request_id: str
    expected_intent_revision: int
    intent_revision: int
    prepared_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        if type(self.worker_id) is not str or _WORKER.fullmatch(self.worker_id) is None:
            raise ValueError("invalid audio epoch preparation")
        for name in ("runtime_instance_epoch", "prepare_request_id"):
            value = getattr(self, name)
            try:
                parsed = UUID(value) if type(value) is str else None
            except ValueError:
                parsed = None
            if parsed is None or parsed.int == 0 or str(parsed) != value:
                raise ValueError("invalid audio epoch preparation")
        if any(not _digest(getattr(self, name)) for name in (
            "qualification_revision", "deployment_revision", "serving_contract_revision"
        )):
            raise ValueError("invalid audio epoch preparation")
        if (type(self.expected_intent_revision) is not int
                or type(self.intent_revision) is not int
                or not 0 <= self.expected_intent_revision < _MAX_INT
                or self.intent_revision != self.expected_intent_revision + 1):
            raise ValueError("invalid audio epoch preparation")
        prepared, expires = _timestamp(self.prepared_at), _timestamp(self.expires_at)
        if expires <= prepared:
            raise ValueError("invalid audio epoch preparation")
        object.__setattr__(self, "prepared_at", prepared)
        object.__setattr__(self, "expires_at", expires)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "audio-epoch-preparation-v1", "state": "prepared",
            "record_revision": 1, "can_claim": False, "can_activate": False,
            "worker_id": self.worker_id, "runtime_instance_epoch": self.runtime_instance_epoch,
            "qualification_revision": self.qualification_revision,
            "deployment_revision": self.deployment_revision,
            "serving_contract_revision": self.serving_contract_revision,
            "prepare_request_id": self.prepare_request_id,
            "expected_intent_revision": self.expected_intent_revision,
            "intent_revision": self.intent_revision, "prepared_at": _iso(self.prepared_at),
            "expires_at": _iso(self.expires_at),
        }


def validate_prepare_arguments(worker_id, epoch, qualification_revision,
                               expected_intent_revision, prepare_request_id,
                               ttl_seconds) -> None:
    def valid_uuid(value: object) -> bool:
        try:
            parsed = UUID(value) if type(value) is str else None
            return parsed is not None and parsed.int != 0 and str(parsed) == value
        except (ValueError, TypeError):
            return False

    if (type(worker_id) is not str or _WORKER.fullmatch(worker_id) is None
            or not valid_uuid(epoch) or not _digest(qualification_revision)
            or not valid_uuid(prepare_request_id)
            or type(expected_intent_revision) is not int
            or not 0 <= expected_intent_revision <= _MAX_INT - 2
            or type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 300):
        raise ValueError("invalid audio epoch preparation") from None


def preparation_matches(record: AudioEpochPreparation, qualification_revision: str,
                        expected_intent_revision: int, prepare_request_id: str) -> bool:
    return (record.qualification_revision == qualification_revision
            and record.expected_intent_revision == expected_intent_revision
            and record.prepare_request_id == prepare_request_id)
