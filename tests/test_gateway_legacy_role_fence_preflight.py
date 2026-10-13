"""Disposable PostgreSQL credential-fencing preflight for #137.

This does NOT implement the durable-owner protocol. It proves what operator
NOLOGIN / CONNECT revocation do and do not guarantee for an *old* Control role.
No test touches production PostgreSQL, systemd services or GPU hardware.

Opt-in: ASTRUMWEAVER_OWNER_PROBE_DSN and separate one-use
ASTRUMWEAVER_OWNER_PROBE_NONCE, with isolated local PostgreSQL 17 trust-auth
Unix socket. The upstream verified_scheduler fixture checks all privileged
connections and applies real TSUMGI migrations only on the verified scratch DB.
"""

from __future__ import annotations

import os
from secrets import compare_digest
from time import monotonic, sleep
from uuid import uuid4

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg import sql

from astrumweaver.control import JobSubmission
from test_gateway_scheduler_compatibility_preflight import (
    DSN,
    _identity_requirements,
    verified_scheduler,
)

pytestmark = pytest.mark.skipif(
    not DSN, reason="requires separately provisioned disposable PG17 socket"
)


def _legacy_connection(role: str):
    """Authenticate a real test legacy role, then verify this socket in-band.

    This deliberately bypasses the test's privileged-connection monkeypatch
    because a role-specific login is the subject of the test. It cannot
    reach a remote database: the DSN, environment, Unix transport, DB, nonce
    and current role are checked. The role has only test-only privileges.
    """
    assert DSN is not None
    _identity_requirements(DSN)
    if any(key.startswith("PG") and value for key, value in os.environ.items()):
        raise RuntimeError("legacy-role probe rejects inherited libpq environment")
    conn = psycopg.Connection.connect(DSN, user=role, connect_timeout=3)
    try:
        row = conn.execute(
            "SELECT current_database(), inet_server_addr(), current_user"
        ).fetchone()
        expected_socket, _, _ = _identity_requirements(DSN)
        if (
            row[0] != "tsumgi137ownerprobe"
            or row[1] is not None
            or row[2] != role
            or conn.info.host != expected_socket
        ):
            raise RuntimeError("legacy test role connection identity mismatch")
        marker = conn.execute(
            "SELECT nonce FROM public.owner_probe_sentinel WHERE id=1"
        ).fetchone()
        if marker is None or not compare_digest(
            marker[0], os.environ["ASTRUMWEAVER_OWNER_PROBE_NONCE"]
        ):
            raise RuntimeError("legacy test role nonce mismatch")
        # Authentication SELECTs opened a transaction.
        conn.rollback()
    except BaseException:
        conn.close()
        raise
    return conn


@pytest.fixture
def legacy_role(verified_scheduler):
    """Create one disposable LOGIN identity, terminate and drop at teardown."""
    assert DSN is not None
    role = "tsumgi_old_control_" + uuid4().hex[:12]
    with psycopg.connect(DSN) as admin:
        admin.execute(
            sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS")
            .format(sql.Identifier(role))
        )
        admin.execute(
            sql.SQL("GRANT USAGE ON SCHEMA public TO {}")
            .format(sql.Identifier(role))
        )
        admin.execute(
            sql.SQL("GRANT SELECT ON public.owner_probe_sentinel TO {}")
            .format(sql.Identifier(role))
        )
        admin.execute(
            sql.SQL("GRANT SELECT, UPDATE ON TABLE jobs TO {}")
            .format(sql.Identifier(role))
        )
    try:
        yield role, verified_scheduler
    finally:
        # Cleanup is limited to the uniquely created scratch role.
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE usename=%s AND pid<>pg_backend_pid()",
                (role,),
            )
            end = monotonic() + 3
            while monotonic() < end:
                count = admin.execute(
                    "SELECT count(*) FROM pg_stat_activity WHERE usename=%s",
                    (role,),
                ).fetchone()[0]
                if count == 0:
                    break
                sleep(0.02)
            else:
                raise AssertionError("scratch legacy sessions did not terminate")
            # Drops global role and all its test-only grants atomically.
            with admin.transaction():
                admin.execute(
                    sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role))
                )
                admin.execute(
                    sql.SQL("DROP ROLE {}").format(sql.Identifier(role))
                )


def test_revoke_connect_only_from_role_is_not_an_activation_fence(legacy_role):
    """Default PUBLIC CONNECT grant defeats a role-only REVOKE CONNECT."""
    role, _repo = legacy_role
    with psycopg.connect(DSN) as admin:
        admin.execute(
            sql.SQL("REVOKE CONNECT ON DATABASE tsumgi137ownerprobe FROM {}")
            .format(sql.Identifier(role))
        )
        allowed = admin.execute(
            "SELECT has_database_privilege(%s,current_database(),'CONNECT')",
            (role,),
        ).fetchone()[0]
    assert allowed is True
    with _legacy_connection(role) as existing:
        assert existing.execute("SELECT current_user").fetchone()[0] == role


def test_nologin_is_not_a_fence_for_an_existing_writer_session(legacy_role):
    """NOLOGIN blocks reconnect but NOT an already-established SQL writer."""
    role, repo = legacy_role
    job = repo.submit_job(JobSubmission(capability="code.review", priority=3))
    existing = _legacy_connection(role)
    try:
        backend_pid = existing.info.backend_pid
        with psycopg.connect(DSN) as admin:
            admin.execute(
                sql.SQL("ALTER ROLE {} NOLOGIN").format(sql.Identifier(role))
            )
        # The already-authenticated old Control connection can still mutate
        # actual TSUMGI jobs, notwithstanding its newly disabled LOGIN.
        existing.execute(
            "UPDATE jobs SET priority=priority+1 WHERE id::text=%s",
            (job.job_id,),
        )
        existing.commit()
        assert repo.get_job(job.job_id).priority == 4

        # A *new* session using that role is refused.
        with pytest.raises(psycopg.OperationalError):
            _legacy_connection(role)

        with psycopg.connect(DSN, autocommit=True) as admin:
            row = admin.execute(
                "SELECT rolcanlogin FROM pg_roles WHERE rolname=%s",
                (role,),
            ).fetchone()
            assert row == (False,)
            active = admin.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE usename=%s",
                (role,),
            ).fetchone()[0]
            assert active >= 1, "NOLOGIN did not remove live sessions"
            ended = admin.execute(
                "SELECT pg_terminate_backend(%s)", (backend_pid,)
            ).fetchone()
            assert ended == (True,)

        # Once the old session is killed, it no longer retains write access.
        with pytest.raises(psycopg.OperationalError):
            existing.execute("SELECT 1")
    finally:
        existing.close()
        repo.cancel_job(job.job_id)

    with psycopg.connect(DSN) as admin:
        assert admin.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE usename=%s",
            (role,),
        ).fetchone()[0] == 0
    with pytest.raises(psycopg.OperationalError):
        _legacy_connection(role)
