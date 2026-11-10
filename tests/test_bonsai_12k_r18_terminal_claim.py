"""#232 R18: terminal Control Job lease-owner clearing is intentional.

A completed InMemoryControlRepository Job releases assigned_worker_id.
These source-only tests require the immutable claimed revisions/epoch to
match the one eligible lab Worker; no production Control/GPU I/O.
"""
from __future__ import annotations

import sys
from copy import deepcopy
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from bonsai_12k_r17_operator import (
    LAB_WORKER,
    Blocked,
    validate_terminal_lab_claim,
)

DEPLOYMENT = "sha256:" + "a" * 64
CONTRACT = "sha256:" + "b" * 64
EPOCH = "56ef5cd8-965f-44b8-a8b0-16a1d7066cb0"


def worker():
    return {
        "worker_id": LAB_WORKER,
        "state": "online",
        "active_jobs": 0,
        "has_serving": True,
        "deployment_revision": DEPLOYMENT,
        "runtime_instance_epoch": EPOCH,
    }


def job():
    return {
        "job_id": "synthetic-inmemory-job",
        "status": "succeeded",
        "assigned_worker_id": None,  # released by complete_job()
        "serving_bound": True,
        "claimed_deployment_revision": DEPLOYMENT,
        "claimed_serving_contract_revision": CONTRACT,
        "claimed_runtime_instance_epoch": EPOCH,
    }


def test_completed_job_drops_transient_owner_but_keeps_claim_identity():
    w, j = worker(), job()
    original_w, original_j = deepcopy(w), deepcopy(j)
    assert validate_terminal_lab_claim(
        j, w, expected_contract_revision=CONTRACT, expected_deployment_revision=DEPLOYMENT,
    ) is None
    assert w == original_w and j == original_j


@pytest.mark.parametrize(("field", "bad"), [
    ("status", "running"),
    ("status", "failed"),
    ("status", "cancelled"),
    ("assigned_worker_id", LAB_WORKER),
    ("assigned_worker_id", "bonsai-deneb"),
    ("serving_bound", False),
    ("claimed_deployment_revision", "sha256:" + "c" * 64),
    ("claimed_deployment_revision", None),
    ("claimed_serving_contract_revision", "sha256:" + "d" * 64),
    ("claimed_serving_contract_revision", None),
    ("claimed_runtime_instance_epoch", "11111111-1111-4111-8111-111111111111"),
    ("claimed_runtime_instance_epoch", None),
])
def test_fake_terminal_or_unmatched_claim_rejected(field, bad):
    j = job()
    j[field] = bad
    with pytest.raises(Blocked, match="lab-terminal-claim-or-epoch-not-verified"):
        validate_terminal_lab_claim(j, worker(), expected_contract_revision=CONTRACT, expected_deployment_revision=DEPLOYMENT)


@pytest.mark.parametrize(("field", "bad"), [
    ("worker_id", "bonsai-spica"),
    ("worker_id", "bonsai-deneb"),
    ("state", "offline"),
    ("state", "suspect"),
    ("has_serving", False),
    ("deployment_revision", None),
    ("deployment_revision", ""),
    ("deployment_revision", "sha256:" + "Z" * 64),
    ("runtime_instance_epoch", None),
    ("runtime_instance_epoch", ""),
    ("runtime_instance_epoch", "not-a-uuid"),
    ("runtime_instance_epoch", "00000000-0000-0000-0000-000000000000"),
    ("runtime_instance_epoch", EPOCH.upper()),
])
def test_foreign_or_offline_worker_record_rejected(field, bad):
    w = worker()
    w[field] = bad
    with pytest.raises(Blocked, match="lab-terminal-worker-identity-invalid"):
        validate_terminal_lab_claim(job(), w, expected_contract_revision=CONTRACT, expected_deployment_revision=DEPLOYMENT)


@pytest.mark.parametrize("bad", [
    "", "PENDING", "sha256:a", "sha256:" + "Z" * 64, None, 5, True,
])
def test_invalid_contract_revision_rejected(bad):
    with pytest.raises(Blocked, match="lab-terminal-claim-shape-invalid"):
        validate_terminal_lab_claim(job(), worker(), expected_contract_revision=bad, expected_deployment_revision=DEPLOYMENT)


def test_bad_record_types_rejected():
    with pytest.raises(Blocked, match="lab-terminal-claim-shape-invalid"):
        validate_terminal_lab_claim(None, worker(), expected_contract_revision=CONTRACT, expected_deployment_revision=DEPLOYMENT)
    with pytest.raises(Blocked, match="lab-terminal-claim-shape-invalid"):
        validate_terminal_lab_claim(job(), None, expected_contract_revision=CONTRACT, expected_deployment_revision=DEPLOYMENT)


@pytest.mark.parametrize(("declared_deployment", "error"), [
    ("sha256:" + "c" * 64, "lab-terminal-worker-identity-invalid"),
    ("", "lab-terminal-claim-shape-invalid"),
    ("sha256:INVALID", "lab-terminal-claim-shape-invalid"),
    (None, "lab-terminal-claim-shape-invalid"),
    (True, "lab-terminal-claim-shape-invalid"),
])
def test_worker_and_claim_cannot_substitute_an_unstaged_deployment(
    declared_deployment, error,
):
    with pytest.raises(Blocked, match=error):
        validate_terminal_lab_claim(
            job(), worker(), expected_contract_revision=CONTRACT,
            expected_deployment_revision=declared_deployment,
        )
