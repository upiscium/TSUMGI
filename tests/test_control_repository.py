from __future__ import annotations

from datetime import timedelta

import pytest

from astrumweaver import JobRequirements, JobResult, ResourceShape, WorkerSpec
from astrumweaver.control import (
    ConflictError,
    InMemoryControlRepository,
    JobStatus,
    JobSubmission,
    WorkerHeartbeat,
    WorkerRegistration,
    WorkerState,
    utc_now,
)


def single_worker(
    worker_id: str = "worker-a",
    *,
    capabilities: frozenset[str] = frozenset({"llm.chat"}),
    max_concurrency: int = 1,
) -> WorkerRegistration:
    return WorkerRegistration(
        spec=WorkerSpec(
            worker_id=worker_id,
            worker_class="modern-single",
            resources=ResourceShape(
                gpu_count=1,
                total_vram_mb=24_576,
                max_single_gpu_vram_mb=24_576,
            ),
            gpu_uuids=(f"GPU-{worker_id}",),
            capabilities=capabilities,
            labels={"runtime_family": "modern"},
        ),
        max_concurrency=max_concurrency,
    )


def multi_gpu_worker() -> WorkerRegistration:
    return WorkerRegistration(
        spec=WorkerSpec(
            worker_id="worker-multi",
            worker_class="multi-gpu",
            resources=ResourceShape(
                gpu_count=2,
                total_vram_mb=24_576,
                max_single_gpu_vram_mb=12_288,
            ),
            gpu_uuids=("GPU-multi-a", "GPU-multi-b"),
            capabilities=frozenset({"llm.chat", "code.review"}),
        )
    )


def test_priority_then_fifo_sequence_is_explicit() -> None:
    now = utc_now()
    repo = InMemoryControlRepository()
    worker = repo.register_worker(single_worker(), now=now)

    low = repo.submit_job(
        JobSubmission(capability="llm.chat", payload={"name": "low"}, priority=1),
        now=now,
    )
    first_high = repo.submit_job(
        JobSubmission(capability="llm.chat", payload={"name": "first"}, priority=10),
        now=now,
    )
    second_high = repo.submit_job(
        JobSubmission(capability="llm.chat", payload={"name": "second"}, priority=10),
        now=now,
    )

    claimed = repo.claim_next_job(worker.worker_id, now=now)
    assert claimed is not None
    assert claimed.job_id == first_high.job_id
    assert first_high.sequence < second_high.sequence
    assert low.sequence < first_high.sequence

    repo.complete_job(
        claimed.job_id,
        JobResult(outputs={"ok": True}),
        worker_id=worker.worker_id,
        lease_token=claimed.lease_token or "",
        now=now,
    )
    next_claim = repo.claim_next_job(worker.worker_id, now=now)
    assert next_claim is not None
    assert next_claim.job_id == second_high.job_id


def test_control_claim_uses_capability_and_new_resource_shape() -> None:
    now = utc_now()
    repo = InMemoryControlRepository()
    worker = repo.register_worker(multi_gpu_worker(), now=now)

    single_large = repo.submit_job(
        JobSubmission(
            capability="llm.chat",
            payload={"kind": "single-large"},
            priority=10,
            requirements=JobRequirements(min_single_gpu_vram_mb=20_000),
        ),
        now=now,
    )
    shardable = repo.submit_job(
        JobSubmission(
            capability="llm.chat",
            payload={"kind": "shardable"},
            priority=1,
            requirements=JobRequirements(
                min_gpu_count=2,
                min_total_vram_mb=20_000,
            ),
        ),
        now=now,
    )

    claimed = repo.claim_next_job(worker.worker_id, now=now)

    assert claimed is not None
    assert claimed.job_id == shardable.job_id
    assert repo.get_job(single_large.job_id).status is JobStatus.QUEUED


def test_job_capability_must_be_advertised_even_without_extra_requirements() -> None:
    now = utc_now()
    repo = InMemoryControlRepository()
    worker = repo.register_worker(single_worker(capabilities=frozenset({"llm.chat"})), now=now)
    repo.submit_job(JobSubmission(capability="image.generate"), now=now)

    assert repo.claim_next_job(worker.worker_id, now=now) is None


def test_idempotency_key_is_replay_safe_and_conflict_checked() -> None:
    repo = InMemoryControlRepository()
    request = JobSubmission(
        capability="llm.chat",
        payload={"prompt": "hello"},
        idempotency_key="request-1",
    )
    first = repo.submit_job(request)
    second = repo.submit_job(request)

    assert second.job_id == first.job_id

    with pytest.raises(ConflictError, match="different job request"):
        repo.submit_job(
            JobSubmission(
                capability="llm.chat",
                payload={"prompt": "changed"},
                idempotency_key="request-1",
            )
        )


def test_retry_requeues_until_max_attempts_then_fails() -> None:
    now = utc_now()
    repo = InMemoryControlRepository()
    worker = repo.register_worker(single_worker(), now=now)
    job = repo.submit_job(
        JobSubmission(capability="llm.chat", max_attempts=2),
        now=now,
    )

    first = repo.claim_next_job(worker.worker_id, now=now)
    assert first is not None
    retry = repo.fail_job(
        job.job_id,
        "transient",
        retryable=True,
        worker_id=worker.worker_id,
        lease_token=first.lease_token or "",
        now=now,
    )
    assert retry.status is JobStatus.QUEUED

    second = repo.claim_next_job(worker.worker_id, now=now)
    assert second is not None
    terminal = repo.fail_job(
        job.job_id,
        "still failing",
        retryable=True,
        worker_id=worker.worker_id,
        lease_token=second.lease_token or "",
        now=now,
    )
    assert terminal.status is JobStatus.FAILED
    assert terminal.attempts == 2


def test_expired_lease_is_fenced_before_recovery_and_old_token_stays_stale() -> None:
    now = utc_now()
    repo = InMemoryControlRepository(lease_seconds=1)
    worker = repo.register_worker(single_worker(), now=now)
    job = repo.submit_job(JobSubmission(capability="llm.chat"), now=now)
    first = repo.claim_next_job(worker.worker_id, now=now)
    assert first is not None and first.lease_token

    expired = now + timedelta(seconds=2)
    with pytest.raises(ConflictError, match="lease has expired"):
        repo.complete_job(
            job.job_id,
            JobResult(outputs={"stale": True}),
            worker_id=worker.worker_id,
            lease_token=first.lease_token,
            now=expired,
        )

    recovered = repo.recover_expired_jobs(now=expired)
    assert [item.job_id for item in recovered] == [job.job_id]

    second = repo.claim_next_job(worker.worker_id, now=expired)
    assert second is not None and second.lease_token
    assert second.lease_token != first.lease_token

    with pytest.raises(ConflictError):
        repo.complete_job(
            job.job_id,
            JobResult(outputs={"stale": True}),
            worker_id=worker.worker_id,
            lease_token=first.lease_token,
            now=expired,
        )


def test_active_heartbeat_renews_fenced_lease_without_trusting_active_count() -> None:
    now = utc_now()
    repo = InMemoryControlRepository(lease_seconds=10)
    worker = repo.register_worker(single_worker(), now=now)
    job = repo.submit_job(JobSubmission(capability="llm.chat"), now=now)
    claimed = repo.claim_next_job(worker.worker_id, now=now)
    assert claimed is not None and claimed.lease_token

    heartbeat = repo.heartbeat_worker(
        worker.worker_id,
        WorkerHeartbeat(
            active_job_id=job.job_id,
            lease_token=claimed.lease_token,
            metadata={"temperature": "nominal"},
        ),
        now=now + timedelta(seconds=5),
    )

    assert heartbeat.active_jobs == 1
    assert heartbeat.metadata["temperature"] == "nominal"
    assert repo.recover_expired_jobs(now=now + timedelta(seconds=12)) == []
    assert repo.get_job(job.job_id).status is JobStatus.RUNNING


def test_stale_worker_becomes_offline_and_cannot_claim() -> None:
    now = utc_now()
    repo = InMemoryControlRepository(worker_ttl_seconds=10)
    worker = repo.register_worker(single_worker(), now=now)
    repo.submit_job(JobSubmission(capability="llm.chat"), now=now)

    assert repo.claim_next_job(
        worker.worker_id, now=now + timedelta(seconds=11)
    ) is None
    assert repo.get_worker(worker.worker_id).state is WorkerState.OFFLINE


def test_draining_worker_finishes_existing_work_but_claims_no_new_job() -> None:
    now = utc_now()
    repo = InMemoryControlRepository()
    worker = repo.register_worker(single_worker(), now=now)
    first_job = repo.submit_job(JobSubmission(capability="llm.chat"), now=now)
    claimed = repo.claim_next_job(worker.worker_id, now=now)
    assert claimed is not None and claimed.job_id == first_job.job_id

    repo.set_worker_state(worker.worker_id, WorkerState.DRAINING, now=now)
    repo.submit_job(JobSubmission(capability="llm.chat"), now=now)
    assert repo.claim_next_job(worker.worker_id, now=now) is None

    completed = repo.complete_job(
        claimed.job_id,
        JobResult(outputs={"ok": True}),
        worker_id=worker.worker_id,
        lease_token=claimed.lease_token or "",
        now=now,
    )
    assert completed.status is JobStatus.SUCCEEDED


def test_overlapping_gpu_ownership_is_rejected_until_released() -> None:
    repo = InMemoryControlRepository()
    first = repo.register_worker(single_worker("worker-a"))

    overlapping = WorkerRegistration(
        spec=WorkerSpec(
            worker_id="worker-b",
            worker_class="modern-single",
            resources=first.spec.resources,
            gpu_uuids=first.spec.gpu_uuids,
            capabilities=frozenset({"llm.chat"}),
        )
    )
    with pytest.raises(ConflictError, match="overlaps"):
        repo.register_worker(overlapping)

    repo.set_worker_state(first.worker_id, WorkerState.OFFLINE)
    second = repo.register_worker(overlapping)
    assert second.state is WorkerState.ONLINE


def test_active_worker_cannot_change_resource_topology_on_reregistration() -> None:
    now = utc_now()
    repo = InMemoryControlRepository()
    worker = repo.register_worker(single_worker(), now=now)
    repo.submit_job(JobSubmission(capability="llm.chat"), now=now)
    claimed = repo.claim_next_job(worker.worker_id, now=now)
    assert claimed is not None

    changed = WorkerRegistration(
        spec=WorkerSpec(
            worker_id=worker.worker_id,
            worker_class="multi-gpu",
            resources=ResourceShape(
                gpu_count=2,
                total_vram_mb=32_768,
                max_single_gpu_vram_mb=16_384,
            ),
            gpu_uuids=("GPU-new-a", "GPU-new-b"),
            capabilities=frozenset({"llm.chat"}),
        )
    )
    with pytest.raises(ConflictError, match="cannot change"):
        repo.register_worker(changed, now=now)


def test_stale_offline_registration_recovers_only_with_matching_identity():
    from dataclasses import replace
    from datetime import timedelta
    from astrumweaver.control.repository import (
        InMemoryControlRepository, ConflictError, OFFLINE_REASON_KEY,
    )
    from astrumweaver.control.models import WorkerState, WorkerRegistration, utc_now
    from astrumweaver import ResourceShape, WorkerSpec
    repo = InMemoryControlRepository(worker_ttl_seconds=1)
    spec = WorkerSpec(
        worker_id="ttl-recover", worker_class="gpu",
        resources=ResourceShape(gpu_count=1, total_vram_mb=11000, max_single_gpu_vram_mb=11000),
        gpu_uuids=("GPU-ttl-recover",), capabilities=frozenset({"llm.chat"}),
    )
    registration = WorkerRegistration(spec=spec)
    repo.register_worker(registration)
    with pytest.raises(ConflictError, match="not eligible"):
        repo.register_worker(registration, recovery=True)
    repo.expire_stale_workers(now=utc_now()+timedelta(seconds=3))
    offline = repo.get_worker(spec.worker_id)
    assert offline.state is WorkerState.OFFLINE
    assert offline.metadata.get(OFFLINE_REASON_KEY) == "heartbeat_timeout"
    wrong = WorkerRegistration(spec=replace(spec, worker_class="other"))
    with pytest.raises(ConflictError, match="identity"):
        repo.register_worker(wrong, recovery=True)
    assert repo.register_worker(registration, recovery=True).state is WorkerState.ONLINE
    assert OFFLINE_REASON_KEY not in repo.get_worker(spec.worker_id).metadata
    repo.set_worker_state(spec.worker_id, WorkerState.OFFLINE)
    with pytest.raises(ConflictError, match="not eligible"):
        repo.register_worker(registration, recovery=True)


def test_control_reserved_offline_metadata_cannot_be_forged_via_heartbeat():
    from astrumweaver.control.repository import InMemoryControlRepository, ConflictError, OFFLINE_REASON_KEY
    from astrumweaver.control.models import WorkerHeartbeat, WorkerRegistration
    from astrumweaver import ResourceShape, WorkerSpec
    repo = InMemoryControlRepository()
    spec = WorkerSpec(worker_id="reserved-metadata", worker_class="gpu",
        resources=ResourceShape(gpu_count=1, total_vram_mb=100, max_single_gpu_vram_mb=100),
        gpu_uuids=("GPU-reserved",), capabilities=frozenset({"llm.chat"}))
    repo.register_worker(WorkerRegistration(spec=spec))
    with pytest.raises(ConflictError, match="Control-owned"):
        repo.heartbeat_worker(spec.worker_id, WorkerHeartbeat(
            metadata={OFFLINE_REASON_KEY: "heartbeat_timeout"}
        ))
    with pytest.raises(ConflictError, match="Control-owned"):
        repo.register_worker(WorkerRegistration(spec=spec,metadata={OFFLINE_REASON_KEY:"heartbeat_timeout"}))


def test_draining_ttl_expiry_does_not_recover_or_revoke_operator_drain():
    from datetime import timedelta
    from astrumweaver.control.repository import (
        InMemoryControlRepository, ConflictError, OFFLINE_REASON_KEY,
    )
    from astrumweaver.control.models import WorkerState, WorkerRegistration, utc_now
    from astrumweaver import WorkerSpec, ResourceShape
    repo = InMemoryControlRepository(worker_ttl_seconds=1)
    spec = WorkerSpec(worker_id="draining-expiry",worker_class="gpu",
        resources=ResourceShape(gpu_count=1,total_vram_mb=100,max_single_gpu_vram_mb=100),
        gpu_uuids=("GPU-draining",),capabilities=frozenset({"llm.chat"}))
    registered = WorkerRegistration(spec=spec)
    repo.register_worker(registered)
    repo.set_worker_state(spec.worker_id, WorkerState.DRAINING)
    repo.expire_stale_workers(now=utc_now()+timedelta(seconds=4))
    expired=repo.get_worker(spec.worker_id)
    assert expired.state is WorkerState.OFFLINE
    assert expired.metadata[OFFLINE_REASON_KEY]=="draining_expired"
    with pytest.raises(ConflictError,match="not eligible"):
        repo.register_worker(registered,recovery=True)


def test_lazy_claim_expiry_marks_only_online_as_recoverable():
    from datetime import timedelta
    from astrumweaver.control.repository import InMemoryControlRepository, OFFLINE_REASON_KEY
    from astrumweaver.control.models import WorkerState, WorkerRegistration, utc_now
    from astrumweaver import WorkerSpec, ResourceShape
    repo=InMemoryControlRepository(worker_ttl_seconds=1)
    spec=WorkerSpec(worker_id="claim-lazy-expiry", worker_class="gpu",
        resources=ResourceShape(gpu_count=1,total_vram_mb=100,max_single_gpu_vram_mb=100),
        gpu_uuids=("GPU-lazy",),capabilities=frozenset({"llm.chat"}))
    repo.register_worker(WorkerRegistration(spec=spec))
    assert repo.claim_next_job(spec.worker_id,now=utc_now()+timedelta(seconds=3)) is None
    assert repo.get_worker(spec.worker_id).state is WorkerState.OFFLINE
    assert repo.get_worker(spec.worker_id).metadata[OFFLINE_REASON_KEY]=="heartbeat_timeout"
