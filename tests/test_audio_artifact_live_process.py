"""Explicit opt-in real localhost socket/Control-process fault acceptance (#174).

Test subprocesses listen on loopback only; private Spool and (when needed)
an isolated PostgreSQL database are required. No installed daemon is touched.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
import os
import socket
import subprocess
import sys
import time

import httpx
import pytest

from astrumweaver.control import JobStatus, PostgresControlRepository
from test_audio_artifact_authority import EPOCH, setup_claim
from test_audio_artifact_authority_postgres import isolated_database
from test_audio_input_channel import WAV

_CLIENT = {"authorization": "Bearer network-test-client"}
_WORKER = {"authorization": "Bearer network-test-worker"}
_ENABLE = os.environ.get("ASTRUMWEAVER_TEST_REAL_SOCKET") == "1"
pytestmark = pytest.mark.skipif(
    not _ENABLE, reason="NOT_VERIFIED: set ASTRUMWEAVER_TEST_REAL_SOCKET=1",
)


def _serve() -> None:
    """Separate uvicorn Control application process, never a production daemon."""
    import uvicorn
    from astrumweaver.audio import ArtifactSpool
    from astrumweaver.audio.input_channel import AudioInputChannel
    from astrumweaver.control import InMemoryControlRepository
    from astrumweaver.control.api import create_app

    port, private_root, dsn, recover = sys.argv[1:5]
    repo = (
        InMemoryControlRepository()
        if dsn == "-"
        else PostgresControlRepository(dsn, lease_seconds=180)
    )
    spool = ArtifactSpool(
        Path(private_root), max_bytes=256, ttl_seconds=300,
        max_total_bytes=512, max_artifacts=2,
        recover_on_startup=recover == "yes",
    )
    channel = AudioInputChannel(repo, spool, max_bytes=256)
    app = create_app(
        repo, client_token="network-test-client",
        worker_token="network-test-worker", audio_input_channel=channel,
        maintenance_interval_seconds=60,
    )
    uvicorn.run(
        app, host="127.0.0.1", port=int(port),
        log_level="warning", access_log=False,
    )


def _port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


@contextmanager
def _running_control(tmp_path, root: Path, *, dsn: str = "-", recover=False):
    pytest.importorskip("uvicorn", reason="NOT_VERIFIED: uvicorn unavailable")
    port = _port()
    log = tmp_path / ("uvicorn-" + str(port) + ".log")
    source = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(source / "src"), str(source / "tests"), env.get("PYTHONPATH", "")]
    )
    with log.open("w") as output:
        process = subprocess.Popen(
            [sys.executable, "-u", "-c",
             "from test_audio_artifact_live_process import _serve; _serve()",
             str(port), str(root), dsn, "yes" if recover else "no"],
            cwd=source, env=env, stdin=subprocess.DEVNULL,
            stdout=output, stderr=subprocess.STDOUT,
        )
        origin = f"http://127.0.0.1:{port}"
        try:
            deadline = time.monotonic() + 8
            while True:
                if process.poll() is not None:
                    raise AssertionError(
                        f"Control failed to start: {log.read_text()[-2000:]}"
                    )
                try:
                    if httpx.get(origin + "/v1/health", timeout=0.4).status_code == 200:
                        break
                except (httpx.HTTPError, OSError):
                    pass
                if time.monotonic() > deadline:
                    raise AssertionError(
                        f"Control did not listen: {log.read_text()[-2000:]}"
                    )
                time.sleep(0.04)
            yield origin, port, process
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)


def _stored_files(root: Path) -> list[Path]:
    return [
        f for directory in root.glob("artifact-spool-*")
        for f in directory.iterdir() if f.is_file()
    ]


def test_real_tcp_half_closed_upload_cannot_stage_partial_wav(tmp_path):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    with _running_control(tmp_path, root) as (origin, port, _):
        with socket.create_connection(("127.0.0.1", port), timeout=3) as sock:
            sock.settimeout(2)
            request = (
                "POST /v1/audio/inputs HTTP/1.1\r\n"
                "Host: 127.0.0.1\r\n"
                "Authorization: Bearer network-test-client\r\n"
                "Content-Type: audio/wav\r\n"
                f"Content-Length: {len(WAV)}\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii")
            sock.sendall(request + WAV[:10])
            # Genuine TCP upload interruption: sender closes writing while
            # the server expects the remainder of Content-Length.
            sock.shutdown(socket.SHUT_WR)
            try:
                sock.recv(1024)
            except (TimeoutError, ConnectionError, OSError):
                pass

        # Health confirms the process still serves requests after disconnect.
        assert httpx.get(origin + "/v1/health", timeout=2).status_code == 200
        assert _stored_files(root) == []

        uploaded = httpx.post(
            origin + "/v1/audio/inputs", content=WAV,
            headers={**_CLIENT, "content-type": "audio/wav"}, timeout=3,
        )
        assert uploaded.status_code == 201, uploaded.text
        files = _stored_files(root)
        assert len(files) == 1
        assert files[0].read_bytes() == WAV


def test_hard_killed_control_restart_refuses_orphaned_live_job_input(
    tmp_path, isolated_database,
):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    repo = PostgresControlRepository(isolated_database, lease_seconds=180)
    seed = setup_claim(repo, datetime.now(UTC), capability="audio.transcribe")

    with _running_control(
        tmp_path, root, dsn=isolated_database,
    ) as (origin, _, process):
        upload = httpx.post(
            origin + "/v1/audio/inputs", content=WAV,
            headers={**_CLIENT, "content-type": "audio/wav"}, timeout=3,
        )
        assert upload.status_code == 201, upload.text
        reference = upload.json()
        from astrumweaver.transport import PROTOCOL_VERSION, SERVING_EXTENSION

        admitted = httpx.post(
            origin + "/v1/audio/inputs/jobs",
            headers=_CLIENT,
            json={
                "protocol_version": PROTOCOL_VERSION,
                "extensions": [SERVING_EXTENSION],
                "capability": "audio.transcribe",
                "serving": seed.serving.to_dict(),
                "deadline_at": (datetime.now(UTC) + timedelta(seconds=120)).isoformat(),
                "idempotency_key": "tcp-process-killed-job",
                "audio_input_uri": reference["uri"],
            },
            timeout=3,
        )
        assert admitted.status_code == 201, admitted.text
        job_id = admitted.json()["job_id"]
        claim = repo.claim_next_job("other", runtime_instance_epoch=EPOCH)
        assert claim is not None and claim.job_id == job_id

        headers = {
            **_WORKER,
            "x-audio-attempt": str(claim.attempts),
            "x-audio-lease-token": claim.lease_token,
            "x-audio-runtime-epoch": EPOCH,
        }
        endpoint = f"/v1/workers/other/jobs/{job_id}/audio/input"
        readable = httpx.get(origin + endpoint, headers=headers, timeout=3)
        assert readable.status_code == 200, readable.text
        assert readable.content == WAV
        old_dirs = tuple(root.glob("artifact-spool-*"))
        assert len(old_dirs) == 1
        assert len(_stored_files(root)) == 1

        # Full HTTP-serving application process dies while Job is RUNNING.
        process.kill()
        assert process.wait(timeout=5) != 0
        assert repo.get_job(job_id).status is JobStatus.RUNNING

    # Explicit single-owner restart: no other active Control shares this root.
    with _running_control(
        tmp_path, root, dsn=isolated_database, recover=True,
    ) as (origin, _, _):
        assert not old_dirs[0].exists()
        assert _stored_files(root) == []
        current = repo.get_job(job_id)
        assert current.status is JobStatus.RUNNING
        assert current.payload["audio_input"] == reference
        denied = httpx.get(origin + endpoint, headers=headers, timeout=3)
        assert denied.status_code == 404, denied.text
        assert "audio input unavailable" in denied.text
        assert repo.get_job(job_id).status is JobStatus.RUNNING
