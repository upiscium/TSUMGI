"""Bounded Wave2E faults against actual private disk and PostgreSQL.

These tests exercise current single-Control MVI behavior, not crash durability,
multi-Control failover or a real network COMMIT-ACK disconnection.
"""

from datetime import UTC, datetime, timedelta
import errno

import httpx
import pytest

from astrumweaver.audio import ArtifactSpool, AudioOutputChannel
from astrumweaver.audio.input_channel import AudioInputChannel
from astrumweaver.control import JobStatus, JobSubmission, PostgresControlRepository
from astrumweaver.control.api import create_app
from astrumweaver.control.repository import NotFoundError
from test_audio_artifact_authority import EPOCH, setup_claim
from test_audio_artifact_authority_postgres import isolated_database


from test_audio_input_channel import WAV as INPUT_WAV

WAV = b"RIFF" + (24).to_bytes(4, "little") + b"WAVE" + b"fmt " + b"fault-test-wave"
CLIENT = {"authorization": "Bearer client-secret"}
WORKER = {"authorization": "Bearer worker-secret"}


def new_input(tmp_path, repository):
    root = tmp_path / "input"
    root.mkdir(mode=0o700)
    spool = ArtifactSpool(
        root, max_bytes=256, max_total_bytes=512,
        max_artifacts=2, ttl_seconds=300,
    )
    channel = AudioInputChannel(repository, spool, max_bytes=256)
    return root, spool, channel


def new_output(tmp_path, repository):
    root = tmp_path / "output"
    root.mkdir(mode=0o700)
    spool = ArtifactSpool(
        root, max_bytes=256, max_total_bytes=512,
        max_artifacts=2, ttl_seconds=300,
    )
    channel = AudioOutputChannel(repository, spool, max_bytes=256)
    return root, spool, channel


@pytest.mark.asyncio
async def test_truncated_http_input_is_not_staged_or_admitted(
    tmp_path, isolated_database,
):
    repo = PostgresControlRepository(isolated_database)
    _, spool, channel = new_input(tmp_path, repo)
    app = create_app(
        repo, client_token="client-secret", worker_token="worker-secret",
        audio_input_channel=channel,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://control",
    ) as client:
        response = await client.post(
            "/v1/audio/inputs", content=INPUT_WAV,
            headers={**CLIENT, "content-type": "audio/wav",
                     "content-length": str(len(INPUT_WAV) + 7)},
        )
        assert response.status_code == 400, response.text
        assert "incomplete audio output" in response.text

    assert not channel._inputs
    assert not spool._records
    assert spool._total_bytes == 0
    assert not tuple((tmp_path / "input" / spool._name).iterdir())


@pytest.mark.asyncio
async def test_disk_full_during_output_staging_does_not_publish_or_consume_quota(
    tmp_path, isolated_database, monkeypatch,
):
    import astrumweaver.audio.artifact_spool as spool_module

    repo = PostgresControlRepository(isolated_database, lease_seconds=60)
    claim = setup_claim(repo, datetime.now(UTC))
    _, spool, channel = new_output(tmp_path, repo)
    app = create_app(
        repo, client_token="client-secret", worker_token="worker-secret",
        audio_output_channel=channel,
    )
    endpoint = f"/v1/workers/owner/jobs/{claim.job_id}/audio/output"
    headers = {
        **WORKER, "content-type": "audio/wav",
        "x-audio-attempt": str(claim.attempts),
        "x-audio-lease-token": claim.lease_token,
        "x-audio-runtime-epoch": EPOCH,
    }

    real_write = spool_module.os.write
    calls = 0

    def partial_then_disk_full(fd, data):
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_write(fd, data[:5])
        raise OSError(errno.ENOSPC, "isolated simulated disk full")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://control",
    ) as client:
        with monkeypatch.context() as patch:
            patch.setattr(spool_module.os, "write", partial_then_disk_full)
            response = await client.post(endpoint, content=WAV, headers=headers)
        assert response.status_code == 500
        assert claim.lease_token not in response.text
        assert repo.get_job(claim.job_id).status is JobStatus.RUNNING
        assert not channel._staged
        assert not spool._records
        assert spool._total_bytes == 0
        assert not tuple((tmp_path / "output" / spool._name).iterdir())

        # The same currently fenced claim can retry and obtain a valid file.
        recovered = await client.post(endpoint, content=WAV, headers=headers)
        assert recovered.status_code == 201, recovered.text
        assert recovered.json()["size_bytes"] == len(WAV)
        assert len(spool._records) == 1


def test_single_control_cold_restart_reclaims_orphan_but_does_not_restore_input(
    tmp_path, isolated_database,
):
    repo = PostgresControlRepository(isolated_database, lease_seconds=60)
    existing = setup_claim(repo, datetime.now(UTC), capability="audio.transcribe")
    root, old_spool, old_channel = new_input(tmp_path, repo)
    ref = old_channel.stage(INPUT_WAV)
    job = old_channel.submit(
        JobSubmission(
            capability="audio.transcribe", serving=existing.serving,
            deadline_at=datetime.now(UTC) + timedelta(seconds=120),
            idempotency_key="input-before-simulated-cold-restart",
        ),
        artifact_uri=ref["uri"],
    )
    claim = repo.claim_next_job("other", runtime_instance_epoch=EPOCH)
    assert claim is not None and claim.job_id == job.job_id
    assert old_channel.read_for_worker(
        job.job_id, worker_id="other", attempt=claim.attempts,
        lease_token=claim.lease_token, runtime_instance_epoch=EPOCH,
    ) == INPUT_WAV
    old_directory = root / old_spool._name
    assert old_directory.is_dir()

    # Simulated cold startup: previous Control/Spool instances stop using the
    # root. No concurrent instance is permitted by this recovery contract.
    del old_channel, old_spool
    resumed_spool = ArtifactSpool(
        root, max_bytes=256, max_total_bytes=512, max_artifacts=2,
        ttl_seconds=300, recover_on_startup=True,
    )
    resumed_channel = AudioInputChannel(repo, resumed_spool, max_bytes=256)
    assert not old_directory.exists()
    assert not resumed_spool._records
    assert repo.get_job(job.job_id).payload["audio_input"] == ref
    assert repo.get_job(job.job_id).status is JobStatus.RUNNING

    # Durable Job metadata alone never recreates a readable private file.
    with pytest.raises(NotFoundError, match="unavailable"):
        resumed_channel.read_for_worker(
            job.job_id, worker_id="other", attempt=claim.attempts,
            lease_token=claim.lease_token, runtime_instance_epoch=EPOCH,
        )


def test_hard_exited_spool_process_leaves_recoverable_private_orphan(tmp_path):
    """A separate process dies without spool cleanup; cold recovery is explicit."""
    import os
    from pathlib import Path
    import subprocess
    import sys

    root = tmp_path / "crash-spool"
    root.mkdir(mode=0o700)
    source_root = Path(__file__).resolve().parents[1] / "src"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(source_root) + os.pathsep + environment.get("PYTHONPATH", "")
    script = (
        "import os,sys\n"
        "from astrumweaver.audio.artifact_spool import ArtifactSpool\n"
        "spool=ArtifactSpool(sys.argv[1],max_bytes=256,ttl_seconds=300,"
        "max_total_bytes=512,max_artifacts=2)\n"
        "spool.stage(b'RIFF' + (16).to_bytes(4,'little') + b'WAVEabcd',scope='crash')\n"
        "os._exit(37)\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script, str(root)], cwd=source_root.parent,
        env=environment, capture_output=True, text=True, timeout=15,
    )
    assert completed.returncode == 37, completed.stderr
    (old_directory,) = tuple(root.glob("artifact-spool-*"))
    (orphan,) = tuple(old_directory.iterdir())
    assert orphan.is_file()
    assert orphan.read_bytes().startswith(b"RIFF")

    recovered = ArtifactSpool(
        root, max_bytes=256, ttl_seconds=300,
        max_total_bytes=512, max_artifacts=2, recover_on_startup=True,
    )
    assert not old_directory.exists()
    assert recovered._records == {}
    assert recovered._total_bytes == 0
    assert recovered.stage(WAV, scope="new-process")["size"] == len(WAV)
