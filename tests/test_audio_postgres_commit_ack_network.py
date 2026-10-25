"""Real PostgreSQL-wire COMMIT acknowledgement loss; explicit opt-in (#174).

An isolated loopback proxy forwards all traffic except a backend
CommandComplete(COMMIT) frame, which it observes and drops after PostgreSQL
has finished committing. Only the test's disposable database is targeted.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import select
import socket
import threading
import time

import pytest

from astrumweaver.audio import ArtifactSpool
from astrumweaver.audio.input_channel import AudioInputChannel
from astrumweaver.control import JobSubmission, PostgresControlRepository
from astrumweaver.control.repository import StorageUnavailable
from test_audio_artifact_authority import EPOCH, setup_claim
from test_audio_artifact_authority_postgres import isolated_database
from test_audio_input_channel import WAV

pytestmark = pytest.mark.skipif(
    os.environ.get("ASTRUMWEAVER_TEST_REAL_PG_ACK") != "1",
    reason="NOT_VERIFIED: set ASTRUMWEAVER_TEST_REAL_PG_ACK=1",
)


class _DropCommitAcknowledgement:
    """Single-use PostgreSQL protocol relay restricted to loopback."""

    def __init__(self, source_dsn: str):
        import psycopg

        original = psycopg.conninfo.conninfo_to_dict(source_dsn)
        assert original["host"].startswith("/"), "test DB must use a Unix socket"
        self.target = str(
            Path(original["host"]) / (".s.PGSQL." + original["port"])
        )
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.listener.settimeout(10)
        self.port = self.listener.getsockname()[1]
        self.dsn = psycopg.conninfo.make_conninfo(
            source_dsn, host="127.0.0.1", port=self.port,
            sslmode="disable", connect_timeout=4,
        )
        self.ack_dropped = threading.Event()
        self.error = None
        self.thread = threading.Thread(target=self._relay, daemon=True)

    def _relay(self):
        try:
            with self.listener.accept()[0] as client:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as backend:
                    backend.connect(self.target)
                    buffered = bytearray()
                    deadline = time.monotonic() + 12
                    while time.monotonic() < deadline:
                        readable, _, _ = select.select(
                            [client, backend], [], [], 1,
                        )
                        if client in readable:
                            inbound = client.recv(65536)
                            if not inbound:
                                break
                            backend.sendall(inbound)

                        if backend in readable:
                            inbound = backend.recv(65536)
                            if not inbound:
                                break
                            buffered.extend(inbound)
                            # Server messages use type byte + network-order
                            # length (includes 4-byte length, not type).
                            while len(buffered) >= 5:
                                length = int.from_bytes(buffered[1:5], "big")
                                if length < 4 or length > 16_777_216:
                                    raise AssertionError("invalid backend frame")
                                if len(buffered) < length + 1:
                                    break
                                frame = bytes(buffered[:length + 1])
                                del buffered[:length + 1]
                                if (frame[:1] == b"C"
                                        and frame[5:] == b"COMMIT\x00"):
                                    # Do not forward the successful commit
                                    # acknowledgement to the libpq client.
                                    self.ack_dropped.set()
                                    return
                                client.sendall(frame)
        except Exception as exc:
            self.error = exc
        finally:
            self.listener.close()

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.listener.close()
        self.thread.join(timeout=5)
        assert not self.thread.is_alive(), "relay did not stop"
        assert self.error is None, f"relay: {self.error!r}"


class _FailoverRepository:
    """One uncertain submit, then reconnection to the real primary."""

    def __init__(self, direct):
        self.direct = direct
        self.first = None
        self.first_used = False

    def __getattr__(self, name):
        return getattr(self.direct, name)

    def submit_job(self, submission):
        if not self.first_used:
            self.first_used = True
            assert self.first is not None
            return self.first.submit_job(submission)
        return self.direct.submit_job(submission)


def test_lost_real_pg_commit_ack_preserves_input_and_reconciles_without_duplication(
    tmp_path, isolated_database,
):
    import psycopg

    direct = PostgresControlRepository(isolated_database, lease_seconds=180)
    seed = setup_claim(direct, datetime.now(UTC), capability="audio.transcribe")

    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    spool = ArtifactSpool(
        root, max_bytes=256, max_total_bytes=512,
        max_artifacts=2, ttl_seconds=300,
    )
    repo = _FailoverRepository(direct)
    channel = AudioInputChannel(repo, spool, max_bytes=256)
    ref = channel.stage(WAV)
    admission = JobSubmission(
        capability="audio.transcribe",
        serving=seed.serving,
        deadline_at=datetime.now(UTC) + timedelta(seconds=120),
        idempotency_key="tcp-proxy-commit-ack-drop",
    )

    with _DropCommitAcknowledgement(isolated_database) as proxy:
        repo.first = PostgresControlRepository(proxy.dsn, lease_seconds=180)
        with pytest.raises(StorageUnavailable, match="unconfirmed"):
            channel.submit(admission, artifact_uri=ref["uri"])
        assert proxy.ack_dropped.wait(timeout=4)

    artifact_id = ref["uri"].removeprefix("tsumgi-audio-input:")
    assert channel._inputs[artifact_id].job_id is None
    assert spool.read(artifact_id, scope=channel._inputs[artifact_id].scope) == WAV

    # Independent direct connection confirms that COMMIT *did* persist the
    # Job, despite the libpq client seeing transport failure instead of ACK.
    with psycopg.connect(isolated_database) as connection:
        found = connection.execute(
            "SELECT id, payload FROM jobs WHERE idempotency_key = %s",
            (admission.idempotency_key,),
        ).fetchall()
    assert len(found) == 1
    assert found[0][1]["audio_input"] == ref
    durable_job_id = str(found[0][0])

    # The currently authorized Worker can reconcile without a client retry.
    claim = direct.claim_next_job("other", runtime_instance_epoch=EPOCH)
    assert claim is not None and claim.job_id == durable_job_id
    assert channel.read_for_worker(
        durable_job_id, worker_id="other", attempt=claim.attempts,
        lease_token=claim.lease_token, runtime_instance_epoch=EPOCH,
    ) == WAV
    assert channel._inputs[artifact_id].job_id == durable_job_id
    assert channel.submit(admission, artifact_uri=ref["uri"]).job_id == durable_job_id

    with psycopg.connect(isolated_database) as connection:
        count = connection.execute(
            "SELECT count(*) FROM jobs WHERE idempotency_key = %s",
            (admission.idempotency_key,),
        ).fetchone()[0]
    assert count == 1
