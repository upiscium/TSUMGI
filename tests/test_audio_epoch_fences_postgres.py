"""Real PostgreSQL verification for audio epoch tombstones; skips are NOT_VERIFIED."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from astrumweaver.control import PostgresControlRepository
from audio_epoch_fence_cases import AudioEpochFenceCases, clock, fence
from test_audio_artifact_authority import EPOCH, setup_claim
from test_audio_artifact_authority_postgres import isolated_database, wait_for_blocker
from test_audio_epoch_acceptance_postgres import postgres17_database


@pytest.fixture
def authority_repo(postgres17_database):
    repo = PostgresControlRepository(postgres17_database, lease_seconds=30, pool_min_size=0, pool_max_size=2)
    try:
        yield repo
    finally:
        repo.close()


class TestPostgresAudioEpochFences(AudioEpochFenceCases):
    """Each inherited test uses a fresh disposable database."""


def test_fence_is_independently_read_after_repository_reopen(authority_repo, postgres17_database, clock):
    from astrumweaver.control import ConflictError
    from astrumweaver.execution import JobResult

    claim = setup_claim(authority_repo, clock.now)
    expected = fence(authority_repo)
    authority_repo.close()
    reopened = PostgresControlRepository(postgres17_database, pool_min_size=0, pool_max_size=1)
    try:
        assert reopened.get_audio_epoch_fence("owner", EPOCH) == expected
        with pytest.raises(ConflictError):
            reopened.complete_job(claim.job_id, JobResult(text="synthetic"), worker_id="owner",
                lease_token=claim.lease_token, runtime_instance_epoch=EPOCH)
    finally:
        reopened.close()


def test_fence_does_not_wait_for_a_job_row_lock(authority_repo, postgres17_database, clock):
    import psycopg

    repo = authority_repo
    claim = setup_claim(repo, clock.now)
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        with psycopg.connect(postgres17_database) as blocker:
            blocker.execute("SELECT id FROM jobs WHERE id::text = %s FOR UPDATE", (claim.job_id,))
            future = pool.submit(fence, repo)
            record = future.result(timeout=5)
            assert record.runtime_instance_epoch == EPOCH
            blocker.rollback()
    finally:
        pool.shutdown(wait=True)


def test_worker_row_lock_serializes_fence_without_job_mutation(authority_repo, postgres17_database, clock):
    import psycopg

    repo = authority_repo
    claim = setup_claim(repo, clock.now)
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        with psycopg.connect(postgres17_database) as blocker, psycopg.connect(
            postgres17_database, autocommit=True,
        ) as observer:
            blocker.execute("SELECT id FROM workers WHERE id = 'owner' FOR UPDATE")
            future = pool.submit(fence, repo)
            wait_for_blocker(observer, blocker.info.backend_pid, future)
            blocker.commit()
            record = future.result(timeout=10)
            assert repo.get_audio_epoch_fence("owner", EPOCH) == record
            assert repo.get_job(claim.job_id).status.value == "running"
    finally:
        pool.shutdown(wait=True)


@pytest.mark.parametrize("operation", ["complete", "event", "heartbeat", "fail"])
def test_positive_mutation_waits_for_worker_lock(
    authority_repo, postgres17_database, clock, operation,
):
    import psycopg

    repo = authority_repo
    claim = setup_claim(repo, clock.now)
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        with psycopg.connect(postgres17_database) as blocker, psycopg.connect(
            postgres17_database, autocommit=True,
        ) as observer:
            blocker.execute("SELECT id FROM workers WHERE id = 'owner' FOR UPDATE")
            future = pool.submit(_positive_operation, repo, claim, clock.now, operation)
            wait_for_blocker(observer, blocker.info.backend_pid, future)
            blocker.commit()
            assert future.result(timeout=5) is not None
    finally:
        pool.shutdown(wait=True)


@pytest.mark.parametrize("operation", ["complete", "event", "heartbeat", "fail"])
def test_fence_commits_while_job_locked_then_waiting_positive_operation_is_denied(
    authority_repo, postgres17_database, clock, operation,
):
    import psycopg
    from astrumweaver.control import ConflictError

    repo = authority_repo
    claim = setup_claim(repo, clock.now)
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        with psycopg.connect(postgres17_database) as blocker, psycopg.connect(
            postgres17_database, autocommit=True,
        ) as observer:
            blocker.execute("SELECT id FROM jobs WHERE id::text = %s FOR UPDATE", (claim.job_id,))
            future = pool.submit(_positive_operation, repo, claim, clock.now, operation)
            wait_for_blocker(observer, blocker.info.backend_pid, future)
            # The negative-only tombstone takes the Worker lock but no Job lock.
            record = fence(repo)
            assert record.runtime_instance_epoch == EPOCH
            blocker.commit()
            with pytest.raises(ConflictError):
                future.result(timeout=5)
    finally:
        pool.shutdown(wait=True)


def _positive_operation(repo, claim, now, operation):
    if operation == "complete":
        from astrumweaver.execution import JobResult

        return repo.complete_job(claim.job_id, JobResult(text="synthetic"),
            worker_id="owner", lease_token=claim.lease_token,
            runtime_instance_epoch=EPOCH, now=now)
    if operation == "event":
        return repo.append_job_event(claim.job_id, worker_id="owner",
            lease_token=claim.lease_token, runtime_instance_epoch=EPOCH,
            kind="progress", payload={}, now=now)
    if operation == "heartbeat":
        from astrumweaver.control import WorkerHeartbeat

        return repo.heartbeat_worker("owner", WorkerHeartbeat(
            runtime_instance_epoch=EPOCH, active_job_id=claim.job_id,
            lease_token=claim.lease_token,
        ), now=now)
    return repo.fail_job(claim.job_id, "synthetic", retryable=True,
        worker_id="owner", lease_token=claim.lease_token,
        runtime_instance_epoch=EPOCH, now=now)
