"""Control-plane repository contracts and in-memory reference backend.

The in-memory backend is intentionally feature-equivalent to the durable
repository contract for unit tests. Production durability is provided by the
PostgreSQL backend in postgres.py.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import RLock
from typing import Protocol
from uuid import uuid4

from ..execution import JobResult
from ..scheduling import worker_matches
from ..serving import (
    ServingJobBinding,
    WorkerServingAdvertisement,
    worker_serving_accepts_job,
)
from .models import (
    JobEventRecord,
    JobRecord,
    JobStatus,
    JobSubmission,
    WorkerHeartbeat,
    WorkerRecord,
    WorkerRegistration,
    WorkerState,
    utc_now,
)
from .audio_epoch import (
    AudioEpochFence,
    fence_matches,
    validate_fence_arguments,
    validate_fence_lookup,
    worker_matches_fence,
)
from .audio_prepare import (
    AudioEpochPreparation,
    AudioPreparationQualification,
    preparation_matches,
    validate_prepare_arguments,
    validate_prepare_lookup,
    worker_spec_revision,
)


# Control-owned metadata marker, not supplied by a Worker registration/heartbeat.
# Only TTL expiry qualifies for bounded automatic recovery; administrative OFFLINE
# and failed-owner epoch transitions must remain fail-closed.
OFFLINE_REASON_KEY = "_tsumgi_control_offline_reason"
RECOVERABLE_OFFLINE_REASON = "heartbeat_timeout"


class RepositoryError(RuntimeError):
    """Base control repository error."""


class NotFoundError(RepositoryError):
    """Requested durable entity does not exist."""


class ConflictError(RepositoryError):
    """Requested transition conflicts with current durable state."""


class DeadlineExceededError(RepositoryError):
    """A bounded serving request can no longer be admitted."""


class NoCompatibleDeployment(RepositoryError):
    """No live deployment satisfies the immutable serving binding."""


class OverloadedError(RepositoryError):
    """All live compatible serving replicas are at capacity."""


class EventBufferFull(RepositoryError):
    """A bounded Job event buffer cannot accept another event."""


class StorageUnavailable(RepositoryError):
    """Durable storage cannot be reached safely."""


class ControlRepository(Protocol):
    def store_audio_preparation_qualification(
        self, qualification: AudioPreparationQualification,
    ) -> AudioPreparationQualification: ...

    def revoke_audio_preparation_qualification(
        self, qualification_revision: str, *, now: datetime | None = None,
    ) -> None: ...

    def prepare_audio_epoch(
        self, worker_id: str, runtime_instance_epoch: str, *,
        qualification_revision: str, expected_intent_revision: int,
        prepare_request_id: str, ttl_seconds: int = 60, now: datetime | None = None,
    ) -> AudioEpochPreparation: ...

    def get_audio_epoch_preparation(
        self, worker_id: str, runtime_instance_epoch: str,
    ) -> AudioEpochPreparation: ...

    def fence_audio_epoch(
        self, worker_id: str, runtime_instance_epoch: str, *,
        expected_deployment_revision: str, expected_serving_contract_revision: str,
        fence_request_id: str, now: datetime | None = None,
    ) -> AudioEpochFence: ...

    def get_audio_epoch_fence(
        self, worker_id: str, runtime_instance_epoch: str,
    ) -> AudioEpochFence: ...

    def check_storage(self) -> None: ...

    def register_worker(
        self, registration: WorkerRegistration, *, recovery: bool = False,
        now: datetime | None = None
    ) -> WorkerRecord: ...

    def heartbeat_worker(
        self,
        worker_id: str,
        heartbeat: WorkerHeartbeat | None = None,
        *,
        now: datetime | None = None,
    ) -> WorkerRecord: ...

    def submit_job(
        self, submission: JobSubmission, *, now: datetime | None = None,
        admission_deadline_at: datetime | None = None,
    ) -> JobRecord: ...

    def get_worker(self, worker_id: str) -> WorkerRecord: ...

    def list_workers(self) -> list[WorkerRecord]: ...

    def set_worker_state(
        self,
        worker_id: str,
        state: WorkerState,
        *,
        runtime_instance_epoch: str | None = None,
        now: datetime | None = None,
    ) -> WorkerRecord: ...

    def expire_stale_workers(
        self, *, now: datetime | None = None
    ) -> list[WorkerRecord]: ...

    def expire_deadline_jobs(
        self, *, now: datetime | None = None
    ) -> list[JobRecord]: ...

    def get_job(self, job_id: str) -> JobRecord: ...

    def get_job_by_idempotency_key(
        self, key: str, *, admission_deadline_at: datetime | None = None
    ) -> JobRecord | None: ...

    def append_job_event(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_token: str,
        runtime_instance_epoch: str | None,
        kind: str,
        payload: Mapping[str, object],
        now: datetime | None = None,
    ) -> JobEventRecord: ...

    def list_job_events(
        self,
        job_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 128,
    ) -> list[JobEventRecord]: ...

    def claim_next_job(
        self,
        worker_id: str,
        *,
        runtime_instance_epoch: str | None = None,
        now: datetime | None = None,
    ) -> JobRecord | None: ...

    def complete_job(
        self,
        job_id: str,
        result: JobResult,
        *,
        worker_id: str,
        lease_token: str,
        runtime_instance_epoch: str | None = None,
        now: datetime | None = None,
    ) -> JobRecord: ...

    def fail_job(
        self,
        job_id: str,
        error: str | dict[str, object],
        *,
        retryable: bool,
        worker_id: str,
        lease_token: str,
        runtime_instance_epoch: str | None = None,
        now: datetime | None = None,
    ) -> JobRecord: ...

    def cancel_job(
        self, job_id: str, *, now: datetime | None = None,
        admission_deadline_at: datetime | None = None,
    ) -> JobRecord: ...

    def recover_expired_jobs(
        self, *, now: datetime | None = None
    ) -> list[JobRecord]: ...

def _aware(value: datetime | None) -> datetime:
    current = value or utc_now()
    if current.tzinfo is None:
        return current.replace(tzinfo=UTC)
    return current.astimezone(UTC)


def _lease_expiry(
    timestamp: datetime,
    lease_seconds: int,
    deadline_at: datetime | None,
) -> datetime:
    expiry = timestamp + timedelta(seconds=lease_seconds)
    return expiry if deadline_at is None else min(expiry, deadline_at)


def _event_size(kind: str, payload: Mapping[str, object]) -> int:
    if not isinstance(payload, Mapping):
        raise TypeError("event payload must be a mapping")
    try:
        encoded = json.dumps(
            {"kind": kind, "payload": dict(payload)},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise TypeError("event payload must be JSON-compatible") from exc
    return len(encoded)


def _failure_payload(error: str | dict[str, object], *, retryable: bool) -> dict[str, object]:
    if isinstance(error, str):
        return {"message": error, "retryable": retryable}
    payload = dict(error)
    payload["retryable"] = retryable
    return payload


def _assert_worker_epoch(
    worker: WorkerRecord, runtime_instance_epoch: str | None
) -> None:
    if worker.serving is None:
        if runtime_instance_epoch is not None:
            raise ConflictError("worker has no serving runtime instance")
        return
    if runtime_instance_epoch != worker.serving.runtime_instance.epoch:
        raise ConflictError("worker runtime instance is stale")


def _validate_audio_attempt_arguments(
    job_id: str, worker_id: str, attempt: int, lease_token: str,
    runtime_instance_epoch: str,
) -> None:
    if (
        any(type(value) is not str or not value.strip() or "\x00" in value
            for value in (job_id, worker_id, lease_token, runtime_instance_epoch))
        or type(attempt) is not int
        or attempt < 1
    ):
        raise ConflictError("audio artifact authority rejected")


def _assert_audio_artifact_attempt(
    job: JobRecord | None, worker: WorkerRecord | None, *,
    job_id: str, worker_id: str, attempt: int, lease_token: str,
    runtime_instance_epoch: str, now: datetime,
) -> None:
    """Check locked authoritative records, never produce a reusable grant."""
    try:
        valid = (
            isinstance(job, JobRecord)
            and isinstance(worker, WorkerRecord)
            and job.job_id == job_id
            and job.status is JobStatus.RUNNING
            and job.capability in ("audio.synthesize", "audio.transcribe")
            and isinstance(job.serving, ServingJobBinding)
            and replace(job.serving) == job.serving
            and worker.worker_id == worker_id == job.assigned_worker_id
            and (worker.state is WorkerState.ONLINE
                 or worker.state is WorkerState.DRAINING)
            and type(job.attempts) is int
            and job.attempts == attempt
            and job.lease_token == lease_token
            and isinstance(job.lease_expires_at, datetime)
            and job.lease_expires_at.tzinfo is not None
            and job.lease_expires_at > now
            and isinstance(job.deadline_at, datetime)
            and job.deadline_at.tzinfo is not None
            and job.deadline_at > now
            and isinstance(worker.serving, WorkerServingAdvertisement)
            and runtime_instance_epoch == job.claimed_runtime_instance_epoch
            and runtime_instance_epoch == worker.serving.runtime_instance.epoch
            and job.claimed_deployment_revision == job.serving.deployment_revision
            and job.claimed_serving_contract_revision == job.serving.serving_contract_revision
            and job.capability in worker.spec.capabilities
            and worker_matches(worker.spec, job.requirements)
            and worker_serving_accepts_job(worker.serving, job.capability, job.serving)
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        raise ConflictError("audio artifact authority rejected") from None
    if not valid:
        raise ConflictError("audio artifact authority rejected")


def _submission_matches(record: JobRecord, submission: JobSubmission) -> bool:
    if record.capability != submission.capability:
        return False
    if dict(record.payload) != dict(submission.payload):
        return False
    if record.requirements != submission.requirements:
        return False
    if record.priority != submission.priority:
        return False
    if record.max_attempts != submission.max_attempts:
        return False
    if record.serving != submission.serving:
        return False
    if submission.deadline_at is not None:
        if record.deadline_at != _aware(submission.deadline_at):
            return False
    elif record.deadline_at is not None:
        return False
    if submission.available_at is not None:
        return record.available_at == _aware(submission.available_at)
    return True


class InMemoryControlRepository:
    """Deterministic reference implementation of control-plane semantics."""

    def __init__(
        self,
        *,
        worker_ttl_seconds: int = 60,
        lease_seconds: int = 300,
        event_max_count: int = 4096,
        event_max_payload_bytes: int = 65536,
        event_max_total_bytes: int = 4 * 1024 * 1024,
        event_max_read: int = 512,
    ) -> None:
        for value, name in (
            (worker_ttl_seconds, "worker_ttl_seconds"),
            (lease_seconds, "lease_seconds"),
            (event_max_count, "event_max_count"),
            (event_max_payload_bytes, "event_max_payload_bytes"),
            (event_max_total_bytes, "event_max_total_bytes"),
            (event_max_read, "event_max_read"),
        ):
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        self.worker_ttl_seconds = worker_ttl_seconds
        self.lease_seconds = lease_seconds
        self.event_max_count = event_max_count
        self.event_max_payload_bytes = event_max_payload_bytes
        self.event_max_total_bytes = event_max_total_bytes
        self.event_max_read = event_max_read
        self._workers: dict[str, WorkerRecord] = {}
        self._jobs: dict[str, JobRecord] = {}
        self._events: dict[str, list[JobEventRecord]] = {}
        self._event_bytes: dict[str, int] = {}
        self._idempotency: dict[str, str] = {}
        self._audio_epoch_fences: dict[tuple[str, str], AudioEpochFence] = {}
        self._audio_preparation_qualifications: dict[str, AudioPreparationQualification] = {}
        self._revoked_audio_qualifications: set[str] = set()
        self._audio_epoch_preparations: dict[tuple[str, str], AudioEpochPreparation] = {}
        self._sequence = 0
        self._lock = RLock()

    def check_storage(self) -> None:
        return None

    def _assert_audio_epoch_not_fenced(self, worker_id: str, epoch: str | None) -> None:
        if any(key[0] == worker_id for key in self._audio_epoch_preparations):
            raise ConflictError("audio epoch is not active")
        if epoch is not None and (worker_id, epoch) in self._audio_epoch_fences:
            raise ConflictError("audio epoch is fenced")

    def store_audio_preparation_qualification(
        self, qualification: AudioPreparationQualification,
    ) -> AudioPreparationQualification:
        if not isinstance(qualification, AudioPreparationQualification):
            raise ValueError("invalid audio epoch preparation")
        record = AudioPreparationQualification.from_dict(qualification.to_dict())
        revision = record.qualification_revision
        with self._lock:
            if record.worker_id not in self._workers:
                raise NotFoundError("worker not found")
            prior = self._audio_preparation_qualifications.get(revision)
            if prior is not None:
                if prior.to_dict() == record.to_dict():
                    return prior
                raise ConflictError("audio preparation qualification conflict")
            self._audio_preparation_qualifications[revision] = record
            return record

    def revoke_audio_preparation_qualification(
        self, qualification_revision: str, *, now: datetime | None = None,
    ) -> None:
        del now
        if type(qualification_revision) is not str or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", qualification_revision
        ):
            raise ValueError("invalid audio epoch preparation")
        with self._lock:
            if qualification_revision not in self._audio_preparation_qualifications:
                raise NotFoundError("audio preparation qualification not found")
            self._revoked_audio_qualifications.add(qualification_revision)

    def prepare_audio_epoch(
        self, worker_id: str, runtime_instance_epoch: str, *,
        qualification_revision: str, expected_intent_revision: int,
        prepare_request_id: str, ttl_seconds: int = 60, now: datetime | None = None,
    ) -> AudioEpochPreparation:
        validate_prepare_arguments(
            worker_id, runtime_instance_epoch, qualification_revision,
            expected_intent_revision, prepare_request_id, ttl_seconds,
        )
        key = (worker_id, runtime_instance_epoch)
        with self._lock:
            timestamp = _aware(now)
            worker = self._workers.get(worker_id)
            if worker is None:
                raise NotFoundError("worker not found")
            qualification = self._audio_preparation_qualifications.get(qualification_revision)
            if (qualification is None or qualification.worker_id != worker_id
                    or not qualification.reviewed
                    or qualification_revision in self._revoked_audio_qualifications
                    or not qualification.issued_at <= timestamp < qualification.expires_at):
                raise ConflictError("audio epoch preparation rejected")
            advertisement = worker.serving
            audio_contracts = [] if advertisement is None else [
                item for item in advertisement.contracts
                if item.capability in {"audio.synthesize", "audio.transcribe"}
            ]
            if (advertisement is None or len(advertisement.contracts) != 1
                    or len(audio_contracts) != 1
                    or advertisement.deployment_revision != qualification.deployment_revision
                    or audio_contracts[0].revision != qualification.serving_contract_revision
                    or worker_spec_revision(worker) != qualification.worker_spec_revision):
                raise ConflictError("audio epoch preparation rejected")
            if (worker.state is not WorkerState.OFFLINE or worker.active_jobs != 0
                    or self._active_job_count(worker_id) != 0
                    or any(job.status is JobStatus.RUNNING and job.assigned_worker_id == worker_id
                           for job in self._jobs.values())):
                raise ConflictError("audio epoch preparation rejected")
            if key in self._audio_epoch_fences:
                raise ConflictError("audio epoch preparation rejected")
            existing = self._audio_epoch_preparations.get(key)
            history = [item for (wid, _), item in self._audio_epoch_preparations.items()
                       if wid == worker_id]
            max_intent = max((item.intent_revision for item in history), default=0)
            if existing is not None:
                if (preparation_matches(existing, qualification_revision,
                                        expected_intent_revision, prepare_request_id)
                        and existing.expires_at > timestamp
                        and max_intent == existing.intent_revision):
                    return existing
                raise ConflictError("audio epoch preparation rejected")
            if (runtime_instance_epoch == (
                    None if worker.serving is None else worker.serving.runtime_instance.epoch
                )
                    or any(job.assigned_worker_id == worker_id
                           and job.claimed_runtime_instance_epoch == runtime_instance_epoch
                           for job in self._jobs.values())
                    or expected_intent_revision != max_intent
                    or any(item.prepare_request_id == prepare_request_id
                           for item in self._audio_epoch_preparations.values())
                    or any(item.expires_at > timestamp
                           and item.qualification_revision not in self._revoked_audio_qualifications
                           for item in history)):
                raise ConflictError("audio epoch preparation rejected")
            record = AudioEpochPreparation(
                worker_id, runtime_instance_epoch, qualification_revision,
                advertisement.deployment_revision, audio_contracts[0].revision,
                prepare_request_id, expected_intent_revision, expected_intent_revision + 1,
                timestamp, min(qualification.expires_at, timestamp + timedelta(seconds=ttl_seconds)),
            )
            self._audio_epoch_preparations[key] = record
            return record

    def get_audio_epoch_preparation(
        self, worker_id: str, runtime_instance_epoch: str,
    ) -> AudioEpochPreparation:
        validate_prepare_lookup(worker_id, runtime_instance_epoch)
        with self._lock:
            try:
                return self._audio_epoch_preparations[(worker_id, runtime_instance_epoch)]
            except KeyError as exc:
                raise NotFoundError("audio epoch preparation not found") from exc

    def fence_audio_epoch(
        self, worker_id: str, runtime_instance_epoch: str, *,
        expected_deployment_revision: str, expected_serving_contract_revision: str,
        fence_request_id: str, now: datetime | None = None,
    ) -> AudioEpochFence:
        validate_fence_arguments(
            worker_id, runtime_instance_epoch, expected_deployment_revision,
            expected_serving_contract_revision, fence_request_id,
        )
        key = (worker_id, runtime_instance_epoch)
        with self._lock:
            existing = self._audio_epoch_fences.get(key)
            if existing is not None:
                if fence_matches(
                    existing,
                    expected_deployment_revision=expected_deployment_revision,
                    expected_serving_contract_revision=expected_serving_contract_revision,
                    fence_request_id=fence_request_id,
                ):
                    return existing
                raise ConflictError("audio epoch fence conflict")
            if any(item.fence_request_id == fence_request_id for item in self._audio_epoch_fences.values()):
                raise ConflictError("audio epoch fence conflict")
            worker = self._workers.get(worker_id)
            if worker is None:
                raise NotFoundError("worker not found")
            if not worker_matches_fence(
                worker, runtime_instance_epoch, expected_deployment_revision,
                expected_serving_contract_revision,
            ):
                raise ConflictError("audio epoch fence conflict")
            record = AudioEpochFence(
                worker_id, runtime_instance_epoch, expected_deployment_revision,
                expected_serving_contract_revision, fence_request_id, _aware(now),
            )
            self._audio_epoch_fences[key] = record
            return record

    def get_audio_epoch_fence(
        self, worker_id: str, runtime_instance_epoch: str,
    ) -> AudioEpochFence:
        validate_fence_lookup(worker_id, runtime_instance_epoch)
        with self._lock:
            try:
                return self._audio_epoch_fences[(worker_id, runtime_instance_epoch)]
            except KeyError as exc:
                raise NotFoundError("audio epoch fence not found") from exc

    def _authorize_audio_artifact_attempt(
        self, job_id: str, *, worker_id: str, attempt: int, lease_token: str,
        runtime_instance_epoch: str,
    ) -> None:
        """Call-time authority only; future transfer/publication must revalidate.

        The shared Worker Bearer establishes role, not per-Worker identity.
        No artifact metadata or client payload contributes authority here.
        """
        _validate_audio_attempt_arguments(
            job_id, worker_id, attempt, lease_token, runtime_instance_epoch
        )
        with self._lock:
            try:
                self._assert_audio_epoch_not_fenced(worker_id, runtime_instance_epoch)
            except ConflictError:
                raise ConflictError("audio artifact authority rejected") from None
            _assert_audio_artifact_attempt(
                self._jobs.get(job_id), self._workers.get(worker_id),
                job_id=job_id, worker_id=worker_id, attempt=attempt,
                lease_token=lease_token, runtime_instance_epoch=runtime_instance_epoch,
                now=utc_now(),
            )

    def _active_job_count(self, worker_id: str) -> int:
        return sum(
            1
            for job in self._jobs.values()
            if job.status is JobStatus.RUNNING and job.assigned_worker_id == worker_id
        )

    def _assert_gpu_ownership_available(
        self, registration: WorkerRegistration, *, ignore_worker_id: str | None = None
    ) -> None:
        requested = set(registration.spec.gpu_uuids)
        if not requested:
            return
        for worker in self._workers.values():
            if worker.worker_id == ignore_worker_id:
                continue
            owns_resources = worker.state is not WorkerState.OFFLINE or worker.active_jobs > 0
            if owns_resources and requested.intersection(worker.spec.gpu_uuids):
                raise ConflictError(
                    f"GPU identity overlaps with worker {worker.worker_id}"
                )

    def register_worker(
        self, registration: WorkerRegistration, *, recovery: bool = False,
        now: datetime | None = None
    ) -> WorkerRecord:
        timestamp = _aware(now)
        if OFFLINE_REASON_KEY in registration.metadata:
            raise ConflictError("Worker cannot supply Control-owned offline reason")
        with self._lock:
            existing = self._workers.get(registration.spec.worker_id)
            epoch = (
                None if registration.serving is None
                else registration.serving.runtime_instance.epoch
            )
            self._assert_audio_epoch_not_fenced(registration.spec.worker_id, epoch)
            if any(key[0] == registration.spec.worker_id for key in self._audio_epoch_preparations):
                raise ConflictError("audio epoch is not active")
            if epoch is None and any(
                key[0] == registration.spec.worker_id for key in self._audio_epoch_fences
            ):
                raise ConflictError("audio epoch is fenced")
            if recovery:
                if (existing is None or existing.state is not WorkerState.OFFLINE
                    or existing.metadata.get(OFFLINE_REASON_KEY) != RECOVERABLE_OFFLINE_REASON):
                    raise ConflictError("Worker is not eligible for explicit recovery")
                if (existing.spec != registration.spec
                    or existing.max_concurrency != registration.max_concurrency
                    or existing.serving != registration.serving):
                    raise ConflictError("recovery Worker identity or runtime epoch differs")
                if existing.active_jobs or self._active_job_count(registration.spec.worker_id):
                    raise ConflictError("recovery forbidden while Worker leases are active")
            if existing and existing.active_jobs > 0:
                if registration.spec != existing.spec:
                    raise ConflictError(
                        "worker resource/topology cannot change while jobs are active"
                    )
                if registration.max_concurrency < existing.active_jobs:
                    raise ConflictError(
                        "max_concurrency cannot be lower than active job count"
                    )
                if registration.serving != existing.serving:
                    raise ConflictError(
                        "worker serving deployment cannot change while jobs are active"
                    )
            self._assert_gpu_ownership_available(
                registration, ignore_worker_id=registration.spec.worker_id
            )
            registered_at = existing.registered_at if existing else timestamp
            record = WorkerRecord(
                spec=registration.spec,
                max_concurrency=registration.max_concurrency,
                state=WorkerState.ONLINE,
                active_jobs=self._active_job_count(registration.spec.worker_id),
                registered_at=registered_at,
                last_seen_at=timestamp,
                metadata=registration.metadata,
                serving=registration.serving,
            )
            self._workers[record.worker_id] = record
            return record

    def get_worker(self, worker_id: str) -> WorkerRecord:
        with self._lock:
            try:
                return self._workers[worker_id]
            except KeyError as exc:
                raise NotFoundError(f"worker not found: {worker_id}") from exc

    def list_workers(self) -> list[WorkerRecord]:
        """Every registered Worker (any state), ordered by id."""
        with self._lock:
            return sorted(self._workers.values(), key=lambda record: record.worker_id)

    def set_worker_state(
        self,
        worker_id: str,
        state: WorkerState,
        *,
        runtime_instance_epoch: str | None = None,
        now: datetime | None = None,
    ) -> WorkerRecord:
        timestamp = _aware(now)
        with self._lock:
            current = self.get_worker(worker_id)
            _assert_worker_epoch(current, runtime_instance_epoch)
            self._assert_audio_epoch_not_fenced(
                worker_id,
                None if current.serving is None else current.serving.runtime_instance.epoch,
            )
            if state is not WorkerState.OFFLINE:
                registration = WorkerRegistration(
                    spec=current.spec,
                    max_concurrency=current.max_concurrency,
                    metadata=current.metadata,
                    serving=current.serving,
                )
                self._assert_gpu_ownership_available(
                    registration, ignore_worker_id=worker_id
                )
            metadata = dict(current.metadata)
            if state is WorkerState.OFFLINE:
                metadata[OFFLINE_REASON_KEY] = "operator"
            else:
                metadata.pop(OFFLINE_REASON_KEY, None)
            updated = replace(current, state=state, last_seen_at=timestamp, metadata=metadata)
            self._workers[worker_id] = updated
            return updated

    def heartbeat_worker(
        self,
        worker_id: str,
        heartbeat: WorkerHeartbeat | None = None,
        *,
        now: datetime | None = None,
    ) -> WorkerRecord:
        timestamp = _aware(now)
        heartbeat = heartbeat or WorkerHeartbeat()
        if OFFLINE_REASON_KEY in heartbeat.metadata:
            raise ConflictError("Worker cannot override Control-owned offline reason")
        with self._lock:
            current = self.get_worker(worker_id)
            _assert_worker_epoch(current, heartbeat.runtime_instance_epoch)
            self._assert_audio_epoch_not_fenced(worker_id, heartbeat.runtime_instance_epoch)
            if current.state is WorkerState.OFFLINE and heartbeat.state in {
                WorkerState.ONLINE, WorkerState.DRAINING,
            }:
                raise ConflictError("offline worker must explicitly reacquire GPU ownership")

            if heartbeat.active_job_id is not None or heartbeat.lease_token is not None:
                if not heartbeat.active_job_id or not heartbeat.lease_token:
                    raise ConflictError(
                        "active_job_id and lease_token must be supplied together"
                    )
                job = self.get_job(heartbeat.active_job_id)
                self._assert_running(
                    job,
                    worker_id=worker_id,
                    lease_token=heartbeat.lease_token,
                    runtime_instance_epoch=heartbeat.runtime_instance_epoch,
                    now=timestamp,
                )
                renewed = job.with_updates(
                    lease_expires_at=_lease_expiry(
                        timestamp, self.lease_seconds, job.deadline_at
                    ),
                    updated_at=timestamp,
                )
                self._jobs[job.job_id] = renewed

            state = heartbeat.state or current.state
            metadata = {**dict(current.metadata), **dict(heartbeat.metadata)}
            updated = replace(
                current,
                state=state,
                active_jobs=self._active_job_count(worker_id),
                last_seen_at=timestamp,
                metadata=metadata,
            )
            self._workers[worker_id] = updated
            return updated

    def expire_stale_workers(
        self, *, now: datetime | None = None
    ) -> list[WorkerRecord]:
        timestamp = _aware(now)
        cutoff = timestamp - timedelta(seconds=self.worker_ttl_seconds)
        expired: list[WorkerRecord] = []
        with self._lock:
            for worker_id, worker in list(self._workers.items()):
                if worker.state is WorkerState.OFFLINE or worker.last_seen_at >= cutoff:
                    continue
                reason = (
                    RECOVERABLE_OFFLINE_REASON
                    if worker.state is WorkerState.ONLINE else "draining_expired"
                )
                metadata = {**dict(worker.metadata), OFFLINE_REASON_KEY: reason}
                updated = replace(worker, state=WorkerState.OFFLINE, metadata=metadata)
                self._workers[worker_id] = updated
                expired.append(updated)
        return expired

    def expire_deadline_jobs(
        self, *, now: datetime | None = None
    ) -> list[JobRecord]:
        timestamp = _aware(now)
        expired: list[JobRecord] = []
        with self._lock:
            for job_id, job in list(self._jobs.items()):
                if (
                    job.status not in {JobStatus.QUEUED, JobStatus.RUNNING}
                    or job.deadline_at is None
                    or job.deadline_at > timestamp
                ):
                    continue
                if job.status is JobStatus.RUNNING:
                    self._release_worker_capacity(job.assigned_worker_id)
                updated = job.with_updates(
                    status=JobStatus.FAILED,
                    error={
                        "type": "deadline_expired",
                        "message": "job deadline expired",
                        "retryable": False,
                    },
                    assigned_worker_id=None,
                    lease_token=None,
                    lease_expires_at=None,
                    finished_at=timestamp,
                    updated_at=timestamp,
                )
                self._jobs[job_id] = updated
                expired.append(updated)
        return expired

    def submit_job(
        self, submission: JobSubmission, *, now: datetime | None = None,
        admission_deadline_at: datetime | None = None,
    ) -> JobRecord:
        timestamp = _aware(now)
        if admission_deadline_at is not None and (
            not isinstance(admission_deadline_at, datetime)
            or admission_deadline_at.tzinfo is None
        ):
            raise ValueError("admission deadline must be timezone-aware")
        if (
            admission_deadline_at is not None
            and submission.deadline_at is not None
            and admission_deadline_at > submission.deadline_at
        ):
            raise ValueError("admission deadline cannot exceed serving Job deadline")
        with self._lock:
            # Check a fresh wall clock under the lock. An injected request
            # creation timestamp must not make a late submission admissible.
            if (
                admission_deadline_at is not None
                and utc_now() >= admission_deadline_at
            ):
                raise DeadlineExceededError("admission deadline expired")
            # Resolve durable idempotency before transient admission state.
            if submission.idempotency_key:
                existing_id = self._idempotency.get(submission.idempotency_key)
                if existing_id is not None:
                    existing = self._jobs[existing_id]
                    if not _submission_matches(existing, submission):
                        raise ConflictError(
                            "idempotency key already belongs to a different job request"
                        )
                    return existing

            if (
                submission.deadline_at is not None
                and _aware(submission.deadline_at) <= timestamp
            ):
                raise DeadlineExceededError("job deadline has expired")

            if submission.serving is not None:
                cutoff = timestamp - timedelta(seconds=self.worker_ttl_seconds)
                compatible = [
                    worker
                    for worker in self._workers.values()
                    if worker.state is WorkerState.ONLINE
                    and not any(key[0] == worker.worker_id for key in self._audio_epoch_preparations)
                    and (
                        worker.serving is None
                        or (worker.worker_id, worker.serving.runtime_instance.epoch)
                        not in self._audio_epoch_fences
                    )
                    and worker.last_seen_at >= cutoff
                    and submission.capability in worker.spec.capabilities
                    and worker_matches(worker.spec, submission.requirements)
                    and worker_serving_accepts_job(
                        worker.serving, submission.capability, submission.serving
                    )
                ]
                if not compatible:
                    raise NoCompatibleDeployment(
                        "no compatible serving deployment is available"
                    )
                if all(
                    worker.active_jobs >= worker.max_concurrency
                    for worker in compatible
                ):
                    raise OverloadedError(
                        "all compatible serving deployments are at capacity"
                    )

            self._sequence += 1
            record = JobRecord(
                job_id=str(uuid4()),
                capability=submission.capability,
                payload=submission.payload,
                requirements=submission.requirements,
                priority=submission.priority,
                sequence=self._sequence,
                status=JobStatus.QUEUED,
                attempts=0,
                max_attempts=submission.max_attempts,
                idempotency_key=submission.idempotency_key,
                created_at=timestamp,
                available_at=_aware(submission.available_at)
                if submission.available_at is not None
                else timestamp,
                deadline_at=(
                    None
                    if submission.deadline_at is None
                    else _aware(submission.deadline_at)
                ),
                serving=submission.serving,
                updated_at=timestamp,
            )
            self._jobs[record.job_id] = record
            if submission.idempotency_key:
                self._idempotency[submission.idempotency_key] = record.job_id
            return record

    def get_job(self, job_id: str) -> JobRecord:
        with self._lock:
            try:
                return self._jobs[job_id]
            except KeyError as exc:
                raise NotFoundError(f"job not found: {job_id}") from exc

    def get_job_by_idempotency_key(
        self, key: str, *, admission_deadline_at: datetime | None = None
    ) -> JobRecord | None:
        with self._lock:
            job_id = self._idempotency.get(key)
            return None if job_id is None else self._jobs[job_id]

    def list_jobs(self) -> list[JobRecord]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda item: item.sequence)

    def append_job_event(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_token: str,
        runtime_instance_epoch: str | None,
        kind: str,
        payload: Mapping[str, object],
        now: datetime | None = None,
    ) -> JobEventRecord:
        timestamp = _aware(now)
        normalized_kind = str(kind).strip()
        if not normalized_kind:
            raise ValueError("event kind must not be blank")
        size = _event_size(normalized_kind, payload)
        if size > self.event_max_payload_bytes:
            raise EventBufferFull("job event payload exceeds the configured limit")
        with self._lock:
            job = self.get_job(job_id)
            self._assert_running(
                job,
                worker_id=worker_id,
                lease_token=lease_token,
                runtime_instance_epoch=runtime_instance_epoch,
                now=timestamp,
            )
            if runtime_instance_epoch != job.claimed_runtime_instance_epoch:
                raise ConflictError(f"job {job.job_id} runtime instance is stale")
            events = self._events.setdefault(job_id, [])
            total = self._event_bytes.get(job_id, 0)
            if len(events) >= self.event_max_count:
                raise EventBufferFull("job event count exceeds the configured limit")
            if total + size > self.event_max_total_bytes:
                raise EventBufferFull("job event buffer exceeds the configured byte limit")
            record = JobEventRecord(
                job_id=job.job_id,
                attempt=job.attempts,
                sequence=len(events) + 1,
                worker_id=worker_id,
                runtime_instance_epoch=job.claimed_runtime_instance_epoch,
                kind=normalized_kind,
                payload=dict(payload),
                created_at=timestamp,
            )
            events.append(record)
            self._event_bytes[job_id] = total + size
            return record

    def list_job_events(
        self,
        job_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 128,
    ) -> list[JobEventRecord]:
        if type(after_sequence) is not int or after_sequence < 0:
            raise ValueError("after_sequence must be a non-negative integer")
        if type(limit) is not int or not 1 <= limit <= self.event_max_read:
            raise ValueError("event read limit is outside the configured bound")
        with self._lock:
            self.get_job(job_id)
            return [
                event
                for event in self._events.get(job_id, ())
                if event.sequence > after_sequence
            ][:limit]

    def claim_next_job(
        self,
        worker_id: str,
        *,
        runtime_instance_epoch: str | None = None,
        now: datetime | None = None,
    ) -> JobRecord | None:
        timestamp = _aware(now)
        with self._lock:
            self._assert_audio_epoch_not_fenced(worker_id, runtime_instance_epoch)
            worker = self.get_worker(worker_id)
            _assert_worker_epoch(worker, runtime_instance_epoch)
            if timestamp - worker.last_seen_at > timedelta(seconds=self.worker_ttl_seconds):
                reason = (
                    RECOVERABLE_OFFLINE_REASON
                    if worker.state is WorkerState.ONLINE else "draining_expired"
                )
                metadata = {**dict(worker.metadata), OFFLINE_REASON_KEY: reason}
                self._workers[worker_id] = replace(
                    worker, state=WorkerState.OFFLINE, metadata=metadata
                )
                return None
            if worker.state is not WorkerState.ONLINE:
                return None
            if worker.active_jobs >= worker.max_concurrency:
                return None

            eligible = [
                job
                for job in self._jobs.values()
                if job.status is JobStatus.QUEUED
                and job.available_at <= timestamp
                and (job.deadline_at is None or job.deadline_at > timestamp)
                and job.attempts < job.max_attempts
                and job.capability in worker.spec.capabilities
                and worker_matches(worker.spec, job.requirements)
                and worker_serving_accepts_job(worker.serving, job.capability, job.serving)
            ]
            if not eligible:
                return None
            eligible.sort(key=lambda job: (-job.priority, job.sequence))
            selected = eligible[0]
            claimed = selected.with_updates(
                status=JobStatus.RUNNING,
                attempts=selected.attempts + 1,
                assigned_worker_id=worker_id,
                lease_token=str(uuid4()),
                lease_expires_at=_lease_expiry(
                    timestamp, self.lease_seconds, selected.deadline_at
                ),
                started_at=timestamp,
                finished_at=None,
                updated_at=timestamp,
                error=None,
                claimed_deployment_revision=(
                    None if selected.serving is None else worker.serving.deployment_revision
                ),
                claimed_serving_contract_revision=(
                    None if selected.serving is None else selected.serving.serving_contract_revision
                ),
                claimed_runtime_instance_epoch=(
                    None
                    if selected.serving is None
                    else worker.serving.runtime_instance.epoch
                ),
            )
            self._jobs[claimed.job_id] = claimed
            self._workers[worker_id] = replace(
                worker, active_jobs=worker.active_jobs + 1
            )
            return claimed

    def _assert_running(
        self,
        job: JobRecord,
        *,
        worker_id: str,
        lease_token: str,
        runtime_instance_epoch: str | None,
        now: datetime,
    ) -> None:
        self._assert_audio_epoch_not_fenced(
            worker_id, job.claimed_runtime_instance_epoch
        )
        if job.status is not JobStatus.RUNNING:
            raise ConflictError(f"job {job.job_id} is not running")
        if job.assigned_worker_id != worker_id:
            raise ConflictError(f"job {job.job_id} is assigned to another worker")
        if not lease_token or job.lease_token != lease_token:
            raise ConflictError(f"job {job.job_id} lease token is stale")
        if job.lease_expires_at is None or job.lease_expires_at <= now:
            raise ConflictError(f"job {job.job_id} lease has expired")
        if job.deadline_at is not None and job.deadline_at <= now:
            raise ConflictError(f"job {job.job_id} deadline has expired")
        if (
            job.claimed_runtime_instance_epoch is not None
            and runtime_instance_epoch != job.claimed_runtime_instance_epoch
        ):
            raise ConflictError(f"job {job.job_id} runtime instance is stale")

    def _release_worker_capacity(self, worker_id: str | None) -> None:
        if worker_id is None:
            return
        worker = self._workers.get(worker_id)
        if worker is None:
            return
        self._workers[worker_id] = replace(
            worker, active_jobs=max(worker.active_jobs - 1, 0)
        )

    def complete_job(
        self,
        job_id: str,
        result: JobResult,
        *,
        worker_id: str,
        lease_token: str,
        runtime_instance_epoch: str | None = None,
        now: datetime | None = None,
    ) -> JobRecord:
        timestamp = _aware(now)
        if not isinstance(result, JobResult):
            raise TypeError("result must be JobResult")
        with self._lock:
            job = self.get_job(job_id)
            self._assert_running(
                job,
                worker_id=worker_id,
                lease_token=lease_token,
                runtime_instance_epoch=runtime_instance_epoch,
                now=timestamp,
            )
            completed = job.with_updates(
                status=JobStatus.SUCCEEDED,
                result=result,
                error=None,
                assigned_worker_id=None,
                lease_token=None,
                lease_expires_at=None,
                finished_at=timestamp,
                updated_at=timestamp,
            )
            self._jobs[job_id] = completed
            self._release_worker_capacity(worker_id)
            return completed

    def fail_job(
        self,
        job_id: str,
        error: str | dict[str, object],
        *,
        retryable: bool,
        worker_id: str,
        lease_token: str,
        runtime_instance_epoch: str | None = None,
        now: datetime | None = None,
    ) -> JobRecord:
        timestamp = _aware(now)
        with self._lock:
            job = self.get_job(job_id)
            self._assert_running(
                job,
                worker_id=worker_id,
                lease_token=lease_token,
                runtime_instance_epoch=runtime_instance_epoch,
                now=timestamp,
            )
            should_retry = (
                retryable
                and job.attempts < job.max_attempts
                and (job.deadline_at is None or job.deadline_at > timestamp)
            )
            failed = job.with_updates(
                status=JobStatus.QUEUED if should_retry else JobStatus.FAILED,
                error=_failure_payload(error, retryable=should_retry),
                assigned_worker_id=None,
                lease_token=None,
                lease_expires_at=None,
                started_at=None if should_retry else job.started_at,
                finished_at=None if should_retry else timestamp,
                available_at=timestamp if should_retry else job.available_at,
                claimed_deployment_revision=(
                    None if should_retry else job.claimed_deployment_revision
                ),
                claimed_serving_contract_revision=(
                    None if should_retry else job.claimed_serving_contract_revision
                ),
                claimed_runtime_instance_epoch=(
                    None if should_retry else job.claimed_runtime_instance_epoch
                ),
                updated_at=timestamp,
            )
            self._jobs[job_id] = failed
            self._release_worker_capacity(worker_id)
            return failed

    def cancel_job(
        self, job_id: str, *, now: datetime | None = None,
        admission_deadline_at: datetime | None = None,
    ) -> JobRecord:
        if admission_deadline_at is not None:
            if not isinstance(admission_deadline_at, datetime) or admission_deadline_at.tzinfo is None:
                raise ValueError("admission_deadline_at must be timezone-aware")
            if utc_now() >= admission_deadline_at:
                raise DeadlineExceededError("chat cancellation deadline expired")
        timestamp = _aware(now)
        with self._lock:
            if admission_deadline_at is not None and utc_now() >= admission_deadline_at:
                raise DeadlineExceededError("chat cancellation deadline expired")
            job = self.get_job(job_id)
            if job.status in {
                JobStatus.SUCCEEDED,
                JobStatus.FAILED,
                JobStatus.CANCELLED,
            }:
                return job
            if job.status is JobStatus.RUNNING:
                self._release_worker_capacity(job.assigned_worker_id)
            cancelled = job.with_updates(
                status=JobStatus.CANCELLED,
                assigned_worker_id=None,
                lease_token=None,
                lease_expires_at=None,
                finished_at=timestamp,
                updated_at=timestamp,
            )
            self._jobs[job_id] = cancelled
            return cancelled

    def recover_expired_jobs(
        self, *, now: datetime | None = None
    ) -> list[JobRecord]:
        timestamp = _aware(now)
        recovered: list[JobRecord] = []
        with self._lock:
            for job_id, job in list(self._jobs.items()):
                if (
                    job.status is not JobStatus.RUNNING
                    or job.lease_expires_at is None
                    or job.lease_expires_at > timestamp
                ):
                    continue
                self._release_worker_capacity(job.assigned_worker_id)
                deadline_expired = (
                    job.deadline_at is not None and job.deadline_at <= timestamp
                )
                should_retry = (
                    not deadline_expired and job.attempts < job.max_attempts
                )
                updated = job.with_updates(
                    status=JobStatus.QUEUED if should_retry else JobStatus.FAILED,
                    error=(
                        {
                            "type": "deadline_expired",
                            "message": "job deadline expired",
                            "retryable": False,
                        }
                        if deadline_expired
                        else {
                            "message": "worker lease expired",
                            "retryable": should_retry,
                        }
                    ),
                    assigned_worker_id=None,
                    lease_token=None,
                    lease_expires_at=None,
                    started_at=None if should_retry else job.started_at,
                    finished_at=None if should_retry else timestamp,
                    available_at=timestamp if should_retry else job.available_at,
                    claimed_deployment_revision=(
                        None if should_retry else job.claimed_deployment_revision
                    ),
                    claimed_serving_contract_revision=(
                        None if should_retry else job.claimed_serving_contract_revision
                    ),
                    claimed_runtime_instance_epoch=(
                        None if should_retry else job.claimed_runtime_instance_epoch
                    ),
                    updated_at=timestamp,
                )
                self._jobs[job_id] = updated
                recovered.append(updated)
        return recovered


__all__ = [
    "ConflictError",
    "ControlRepository",
    "DeadlineExceededError",
    "EventBufferFull",
    "InMemoryControlRepository",
    "NoCompatibleDeployment",
    "NotFoundError",
    "OverloadedError",
    "RepositoryError",
    "StorageUnavailable",
]
