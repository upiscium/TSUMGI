"""Negative-only, durable audio runtime epoch revocation model."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

if TYPE_CHECKING:
    from .models import WorkerRecord

_REVISION = re.compile(r"sha256:[a-f0-9]{64}\Z")
_AUDIO = frozenset({"audio.synthesize", "audio.transcribe"})


def _uuid(value: object) -> bool:
    if type(value) is not str:
        return False
    try:
        return str(UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _worker_id(value: object) -> bool:
    return type(value) is str and bool(value.strip()) and len(value) <= 256 and "\x00" not in value


def validate_fence_arguments(
    worker_id: str, runtime_instance_epoch: str,
    expected_deployment_revision: str,
    expected_serving_contract_revision: str, fence_request_id: str,
) -> None:
    if not (
        _worker_id(worker_id) and _uuid(runtime_instance_epoch)
        and isinstance(expected_deployment_revision, str)
        and _REVISION.fullmatch(expected_deployment_revision)
        and isinstance(expected_serving_contract_revision, str)
        and _REVISION.fullmatch(expected_serving_contract_revision)
        and _uuid(fence_request_id)
    ):
        raise ValueError("invalid audio epoch fence request")


def validate_fence_lookup(worker_id: str, runtime_instance_epoch: str) -> None:
    if not _worker_id(worker_id) or not _uuid(runtime_instance_epoch):
        raise ValueError("invalid audio epoch fence request")


@dataclass(frozen=True, slots=True)
class AudioEpochFence:
    worker_id: str
    runtime_instance_epoch: str
    deployment_revision: str
    serving_contract_revision: str
    fence_request_id: str
    fenced_at: datetime

    def __post_init__(self) -> None:
        validate_fence_arguments(
            self.worker_id, self.runtime_instance_epoch, self.deployment_revision,
            self.serving_contract_revision, self.fence_request_id,
        )
        if not isinstance(self.fenced_at, datetime) or self.fenced_at.tzinfo is None:
            raise ValueError("fenced_at must be timezone-aware")
        object.__setattr__(self, "fenced_at", self.fenced_at.astimezone(UTC))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "audio-epoch-fence-v1",
            "state": "fenced",
            "record_revision": 1,
            "worker_id": self.worker_id,
            "runtime_instance_epoch": self.runtime_instance_epoch,
            "deployment_revision": self.deployment_revision,
            "serving_contract_revision": self.serving_contract_revision,
            "fence_request_id": self.fence_request_id,
            "fenced_at": self.fenced_at.isoformat().replace("+00:00", "Z"),
        }


def fence_matches(
    record: AudioEpochFence, *, expected_deployment_revision: str,
    expected_serving_contract_revision: str, fence_request_id: str,
) -> bool:
    return (
        record.deployment_revision == expected_deployment_revision
        and record.serving_contract_revision == expected_serving_contract_revision
        and record.fence_request_id == fence_request_id
    )


def worker_matches_fence(
    worker: WorkerRecord, epoch: str, deployment_revision: str,
    serving_contract_revision: str,
) -> bool:
    serving = worker.serving
    if serving is None or serving.runtime_instance.epoch != epoch:
        return False
    capabilities = frozenset(worker.spec.capabilities)
    if not capabilities or not capabilities.issubset(_AUDIO) or len(serving.contracts) != 1:
        return False
    contract = serving.contracts[0]
    return (
        contract.capability in _AUDIO
        and contract.deployment_revision == deployment_revision
        and contract.revision == serving_contract_revision
        and serving.deployment_revision == deployment_revision
    )


__all__ = [
    "AudioEpochFence", "fence_matches", "validate_fence_arguments",
    "validate_fence_lookup", "worker_matches_fence",
]
