"""Real PostgreSQL 17 acceptance for audio runtime-epoch scenarios.

Skipped PostgreSQL cases are NOT_VERIFIED. An acceptance run must set
ASTRUMWEAVER_REQUIRE_AUDIO_EPOCH_POSTGRES=1. These cases have no xfail
markers; --runxfail is still appropriate when collecting acceptance evidence.
"""
from __future__ import annotations

import importlib.util
import os

import pytest

from astrumweaver.control import PostgresControlRepository
from audio_epoch_acceptance import (
    EpochAcceptanceCases,
    make_harness,
    snapshot_repository,
)
from test_audio_artifact_authority_postgres import isolated_database


def _strict_mode() -> bool:
    value = os.environ.get("ASTRUMWEAVER_REQUIRE_AUDIO_EPOCH_POSTGRES", "0")
    if value not in {"0", "1"}:
        pytest.fail("ASTRUMWEAVER_REQUIRE_AUDIO_EPOCH_POSTGRES must be 0 or 1")
    return value == "1"


@pytest.fixture
def postgres17_database(request):
    strict = _strict_mode()
    if importlib.util.find_spec("psycopg") is None:
        if strict:
            pytest.fail("Required PostgreSQL acceptance prerequisite missing: psycopg")
        pytest.skip("NOT_VERIFIED: psycopg driver unavailable")
    if not os.environ.get("ASTRUMWEAVER_TEST_DATABASE_URL"):
        if strict:
            pytest.fail("Required PostgreSQL acceptance prerequisite missing: test database URL")
        pytest.skip("NOT_VERIFIED: ASTRUMWEAVER_TEST_DATABASE_URL absent")

    try:
        dsn = request.getfixturevalue("isolated_database")
    except pytest.skip.Exception:
        if strict:
            pytest.fail("Required isolated PostgreSQL database could not be provisioned")
        pytest.skip("NOT_VERIFIED: isolated PostgreSQL database unavailable")

    import psycopg

    # The isolated_database fixture creates and migrates a fresh database.
    # Validate its actual server; never substitute a memory repository.
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
def epoch_harness(postgres17_database, tmp_path, monkeypatch):
    dsn = postgres17_database

    def observer():
        import psycopg

        # Separate real connections, no cached Job objects or SQL mutations.
        # The database was created uniquely for this test, never a shared DB.
        with psycopg.connect(dsn) as connection:
            rows = connection.execute("SELECT id::text FROM jobs ORDER BY id LIMIT 129").fetchall()
            raw = {
                "workers": connection.execute("SELECT * FROM workers ORDER BY id LIMIT 129").fetchall(),
                "jobs": connection.execute("SELECT * FROM jobs ORDER BY id LIMIT 129").fetchall(),
                "events": connection.execute(
                    "SELECT * FROM job_events ORDER BY job_id, sequence LIMIT 129",
                ).fetchall(),
                "fences": connection.execute(
                    "SELECT * FROM audio_epoch_fences ORDER BY worker_id, runtime_instance_epoch LIMIT 129",
                ).fetchall(),
            }
        assert len(rows) <= 128
        assert all(len(values) <= 128 for values in raw.values())
        repo = PostgresControlRepository(dsn, pool_min_size=0, pool_max_size=1)
        try:
            snapshot = snapshot_repository(repo, [row[0] for row in rows])
            # Include SQL-only columns, not just the public transport projection.
            snapshot["database_rows"] = raw
            return snapshot
        finally:
            repo.close()

    with make_harness(
        tmp_path,
        monkeypatch,
        database_url=dsn,
        observer=observer,
    ) as harness:
        yield harness


class TestPostgresAudioEpochAcceptance(EpochAcceptanceCases):
    """Run shared HTTP acceptance scenarios against isolated PostgreSQL 17.

    Passing cases cover only the standard daemon/control-repository assembly,
    not a TCP daemon, native engine, or the separate qualification/admission protocol.
    """
