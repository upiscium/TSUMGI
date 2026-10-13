"""Opt-in server-bounded chat admission tests on disposable PostgreSQL 17."""

from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from threading import Event, Lock
from time import monotonic, sleep

import pytest

psycopg = pytest.importorskip("psycopg")

from astrumweaver.control import (
    DeadlineExceededError,
    JobStatus,
    JobSubmission,
    PostgresControlRepository,
    StorageUnavailable,
    utc_now,
)
from astrumweaver.control.migrate import apply_migrations
from astrumweaver.control import postgres as postgres_module
from astrumweaver.gateway.api import ChatGatewayService
from astrumweaver.gateway.chat import ChatProfileCatalog
from test_chat_gateway_admission_cancellation import _wait_for
from test_chat_gateway_api import _ConnectedRequest, register_worker, serving_values


DATABASE_URL = os.environ.get("ASTRUMWEAVER_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="requires dedicated disposable PostgreSQL database"
)


@pytest.fixture(autouse=True)
def isolated_database():
    assert DATABASE_URL is not None
    apply_migrations(DATABASE_URL)
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        conn.execute("TRUNCATE TABLE jobs, workers RESTART IDENTITY CASCADE")


def _count_jobs() -> int:
    assert DATABASE_URL is not None
    with psycopg.connect(DATABASE_URL) as conn:
        return int(conn.execute("SELECT count(*) FROM jobs").fetchone()[0])


def _job_statuses() -> list[JobStatus]:
    assert DATABASE_URL is not None
    with psycopg.connect(DATABASE_URL) as conn:
        return [
            JobStatus(row[0])
            for row in conn.execute("SELECT status FROM jobs").fetchall()
        ]


def test_postgres_bounded_submission_commits_before_real_deadline():
    repo = PostgresControlRepository(DATABASE_URL)
    record = repo.submit_job(
        JobSubmission(capability="code.review"),
        admission_deadline_at=utc_now() + timedelta(seconds=8),
    )
    assert record.status is JobStatus.QUEUED
    assert repo.get_job(record.job_id).status is JobStatus.QUEUED
    assert _count_jobs() == 1


def test_postgres_expired_deadline_rejected_before_admission():
    repo = PostgresControlRepository(DATABASE_URL)
    with pytest.raises(DeadlineExceededError):
        repo.submit_job(
            JobSubmission(capability="code.review"),
            admission_deadline_at=utc_now() - timedelta(milliseconds=5),
        )
    assert _count_jobs() == 0


def test_postgres_lock_contention_cannot_commit_after_deadline():
    repo = PostgresControlRepository(DATABASE_URL)
    deadline = utc_now() + timedelta(seconds=1.0)
    started = monotonic()
    with psycopg.connect(DATABASE_URL) as locker:
        locker.execute("LOCK TABLE jobs IN ACCESS EXCLUSIVE MODE")
        with ThreadPoolExecutor(max_workers=1) as pool:
            operation = pool.submit(
                repo.submit_job,
                JobSubmission(capability="code.review"),
                admission_deadline_at=deadline,
            )
            try:
                with pytest.raises((StorageUnavailable, DeadlineExceededError)):
                    operation.result(timeout=4)
            finally:
                # Always release the exclusive lock, even when the repository
                # implementation unexpectedly blocks or test assertions fail.
                locker.rollback()
    assert monotonic() - started < 4
    assert _count_jobs() == 0


class PausedBeforeTransactionCommit(PostgresControlRepository):
    def __init__(self, database_url: str):
        super().__init__(database_url)
        self.post_sql_pause_reached = Event()

    @contextmanager
    def _transaction(self, *, admission_deadline_at=None):
        with super()._transaction(
            admission_deadline_at=admission_deadline_at
        ) as connection:
            yield connection
            # Insert returned but the SQL transaction has not committed yet.
            self.post_sql_pause_reached.set()
            sleep(1.5)


def test_postgres_fresh_clock_before_commit_rejects_elapsed_deadline():
    repo = PausedBeforeTransactionCommit(DATABASE_URL)
    with pytest.raises((DeadlineExceededError, StorageUnavailable)):
        repo.submit_job(
            JobSubmission(capability="code.review"),
            admission_deadline_at=utc_now() + timedelta(seconds=1),
        )
    assert repo.post_sql_pause_reached.is_set()
    assert _count_jobs() == 0


def test_postgres_unbounded_existing_direct_job_path_is_unchanged():
    repo = PostgresControlRepository(DATABASE_URL)
    job = repo.submit_job(JobSubmission(capability="code.review"))
    assert repo.get_job(job.job_id).status is JobStatus.QUEUED



class ObservablePostgresRepository(PostgresControlRepository):
    def __init__(self, database_url: str):
        super().__init__(database_url)
        self.entered = Event()
        self.submissions = 0
        self._counter_lock = Lock()

    def submit_job(self, *args, **kwargs):
        with self._counter_lock:
            self.submissions += 1
            self.entered.set()
        return super().submit_job(*args, **kwargs)


def _gateway(repo: ObservablePostgresRepository, *, timeout=4):
    profile, serving = serving_values(timeout=timeout)
    register_worker(repo, serving)
    return ChatGatewayService(
        repo, ChatProfileCatalog((profile,)),
        max_pending_admissions=1,
        poll_interval_seconds=0.005,
    )


_BODY = {
    "model": "local-code-v1",
    "messages": [{"role": "user", "content": "postgres late admission"}],
    "stream": True,
}


@pytest.mark.asyncio
async def test_gateway_postgres_client_cancel_then_lock_release_cancels_late_job():
    repo = ObservablePostgresRepository(DATABASE_URL)
    service = _gateway(repo, timeout=5)
    with psycopg.connect(DATABASE_URL) as locker:
        locker.execute("LOCK TABLE jobs IN ACCESS EXCLUSIVE MODE")
        request = asyncio.create_task(
            service.stream(_ConnectedRequest(), _BODY, request_size_bytes=99)
        )
        try:
            assert await asyncio.to_thread(repo.entered.wait, 2)
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            assert service._admissions_in_flight == 1
            locker.rollback()
            await _wait_for(lambda: _count_jobs() == 1, seconds=5)
            await _wait_for(
                lambda: _job_statuses() == [JobStatus.CANCELLED],
                seconds=5,
            )
            await _wait_for(lambda: service._admissions_in_flight == 0)
            assert not service._pending_admission_cleanup
            assert repo.get_worker("chat-worker").active_jobs == 0
        finally:
            locker.rollback()
            if not request.done():
                request.cancel()
            await asyncio.gather(request, return_exceptions=True)


@pytest.mark.asyncio
async def test_gateway_postgres_cancel_while_lock_exceeds_deadline_creates_no_job():
    repo = ObservablePostgresRepository(DATABASE_URL)
    service = _gateway(repo, timeout=1.2)
    with psycopg.connect(DATABASE_URL) as locker:
        locker.execute("LOCK TABLE jobs IN ACCESS EXCLUSIVE MODE")
        request = asyncio.create_task(
            service.stream(_ConnectedRequest(), _BODY, request_size_bytes=99)
        )
        try:
            assert await asyncio.to_thread(repo.entered.wait, 2)
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            assert service._admissions_in_flight == 1
            # Even while the lock is still held, SQL must finish by a real
            # server-side deadline, allowing the detached Task to release it.
            await _wait_for(
                lambda: service._admissions_in_flight == 0, seconds=4
            )
            assert not service._pending_admission_cleanup
            assert service._unexpected_cleanup_failures == 0
        finally:
            locker.rollback()
            if not request.done():
                request.cancel()
            await asyncio.gather(request, return_exceptions=True)
    assert _count_jobs() == 0


def test_postgres_actual_statement_timeout_limits_blocking_server_query():
    repo = PostgresControlRepository(DATABASE_URL)
    before = monotonic()
    with pytest.raises(StorageUnavailable):
        with repo._transaction(
            admission_deadline_at=utc_now() + timedelta(seconds=1)
        ) as conn:
            conn.execute("SELECT pg_sleep(5)")
    assert monotonic() - before < 4
    assert _count_jobs() == 0



def test_postgres_does_not_relax_tighter_operator_timeout_options():
    operator_dsn = psycopg.conninfo.make_conninfo(
        DATABASE_URL,
        options=(
            "-c statement_timeout=200 -c lock_timeout=150 "
            "-c transaction_timeout=400"
        ),
    )
    repo = PostgresControlRepository(operator_dsn)
    with repo._transaction(
        admission_deadline_at=utc_now() + timedelta(seconds=8)
    ) as conn:
        settings = {
            str(row["name"]): int(row["value_ms"])
            for row in conn.execute(
                """SELECT name, setting::bigint AS value_ms FROM pg_settings
                WHERE name IN ('statement_timeout', 'lock_timeout',
                               'transaction_timeout')"""
            ).fetchall()
        }
        assert settings == {
            "statement_timeout": 200,
            "lock_timeout": 150,
            "transaction_timeout": 400,
        }
    # Even after a successful transaction, the operator session cap stays strict.
    record = repo.submit_job(
        JobSubmission(capability="code.review"),
        admission_deadline_at=utc_now() + timedelta(seconds=8),
    )
    assert record.status is JobStatus.QUEUED


def _dsn_without_timeout_or_service():
    options = psycopg.conninfo.conninfo_to_dict(DATABASE_URL)
    options.pop("connect_timeout", None)
    options.pop("service", None)
    return psycopg.conninfo.make_conninfo("", **options)


@pytest.mark.parametrize(
    ("operator_timeout", "expected_timeout"),
    [("1", 1), ("9", 5)],
)
def test_postgres_bounded_connect_preserves_env_timeout(
    monkeypatch, operator_timeout, expected_timeout
):
    monkeypatch.delenv("PGSERVICE", raising=False)
    monkeypatch.setenv("PGCONNECT_TIMEOUT", operator_timeout)
    captured = []
    connect = psycopg.connect

    def recording_connect(conninfo, **kwargs):
        captured.append(psycopg.conninfo.conninfo_to_dict(conninfo)["connect_timeout"])
        return connect(conninfo, **kwargs)

    monkeypatch.setattr(postgres_module.psycopg, "connect", recording_connect)
    repo = PostgresControlRepository(_dsn_without_timeout_or_service())
    with repo._connection(admission_deadline_at=utc_now() + timedelta(seconds=8)):
        pass
    assert captured == [str(expected_timeout)]


def test_postgres_explicit_timeout_takes_precedence_over_env(monkeypatch):
    monkeypatch.delenv("PGSERVICE", raising=False)
    monkeypatch.setenv("PGCONNECT_TIMEOUT", "1")
    captured = []
    connect = psycopg.connect

    def recording_connect(conninfo, **kwargs):
        captured.append(psycopg.conninfo.conninfo_to_dict(conninfo)["connect_timeout"])
        return connect(conninfo, **kwargs)

    monkeypatch.setattr(postgres_module.psycopg, "connect", recording_connect)
    dsn = psycopg.conninfo.make_conninfo(
        _dsn_without_timeout_or_service(), connect_timeout=3
    )
    with PostgresControlRepository(dsn)._connection(
        admission_deadline_at=utc_now() + timedelta(seconds=8)
    ):
        pass
    assert captured == ["3"]


@pytest.mark.parametrize("selection", ["dsn", "environment"])
def test_postgres_service_timeout_unresolved_fails_closed(
    monkeypatch, tmp_path, selection
):
    service_file = tmp_path / "pg_service.conf"
    service_file.write_text("[bounded_test]\nconnect_timeout=1\n", encoding="utf-8")
    monkeypatch.setenv("PGSERVICEFILE", str(service_file))
    monkeypatch.setenv("PGCONNECT_TIMEOUT", "9")
    dsn = _dsn_without_timeout_or_service()
    if selection == "dsn":
        monkeypatch.delenv("PGSERVICE", raising=False)
        dsn = psycopg.conninfo.make_conninfo(dsn, service="bounded_test")
    else:
        monkeypatch.setenv("PGSERVICE", "bounded_test")

    def no_connect(*args, **kwargs):
        raise AssertionError("unknown service timeout must not be overridden")

    monkeypatch.setattr(postgres_module.psycopg, "connect", no_connect)
    repo = PostgresControlRepository(dsn)
    with pytest.raises(StorageUnavailable, match="explicit connect_timeout"):
        with repo._connection(admission_deadline_at=utc_now() + timedelta(seconds=8)):
            pass


def test_postgres_explicit_timeout_overrides_service_timeout(monkeypatch, tmp_path):
    service_file = tmp_path / "pg_service.conf"
    service_file.write_text("[bounded_test]\nconnect_timeout=1\n", encoding="utf-8")
    monkeypatch.setenv("PGSERVICEFILE", str(service_file))
    monkeypatch.setenv("PGSERVICE", "bounded_test")
    monkeypatch.setenv("PGCONNECT_TIMEOUT", "4")
    dsn = psycopg.conninfo.make_conninfo(
        _dsn_without_timeout_or_service(), connect_timeout=2
    )
    with PostgresControlRepository(dsn)._connection(
        admission_deadline_at=utc_now() + timedelta(seconds=8)
    ) as conn:
        # Explicit DSN takes priority over the service and env; the bounded
        # rewrite must not replace it with the longer five-second cap.
        assert conn.info.get_parameters()["connect_timeout"] == "2"


def test_postgres_bounded_admission_rejects_mock_pg16_before_any_sql(monkeypatch):
    class OldConnection:
        info = SimpleNamespace(server_version=160000)
        closed = False
        def execute(self, *args, **kwargs):
            raise AssertionError("unsupported old PostgreSQL must not execute SQL")
        def close(self):
            self.closed = True

    conn = OldConnection()
    monkeypatch.setattr(
        postgres_module.psycopg, "connect", lambda *args, **kwargs: conn
    )
    with pytest.raises(StorageUnavailable, match="PostgreSQL 17"):
        PostgresControlRepository(DATABASE_URL).submit_job(
            JobSubmission(capability="code.review"),
            admission_deadline_at=utc_now() + timedelta(seconds=10),
        )
    assert conn.closed


def test_postgres_bounded_admission_rejects_clock_skew_before_job_write(monkeypatch):
    # Deliberately skew only application time used in this repository.
    # PostgreSQL clock_timestamp remains actual DB wall clock.
    actual_now = datetime.now(UTC)
    with monkeypatch.context() as patch:
        patch.setattr(
            postgres_module, "utc_now",
            lambda: datetime.now(UTC) + timedelta(seconds=6),
        )
        with pytest.raises(StorageUnavailable, match="clock skew"):
            PostgresControlRepository(DATABASE_URL).submit_job(
                JobSubmission(capability="code.review"),
                admission_deadline_at=actual_now + timedelta(seconds=15),
            )
    assert _count_jobs() == 0
