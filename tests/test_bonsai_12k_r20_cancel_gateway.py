"""#232 R20 synthetic-only cancellation claim checks (no live Spica)."""
from __future__ import annotations

import hashlib
import sys
from copy import deepcopy
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from bonsai_12k_r20_cancel_gateway import (
    LAB_WORKER,
    make_cancellation_body,
    verify_cancellation_claim,
)
from bonsai_12k_spica_fixtures import make_request

DEP = "sha256:" + "a" * 64
CONTRACT = "sha256:" + "b" * 64
EPOCH = "d52ba5b4-c2b1-4f52-8661-e726a6e3a068"
JOB_ID = "synthetic-actual-lab-only-job"


def worker():
    return {
        "worker_id": LAB_WORKER,
        "state": "online",
        "active_jobs": 0,
        "has_serving": True,
        "deployment_revision": DEP,
        "runtime_instance_epoch": EPOCH,
    }


def before():
    return {
        "job_id": JOB_ID,
        "status": "running",
        "assigned_worker_id": LAB_WORKER,
        "serving_bound": True,
        "claimed_deployment_revision": DEP,
        "claimed_serving_contract_revision": CONTRACT,
        "claimed_runtime_instance_epoch": EPOCH,
    }


def after():
    return {
        **before(), "status": "cancelled",
        "assigned_worker_id": None,
    }


def inspect(*, job_before=None, job_after=None, lab_worker=None, deployment=DEP,
            contract=CONTRACT):
    return verify_cancellation_claim(
        before_job=before() if job_before is None else job_before,
        after_job=after() if job_after is None else job_after,
        worker=worker() if lab_worker is None else lab_worker,
        staged_deployment=deployment, staged_contract=contract,
    )


def test_running_claim_to_cancelled_transition_remains_lab_only():
    b, a, w = before(), after(), worker()
    originals = deepcopy((b, a, w))
    got = inspect(job_before=b, job_after=a, lab_worker=w)
    assert (b, a, w) == originals
    assert got == {
        "job_id": JOB_ID,
        "status": "cancelled",
        "deployment_revision": DEP,
        "contract_revision": CONTRACT,
        "runtime_instance_epoch": EPOCH,
    }
    assert not any("production" in x for x in got)


@pytest.mark.parametrize(("field", "bad"), [
    ("status", "queued"),
    ("status", "succeeded"),
    ("status", "cancelled"),
    ("status", "failed"),
    ("assigned_worker_id", None),
    ("assigned_worker_id", "bonsai-deneb"),
    ("serving_bound", False),
    ("claimed_deployment_revision", "sha256:" + "c" * 64),
    ("claimed_serving_contract_revision", "sha256:" + "c" * 64),
    ("claimed_runtime_instance_epoch", "22222222-2222-4222-8222-222222222222"),
])
def test_must_see_running_claim_bound_to_lab_before_disconnection(field, bad):
    b = before()
    b[field] = bad
    with pytest.raises(ValueError, match="cancel-was-not-claimed-before-client-disconnect"):
        inspect(job_before=b)


@pytest.mark.parametrize(("field", "bad"), [
    ("job_id", "different-job"),
    ("status", "running"),
    ("status", "succeeded"),
    ("status", "failed"),
    ("assigned_worker_id", LAB_WORKER),
    ("serving_bound", False),
    ("claimed_deployment_revision", "sha256:" + "c" * 64),
    ("claimed_serving_contract_revision", "sha256:" + "c" * 64),
    ("claimed_runtime_instance_epoch", "22222222-2222-4222-8222-222222222222"),
])
def test_after_disconnection_must_be_durable_terminal_cancel_with_same_claim(
    field, bad,
):
    a = after()
    a[field] = bad
    with pytest.raises(ValueError, match="cancel-not-durably-terminal-or-claim-changed"):
        inspect(job_after=a)


@pytest.mark.parametrize(("field", "bad"), [
    ("worker_id", "bonsai-deneb"),
    ("worker_id", "bonsai-spica"),
    ("state", "offline"),
    ("has_serving", False),
    ("active_jobs", 1),
    ("active_jobs", True),
    ("deployment_revision", "sha256:" + "c" * 64),
    ("runtime_instance_epoch", ""),
])
def test_no_foreign_or_still_busy_worker_in_cancel_accepted(field, bad):
    w = worker()
    w[field] = bad
    with pytest.raises(ValueError, match="cancel-worker-not-eligible-or-still-busy"):
        inspect(lab_worker=w)


@pytest.mark.parametrize("bad", [
    "", "sha256:a", "sha256:" + "G" * 64, "a" * 64, None, True,
])
def test_malformed_or_unstaged_deployment_and_contract_fail_closed(bad):
    for field in ("deployment", "contract"):
        kwargs = {field: bad}
        with pytest.raises(ValueError, match="cancel-staged-revision-invalid"):
            inspect(**kwargs)


@pytest.mark.parametrize(("before_id", "after_id"), [
    ("", ""),
    (None, None),
    ("a" * 129, "a" * 129),
])
def test_job_id_must_be_bounded_exact_unchanged_identity(before_id, after_id):
    b, a = before(), after()
    b["job_id"] = before_id
    a["job_id"] = after_id
    with pytest.raises(ValueError, match="cancel-was-not-claimed-before-client-disconnect"):
        inspect(job_before=b, job_after=a)


def test_long_stream_cancel_request_is_pinned_and_fits_http_budget():
    raw = make_cancellation_body(make_request)
    assert b'"stream":true' in raw
    assert b'"include_usage":true' in raw
    assert b'"bonsai2-27b-12k-lab"' in raw
    assert 45000 < len(raw) < 65536
    assert len(hashlib.sha256(raw).hexdigest()) == 64


def test_cancellation_recipe_requires_pinned_long_input():
    bad = lambda *_a, **_k: {
        "model": "bonsai2-27b-12k-lab",
        "max_tokens": 16,
        "messages": [{"role": "user", "content": "short"}],
    }
    with pytest.raises(ValueError, match="cancel-raw-http-body-size-invalid"):
        make_cancellation_body(bad)


def test_cancellation_recipe_cannot_choose_different_model():
    bad = lambda *_a, **_k: {
        "model": "bonsai2-27b",
        "max_tokens": 16,
        "messages": [{"role": "user", "content": "a" * 47000}],
    }
    with pytest.raises(ValueError, match="unqualified-cancel-request-recipe"):
        make_cancellation_body(bad)


def test_cancellation_probe_never_mutates_caller_long_recipe():
    doc = make_request("long", model="bonsai2-27b-12k-lab")
    raw = make_cancellation_body(lambda *_a, **_k: deepcopy(doc))
    assert "stream" not in doc and "stream_options" not in doc
    assert raw


@pytest.mark.parametrize("bad", [
    "not-a-uuid",
    "00000000-0000-0000-0000-000000000000",
    EPOCH.upper(),
    "33333333-3333-3333-3333-333333333333x",
])
def test_malformed_or_noncanonical_worker_epoch_cannot_pass_cancel(bad):
    w = worker()
    w["runtime_instance_epoch"] = bad
    b, a = before(), after()
    b["claimed_runtime_instance_epoch"] = bad
    a["claimed_runtime_instance_epoch"] = bad
    with pytest.raises(ValueError, match="cancel-worker-epoch-invalid"):
        inspect(job_before=b, job_after=a, lab_worker=w)
