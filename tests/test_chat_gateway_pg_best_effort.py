"""Real, disposable PostgreSQL 17 admission/cancellation acceptance (#137).

Unlike the opt-in shared-DSN tests, this module never reads a database URL from
the environment. Run with PostgreSQL 17 binaries and psycopg in the test shell.
Stage 3A acknowledgement/UNKNOWN tests intentionally fail on the Stage 2 base;
they must not be turned into skips or xfails to make a regression run green.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic
from uuid import uuid4

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
from astrumweaver.gateway.api import ChatGatewayService
from astrumweaver.gateway.chat import ChatGatewayError, ChatProfileCatalog
from test_chat_gateway_api import _ConnectedRequest, register_worker, serving_values


@dataclass(frozen=True)
class PrivatePG:
    dsn: str
    data: Path
    socket: Path
    identifier: str
    database: str

    def guard(self) -> None:
        """Recheck server, socket, data directory, database and sentinel."""
        assert (self.data / ".tsumgi-test-owner").read_text() == self.identifier
        assert not self.data.is_symlink() and not self.socket.is_symlink()
        with psycopg.connect(self.dsn, connect_timeout=2) as connection:
            version, directory, database, system_id, tcp_address = connection.execute(
                """SELECT current_setting('server_version_num')::int,
                          current_setting('data_directory'), current_database(),
                          system_identifier::text, inet_server_addr()
                   FROM pg_control_system()"""
            ).fetchone()
        assert 170000 <= version < 180000, version
        assert Path(directory).resolve() == self.data.resolve()
        assert database == self.database
        assert system_id == self.identifier
        assert tcp_address is None  # not a network-exposed PostgreSQL server

    def rows(self):
        self.guard()
        with psycopg.connect(self.dsn, connect_timeout=2) as connection:
            return connection.execute(
                "SELECT id::text, status, idempotency_key FROM jobs ORDER BY id"
            ).fetchall()


@pytest.fixture
def private_pg() -> PrivatePG:
    initdb, pg_ctl = shutil.which("initdb"), shutil.which("pg_ctl")
    if not (initdb and pg_ctl):
        pytest.skip("opt-in real PostgreSQL 17 test requires initdb and pg_ctl in PATH")
    # Keep the socket path under the Unix sockaddr length limit. No external
    # DSN, shared service, or pre-existing cluster is ever used.
    root = Path(tempfile.mkdtemp(prefix="tpg-", dir="/tmp")).resolve()
    data, socket = root / "data", root / "sock"
    socket.mkdir(mode=0o700)
    token = uuid4().hex
    database = "tsumgi_" + token[:16]
    port = 20000 + int(token[:6], 16) % 35000
    started = False
    owned = False
    try:
        subprocess.run(
            [initdb, "-D", str(data), "-A", "trust", "-U", "tsumgi_test",
             "--encoding=UTF8", "--no-instructions"],
            check=True, capture_output=True, text=True, timeout=30,
        )
        # Data is freshly created inside our own root, not a symlink or mount.
        assert data.resolve().parent == root and not data.is_symlink()
        assert data.stat().st_dev == root.stat().st_dev
        subprocess.run(
            [pg_ctl, "-D", str(data), "-w", "-t", "15", "-l",
             str(root / "postgres.log"), "-o",
             f"-c listen_addresses='' -c unix_socket_directories={socket} -p {port}",
             "start"],
            check=True, capture_output=True, text=True, timeout=20,
        )
        started = True
        maintenance = psycopg.conninfo.make_conninfo(
            "", host=str(socket), port=port, user="tsumgi_test", dbname="postgres"
        )
        with psycopg.connect(maintenance, connect_timeout=2) as connection:
            version, directory, system_id, tcp_address = connection.execute(
                """SELECT current_setting('server_version_num')::int,
                          current_setting('data_directory'),
                          system_identifier::text, inet_server_addr()
                   FROM pg_control_system()"""
            ).fetchone()
        assert 170000 <= version < 180000
        assert Path(directory).resolve() == data.resolve()
        assert tcp_address is None and system_id
        (data / ".tsumgi-test-owner").write_text(system_id)
        owned = True
        # All checks above precede the first database/schema mutation.
        with psycopg.connect(maintenance, autocommit=True, connect_timeout=2) as conn:
            conn.execute(f'CREATE DATABASE "{database}"')
        dsn = psycopg.conninfo.make_conninfo(
            maintenance, dbname=database, connect_timeout="2"
        )
        cluster = PrivatePG(dsn, data, socket, system_id, database)
        cluster.guard()
        apply_migrations(dsn)
        cluster.guard()
        yield cluster
    finally:
        # Never stop or remove an unidentified cluster, even if setup fails.
        if started and owned:
            with psycopg.connect(maintenance, connect_timeout=2) as conn:
                actual_dir, actual_id = conn.execute(
                    "SELECT current_setting('data_directory'), system_identifier::text "
                    "FROM pg_control_system()"
                ).fetchone()
            assert Path(actual_dir).resolve() == data.resolve()
            assert actual_id == (data / ".tsumgi-test-owner").read_text()
            subprocess.run(
                [pg_ctl, "-D", str(data), "-w", "-t", "15", "stop", "-m", "immediate"],
                check=True, capture_output=True, text=True, timeout=20,
            )
        if not started or owned:
            # Only the freshly allocated root, after a successful stop (or
            # before startup), can be removed. Never touch any shared DB.
            assert root.name.startswith("tpg-") and root.parent == Path("/tmp")
            assert not root.is_symlink()
            assert not data.exists() or root.stat().st_dev == data.stat().st_dev
            shutil.rmtree(root)


def _service(repo, *, timeout=8) -> ChatGatewayService:
    profile, serving = serving_values(timeout=timeout)
    register_worker(repo, serving)
    return ChatGatewayService(
        repo, ChatProfileCatalog((profile,)),
        max_pending_admissions=1, poll_interval_seconds=0.01,
    )


BODY = {
    "model": "local-code-v1",
    "messages": [{"role": "user", "content": "isolated PG17 acceptance"}],
    "stream": True,
}


async def _until(predicate, *, seconds=5):
    deadline = monotonic() + seconds
    while monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.025)
    pytest.fail("bounded PostgreSQL observation did not converge")


class ObservedRepository(PostgresControlRepository):
    def __init__(self, dsn):
        super().__init__(dsn)
        self.entered = Event()
        self.cancel_calls = []
        self.cancel_lock = Lock()

    def submit_job(self, *args, **kwargs):
        self.entered.set()
        return super().submit_job(*args, **kwargs)

    def cancel_job(self, job_id, *args, **kwargs):
        with self.cancel_lock:
            self.cancel_calls.append(job_id)
        return super().cancel_job(job_id, *args, **kwargs)


@pytest.mark.asyncio
async def test_locked_cancel_then_late_commit_cancels_exact_owned_job(private_pg):
    """Stage 1/2: a blocked real INSERT survives request Task cancellation."""
    private_pg.guard()
    repo = ObservedRepository(private_pg.dsn)
    service = _service(repo)
    with psycopg.connect(private_pg.dsn, connect_timeout=2) as locker:
        locker.execute("LOCK TABLE jobs IN ACCESS EXCLUSIVE MODE")
        request = asyncio.create_task(service.stream(_ConnectedRequest(), BODY, request_size_bytes=99))
        try:
            assert await asyncio.to_thread(repo.entered.wait, 2)
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            assert service._admissions_in_flight == 1
            locker.rollback()
            await _until(lambda: len(private_pg.rows()) == 1)
            await _until(lambda: private_pg.rows()[0][1] == "cancelled")
            await _until(lambda: service._admissions_in_flight == 0)
            assert repo.cancel_calls == [private_pg.rows()[0][0]]
            assert repo.get_worker("chat-worker").active_jobs == 0
        finally:
            locker.rollback()
            if not request.done():
                request.cancel()
            await asyncio.gather(request, return_exceptions=True)


@pytest.mark.asyncio
async def test_locked_admission_expires_without_creating_job(private_pg):
    """Stage 2 server bound, even while the exclusive lock is held."""
    private_pg.guard()
    repo = ObservedRepository(private_pg.dsn)
    service = _service(repo, timeout=1.2)
    with psycopg.connect(private_pg.dsn, connect_timeout=2) as locker:
        locker.execute("LOCK TABLE jobs IN ACCESS EXCLUSIVE MODE")
        request = asyncio.create_task(service.stream(_ConnectedRequest(), BODY, request_size_bytes=99))
        try:
            assert await asyncio.to_thread(repo.entered.wait, 2)
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            # While PostgreSQL holds a write lock, an unconfirmed admission
            # retains its finite permit; releasing the lock allows settlement.
            assert service._admissions_in_flight == 1
            # Hold the exclusive lock past the 1.2s admission deadline;
            # the DB transaction must expire, not silently submit a Job.
            await asyncio.sleep(1.7)
            locker.rollback()
            await _until(lambda: service._admissions_in_flight == 0, seconds=10)
            assert not service._pending_admission_cleanup
        finally:
            locker.rollback()
            if not request.done():
                request.cancel()
            await asyncio.gather(request, return_exceptions=True)
    assert private_pg.rows() == []


class LostAcknowledgement(ObservedRepository):
    def __init__(self, dsn):
        super().__init__(dsn)
        self.committed_id = None
        self.submission_key = None

    def submit_job(self, submission, **kwargs):
        self.submission_key = submission.idempotency_key
        record = super().submit_job(submission, **kwargs)
        self.committed_id = record.job_id
        raise StorageUnavailable("simulated lost COMMIT acknowledgement after real commit")


@pytest.mark.asyncio
async def test_lost_commit_ack_reconciles_and_cancels_owned_job(private_pg):
    """Stage 3A: never treat an exception after COMMIT as proof of no Job."""
    private_pg.guard()
    repo = LostAcknowledgement(private_pg.dsn)
    service = _service(repo)
    with pytest.raises(ChatGatewayError) as error:
        await service.stream(_ConnectedRequest(), BODY, request_size_bytes=99)
    assert error.value.status_code == 503
    assert repo.submission_key, "Stage 3A must assign an owned durable identity"
    await _until(lambda: len(private_pg.rows()) == 1)
    assert private_pg.rows()[0][0] == repo.committed_id
    assert private_pg.rows()[0][2] == repo.submission_key
    await _until(lambda: private_pg.rows()[0][1] == "cancelled")
    assert set(repo.cancel_calls) == {repo.committed_id}


class DelayedAmbiguousCommit(ObservedRepository):
    def __init__(self, dsn):
        super().__init__(dsn)
        self.release = Event()
        self.finished = Event()
        self.submission_key = None
        self.late_error = None
        self.thread = None

    def submit_job(self, submission, **kwargs):
        self.submission_key = submission.idempotency_key
        self.entered.set()

        def late():
            try:
                if not self.release.wait(5):
                    raise AssertionError("late server-side commit was never released")
                super(DelayedAmbiguousCommit, self).submit_job(submission, **kwargs)
            except BaseException as exc:
                self.late_error = exc
            finally:
                self.finished.set()

        self.thread = Thread(target=late, daemon=True)
        self.thread.start()
        raise StorageUnavailable("unknown outcome; commit still pending")


@pytest.mark.asyncio
async def test_readback_miss_before_late_commit_remains_unknown(private_pg):
    """Stage 3A: a negative readback is not durable proof of NO_JOB.

    The deliberately delayed commit exposes the best-effort boundary: this test
    does NOT assert no orphan. It requires stable identity, reports UNKNOWN to
    the caller and checks the later actual durable outcome, including an orphan
    if cancellation cannot be confirmed. No synthetic in-memory Job is used.
    """
    private_pg.guard()
    repo = DelayedAmbiguousCommit(private_pg.dsn)
    service = _service(repo)
    try:
        with pytest.raises(ChatGatewayError) as error:
            await service.stream(_ConnectedRequest(), BODY, request_size_bytes=99)
        assert error.value.status_code == 503
        assert repo.submission_key, "UNKNOWN requires an owned lookup identity"
        assert private_pg.rows() == []  # the readback miss precedes COMMIT
        # A readback miss is UNKNOWN; absent persistent Owner state, a
        # later COMMIT can still create a claimable orphan. This limitation
        # is explicitly accepted by the bounded best-effort contract.
        assert service._unresolved_admissions >= 1
        repo.release.set()
        assert await asyncio.to_thread(repo.finished.wait, 5)
        assert repo.late_error is None, repo.late_error
        await _until(lambda: len(private_pg.rows()) == 1)
        assert private_pg.rows()[0][2] == repo.submission_key
        # Explicit negative control: the orphan is QUEUED, demonstrating the
        # accepted limitation. Test-owned cleanup cancels it by exact ID.
        assert private_pg.rows()[0][1] == "queued"
        orphan_id = private_pg.rows()[0][0]
        await asyncio.to_thread(repo.cancel_job, orphan_id)
        assert private_pg.rows()[0][1] == "cancelled"
    finally:
        repo.release.set()
        if repo.thread is not None:
            await asyncio.to_thread(repo.thread.join, 6)


class LostCancellationAcknowledgements(ObservedRepository):
    def cancel_job(self, job_id, *args, **kwargs):
        with self.cancel_lock:
            attempt = len(self.cancel_calls)
        record = super().cancel_job(job_id, *args, **kwargs)
        if attempt < 2:
            raise StorageUnavailable("cancellation ACK lost after durable transition")
        return record


@pytest.mark.asyncio
async def test_postadmit_cancel_ack_loss_keeps_exact_job_and_marks_unresolved(private_pg):
    """A lost post-admission cancel ACK is not reported as proven success."""
    private_pg.guard()
    repo = LostCancellationAcknowledgements(private_pg.dsn)
    service = _service(repo, timeout=3)
    request = asyncio.create_task(service.complete(_ConnectedRequest(), BODY, request_size_bytes=99))
    try:
        await _until(lambda: len(private_pg.rows()) == 1)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        await _until(lambda: len(repo.cancel_calls) == 1, seconds=4)
        await _until(lambda: service._unresolved_cancellations == 1, seconds=4)
        assert set(repo.cancel_calls) == {private_pg.rows()[0][0]}
        # The server committed CANCELLED, but the caller lost its ACK; the
        # service correctly reports the outcome as unconfirmed best-effort.
        assert private_pg.rows()[0][1] == "cancelled"
    finally:
        if not request.done():
            request.cancel()
        await asyncio.gather(request, return_exceptions=True)


def test_operator_gucs_expired_deadline_and_legacy_nonchat(private_pg):
    """Stage 2 bounds remain strict; non-chat opt-out remains functional."""
    private_pg.guard()
    dsn = psycopg.conninfo.make_conninfo(
        private_pg.dsn,
        options="-c statement_timeout=200 -c lock_timeout=150 -c transaction_timeout=400",
    )
    repo = PostgresControlRepository(dsn)
    with repo._transaction(admission_deadline_at=utc_now() + timedelta(seconds=6)) as conn:
        settings = {
            row["name"]: row["setting"]
            for row in conn.execute(
                "SELECT name, setting::int AS setting FROM pg_settings WHERE name IN "
                "('statement_timeout', 'lock_timeout', 'transaction_timeout')"
            ).fetchall()
        }
        assert settings == {
            "statement_timeout": 200, "lock_timeout": 150, "transaction_timeout": 400,
        }
    with pytest.raises(DeadlineExceededError):
        repo.submit_job(
            JobSubmission(capability="llm.chat"),
            admission_deadline_at=utc_now() - timedelta(seconds=1),
        )
    assert private_pg.rows() == []
    legacy = repo.submit_job(JobSubmission(capability="code.review"))
    assert legacy.status is JobStatus.QUEUED
    assert private_pg.rows() == [(legacy.job_id, "queued", None)]
