"""Real PostgreSQL acceptance: skips are NOT_VERIFIED, never PostgreSQL PASS.

Each test gets a disposable database, not TRUNCATE of a shared host database.
Requires ASTRUMWEAVER_TEST_DATABASE_URL and CREATEDB on that test-only server.
No mocked connections, locks, transactions, or authorization outcomes.
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from time import monotonic, sleep
from uuid import uuid4

import pytest

from astrumweaver.control import PostgresControlRepository
from astrumweaver.control.migrate import apply_migrations
from test_audio_artifact_authority import (
    AuthorityCases, EPOCH, NEXT_EPOCH, authorize, clock, rejected,
    setup_claim,
)


@pytest.fixture
def isolated_database():
    psycopg = pytest.importorskip("psycopg", reason="NOT_VERIFIED: real PostgreSQL driver unavailable")
    source = os.environ.get("ASTRUMWEAVER_TEST_DATABASE_URL")
    if not source:
        pytest.skip("NOT_VERIFIED: ASTRUMWEAVER_TEST_DATABASE_URL absent")
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    name = "audio_authority_" + uuid4().hex
    # Connection failures are failures, not a silent fallback to memory.
    with psycopg.connect(source, autocommit=True) as admin:
        try:
            admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8'").format(sql.Identifier(name)))
        except psycopg.errors.InsufficientPrivilege:
            pytest.skip("NOT_VERIFIED: isolated PostgreSQL fixture requires CREATEDB")
        try:
            dsn = make_conninfo(source, dbname=name, application_name=name,
                                options="-c statement_timeout=10000 -c lock_timeout=8000")
            apply_migrations(dsn)
            yield dsn
        finally:
            admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()", (name,))
            admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))


@pytest.fixture
def authority_repo(isolated_database):
    return PostgresControlRepository(isolated_database, lease_seconds=30)


class TestPostgresAudioAuthority(AuthorityCases):
    """Identical expected behavior, independently evaluated against durable state."""


def wait_for_blocker(observer, blocker_pid, future):
    """Observe a real server lock wait; elapsed time alone is not evidence."""
    deadline = monotonic() + 5
    while monotonic() < deadline:
        rows = observer.execute(
            "SELECT pid FROM pg_stat_activity WHERE datname = current_database() "
            "AND %s = ANY(pg_blocking_pids(pid))", (blocker_pid,),
        ).fetchall()
        if rows:
            return rows[0][0]
        if future.done():
            future.result()  # Surface the actual exception rather than a timeout.
            pytest.fail("authority returned without acquiring the required row lock")
        sleep(0.01)
    pytest.fail("no PostgreSQL row-lock wait observed")


def test_job_lock_precedes_worker_lock(authority_repo, isolated_database, clock):
    import psycopg
    repo = authority_repo
    claim = setup_claim(repo, clock.now)
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        with psycopg.connect(isolated_database) as blocker, psycopg.connect(isolated_database, autocommit=True) as observer:
            blocker.execute("SELECT id FROM jobs WHERE id::text = %s FOR UPDATE", (claim.job_id,))
            pid = blocker.info.backend_pid
            future = pool.submit(authorize, repo, claim)
            wait_for_blocker(observer, pid, future)
            # If authority locked Worker first, NOWAIT fails here. The Job
            # blocker guarantees that authority cannot have released it yet.
            blocker.execute("SELECT id FROM workers WHERE id = %s FOR UPDATE NOWAIT", ("owner",))
            blocker.commit()
            assert future.result(timeout=10) is None
    finally:
        pool.shutdown(wait=True)


@pytest.mark.parametrize("boundary", ["cancel", "retry"])
def test_authority_waits_for_committed_job_transition(authority_repo, isolated_database, clock, boundary):
    import psycopg
    repo = authority_repo
    claim = setup_claim(repo, clock.now)
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        with psycopg.connect(isolated_database) as transaction, psycopg.connect(isolated_database, autocommit=True) as observer:
            # Stage the same durable transition atomically, hold it uncommitted,
            # and prove authority neither reads old state nor returns early.
            transaction.execute("SELECT id FROM jobs WHERE id::text = %s FOR UPDATE", (claim.job_id,))
            if boundary == "cancel":
                transaction.execute("UPDATE jobs SET status = 'cancelled', assigned_worker_id = NULL, lease_token = NULL, lease_expires_at = NULL WHERE id::text = %s", (claim.job_id,))
            else:
                transaction.execute("UPDATE jobs SET status = 'queued', assigned_worker_id = NULL, lease_token = NULL, lease_expires_at = NULL, claimed_deployment_revision = NULL, claimed_serving_contract_revision = NULL, claimed_runtime_instance_epoch = NULL WHERE id::text = %s", (claim.job_id,))
            transaction.execute("UPDATE workers SET active_jobs = 0 WHERE id = 'owner'")
            future = pool.submit(authorize, repo, claim)
            wait_for_blocker(observer, transaction.info.backend_pid, future)
            transaction.commit()
            from astrumweaver.control import ConflictError
            from test_audio_artifact_authority import REJECTION
            with pytest.raises(ConflictError, match="^" + REJECTION + "$"):
                future.result(timeout=10)
        rejected(repo, claim)
        if boundary == "retry":
            newer = repo.claim_next_job("owner", runtime_instance_epoch=EPOCH, now=clock.now)
            assert newer is not None and newer.attempts == 2
            rejected(repo, claim)
            assert authorize(repo, newer) is None
    finally:
        pool.shutdown(wait=True)


def test_worker_lock_and_epoch_recheck(authority_repo, isolated_database, clock):
    import psycopg
    repo = authority_repo
    claim = setup_claim(repo, clock.now)
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        with psycopg.connect(isolated_database) as transaction, psycopg.connect(isolated_database, autocommit=True) as observer:
            transaction.execute("SELECT id FROM workers WHERE id = 'owner' FOR UPDATE")
            future = pool.submit(authorize, repo, claim)
            wait_for_blocker(observer, transaction.info.backend_pid, future)
            # Authority must already hold Job while waiting for Worker.
            with pytest.raises(psycopg.errors.LockNotAvailable):
                observer.execute("SELECT id FROM jobs WHERE id::text = %s FOR UPDATE NOWAIT", (claim.job_id,))
            transaction.execute("UPDATE workers SET serving = jsonb_set(serving, '{runtime_instance,epoch}', to_jsonb(%s::text)) WHERE id = 'owner'", (NEXT_EPOCH,))
            transaction.commit()
            from astrumweaver.control import ConflictError
            from test_audio_artifact_authority import REJECTION
            with pytest.raises(ConflictError, match="^" + REJECTION + "$"):
                future.result(timeout=10)
        rejected(repo, claim)
    finally:
        pool.shutdown(wait=True)


def test_malformed_durable_binding_is_redacted_without_mutation(authority_repo, isolated_database, clock):
    import psycopg
    from astrumweaver.control import ConflictError
    from test_audio_artifact_authority import REJECTION

    claim = setup_claim(authority_repo, clock.now)
    with psycopg.connect(isolated_database, autocommit=True) as connection:
        # Preserve SQL identity constraints but break the binding decoder.
        connection.execute("UPDATE jobs SET serving = serving - 'operation_schema' WHERE id::text = %s", (claim.job_id,))
        def raw_state():
            return (
                connection.execute("SELECT * FROM jobs ORDER BY id").fetchall(),
                connection.execute("SELECT * FROM workers ORDER BY id").fetchall(),
            )
        before = raw_state()
        with pytest.raises(ConflictError) as caught:
            authorize(authority_repo, claim)
        assert caught.value.args == (REJECTION,)
        assert claim.lease_token not in repr(caught.value)
        assert raw_state() == before
