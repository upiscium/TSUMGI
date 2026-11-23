"""Strict per-worker identity authorization on isolated PostgreSQL 17."""
from __future__ import annotations

import pytest

from astrumweaver.control import PostgresControlRepository
from audio_epoch_acceptance import make_harness, snapshot_repository
from test_audio_artifact_authority_postgres import isolated_database
from test_audio_epoch_acceptance_postgres import postgres17_database
from worker_identity_cases import OWN, WORKERS, WorkerIdentityCases


@pytest.fixture
def identity_harness(postgres17_database, tmp_path, monkeypatch):
    dsn = postgres17_database

    def observer():
        import psycopg

        with psycopg.connect(dsn) as connection:
            ids = connection.execute("SELECT id::text FROM jobs ORDER BY id LIMIT 129").fetchall()
            rows = {
                "workers": connection.execute("SELECT * FROM workers ORDER BY id LIMIT 129").fetchall(),
                "jobs": connection.execute("SELECT * FROM jobs ORDER BY id LIMIT 129").fetchall(),
                "events": connection.execute(
                    "SELECT * FROM job_events ORDER BY job_id, sequence LIMIT 129",
                ).fetchall(),
                "fences": connection.execute(
                    "SELECT * FROM audio_epoch_fences ORDER BY worker_id, runtime_instance_epoch LIMIT 129",
                ).fetchall(),
            }
        assert len(ids) <= 128 and all(len(values) <= 128 for values in rows.values())
        repo = PostgresControlRepository(dsn, pool_min_size=0, pool_max_size=1)
        try:
            snapshot = snapshot_repository(repo, [row[0] for row in ids])
            snapshot["database_rows"] = rows
            return snapshot
        finally:
            repo.close()

    with make_harness(
        tmp_path, monkeypatch, database_url=dsn, observer=observer,
        worker_credentials=WORKERS, worker_token=OWN,
    ) as harness:
        yield harness


class TestPostgresWorkerIdentity(WorkerIdentityCases):
    """Same HTTP/effect cases with a separately observed durable database."""
