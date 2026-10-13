"""Isolated PostgreSQL lock-order feasibility probe, NOT a TSUMGI implementation.

A tiny stand-in schema demonstrates Owner -> Job -> Worker row lock ordering.
It cannot qualify actual PostgresControlRepository.claim_next_job, migration,
Worker runtime, cancellation ACK, release rollout or production readiness.
Run only against a dedicated ephemeral PostgreSQL 17 database with:
ASTRUMWEAVER_OWNER_PROBE_DSN (database name tsumgi137ownerprobe, Unix /tmp
socket path beginning /tmp/tsumgi137-owner-pg-).
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from time import monotonic, sleep
from uuid import uuid4

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg import sql
from psycopg.rows import dict_row


DSN = os.environ.get("ASTRUMWEAVER_OWNER_PROBE_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="owner lock probe DSN not set")


def _connect(schema: str, *, application_name: str | None = None):
    assert DSN is not None
    return psycopg.connect(
        DSN, options=f"-c search_path={schema}", row_factory=dict_row,
        connect_timeout=3, application_name=application_name or "ownerprobe",
    )


@pytest.fixture
def sandbox_schema():
    assert DSN is not None
    options = psycopg.conninfo.conninfo_to_dict(DSN)
    assert options.get("dbname") == "tsumgi137ownerprobe"
    assert options.get("host", "").startswith("/tmp/tsumgi137-owner-pg-")
    assert int(options.get("port", "0")) == 55472
    schema = "ownerprobe_" + uuid4().hex
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        conn.execute(
            """CREATE TABLE workers (
              id text PRIMARY KEY, active_jobs integer NOT NULL DEFAULT 0,
              max_concurrency integer NOT NULL DEFAULT 1)"""
        )
        conn.execute(
            """CREATE TABLE jobs (
              id uuid PRIMARY KEY, status text NOT NULL,
              priority integer NOT NULL DEFAULT 0,
              sequence bigint GENERATED ALWAYS AS IDENTITY)"""
        )
        conn.execute(
            """CREATE TABLE owners (
              id uuid PRIMARY KEY, state text NOT NULL,
              lease_expires_at timestamptz NOT NULL,
              job_id uuid UNIQUE NULL REFERENCES jobs(id))"""
        )
        conn.execute("INSERT INTO workers(id) VALUES ('worker')")
    try:
        yield schema
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )


def _prepare(schema: str, *, with_job: bool = True,
             lease_expired: bool = False, priority: int = 10):
    owner_id, job_id = uuid4(), uuid4()
    lease = datetime.now(UTC) + timedelta(
        seconds=(-5 if lease_expired else 30)
    )
    with _connect(schema) as conn:
        if with_job:
            conn.execute(
                "INSERT INTO jobs(id,status,priority) VALUES (%s,'queued',%s)",
                (job_id, priority),
            )
        conn.execute(
            "INSERT INTO owners(id,state,lease_expires_at,job_id)"
            " VALUES (%s,'ACTIVE',%s,%s)",
            (owner_id, lease, job_id if with_job else None),
        )
    return owner_id, job_id


def _try_claim(schema: str, *, block_before_commit=None) -> bool:
    """Prototype: read-only selection, lock/recheck Owner, Job, Worker."""
    with _connect(schema) as conn:
        with conn.transaction():
            candidates = conn.execute(
                """SELECT j.id AS job_id, o.id AS owner_id
                   FROM jobs j LEFT JOIN owners o ON o.job_id=j.id
                   WHERE j.status='queued'
                   ORDER BY j.priority DESC, j.sequence ASC"""
            ).fetchall()
            for candidate in candidates:
                if candidate["owner_id"] is not None:
                    owner = conn.execute(
                        "SELECT *, clock_timestamp() AS db_clock "
                        "FROM owners WHERE id=%s FOR UPDATE SKIP LOCKED",
                        (candidate["owner_id"],),
                    ).fetchone()
                    if owner is None or owner["state"] != "ACTIVE":
                        continue
                    if owner["lease_expires_at"] <= owner["db_clock"]:
                        continue
                job = conn.execute(
                    "SELECT * FROM jobs WHERE id=%s FOR UPDATE SKIP LOCKED",
                    (candidate["job_id"],),
                ).fetchone()
                if job is None or job["status"] != "queued":
                    continue
                worker = conn.execute(
                    "SELECT * FROM workers WHERE id='worker' FOR UPDATE"
                ).fetchone()
                if worker["active_jobs"] >= worker["max_concurrency"]:
                    return False
                conn.execute(
                    "UPDATE jobs SET status='running' WHERE id=%s",
                    (job["id"],),
                )
                conn.execute(
                    "UPDATE workers SET active_jobs=active_jobs+1"
                    " WHERE id='worker'"
                )
                if block_before_commit:
                    block_before_commit()
                return True
    return False


def _cancel(schema: str, owner_id, *, before_commit=None,
            application_name: str | None = None):
    """Prototype: Owner -> Job -> Worker and durable cancel-first intent."""
    with _connect(schema, application_name=application_name) as conn:
        with conn.transaction():
            owner = conn.execute(
                "SELECT * FROM owners WHERE id=%s FOR UPDATE", (owner_id,)
            ).fetchone()
            conn.execute(
                "UPDATE owners SET state='CANCEL_REQUESTED' WHERE id=%s",
                (owner_id,),
            )
            if owner["job_id"] is not None:
                job = conn.execute(
                    "SELECT * FROM jobs WHERE id=%s FOR UPDATE",
                    (owner["job_id"],),
                ).fetchone()
                if job["status"] in ("queued", "running"):
                    if job["status"] == "running":
                        conn.execute(
                            "SELECT * FROM workers WHERE id='worker' FOR UPDATE"
                        )
                        conn.execute(
                            "UPDATE workers SET active_jobs=active_jobs-1"
                            " WHERE id='worker'"
                        )
                    conn.execute(
                        "UPDATE jobs SET status='cancelled' WHERE id=%s",
                        (owner["job_id"],),
                    )
            if before_commit:
                before_commit()


def _inspect(schema: str):
    with _connect(schema) as conn:
        owners = conn.execute("SELECT state FROM owners").fetchall()
        jobs = conn.execute("SELECT status FROM jobs ORDER BY sequence").fetchall()
        worker = conn.execute(
            "SELECT active_jobs FROM workers WHERE id='worker'"
        ).fetchone()
    return [r["state"] for r in owners], [r["status"] for r in jobs], worker["active_jobs"]


def test_cancel_first_prevents_claim_and_preserves_capacity(sandbox_schema):
    schema = sandbox_schema
    owner_id, _ = _prepare(schema)
    entered, release = Event(), Event()

    def pending_cancel():
        def block():
            entered.set()
            assert release.wait(timeout=3)
        _cancel(schema, owner_id, before_commit=block)

    with ThreadPoolExecutor(max_workers=2) as pool:
        cancelled = pool.submit(pending_cancel)
        try:
            assert entered.wait(timeout=3)
            # SKIP LOCKED cannot acquire a held owner fence.
            assert not _try_claim(schema)
        finally:
            release.set()
        cancelled.result(timeout=4)
    assert not _try_claim(schema)
    assert _inspect(schema) == (
        ["CANCEL_REQUESTED"], ["cancelled"], 0
    )


def test_claim_first_then_cancel_releases_worker_capacity(sandbox_schema):
    schema = sandbox_schema
    owner_id, _ = _prepare(schema)
    entered, release = Event(), Event()

    def pending_claim():
        def block():
            entered.set()
            assert release.wait(timeout=3)
        return _try_claim(schema, block_before_commit=block)

    cancel_application = "ownerprobe_cancel_" + uuid4().hex[:10]
    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed = pool.submit(pending_claim)
        try:
            assert entered.wait(timeout=3)
            cancelled = pool.submit(
                _cancel, schema, owner_id,
                application_name=cancel_application,
            )
            # Independently observe the real PostgreSQL lock wait rather than
            # just assuming it occurred because the Future has not finished.
            end = monotonic() + 2
            seen_lock_wait = False
            with _connect(schema) as observer:
                while monotonic() < end:
                    row = observer.execute(
                        "SELECT wait_event_type FROM pg_stat_activity "
                        "WHERE application_name=%s",
                        (cancel_application,),
                    ).fetchone()
                    if row and row["wait_event_type"] == "Lock":
                        seen_lock_wait = True
                        break
                    if cancelled.done():
                        break
                    sleep(0.01)
            assert seen_lock_wait, "cancellation did not block on owner row"
            assert not cancelled.done()
        finally:
            release.set()
        assert claimed.result(timeout=4)
        cancelled.result(timeout=4)
    assert _inspect(schema) == (
        ["CANCEL_REQUESTED"], ["cancelled"], 0
    )


def test_cancel_before_admit_rejects_late_job(sandbox_schema):
    schema = sandbox_schema
    owner_id, _ = _prepare(schema, with_job=False)
    _cancel(schema, owner_id)
    with _connect(schema) as conn:
        with conn.transaction():
            owner = conn.execute(
                "SELECT * FROM owners WHERE id=%s FOR UPDATE", (owner_id,)
            ).fetchone()
            assert owner["state"] == "CANCEL_REQUESTED"
            assert owner["job_id"] is None
    assert _inspect(schema) == (["CANCEL_REQUESTED"], [], 0)


def test_expired_owner_blocks_claim_but_legacy_job_remains_claimable(sandbox_schema):
    schema = sandbox_schema
    _prepare(schema, lease_expired=True, priority=100)
    with _connect(schema) as conn:
        conn.execute(
            "INSERT INTO jobs(id,status,priority) VALUES (%s,'queued',1)",
            (uuid4(),),
        )
    assert _try_claim(schema)
    assert _inspect(schema) == (
        ["ACTIVE"], ["queued", "running"], 1
    )
