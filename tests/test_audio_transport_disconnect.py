"""Real loopback TCP interruption with an opt-in private audio input API."""

import socket
import threading
from contextlib import contextmanager

import pytest

from test_audio_artifact_authority_postgres import isolated_database

from astrumweaver.audio import ArtifactSpool
from astrumweaver.audio.input_channel import AudioInputChannel
from astrumweaver.control import InMemoryControlRepository
from astrumweaver.control.api import create_app


@contextmanager
def serve_on_loopback(app):
    import uvicorn

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    sock.settimeout(0.2)
    port = sock.getsockname()[1]
    config = uvicorn.Config(
        app, log_level="critical", lifespan="off", access_log=False,
        timeout_keep_alive=1,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        import time
        for _ in range(200):
            if server.started:
                break
            if not thread.is_alive():
                raise RuntimeError("server failed to start")
            time.sleep(0.01)
        assert server.started
        yield port
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive(), "isolated loopback server did not stop"
        sock.close()


def test_real_tcp_abort_during_authenticated_audio_upload_no_artifact(tmp_path):
    repo = InMemoryControlRepository()
    root = tmp_path / "spool"
    root.mkdir(mode=0o700)
    spool = ArtifactSpool(
        root, max_bytes=256, max_total_bytes=512,
        max_artifacts=2, ttl_seconds=120,
    )
    channel = AudioInputChannel(repo, spool, max_bytes=256)
    app = create_app(
        repo, client_token="local-client", worker_token="local-worker",
        audio_input_channel=channel,
    )
    request = (
        b"POST /v1/audio/inputs HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        b"Authorization: Bearer local-client\r\n"
        b"Content-Type: audio/wav\r\n"
        b"Content-Length: 200\r\n"
        b"Connection: close\r\n\r\n"
        b"RIFF" + bytes(8)
    )
    with serve_on_loopback(app) as port:
        with socket.create_connection(("127.0.0.1", port), timeout=3) as client:
            client.sendall(request)
            # Half-close the real TCP write side while 188 declared bytes
            # remain outstanding. The socket teardown is server-observed.
            client.shutdown(socket.SHUT_WR)
            client.settimeout(3)
            response = bytearray()
            while True:
                try:
                    part = client.recv(4096)
                except (TimeoutError, ConnectionResetError):
                    break
                if not part:
                    break
                response.extend(part)
            # The transport may close without sending any HTTP response.
            # A successful 201 is forbidden, but receiving 400 is optional.
            assert b" 201 " not in response.split(b"\r\n", 1)[0], response[:250]

        import httpx
        from test_audio_input_channel import WAV
        live = httpx.post(
            f"http://127.0.0.1:{port}/v1/audio/inputs",
            content=WAV,
            headers={"authorization": "Bearer local-client",
                     "content-type": "audio/wav"},
            timeout=3,
        )
        assert live.status_code == 201, live.text
        staged = live.json()
        channel.discard(staged["uri"])

    assert repo.list_jobs() == []
    assert not channel._inputs
    assert not spool._records
    assert spool._total_bytes == 0
    assert not tuple((root / spool._name).iterdir())


def test_pg_commit_ack_socket_dropped_after_actual_commit(
    tmp_path, isolated_database,
):
    """Drop a real libpq socket after transmitting COMMIT; prove DB durability.

    The loopback TCP proxy uses a private PostgreSQL unix-socket database as
    its only upstream. It drops the backend acknowledgement for the COMMIT
    command, leaving the client with an ambiguous result, then the durable
    Job is re-read through a distinct direct PostgreSQL connection.
    """
    import select
    import time
    import psycopg
    from datetime import UTC, datetime, timedelta
    from astrumweaver.control import PostgresControlRepository, JobSubmission
    from astrumweaver.control.repository import StorageUnavailable
    from test_audio_artifact_authority import setup_claim, EPOCH
    from test_audio_input_channel import WAV

    direct = PostgresControlRepository(isolated_database, lease_seconds=60)
    compatible = setup_claim(direct, datetime.now(UTC), capability="audio.transcribe")
    input_root = tmp_path / "input"
    input_root.mkdir(mode=0o700)
    spool = ArtifactSpool(
        input_root, max_bytes=256, max_total_bytes=512,
        max_artifacts=2, ttl_seconds=300,
    )
    channel = AudioInputChannel(direct, spool, max_bytes=256)
    staged = channel.stage(WAV)
    submission = JobSubmission(
        capability="audio.transcribe", serving=compatible.serving,
        idempotency_key="real-network-commit-ack-lost-174",
        deadline_at=datetime.now(UTC) + timedelta(seconds=120),
    )

    upstream_options = psycopg.conninfo.conninfo_to_dict(isolated_database)
    upstream_directory = upstream_options["host"]
    upstream_port = int(upstream_options["port"])
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(8)
    port = listener.getsockname()[1]
    commit_seen = threading.Event()
    ack_dropped = threading.Event()
    proxy_done = threading.Event()
    errors = []

    def proxy():
        try:
            client, _ = listener.accept()
            with client, socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.connect(f"{upstream_directory}/.s.PGSQL.{upstream_port}")
                client.settimeout(8)
                server.settimeout(8)
                while True:
                    ready, _, _ = select.select([client, server], [], [], 8)
                    if not ready:
                        raise TimeoutError("proxy stalled")
                    for src in ready:
                        data = src.recv(65536)
                        if not data:
                            return
                        if src is client:
                            if b"COMMIT\x00" in data.upper():
                                commit_seen.set()
                            server.sendall(data)
                        elif commit_seen.is_set():
                            # The backend sent the COMMIT completion packet.
                            # This packet never reaches the client.
                            ack_dropped.set()
                            return
                        else:
                            client.sendall(data)
        except BaseException as exc:
            errors.append(repr(exc))
        finally:
            proxy_done.set()
            listener.close()

    thread = threading.Thread(target=proxy, daemon=True)
    thread.start()
    intercepted_dsn = psycopg.conninfo.make_conninfo(
        isolated_database, host="127.0.0.1", port=port,
        sslmode="disable", connect_timeout=5,
    )
    channel.repository = PostgresControlRepository(intercepted_dsn, lease_seconds=60)
    try:
        with pytest.raises(StorageUnavailable):
            channel.submit(submission, artifact_uri=staged["uri"])
    finally:
        channel.repository = direct
        thread.join(timeout=10)
        listener.close()
    assert not thread.is_alive()
    assert not errors, errors
    assert commit_seen.is_set(), "client did not transmit COMMIT over proxy"
    assert ack_dropped.is_set(), "backend COMMIT ACK was not dropped"

    # Independent direct, durable database read; no proxy or second submission.
    with psycopg.connect(isolated_database) as connection:
        stored = connection.execute(
            "SELECT id, payload FROM jobs WHERE idempotency_key = %s",
            (submission.idempotency_key,),
        ).fetchall()
    assert len(stored) == 1
    job_id = str(stored[0][0])
    assert stored[0][1]["audio_input"] == staged
    ident = staged["uri"].removeprefix("tsumgi-audio-input:")
    assert channel._inputs[ident].job_id is None
    assert channel._inputs[ident].submission is not None
    claim = direct.claim_next_job("other", runtime_instance_epoch=EPOCH)
    assert claim is not None and claim.job_id == job_id
    assert channel.read_for_worker(
        job_id, worker_id="other", attempt=claim.attempts,
        lease_token=claim.lease_token, runtime_instance_epoch=EPOCH,
    ) == WAV
