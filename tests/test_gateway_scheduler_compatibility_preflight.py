"""Real Control scheduler preflight on a disposable PostgreSQL 17 database.

The tests deliberately run the UNMODIFIED PostgresControlRepository to establish
a genuine baseline and expose the unsafe mixed-Control case. They MUST NOT
run against a preexisting production or remotely accessible database.

Enable only with a fresh locally provisioned ASTRUMWEAVER_OWNER_PROBE_DSN and
ASTRUMWEAVER_OWNER_PROBE_NONCE. Every actual PostgreSQL connection opened by
the repository and migration entrypoint is intercepted and verified on its
*own* connection before any production code can issue SQL.

This is NOT new owner-aware scheduling, versioned claim activation, or a
migration acceptance test. Normal test runs skip the entire module.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg.rows import dict_row

from astrumweaver import JobRequirements, ResourceShape, WorkerSpec
from astrumweaver.control import (
    JobStatus,
    JobSubmission,
    PostgresControlRepository,
    WorkerRegistration,
)
from astrumweaver.control.migrate import apply_migrations
from test_gateway_owner_claim_sql_probe import (
    DSN,
    _identity_requirements,
)

pytestmark = pytest.mark.skipif(
    not DSN, reason="requires isolated Owner probe PG17 DSN and out-of-band nonce"
)


def _verify_actual_connection(connection, dsn: str) -> None:
    """Verify on the same opened connection, with no change to row factory."""
    host, data_directory, nonce = _identity_requirements(dsn)
    with connection.cursor(row_factory=dict_row) as cursor:
        identity = cursor.execute(
            "SELECT current_database() AS db, inet_server_addr() AS tcp_addr,"
            " current_setting('data_directory') AS data_dir,"
            " current_setting('unix_socket_directories') AS socket_dirs,"
            " current_setting('server_version_num')::int AS version"
        ).fetchone()
        if (
            identity["db"] != "tsumgi137ownerprobe"
            or identity["tcp_addr"] is not None
            or identity["version"] < 170000
            or os.path.realpath(identity["data_dir"]) != os.path.realpath(data_directory)
            or host not in identity["socket_dirs"].split(",")
        ):
            raise RuntimeError("real Control probe server identity mismatch")
        row = cursor.execute(
            "SELECT nonce FROM public.owner_probe_sentinel WHERE id=1"
        ).fetchone()
        from secrets import compare_digest

        if row is None or not compare_digest(row["nonce"], nonce):
            raise RuntimeError("real Control probe nonce mismatch")


@pytest.fixture()
def verified_scheduler(monkeypatch):
    """Fence all psycopg connections BEFORE applying actual SQL migrations."""
    if DSN is None:
        pytest.skip("disposable PostgreSQL DSN not present")
    _identity_requirements(DSN)
    raw_connect = psycopg.connect

    def verified_connect(conninfo="", **kwargs):
        if conninfo != DSN:
            raise RuntimeError("Control probe refused alternate PostgreSQL DSN")
        unexpected = set(kwargs) - {
            "autocommit", "row_factory", "connect_timeout",
        }
        if unexpected:
            raise RuntimeError(
                "Control probe refused alternate connection options"
            )
        _identity_requirements(conninfo)  # reject before creating socket
        connection = raw_connect(conninfo, **kwargs)
        try:
            _verify_actual_connection(connection, conninfo)
            if not connection.autocommit:
                # The verification SELECTs opened an implicit transaction.
                connection.rollback()
        except BaseException:
            connection.close()
            raise
        return connection

    monkeypatch.setattr(psycopg, "connect", verified_connect)
    apply_migrations(DSN)
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute(
            "TRUNCATE TABLE job_events, jobs, workers RESTART IDENTITY CASCADE"
        )
    repo = PostgresControlRepository(DSN)
    yield repo
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute(
            "TRUNCATE TABLE job_events, jobs, workers RESTART IDENTITY CASCADE"
        )


def _register(repo: PostgresControlRepository, *, max_concurrency=1):
    worker_id = "owner-preflight-scheduler"
    spec = WorkerSpec(
        worker_id=worker_id,
        worker_class="cpu-probe",
        capabilities=frozenset({"code.review"}),
        resources=ResourceShape(
            gpu_count=0, total_vram_mb=0, max_single_gpu_vram_mb=0
        ),
    )
    repo.register_worker(
        WorkerRegistration(spec=spec, max_concurrency=max_concurrency)
    )
    return worker_id


def test_actual_control_scheduler_preserves_fifo_after_incompatible_head(
    verified_scheduler,
):
    """Real scheduler skips a high-priority unsatisfiable job, preserves FIFO."""
    repo = verified_scheduler
    worker = _register(repo)
    blocked = repo.submit_job(
        JobSubmission(
            capability="code.review",
            priority=100,
            requirements=JobRequirements(required_labels={"missing": "value"}),
        )
    )
    first = repo.submit_job(JobSubmission(capability="code.review", priority=10))
    second = repo.submit_job(JobSubmission(capability="code.review", priority=10))
    assert first.sequence < second.sequence

    first_claim = repo.claim_next_job(worker)
    assert first_claim is not None and first_claim.job_id == first.job_id
    assert repo.get_job(blocked.job_id).status is JobStatus.QUEUED
    repo.cancel_job(first_claim.job_id)

    second_claim = repo.claim_next_job(worker)
    assert second_claim is not None and second_claim.job_id == second.job_id
    assert repo.get_job(blocked.job_id).status is JobStatus.QUEUED
    repo.cancel_job(second_claim.job_id)
    assert repo.get_worker(worker).active_jobs == 0


def test_actual_control_scheduler_skips_locked_high_priority_job(
    verified_scheduler,
):
    """FOR UPDATE SKIP LOCKED lets an independent ready Job progress."""
    repo = verified_scheduler
    worker = _register(repo)
    first = repo.submit_job(JobSubmission(capability="code.review", priority=20))
    second = repo.submit_job(JobSubmission(capability="code.review", priority=10))

    with psycopg.connect(DSN) as blocker:
        blocker.execute(
            "SELECT id FROM jobs WHERE id::text=%s FOR UPDATE",
            (first.job_id,),
        )
        # A different real Control connection must skip the locked first Job.
        claim = repo.claim_next_job(worker)
        assert claim is not None and claim.job_id == second.job_id
        repo.cancel_job(claim.job_id)
        blocker.rollback()

    later_claim = repo.claim_next_job(worker)
    assert later_claim is not None and later_claim.job_id == first.job_id
    repo.cancel_job(later_claim.job_id)
    assert repo.get_worker(worker).active_jobs == 0


@pytest.mark.parametrize(
    "mark_gateway", [False, True],
    ids=["old-gateway-uses-direct-default", "owner-linked-cancelled"],
)
def test_old_control_can_claim_despite_durable_owner_contract(
    verified_scheduler, mark_gateway,
):
    """NEGATIVE compatibility proof of two old-Control bypasses.

    Even with illustrative FK/CHECK/UNIQUE constraints on the disposable DB,
    old Gateway/Control may omit the origin marker (the default is direct).
    Even correctly marked Gateway Jobs can be claimed by an old Control that
    never inspects the CANCEL_REQUESTED Owner. Both are expected UNSAFE
    outcomes, not acceptance of the persistent fence.
    """
    repo = verified_scheduler
    worker = _register(repo)
    owner_id = uuid4()

    with psycopg.connect(DSN, autocommit=True) as conn:
        # Disposable-only additive illustration, no product migration.
        conn.execute(
            "CREATE TABLE gateway_admissions_scheduler_probe "
            "(id uuid PRIMARY KEY, state text NOT NULL)"
        )
        conn.execute(
            "ALTER TABLE jobs ADD COLUMN gateway_admission_id uuid UNIQUE "
            "REFERENCES gateway_admissions_scheduler_probe(id)"
        )
        conn.execute(
            "ALTER TABLE jobs ADD COLUMN admission_kind text NOT NULL "
            "DEFAULT 'direct' CHECK (admission_kind IN ('direct','gateway'))"
        )
        conn.execute(
            "ALTER TABLE jobs ADD CONSTRAINT jobs_probe_kind_fk_check CHECK ("
            "(admission_kind='gateway' AND gateway_admission_id IS NOT NULL)"
            " OR (admission_kind='direct' AND gateway_admission_id IS NULL))"
        )

    try:
        with psycopg.connect(DSN) as conn:
            conn.execute(
                "INSERT INTO gateway_admissions_scheduler_probe(id,state)"
                " VALUES (%s,'CANCEL_REQUESTED')",
                (owner_id,),
            )
        job = repo.submit_job(
            JobSubmission(capability="code.review", priority=100)
        )
        with psycopg.connect(DSN) as conn:
            if mark_gateway:
                conn.execute(
                    "UPDATE jobs SET gateway_admission_id=%s,"
                    " admission_kind='gateway' WHERE id::text=%s",
                    (owner_id, job.job_id),
                )
            origin = conn.execute(
                "SELECT admission_kind, gateway_admission_id"
                " FROM jobs WHERE id::text=%s", (job.job_id,),
            ).fetchone()
            assert origin == (
                ("gateway", owner_id) if mark_gateway else ("direct", None)
            )
        claimed = repo.claim_next_job(worker)
        assert claimed is not None and claimed.job_id == job.job_id
        assert claimed.status is JobStatus.RUNNING
        # This is an expected *unsafe* outcome, NOT acceptance of owner fence.
        with psycopg.connect(DSN) as conn:
            row = conn.execute(
                "SELECT state FROM gateway_admissions_scheduler_probe"
                " WHERE id=%s", (owner_id,)
            ).fetchone()
            assert row[0] == "CANCEL_REQUESTED"
        repo.cancel_job(job.job_id)
        assert repo.get_worker(worker).active_jobs == 0
    finally:
        # Ensure no production table remains modified after the opt-in probe.
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(
                "ALTER TABLE jobs DROP COLUMN IF EXISTS gateway_admission_id"
            )
            conn.execute(
                "ALTER TABLE jobs DROP COLUMN IF EXISTS admission_kind"
            )
            conn.execute(
                "DROP TABLE IF EXISTS gateway_admissions_scheduler_probe"
            )


def test_disposable_pg_role_rls_could_exclude_owned_jobs_from_old_claim(
    verified_scheduler,
):
    """Prototype a DB-enforced legacy *reader* barrier, not a rollout solution.

    The **unmodified** Control repository is run under a non-owner database
    role, and PostgreSQL RLS permits only direct Job rows. This is an opt-in
    disposable-cluster feasibility check, not an authorization design. An old
    Gateway with permission to insert a direct-default Job still bypasses the
    intended ownership contract, and a superuser/BYPASSRLS role bypasses RLS.
    """
    from contextlib import contextmanager
    from psycopg import sql

    repo = verified_scheduler
    worker = _register(repo)
    owner_id = uuid4()
    role_name = "tsumgi_legacy_probe_" + uuid4().hex[:12]
    gateway_job = repo.submit_job(
        JobSubmission(capability="code.review", priority=100)
    )
    direct_job = repo.submit_job(
        JobSubmission(capability="code.review", priority=10)
    )

    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(
            "CREATE TABLE gateway_admissions_rls_probe ("
            "id uuid PRIMARY KEY, state text NOT NULL)"
        )
        admin.execute(
            "ALTER TABLE jobs ADD COLUMN gateway_admission_id uuid "
            "REFERENCES gateway_admissions_rls_probe(id)"
        )
        admin.execute(
            "ALTER TABLE jobs ADD COLUMN admission_kind text NOT NULL "
            "DEFAULT 'direct'"
        )
        admin.execute(
            "INSERT INTO gateway_admissions_rls_probe(id,state) "
            "VALUES (%s,'CANCEL_REQUESTED')", (owner_id,),
        )
        admin.execute(
            "UPDATE jobs SET admission_kind='gateway', gateway_admission_id=%s "
            "WHERE id::text=%s", (owner_id, gateway_job.job_id),
        )
        admin.execute(
            sql.SQL("CREATE ROLE {} NOLOGIN NOSUPERUSER NOBYPASSRLS")
            .format(sql.Identifier(role_name))
        )

    try:
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(
                sql.SQL("GRANT USAGE ON SCHEMA public TO {}")
                .format(sql.Identifier(role_name))
            )
            admin.execute(
                sql.SQL("GRANT ALL ON TABLE jobs, workers TO {}")
                .format(sql.Identifier(role_name))
            )
            admin.execute(
                sql.SQL("CREATE POLICY scheduler_preflight_direct_select "
                        "ON jobs FOR SELECT TO {} "
                        "USING (admission_kind='direct')")
                .format(sql.Identifier(role_name))
            )
            admin.execute(
                sql.SQL("CREATE POLICY scheduler_preflight_direct_update "
                        "ON jobs FOR UPDATE TO {} "
                        "USING (admission_kind='direct') "
                        "WITH CHECK (admission_kind='direct')")
                .format(sql.Identifier(role_name))
            )
            admin.execute("ALTER TABLE jobs ENABLE ROW LEVEL SECURITY")

        class LegacyRoleControl(PostgresControlRepository):
            @contextmanager
            def _connection(self):
                with super()._connection() as connection:
                    connection.execute(
                        sql.SQL("SET ROLE {}").format(sql.Identifier(role_name))
                    )
                    connection.commit()
                    yield connection

        old_control = LegacyRoleControl(DSN)
        claimed = old_control.claim_next_job(worker)
        assert claimed is not None and claimed.job_id == direct_job.job_id
        assert repo.get_job(gateway_job.job_id).status is JobStatus.QUEUED
        repo.cancel_job(direct_job.job_id)

        # No direct Job remains; role-scoped legacy Claim sees no owned row.
        assert old_control.claim_next_job(worker) is None
        assert repo.get_worker(worker).active_jobs == 0

        # NEGATIVE CONTROL: a table owner/superuser connected to the same
        # database can still execute the *unchanged* Job-first Claim. RLS
        # only provides a fence when the legacy identity is actually confined
        # to a non-owner/non-BYPASSRLS PostgreSQL role.
        privileged_claim = repo.claim_next_job(worker)
        assert privileged_claim is not None
        assert privileged_claim.job_id == gateway_job.job_id
        repo.cancel_job(privileged_claim.job_id)
        assert repo.get_worker(worker).active_jobs == 0
    finally:
        with psycopg.connect(DSN, autocommit=True) as admin:
            # Restore the disposable database before the test fixture cleans up.
            admin.execute("ALTER TABLE jobs DISABLE ROW LEVEL SECURITY")
            admin.execute(
                "DROP POLICY IF EXISTS scheduler_preflight_direct_select ON jobs"
            )
            admin.execute(
                "DROP POLICY IF EXISTS scheduler_preflight_direct_update ON jobs"
            )
            admin.execute(
                "ALTER TABLE jobs DROP COLUMN IF EXISTS gateway_admission_id"
            )
            admin.execute(
                "ALTER TABLE jobs DROP COLUMN IF EXISTS admission_kind"
            )
            admin.execute(
                "DROP TABLE IF EXISTS gateway_admissions_rls_probe"
            )
            admin.execute(
                sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role_name))
            )
            admin.execute(
                sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role_name))
            )
