"""Isolated PostgreSQL lock-order feasibility probe, NOT a TSUMGI implementation.

A tiny stand-in schema demonstrates Owner -> Job -> Worker row lock ordering.
It cannot qualify actual PostgresControlRepository.claim_next_job, migration,
Worker runtime, cancellation ACK, release rollout or production readiness.
Run only against a dedicated ephemeral PostgreSQL 17 database with:
ASTRUMWEAVER_OWNER_PROBE_DSN and independently provisioned random
ASTRUMWEAVER_OWNER_PROBE_NONCE stored in the database sentinel. Checks
server-reported data directory, Unix-socket transport and expected path
before creating any scratch schema. Normal CI never sets these variables.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from secrets import compare_digest
from threading import Event
from time import monotonic, sleep
from uuid import uuid4

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg import sql
from psycopg.rows import dict_row


DSN = os.environ.get("ASTRUMWEAVER_OWNER_PROBE_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="owner lock probe DSN not set")


def _identity_requirements(checked_dsn: str) -> tuple[str, str, str]:
    """Reject alternate libpq routing and load the independently seeded nonce."""
    options = psycopg.conninfo.conninfo_to_dict(checked_dsn)
    if set(options) != {"dbname", "host", "port"}:
        raise RuntimeError("owner-probe DSN contains disallowed routing options")
    host = options["host"]
    socket = Path(host)
    root = socket.parent
    if (
        options["dbname"] != "tsumgi137ownerprobe"
        or options["port"] != "55472"
        or not root.name.startswith("tsumgi137-owner-pg-")
        or str(root.parent) != "/tmp"
        or socket.name != "socket"
    ):
        raise RuntimeError("owner-probe DSN does not target ephemeral socket")
    nonce = os.environ.get("ASTRUMWEAVER_OWNER_PROBE_NONCE", "")
    if len(nonce) != 64 or not all(c in "0123456789abcdef" for c in nonce):
        raise RuntimeError("owner-probe out-of-band nonce missing or invalid")
    return host, str(root / "data"), nonce


def _verify_disposable_connection(conn, checked_dsn: str) -> None:
    """Read-only verify the SAME connection that will later perform DDL/DML.

    This cannot be split into a test connection and an unverified later
    writer: even a valid-looking Unix-socket DSN could resolve elsewhere
    under an unexpected libpq environment or race.
    """
    host, expected_data, nonce = _identity_requirements(checked_dsn)
    identity = conn.execute(
        "SELECT current_database() AS db, inet_server_addr() AS tcp_addr, "
        "current_setting('data_directory') AS data_dir, "
        "current_setting('unix_socket_directories') AS socket_dirs"
    ).fetchone()
    if (
        identity["db"] != "tsumgi137ownerprobe"
        or identity["tcp_addr"] is not None
        or os.path.realpath(identity["data_dir"]) != os.path.realpath(expected_data)
        or host not in identity["socket_dirs"].split(",")
    ):
        raise RuntimeError("owner-probe server does not match ephemeral cluster")
    try:
        record = conn.execute(
            "SELECT nonce FROM public.owner_probe_sentinel WHERE id = 1"
        ).fetchone()
    except psycopg.Error as exc:
        raise RuntimeError("owner-probe sentinel missing") from exc
    if record is None or not compare_digest(record["nonce"], nonce):
        raise RuntimeError("owner-probe cluster identity mismatch")


def _authorize_disposable_database(dsn: str | None = None) -> None:
    """Fail closed by verifying server identity before issuing ANY DDL."""
    checked_dsn = dsn if dsn is not None else DSN
    if not checked_dsn:
        raise RuntimeError("owner-probe DSN missing")
    _identity_requirements(checked_dsn)
    with psycopg.connect(
        checked_dsn, autocommit=True, row_factory=dict_row, connect_timeout=3
    ) as conn:
        _verify_disposable_connection(conn, checked_dsn)


def _connect(schema: str, *, application_name: str | None = None):
    """Open and independently authenticate every prospective mutating socket."""
    if DSN is None:
        raise RuntimeError("owner-probe DSN missing")
    _identity_requirements(DSN)  # fail before libpq is called
    connection = psycopg.connect(
        DSN, options=f"-c search_path={schema}", row_factory=dict_row,
        connect_timeout=3, application_name=application_name or "ownerprobe",
    )
    try:
        _verify_disposable_connection(connection, DSN)
        # Read-only identity SELECTs opened a transaction. Clear it so later
        # explicit transaction() retains its expected top-level behavior.
        connection.rollback()
    except BaseException:
        connection.close()
        raise
    return connection


@pytest.fixture
def sandbox_schema():
    _authorize_disposable_database()
    assert DSN is not None
    schema = "ownerprobe_" + uuid4().hex
    with _connect(schema) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        conn.execute(
            """CREATE TABLE workers (
              id text PRIMARY KEY, active_jobs integer NOT NULL DEFAULT 0,
              max_concurrency integer NOT NULL DEFAULT 1)"""
        )
        conn.execute(
            """CREATE TABLE owners (
              id uuid PRIMARY KEY, state text NOT NULL,
              lease_expires_at timestamptz NOT NULL)"""
        )
        conn.execute(
            """CREATE TABLE jobs (
              id uuid PRIMARY KEY, status text NOT NULL,
              priority integer NOT NULL DEFAULT 0,
              sequence bigint GENERATED ALWAYS AS IDENTITY,
              admission_kind text NOT NULL DEFAULT 'direct'
                CHECK (admission_kind IN ('direct', 'gateway')),
              gateway_admission_id uuid UNIQUE NULL REFERENCES owners(id),
              CHECK (
                (admission_kind = 'gateway' AND gateway_admission_id IS NOT NULL)
                OR
                (admission_kind = 'direct' AND gateway_admission_id IS NULL)
              ))"""
        )
        conn.execute("INSERT INTO workers(id) VALUES ('worker')")
    try:
        yield schema
    finally:
        # Do not trust the creation connection's earlier verification.
        # Authenticate this exact teardown connection BEFORE DROP ... CASCADE.
        with _connect(schema) as conn:
            conn.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )


def _admit(schema: str, owner_id, job_id, *,
           priority: int = 10,
           application_name: str | None = None) -> bool:
    """Owner row MUST be previously committed, then locked before Job INSERT."""
    with _connect(schema, application_name=application_name) as conn:
        with conn.transaction():
            owner = conn.execute(
                "SELECT state, lease_expires_at,"
                " clock_timestamp() AS db_clock"
                " FROM owners WHERE id=%s FOR UPDATE",
                (owner_id,),
            ).fetchone()
            if (
                owner is None
                or owner["state"] != "ACTIVE"
                or owner["lease_expires_at"] <= owner["db_clock"]
            ):
                return False
            conn.execute(
                "INSERT INTO jobs(id,status,priority,admission_kind,"
                "gateway_admission_id) VALUES (%s,'queued',%s,'gateway',%s)",
                (job_id, priority, owner_id),
            )
            return True


def _prepare(schema: str, *, with_job: bool = True,
             lease_expired: bool = False, priority: int = 10,
             lease_seconds: float = 30):
    owner_id, job_id = uuid4(), uuid4()
    lease = datetime.now(UTC) + timedelta(
        seconds=(-5 if lease_expired else lease_seconds)
    )
    # PREPARE commits durable Owner identity in a SEPARATE transaction before
    # ADMIT may start. A crash between the two leaves an Owner but no Job.
    with _connect(schema) as conn:
        conn.execute(
            "INSERT INTO owners(id,state,lease_expires_at)"
            " VALUES (%s,'ACTIVE',%s)",
            (owner_id, lease),
        )
    if with_job and not lease_expired:
        assert _admit(schema, owner_id, job_id, priority=priority)
    elif with_job and lease_expired:
        # Expired owners must not admit a new Job. To model a previously
        # committed Job whose owner later expires, insert while ACTIVE first.
        with _connect(schema) as conn:
            conn.execute(
                "UPDATE owners SET lease_expires_at="
                "clock_timestamp()+interval '10 seconds' WHERE id=%s",
                (owner_id,),
            )
        assert _admit(schema, owner_id, job_id, priority=priority)
        with _connect(schema) as conn:
            conn.execute(
                "UPDATE owners SET lease_expires_at="
                "clock_timestamp()-interval '5 seconds' WHERE id=%s",
                (owner_id,),
            )
    return owner_id, job_id


def _try_claim(schema: str, *, block_before_commit=None,
               after_owner_locked=None,
               application_name: str | None = None) -> bool:
    """Prototype: read-only selection, lock/recheck Owner, Job, Worker."""
    with _connect(schema, application_name=application_name) as conn:
        with conn.transaction():
            candidates = conn.execute(
                """SELECT j.id AS job_id, j.gateway_admission_id AS owner_id
                   FROM jobs j WHERE j.status='queued'
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
                    if after_owner_locked is not None:
                        after_owner_locked()
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
                if candidate["owner_id"] is not None:
                    # Eligibility can lapse while waiting for Job/Worker locks.
                    # The proposed runtime must check DB time AGAIN here.
                    latest = conn.execute(
                        "SELECT state, lease_expires_at,"
                        " clock_timestamp() AS db_clock"
                        " FROM owners WHERE id=%s",
                        (candidate["owner_id"],),
                    ).fetchone()
                    if (
                        latest is None
                        or latest["state"] != "ACTIVE"
                        or latest["lease_expires_at"] <= latest["db_clock"]
                    ):
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
            job = conn.execute(
                "SELECT * FROM jobs WHERE gateway_admission_id=%s FOR UPDATE",
                (owner_id,),
            ).fetchone()
            if job is not None:
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
                        (job["id"],),
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
    owner_id, job_id = _prepare(schema, with_job=False)
    _cancel(schema, owner_id)
    # The durable owner was committed before ADMIT and cancellation won.
    assert _admit(schema, owner_id, job_id) is False
    with _connect(schema) as conn:
        with conn.transaction():
            owner = conn.execute(
                "SELECT * FROM owners WHERE id=%s FOR UPDATE", (owner_id,)
            ).fetchone()
            assert owner["state"] == "CANCEL_REQUESTED"
            assert conn.execute(
                "SELECT count(*) AS count FROM jobs WHERE gateway_admission_id=%s",
                (owner_id,),
            ).fetchone()["count"] == 0
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


def test_probe_rejects_alternate_hostaddr_routing_without_database_mutation():
    assert DSN is not None
    hostile = psycopg.conninfo.make_conninfo(DSN, hostaddr="127.0.0.1")
    with pytest.raises(RuntimeError, match="disallowed routing options"):
        _authorize_disposable_database(hostile)


def test_probe_rejects_wrong_server_nonce_before_creating_schema(monkeypatch):
    assert DSN is not None
    _authorize_disposable_database()
    with _connect("public") as conn:
        before = conn.execute(
            "SELECT count(*) AS count FROM pg_namespace WHERE nspname LIKE 'ownerprobe_%'"
        ).fetchone()["count"]
    with monkeypatch.context() as patch:
        patch.setenv("ASTRUMWEAVER_OWNER_PROBE_NONCE", "0" * 64)
        with pytest.raises(RuntimeError, match="identity mismatch"):
            _authorize_disposable_database()
    with _connect("public") as conn:
        after = conn.execute(
            "SELECT count(*) AS count FROM pg_namespace WHERE nspname LIKE 'ownerprobe_%'"
        ).fetchone()["count"]
    assert after == before


def test_expiry_while_waiting_for_worker_lock_rechecks_fresh_clock(sandbox_schema):
    """A valid early Owner read MUST NOT authorize RUNNING after lock delay."""
    schema = sandbox_schema
    owner_id, _ = _prepare(schema, lease_seconds=1.2)
    worker_locked, release_worker, owner_locked = Event(), Event(), Event()
    blocker_pid: list[int] = []

    def holding_worker():
        with _connect(schema) as conn:
            blocker_pid.append(conn.info.backend_pid)
            with conn.transaction():
                conn.execute(
                    "SELECT * FROM workers WHERE id='worker' FOR UPDATE"
                )
                worker_locked.set()
                assert release_worker.wait(timeout=5)

    claim_application = "ownerprobe_workerwait_" + uuid4().hex[:9]
    with ThreadPoolExecutor(max_workers=2) as pool:
        holder = pool.submit(holding_worker)
        try:
            assert worker_locked.wait(timeout=2)
            claimant = pool.submit(
                _try_claim, schema,
                after_owner_locked=owner_locked.set,
                application_name=claim_application,
            )
            assert owner_locked.wait(timeout=2)
            # Prove that THIS claimant is blocked by the backend which holds
            # the Worker row, not merely by an unrelated PostgreSQL Lock.
            # Also establish that the Owner was still live at observation.
            end = monotonic() + 2
            remaining = None
            with _connect(schema) as observer:
                while monotonic() < end:
                    row = observer.execute(
                        "SELECT wait_event_type, pg_blocking_pids(pid) AS blockers "
                        "FROM pg_stat_activity WHERE application_name=%s",
                        (claim_application,),
                    ).fetchone()
                    if (
                        row
                        and row["wait_event_type"] == "Lock"
                        and blocker_pid
                        and blocker_pid[0] in row["blockers"]
                    ):
                        lease = observer.execute(
                            "SELECT lease_expires_at,"
                            " clock_timestamp() AS db_clock FROM owners"
                            " WHERE id=%s",
                            (owner_id,),
                        ).fetchone()
                        remaining = (
                            lease["lease_expires_at"] - lease["db_clock"]
                        ).total_seconds()
                        break
                    if claimant.done():
                        break
                    sleep(0.01)
            assert remaining is not None, "Worker-row blocker was not observed"
            assert remaining > 0.05, "Owner expired before observed wait"
            assert not claimant.done()
            sleep(remaining + 0.08)  # Owner expires DURING proven lock wait.
        finally:
            release_worker.set()
        holder.result(timeout=5)
        assert claimant.result(timeout=5) is False

    assert _inspect(schema) == (["ACTIVE"], ["queued"], 0)


def test_gateway_job_must_have_owner_fk_on_insert(sandbox_schema):
    schema = sandbox_schema
    with _connect(schema) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO jobs(id,status,admission_kind,priority)"
                " VALUES (%s,'queued','gateway',10)",
                (uuid4(),),
            )
    with _connect(schema) as conn:
        count = conn.execute("SELECT count(*) AS count FROM jobs").fetchone()["count"]
    assert count == 0


def test_one_owner_cannot_bind_two_gateway_jobs(sandbox_schema):
    schema = sandbox_schema
    owner_id, _ = _prepare(schema)
    with _connect(schema) as conn:
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "INSERT INTO jobs(id,status,priority,admission_kind,"
                "gateway_admission_id) VALUES (%s,'queued',10,'gateway',%s)",
                (uuid4(), owner_id),
            )
    assert _inspect(schema) == (["ACTIVE"], ["queued"], 0)


def test_prepared_owner_commits_before_separate_job_admission(sandbox_schema):
    schema = sandbox_schema
    owner_id, job_id = _prepare(schema, with_job=False)
    # A different connection can see the committed Owner before Job exists.
    with _connect(schema) as conn:
        owner = conn.execute(
            "SELECT state FROM owners WHERE id=%s", (owner_id,)
        ).fetchone()
        count = conn.execute(
            "SELECT count(*) AS n FROM jobs"
        ).fetchone()["n"]
    assert owner["state"] == "ACTIVE" and count == 0
    assert _admit(schema, owner_id, job_id)
    assert _inspect(schema) == (["ACTIVE"], ["queued"], 0)


def test_cancel_competes_with_separately_committed_owner_admission(sandbox_schema):
    """CANCEL's Owner lock must linearize against a later ADMIT transaction."""
    schema = sandbox_schema
    owner_id, job_id = _prepare(schema, with_job=False)
    cancelling, release_cancel = Event(), Event()
    application = "ownerprobe_admitwait_" + uuid4().hex[:9]

    def cancel_holding_owner():
        def block():
            cancelling.set()
            assert release_cancel.wait(timeout=3)
        _cancel(schema, owner_id, before_commit=block)

    with ThreadPoolExecutor(max_workers=2) as pool:
        cancelled = pool.submit(cancel_holding_owner)
        try:
            assert cancelling.wait(timeout=2)
            admitted = pool.submit(
                _admit, schema, owner_id, job_id, application_name=application
            )
            end = monotonic() + 2
            saw_wait = False
            with _connect(schema) as observer:
                while monotonic() < end:
                    row = observer.execute(
                        "SELECT wait_event_type FROM pg_stat_activity "
                        "WHERE application_name=%s",
                        (application,),
                    ).fetchone()
                    if row and row["wait_event_type"] == "Lock":
                        saw_wait = True
                        break
                    if admitted.done():
                        break
                    sleep(0.01)
            assert saw_wait, "ADMIT did not wait for cancelling Owner row"
            assert not admitted.done()
        finally:
            release_cancel.set()
        cancelled.result(timeout=4)
        assert admitted.result(timeout=4) is False
    assert _inspect(schema) == (["CANCEL_REQUESTED"], [], 0)


def test_every_mutating_connection_rechecks_disposable_nonce(monkeypatch):
    assert DSN is not None
    _authorize_disposable_database()
    with monkeypatch.context() as patch:
        patch.setenv("ASTRUMWEAVER_OWNER_PROBE_NONCE", "0" * 64)
        with pytest.raises(RuntimeError, match="identity mismatch"):
            # Even a valid-looking schema name cannot cause a write when its
            # actual prospective connection fails independent verification.
            _connect("ownerprobe_untrusted")
