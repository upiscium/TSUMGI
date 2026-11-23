"""Memory rehearsal of the durable audio epoch fence contract."""
from dataclasses import replace
from datetime import datetime

import pytest

from astrumweaver.control import InMemoryControlRepository
from audio_epoch_fence_cases import AudioEpochFenceCases, clock
from test_audio_artifact_authority import EPOCH, serving_values, setup_claim


@pytest.fixture
def authority_repo():
    return InMemoryControlRepository(lease_seconds=30)


class TestMemoryAudioEpochFences(AudioEpochFenceCases):
    """Memory is behavior rehearsal, not durability evidence."""


def test_fence_model_rejects_noncanonical_identity_revision_and_naive_time(authority_repo, clock):
    setup_claim(authority_repo, clock.now)
    binding, _ = serving_values("audio.synthesize")
    record = authority_repo.fence_audio_epoch(
        "owner", EPOCH,
        expected_deployment_revision=binding.deployment_revision,
        expected_serving_contract_revision=binding.serving_contract_revision,
        fence_request_id="32345678-1234-4234-9234-123456789abc",
    )

    for changes in (
        {"runtime_instance_epoch": "bad"},
        {"deployment_revision": "sha256:" + "A" * 64},
        {"worker_id": "w" * 257},
        {"fenced_at": datetime(2030, 1, 1)},
    ):
        with pytest.raises(ValueError):
            replace(record, **changes)
