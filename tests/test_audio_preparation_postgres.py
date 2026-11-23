"""Real PostgreSQL 17 audio-preparation acceptance; skips are NOT_VERIFIED."""
from __future__ import annotations

import importlib.util
import os

import pytest

from tsumgi.control import PostgresControlRepository
from audio_epoch_acceptance import make_harness, snapshot_repository
from audio_preparation_cases import AudioPreparationCases
from test_audio_artifact_authority_postgres import isolated_database
from worker_identity_cases import WORKERS


def _strict_mode():
    value = os.environ.get("TSUMGI_REQUIRE_AUDIO_PREPARATION_POSTGRES", "0")
    if value not in {"0", "1"}:
        pytest.fail("TSUMGI_REQUIRE_AUDIO_PREPARATION_POSTGRES must be 0 or 1")
    return value == "1"


@pytest.fixture
def postgres17_database(request):
    strict = _strict_mode()
    if importlib.util.find_spec("psycopg") is None:
        if strict:
            pytest.fail("Required PostgreSQL acceptance prerequisite missing: psycopg")
        pytest.skip("NOT_VERIFIED: psycopg driver unavailable")
    if not os.environ.get("TSUMGI_TEST_DATABASE_URL"):
        if strict:
            pytest.fail("Required PostgreSQL acceptance prerequisite missing: test database URL")
        pytest.skip("NOT_VERIFIED: TSUMGI_TEST_DATABASE_URL absent")
    try:
        dsn = request.getfixturevalue("isolated_database")
    except pytest.skip.Exception:
        if strict:
            pytest.fail("Required isolated PostgreSQL database could not be provisioned")
        pytest.skip("NOT_VERIFIED: isolated PostgreSQL database unavailable")
    import psycopg

    with psycopg.connect(dsn) as connection:
        version, encoding = connection.execute(
            "SELECT current_setting('server_version_num')::integer, "
            "current_setting('server_encoding')"
        ).fetchone()
    if not 170000 <= version < 180000:
        if strict:
            pytest.fail("Required PostgreSQL acceptance prerequisite missing: PostgreSQL 17")
        pytest.skip("NOT_VERIFIED: database server is not PostgreSQL 17")
    if encoding.upper() != "UTF8":
        pytest.fail("Isolated PostgreSQL database is not UTF8")
    return dsn


@pytest.fixture
def preparation_harness(postgres17_database, tmp_path, monkeypatch):
    dsn = postgres17_database
    qualification_repository = PostgresControlRepository(
        dsn, pool_min_size=0, pool_max_size=2,
    )

    def observer():
        import psycopg

        with psycopg.connect(dsn) as connection:
            ids = connection.execute("SELECT id::text FROM jobs ORDER BY id LIMIT 129").fetchall()
            raw = {
                "workers": connection.execute("SELECT * FROM workers ORDER BY id LIMIT 129").fetchall(),
                "jobs": connection.execute("SELECT * FROM jobs ORDER BY id LIMIT 129").fetchall(),
                "events": connection.execute(
                    "SELECT * FROM job_events ORDER BY job_id, sequence LIMIT 129",
                ).fetchall(),
                "fences": connection.execute(
                    "SELECT * FROM audio_epoch_fences ORDER BY worker_id, runtime_instance_epoch LIMIT 129",
                ).fetchall(),
                "audio_preparation_qualifications": connection.execute(
                    "SELECT * FROM audio_preparation_qualifications "
                    "ORDER BY worker_id, qualification_revision LIMIT 129",
                ).fetchall(),
                "audio_preparation_qualification_revocations": connection.execute(
                    "SELECT * FROM audio_preparation_qualification_revocations "
                    "ORDER BY qualification_revision LIMIT 129",
                ).fetchall(),
                "audio_epoch_preparations": connection.execute(
                    "SELECT * FROM audio_epoch_preparations "
                    "ORDER BY worker_id, runtime_instance_epoch LIMIT 129",
                ).fetchall(),
            }
        assert len(ids) <= 128
        assert all(len(rows) <= 128 for rows in raw.values())
        repo = PostgresControlRepository(dsn, pool_min_size=0, pool_max_size=1)
        try:
            result = snapshot_repository(repo, [row[0] for row in ids])
            result["database_rows"] = raw
            return result
        finally:
            repo.close()

    try:
        with make_harness(
            tmp_path, monkeypatch, database_url=dsn, observer=observer,
            preparation_enabled=True, preparation_ttl_seconds=60,
            worker_credentials=WORKERS, worker_token=WORKERS[next(iter(WORKERS))],
        ) as harness:
            harness.qualification_repository = qualification_repository
            yield harness
    finally:
        qualification_repository.close()


class TestPostgresAudioPreparation(AudioPreparationCases):
    """HTTP and effects oracle through the production daemon and PostgreSQL."""
