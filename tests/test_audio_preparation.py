"""Memory-only rehearsal of audio preparation; never PostgreSQL acceptance."""
from __future__ import annotations

import pytest

from tsumgi.control import InMemoryControlRepository
from audio_epoch_acceptance import make_harness, snapshot_repository
from audio_preparation_cases import AudioPreparationCases
from worker_identity_cases import WORKERS


@pytest.fixture
def preparation_harness(tmp_path, monkeypatch):
    repository = InMemoryControlRepository(worker_ttl_seconds=300, lease_seconds=60)

    def observer():
        return snapshot_repository(repository, [job.job_id for job in repository.list_jobs()])

    with make_harness(
        tmp_path, monkeypatch, database_url="postgresql://unused.invalid/audio-preparation-memory",
        observer=observer, memory_repository=repository, preparation_enabled=True,
        preparation_ttl_seconds=60, worker_credentials=WORKERS,
        worker_token=WORKERS[next(iter(WORKERS))],
    ) as harness:
        harness.qualification_repository = repository
        yield harness


class TestMemoryAudioPreparation(AudioPreparationCases):
    """The in-memory implementation is rehearsal only, not durable evidence."""


def test_default_off_audio_preparation_routes_are_absent(tmp_path, monkeypatch):
    repository = InMemoryControlRepository(worker_ttl_seconds=300, lease_seconds=60)

    def observer():
        return snapshot_repository(repository, [])

    with make_harness(
        tmp_path, monkeypatch, database_url="postgresql://unused.invalid/audio-preparation-off",
        observer=observer, memory_repository=repository, preparation_enabled=False,
        worker_credentials=WORKERS, worker_token=WORKERS[next(iter(WORKERS))],
    ) as harness:
        assert harness.client.post(
            "/v1/workers/epoch-acceptance-synthetic/audio-epochs/"
            "22345678-1234-4234-9234-123456789abc/prepare",
            headers=harness.worker_headers, json={},
        ).status_code == 404
