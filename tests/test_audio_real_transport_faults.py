"""Real loopback transport faults for Issue #174's single-Control artifact MVI.

This tests a real TCP disconnect during HTTP upload and loss of a PostgreSQL
COMMIT acknowledgement *after server-side commit*. All sockets, database state
and Spool files are test-owned; there is no production fault injection.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
import os
import socket
import struct
from threading import Event, Thread
from time import monotonic, sleep

import httpx
import pytest
import uvicorn

from astrumweaver.audio.artifact_spool import ArtifactSpool
from astrumweaver.audio.input_channel import AudioInputChannel
from astrumweaver.control import (
    InMemoryControlRepository, JobSubmission, PostgresControlRepository,
)
from astrumweaver.control.api import create_app
from astrumweaver.control.repository import ConflictError, NotFoundError, StorageUnavailable
from test_audio_artifact_authority import EPOCH, setup_claim
from test_audio_artifact_authority_postgres import isolated_database
from test_audio_input_channel import WAV


def _input_channel(tmp_path, repo):
    root = tmp_path / "input"
    root.mkdir(mode=0o700)
    spool = ArtifactSpool(
        root, max_bytes=256, max_total_bytes=512,
        max_artifacts=3, ttl_seconds=300,
    )
    return AudioInputChannel(repo, spool, max_bytes=256)


class _TrackedUpload:
    """Capture actual ASGI receive signals produced by a loopback TCP client."""

    def __init__(self, app):
        self.app = app
        self.partial_received = Event()
        self.disconnect_received = Event()
        self.request_finished = Event()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] != "/v1/audio/inputs":
            return await self.app(scope, receive, send)

        async def traced_receive():
            message = await receive()
            if message["type"] == "http.request" and message.get("body"):
                self.partial_received.set()
            if message["type"] == "http.disconnect":
                self.disconnect_received.set()
            return message

        try:
            return await self.app(scope, traced_receive, send)
        finally:
            self.request_finished.set()


@contextmanager
def _live_control(app):
    """Only bind an ephemeral 127.0.0.1 socket; release it after the test."""
    server = uvicorn.Server(
        uvicorn.Config(
            app, host="127.0.0.1", port=0, lifespan="off",
            log_level="critical", access_log=False, http="h11",
        )
    )
    thread = Thread(target=server.run, daemon=True, name="tsumgi-audio-http-test")
    thread.start()
    try:
        deadline = monotonic() + 6
        while not server.started and thread.is_alive() and monotonic() < deadline:
            sleep(0.01)
        assert server.started and server.servers, "isolated HTTP server did not start"
        address = server.servers[0].sockets[0].getsockname()
        assert address[0] == "127.0.0.1" and address[1] > 0
        yield address[1]
    finally:
        server.should_exit = True
        thread.join(timeout=6)
        assert not thread.is_alive(), "isolated HTTP server did not stop"


def test_real_tcp_disconnect_mid_upload_never_stages_audio(tmp_path):
    repo = InMemoryControlRepository()
    channel = _input_channel(tmp_path, repo)
    tracked = _TrackedUpload(create_app(
        repo, client_token="client-secret", worker_token="worker-secret",
        audio_input_channel=channel,
    ))

    with _live_control(tracked) as port:
        with socket.create_connection(("127.0.0.1", port), timeout=4) as client:
            request = (
                "POST /v1/audio/inputs HTTP/1.1\r\n"
                "Host: 127.0.0.1\r\n"
                "Authorization: Bearer client-secret\r\n"
                "Content-Type: audio/wav\r\n"
                f"Content-Length: {len(WAV)}\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii")
            assert len(WAV) > 40
            client.sendall(request + WAV[:30])
            assert tracked.partial_received.wait(timeout=4), (
                "server did not receive partial bytes from actual TCP connection"
            )
            client.shutdown(socket.SHUT_RDWR)

        assert tracked.disconnect_received.wait(timeout=4), (
            "ASGI did not observe peer disconnect"
        )
        assert tracked.request_finished.wait(timeout=4), "request handler did not terminate"
        assert channel._inputs == {}
        assert channel.spool._records == {}
        assert channel.spool._total_bytes == 0
        assert list((tmp_path / "input" / channel.spool._name).iterdir()) == []

        # A real new connection succeeds, proving the abort did not poison
        # the server, consume quota, or create an artifact.
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=4) as client:
            result = client.post(
                "/v1/audio/inputs", content=WAV,
                headers={"authorization": "Bearer client-secret",
                         "content-type": "audio/wav"},
            )
            assert result.status_code == 201, result.text
            reference = result.json()
            assert reference["size_bytes"] == len(WAV)
            channel.discard(reference["uri"])
            assert channel.spool._total_bytes == 0


def _read_exact(sock, count):
    parts = bytearray()
    while len(parts) < count:
        received = sock.recv(count - len(parts))
        if not received:
            raise EOFError("unexpected PostgreSQL protocol end")
        parts.extend(received)
    return bytes(parts)


class _DropCommitAckProxy:
    """Forward real PostgreSQL protocol, withholding one successful COMMIT ack.

    PostgreSQL remains on its test-only local Unix socket or localhost TCP
    endpoint. The proxy listens on an ephemeral 127.0.0.1 port. We forward
    startup/auth, SQL results and all request bytes unmodified, except that
    the server's successful 'CommandComplete COMMIT' response and the following
    ReadyForQuery are deliberately *not* delivered to the libpq client.
    """

    def __init__(self, backend_host, backend_port):
        self.backend_host = backend_host
        self.backend_port = int(backend_port)
        self.commit_completed = Event()
        self.server_ready_after_commit = Event()
        self._shutdown = Event()
        self._workers = []

    def __enter__(self):
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(8)
        self.listener.settimeout(0.2)
        self.port = self.listener.getsockname()[1]
        self.acceptor = Thread(target=self._accept_connections, daemon=True)
        self.acceptor.start()
        return self

    def __exit__(self, *_):
        self._shutdown.set()
        self.listener.close()
        self.acceptor.join(timeout=4)
        for thread in self._workers:
            thread.join(timeout=4)
        assert not self.acceptor.is_alive()
        assert not any(thread.is_alive() for thread in self._workers), (
            "PostgreSQL loopback proxy connection was not cleaned up"
        )

    def _accept_connections(self):
        while not self._shutdown.is_set():
            try:
                client, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            worker = Thread(target=self._handle, args=(client,), daemon=True)
            self._workers.append(worker)
            worker.start()

    @staticmethod
    def _forward_client(client, backend):
        try:
            while chunk := client.recv(65536):
                backend.sendall(chunk)
        except (OSError, EOFError):
            pass

    def _handle(self, client):
        # CI's PostgreSQL 17 is bound to 127.0.0.1:5432 with password
        # authentication; local acceptance can use its private Unix socket.
        if self.backend_host.startswith("/"):
            backend = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            address = str(
                Path(self.backend_host) / (".s.PGSQL." + str(self.backend_port))
            )
        else:
            backend = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            address = (self.backend_host, self.backend_port)
        forwarder = None
        try:
            client.settimeout(6)
            backend.settimeout(6)
            backend.connect(address)
            forwarder = Thread(
                target=self._forward_client, args=(client, backend),
                daemon=True,
            )
            forwarder.start()
            while not self._shutdown.is_set():
                # PostgreSQL backend protocol after sslmode=disable startup:
                # 1-byte message tag, 4-byte big-endian length, payload.
                header = _read_exact(backend, 5)
                size = struct.unpack("!I", header[1:5])[0]
                if not 4 <= size <= 16 * 1024 * 1024:
                    raise AssertionError("unexpected PostgreSQL response frame")
                payload = _read_exact(backend, size - 4)
                if (header[0:1] == b"C" and payload == b"COMMIT\x00"
                        and not self.commit_completed.is_set()):
                    # A successful CommandComplete is emitted only after the
                    # database has executed COMMIT. Withhold it and await the
                    # backend's ReadyForQuery before cutting the client link.
                    self.commit_completed.set()
                    while True:
                        next_header = _read_exact(backend, 5)
                        following = struct.unpack("!I", next_header[1:5])[0]
                        assert 4 <= following <= 16 * 1024 * 1024
                        _read_exact(backend, following - 4)
                        if next_header[:1] == b"Z":
                            self.server_ready_after_commit.set()
                            break
                    break
                client.sendall(header + payload)
        except (OSError, EOFError):
            pass
        finally:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            client.close()
            try:
                backend.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            backend.close()
            if forwarder is not None:
                forwarder.join(timeout=2)


def _require_local_pg_destination(source):
    """Preflight the effective host before creating the disposable DB.

    libpq's hostaddr can override host, and PGHOSTADDR or PGSERVICE can
    redirect a connection despite its visible host label. We deliberately
    reject such ambiguity rather than altering any host or DB configuration.
    """
    pytest.importorskip("psycopg", reason="NOT_VERIFIED: PostgreSQL unavailable")
    from psycopg.conninfo import conninfo_to_dict

    if not source:
        pytest.skip("NOT_VERIFIED: dedicated PostgreSQL test DSN absent")
    config = conninfo_to_dict(source)
    if (config.get("service") or os.environ.get("PGSERVICE")
            or os.environ.get("PGHOSTADDR")):
        pytest.skip("NOT_VERIFIED: libpq destination not explicitly local")
    backend_host = config.get("host")
    backend_port = config.get("port")
    if (not backend_host or not backend_port
            or not config.get("dbname") or not config.get("user")):
        pytest.skip("NOT_VERIFIED: explicit local PG host, port, db, user required")
    if backend_host.startswith("/"):
        if config.get("hostaddr"):
            pytest.skip("NOT_VERIFIED: unexpected Unix socket hostaddr")
        if not (Path(backend_host) / f".s.PGSQL.{backend_port}").is_socket():
            pytest.skip("NOT_VERIFIED: explicit local PG Unix socket not present")
    elif (backend_host != "127.0.0.1"
            or config.get("hostaddr") not in (None, "127.0.0.1")):
        pytest.skip("NOT_VERIFIED: non-loopback PostgreSQL destination")
    return backend_host, int(backend_port)


@pytest.mark.parametrize("unsafe_dsn", [
    "host=127.0.0.1 hostaddr=203.0.113.7 port=55464 dbname=test user=test",
    "host=203.0.113.7 port=55464 dbname=test user=test",
    "port=55464 dbname=test user=test",
    "host=127.0.0.1 service=remote port=55464 dbname=test user=test",
])
def test_pg_fault_proxy_rejects_nonlocal_destination_before_db_fixture(
    monkeypatch, unsafe_dsn,
):
    monkeypatch.delenv("PGHOSTADDR", raising=False)
    monkeypatch.delenv("PGSERVICE", raising=False)
    with pytest.raises(pytest.skip.Exception, match="NOT_VERIFIED"):
        _require_local_pg_destination(unsafe_dsn)


def test_pg_fault_proxy_rejects_hostaddr_environment(monkeypatch):
    monkeypatch.setenv("PGHOSTADDR", "203.0.113.7")
    with pytest.raises(pytest.skip.Exception, match="NOT_VERIFIED"):
        _require_local_pg_destination(
            "host=127.0.0.1 port=55464 dbname=test user=test",
        )


def test_real_postgres_commit_ack_lost_after_successful_commit(
    tmp_path, request,
):
    psycopg = pytest.importorskip(
        "psycopg", reason="NOT_VERIFIED: PostgreSQL driver unavailable",
    )
    from psycopg.conninfo import make_conninfo

    # Guard runs before the fixture creates/migrates ANY database.
    backend_host, backend_port = _require_local_pg_destination(
        os.environ.get("ASTRUMWEAVER_TEST_DATABASE_URL"),
    )
    isolated_database = request.getfixturevalue("isolated_database")
    direct_repo = PostgresControlRepository(isolated_database, lease_seconds=60)
    ready = setup_claim(
        direct_repo, datetime.now(UTC), capability="audio.transcribe",
    )

    with _DropCommitAckProxy(backend_host, backend_port) as proxy:
        fault_dsn = make_conninfo(
            isolated_database, host="127.0.0.1", hostaddr="127.0.0.1",
            port=proxy.port, sslmode="disable", connect_timeout=4,
        )
        fault_repo = PostgresControlRepository(fault_dsn, lease_seconds=60)
        channel = _input_channel(tmp_path, fault_repo)
        reference = channel.stage(WAV)
        idem_key = "audio-real-network-commit-ack-loss"
        original = JobSubmission(
            capability="audio.transcribe",
            serving=ready.serving,
            deadline_at=datetime.now(UTC) + timedelta(seconds=120),
            idempotency_key=idem_key,
        )
        with pytest.raises(StorageUnavailable):
            channel.submit(original, artifact_uri=reference["uri"])
        assert proxy.commit_completed.is_set(), (
            "PostgreSQL did not report a successful COMMIT"
        )
        assert proxy.server_ready_after_commit.is_set(), (
            "backend did not finish COMMIT before ACK was dropped"
        )
        aid = reference["uri"].removeprefix("tsumgi-audio-input:")
        assert channel._inputs[aid].job_id is None
        assert channel._inputs[aid].submission is not None
        assert aid in channel.spool._records

        # Independently read the REAL durable DB through its Unix socket;
        # no mocked commit result or synthetic StorageUnavailable exception.
        with psycopg.connect(isolated_database) as connection:
            rows = connection.execute(
                """SELECT id::text, payload->'audio_input'->>'uri' AS input_uri
                FROM jobs WHERE idempotency_key = %s""",
                (idem_key,),
            ).fetchall()
        assert len(rows) == 1
        job_id, input_uri = rows[0]
        assert input_uri == reference["uri"]

        # No client replay: the durable job's current Worker must be able to
        # rebind its retained receipt based on the exact originally pinned
        # submission and the current durable identity/lease.
        claimed = direct_repo.claim_next_job(
            "other", runtime_instance_epoch=EPOCH,
        )
        assert claimed is not None and claimed.job_id == job_id
        assert channel.read_for_worker(
            job_id, worker_id="other", attempt=claimed.attempts,
            lease_token=claimed.lease_token, runtime_instance_epoch=EPOCH,
        ) == WAV
        assert channel._inputs[aid].job_id == job_id
        assert channel.submit(original, artifact_uri=reference["uri"]).job_id == job_id
        with pytest.raises(NotFoundError):
            channel.discard(reference["uri"])
        direct_repo.cancel_job(job_id)
        with pytest.raises(ConflictError):
            channel.read_for_worker(
                job_id, worker_id="other", attempt=claimed.attempts,
                lease_token=claimed.lease_token, runtime_instance_epoch=EPOCH,
            )
