"""Memory rehearsal and prerequisite tests; these are NOT PostgreSQL evidence."""
import importlib.util
from types import SimpleNamespace

import pytest

from astrumweaver.control import InMemoryControlRepository
from audio_epoch_acceptance import EpochAcceptanceCases, make_harness, snapshot_repository


@pytest.fixture
def epoch_harness(tmp_path, monkeypatch):
    repository = InMemoryControlRepository(worker_ttl_seconds=300, lease_seconds=60)

    def observer():
        return snapshot_repository(repository, [job.job_id for job in repository.list_jobs()])

    with make_harness(
        tmp_path, monkeypatch, database_url="postgresql://unused.invalid/memory-rehearsal",
        observer=observer, memory_repository=repository,
    ) as harness:
        yield harness


class TestMemoryAudioEpochRehearsal(EpochAcceptanceCases):
    """Same effect oracle, explicitly non-durable and non-acceptance."""


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("missing", ["driver", "dsn", "isolated_database"])
def test_postgres_acceptance_prerequisites_cannot_silently_pass(monkeypatch, strict, missing):
    from test_audio_epoch_acceptance_postgres import postgres17_database

    monkeypatch.setenv("ASTRUMWEAVER_REQUIRE_AUDIO_EPOCH_POSTGRES", "1" if strict else "0")
    monkeypatch.setattr(importlib.util, "find_spec", lambda _: None if missing == "driver" else object())
    if missing == "dsn":
        monkeypatch.delenv("ASTRUMWEAVER_TEST_DATABASE_URL", raising=False)
    else:
        monkeypatch.setenv("ASTRUMWEAVER_TEST_DATABASE_URL", "postgresql://must-not-connect.invalid/test")

    def fixture(name):
        assert missing == "isolated_database" and name == "isolated_database"
        pytest.skip("NOT_VERIFIED: CREATEDB unavailable")

    exception = pytest.fail.Exception if strict else pytest.skip.Exception
    with pytest.raises(exception) as caught:
        postgres17_database.__wrapped__(SimpleNamespace(getfixturevalue=fixture))
    assert "must-not-connect" not in str(caught.value)
    if not strict:
        assert "NOT_VERIFIED" in str(caught.value)


def test_invalid_strict_mode_is_not_a_skip(monkeypatch):
    from test_audio_epoch_acceptance_postgres import _strict_mode
    monkeypatch.setenv("ASTRUMWEAVER_REQUIRE_AUDIO_EPOCH_POSTGRES", "typo")
    with pytest.raises(pytest.fail.Exception):
        _strict_mode()
