"""Real PostgreSQL run of the #318 lease/retention cases (skips are NOT_VERIFIED)."""

from __future__ import annotations

import pytest

from tsumgi.control import PostgresControlRepository
from test_audio_artifact_authority_postgres import isolated_database  # noqa: F401
from test_audio_lease_retention import LeaseRetentionCases, clock  # noqa: F401


@pytest.fixture
def make_repo(isolated_database):  # noqa: F811
    repos = []

    def build(**kwargs):
        repo = PostgresControlRepository(isolated_database, **kwargs)
        repos.append(repo)
        return repo

    yield build
    for repo in repos:
        repo.close()


class TestPostgres(LeaseRetentionCases):
    pass
