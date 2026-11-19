"""Process-loss acceptance using a disposable PostgreSQL DB and private output root."""

from datetime import UTC, datetime
import multiprocessing
import os

from fastapi.testclient import TestClient

from astrumweaver.audio import ArtifactSpool, AudioOutputChannel
from astrumweaver.control import JobStatus, PostgresControlRepository
from astrumweaver.control.api import create_app
from test_audio_artifact_authority import setup_claim
from test_audio_artifact_authority_postgres import isolated_database
from test_audio_output_channel import (
    CLIENT, WAV, WORKER, completion_body, get_url, output_url, worker_headers,
)


def _app(repository, root, *, recover):
    spool = ArtifactSpool(root, max_bytes=256, ttl_seconds=300, max_artifacts=2,
                          max_total_bytes=512, recover_on_startup=recover)
    return create_app(repository, client_token="client-secret", worker_token="worker-secret",
                      audio_output_channel=AudioOutputChannel(repository, spool, max_bytes=256))


def _crash_after_staging(dsn, root, result):
    repo = PostgresControlRepository(dsn, lease_seconds=120)
    with TestClient(_app(repo, root, recover=False)) as client:
        claim = setup_claim(repo, datetime.now(UTC))
        response = client.post(output_url(claim), content=WAV, headers=worker_headers(claim))
        assert response.status_code == 201
        result.send((claim.job_id, response.json()))
        result.close()
        os._exit(37)  # skip lifespan teardown and all process-local cleanup


def test_running_job_does_not_publish_lost_output_on_exclusive_restart(tmp_path, isolated_database):
    root = tmp_path / "output"
    root.mkdir(mode=0o700)
    ctx = multiprocessing.get_context("spawn")
    reader, writer = ctx.Pipe(duplex=False)
    child = ctx.Process(target=_crash_after_staging, args=(isolated_database, root, writer))
    child.start()
    writer.close()
    try:
        child.join(20)
        assert child.exitcode == 37
        job_id, receipt = reader.recv()
    finally:
        reader.close()
        if child.is_alive():
            child.kill()
            child.join(5)
        child.close()

    orphan, = root.glob("*/*")
    assert orphan.read_bytes() == WAV
    repo = PostgresControlRepository(isolated_database, lease_seconds=120)
    with TestClient(_app(repo, root, recover=True)) as restarted:
        assert not orphan.parent.exists()
        job = repo.get_job(job_id)
        assert job.status is JobStatus.RUNNING and job.attempts == 1
        assert restarted.get(get_url(job, receipt), headers=CLIENT).status_code == 404
        response = restarted.post(
            f"/v1/workers/owner/jobs/{job_id}/complete",
            headers=WORKER, json=completion_body(job, [receipt]),
        )
        assert response.status_code == 409
        assert repo.get_job(job_id).result is None
        assert repo.get_job(job_id).status is JobStatus.RUNNING
