"""#232 R16: isolated synthetic receipts; never read real private Spica files."""
from __future__ import annotations

import hashlib
import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from bonsai_12k_r16_dual_canary_receipts import (
    MANAGED_TESTS,
    NATIVE_TESTS,
    SCHEMA,
    STATUS,
    reconcile_r2_r15_receipts,
)


def vram(samples=1604, min_free=3359, gap=105.1):
    return {
        "abort_reason": None,
        "interval_target_s": 0.075,
        "max_sample_gap_ms": gap,
        "max_sampled_free_mib": 11163,
        "min_sampled_free_mib": min_free,
        "monitor_thread_still_alive": False,
        "safety_abort": False,
        "sampled_not_continuous": True,
        "samples": samples,
        "threshold_mib": 2048,
    }


def native():
    return {
        "schema": "tsumgi-spica-bonsai12k-native-canary-r1",
        "host": "spica",
        "worker_stop_attempted": True,
        "worker_originally_stopped": True,
        "old_gpu_released_before_12k": True,
        "canary_started": True,
        "canary_unit": "tsumgi-bonsai12k-spica-native-20261109-r2.service",
        "native_context_target": 12288,
        "performed_tests": list(NATIVE_TESTS),
        "tests_all_pass": True,
        "vram": vram(),
        "original_8k_worker_restored": True,
        "reason": None,
        "deneb_touched": False,
        "control_catalog_changed": False,
        "gateway_12k_qualified": False,
        "live_rollback_tested": True,
        "native_canary_promoted_to_service": False,
        "semantic_quality_qualified": False,
        "continuous_vram_floor_proven": False,
    }


def managed():
    return {
        "schema_version": "tsumgi-r15-spica-managed12k-bounded-canary-v1",
        "status": "MANAGED_NATIVE_SMOKE_ONLY_RESTORED_8K",
        "reported_tests": list(MANAGED_TESTS),
        "vram": vram(samples=1069, gap=96.44),
        "restored_original_8k": True,
        "managed_12k_source_only_no_serving_advertised": True,
        "control_catalog_modified": False,
        "native_request_bodies_independently_attested": False,
        "new_12k_serving_revision_issued": False,
        "permanent_12k_promotion": False,
        "error": None,
    }


def encode(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True).encode()


def args(*, n=None, m=None):
    n = native() if n is None else n
    m = managed() if m is None else m
    nb, mb = encode(n), encode(m)
    return {
        "native_receipt_bytes": nb,
        "native_expected_sha256": "sha256:" + hashlib.sha256(nb).hexdigest(),
        "managed_receipt_bytes": mb,
        "managed_expected_sha256": "sha256:" + hashlib.sha256(mb).hexdigest(),
    }


def reconcile(**kwargs):
    return reconcile_r2_r15_receipts(**(args() | kwargs))


def test_synthetic_r2_r15_consistency_does_not_issue_serving_identity():
    supplied = args()
    before = deepcopy(supplied)
    output = reconcile_r2_r15_receipts(**supplied)
    assert supplied == before
    assert output["schema_version"] == SCHEMA
    assert output["status"] == STATUS
    assert output["native_r2_receipt_sha256"] == supplied["native_expected_sha256"]
    assert output["managed_r15_receipt_sha256"] == supplied["managed_expected_sha256"]
    assert output["native_r2_reported_tests"] == list(NATIVE_TESTS)
    assert output["managed_r15_reported_tests"] == list(MANAGED_TESTS)
    assert output["native_r2_vram_summary_reported"] == {
        "sample_count_reported": 1604,
        "max_sample_gap_ms_reported": 105.1,
        "min_free_mib_reported": 3359,
    }
    assert output["managed_r15_vram_summary_reported"] == {
        "sample_count_reported": 1069,
        "max_sample_gap_ms_reported": 96.44,
        "min_free_mib_reported": 3359,
    }
    assert all(output[field] is False for field in (
        "receipt_origins_independently_authenticated",
        "actual_http_request_bytes_captured_and_attested",
        "managed12k_serving_advertised_and_claim_tested",
        "gateway12k_tool_stream_cancel_e2e_qualified",
        "current_spica_8k_readback_verified_by_this_parser",
        "safe_live_managed_promotion_authorized",
        "serving_contract_evidence_digest_issued",
        "deployment_execution_digest_issued",
        "control_profile_published",
        "ready_merge_deploy_authorized",
    ))
    assert not any("manifest" in key or "declaration" in key for key in output)


def test_separate_expected_receipt_digest_must_match_exact_bytes():
    original = args()
    original["native_receipt_bytes"] += b" "
    with pytest.raises(ValueError, match="native-receipt-digest-mismatch"):
        reconcile_r2_r15_receipts(**original)
    original = args()
    original["managed_receipt_bytes"] += b" "
    with pytest.raises(ValueError, match="managed-receipt-digest-mismatch"):
        reconcile_r2_r15_receipts(**original)


@pytest.mark.parametrize(("key", "mutated", "error"), [
    ("native_expected_sha256", "PENDING", "native-expected-digest-invalid"),
    ("managed_expected_sha256", "a" * 64, "managed-expected-digest-invalid"),
    ("native_receipt_bytes", b"", "native-receipt-bytes-invalid"),
    ("managed_receipt_bytes", b"x" * 16385, "managed-receipt-bytes-invalid"),
])
def test_bad_or_missing_receipt_sha_and_bounds(key, mutated, error):
    data = args()
    data[key] = mutated
    with pytest.raises(ValueError, match=error):
        reconcile_r2_r15_receipts(**data)


@pytest.mark.parametrize(("role", "key", "bad", "error"), [
    ("native", "host", "deneb", "native-r2-schema-context-or-unit-invalid"),
    ("native", "native_context_target", 8192, "native-r2-schema-context-or-unit-invalid"),
    ("native", "native_context_target", True, "native-r2-schema-context-or-unit-invalid"),
    ("native", "canary_unit", "other-unit", "native-r2-schema-context-or-unit-invalid"),
    ("native", "tests_all_pass", False, "native-r2-acceptance-flag"),
    ("native", "old_gpu_released_before_12k", False, "native-r2-acceptance-flag"),
    ("native", "live_rollback_tested", False, "native-r2-acceptance-flag"),
    ("native", "original_8k_worker_restored", False, "native-r2-acceptance-flag"),
    ("native", "gateway_12k_qualified", True, "native-r2-overclaimed-flag"),
    ("native", "deneb_touched", True, "native-r2-overclaimed-flag"),
    ("native", "continuous_vram_floor_proven", True, "native-r2-overclaimed-flag"),
    ("native", "reason", "native-http-500", "native-r2-test-sequence-or-error-invalid"),
    ("native", "performed_tests", ["small_chat"], "native-r2-test-sequence-or-error-invalid"),
    ("managed", "status", "PRODUCTION_QUALIFIED", "managed-r15-schema-status-or-tests-invalid"),
    ("managed", "reported_tests", ["small_chat"], "managed-r15-schema-status-or-tests-invalid"),
    ("managed", "restored_original_8k", False, "managed-r15-rollback-or-failure-invalid"),
    ("managed", "error", "some-failure", "managed-r15-rollback-or-failure-invalid"),
    ("managed", "managed_12k_source_only_no_serving_advertised", False, "managed-r15-rollback-or-failure-invalid"),
    ("managed", "native_request_bodies_independently_attested", True, "managed-r15-overclaimed-flag"),
    ("managed", "new_12k_serving_revision_issued", True, "managed-r15-overclaimed-flag"),
    ("managed", "control_catalog_modified", True, "managed-r15-overclaimed-flag"),
    ("managed", "permanent_12k_promotion", True, "managed-r15-overclaimed-flag"),
])
def test_source_reported_failure_or_overclaim_is_not_accepted(role, key, bad, error):
    n, m = native(), managed()
    (n if role == "native" else m)[key] = bad
    with pytest.raises(ValueError, match=error):
        reconcile_r2_r15_receipts(**args(n=n, m=m))


@pytest.mark.parametrize(("key", "bad", "error"), [
    ("min_sampled_free_mib", 2047, "vram-observed-envelope-invalid"),
    ("min_sampled_free_mib", True, "vram-observed-envelope-invalid"),
    ("max_sampled_free_mib", 2000, "vram-observed-envelope-invalid"),
    ("samples", 63, "vram-observed-envelope-invalid"),
    ("max_sample_gap_ms", 250.1, "vram-observed-envelope-invalid"),
    ("interval_target_s", float("nan"), "receipt-json-invalid"),
    ("safety_abort", True, "vram-safety-or-sampling-claim-invalid"),
    ("monitor_thread_still_alive", True, "vram-safety-or-sampling-claim-invalid"),
    ("sampled_not_continuous", False, "vram-safety-or-sampling-claim-invalid"),
    ("threshold_mib", 0, "vram-safety-or-sampling-claim-invalid"),
])
def test_invalid_native_or_managed_vram_observations_fail(key, bad, error):
    for role in ("native", "managed"):
        n, m = native(), managed()
        (n if role == "native" else m)["vram"][key] = bad
        with pytest.raises(ValueError, match=(role + "-" + error)):
            reconcile_r2_r15_receipts(**args(n=n, m=m))


def test_extra_future_schema_field_fails_closed():
    n = native()
    n["manufactured_attestation"] = "yes"
    with pytest.raises(ValueError, match="native-r2-schema-context-or-unit-invalid"):
        reconcile_r2_r15_receipts(**args(n=n))
    m = managed()
    m["published_model_id"] = "bonsai2-27b-12k"
    with pytest.raises(ValueError, match="managed-r15-schema-status-or-tests-invalid"):
        reconcile_r2_r15_receipts(**args(m=m))


def test_duplicate_receipt_keys_cannot_pass_even_with_matching_hash():
    original = args()
    bad = original["native_receipt_bytes"].replace(
        b'"tests_all_pass": true', b'"tests_all_pass": true, "tests_all_pass": false',
    )
    assert bad != original["native_receipt_bytes"]
    original["native_receipt_bytes"] = bad
    original["native_expected_sha256"] = "sha256:" + hashlib.sha256(bad).hexdigest()
    with pytest.raises(ValueError, match="native-receipt-json-invalid"):
        reconcile_r2_r15_receipts(**original)


def test_invalid_object_or_invalid_hash_roles_fail_closed():
    data = args()
    data["managed_expected_sha256"] = data["native_expected_sha256"]
    with pytest.raises(ValueError, match="receipt-roles-must-not-share-hash"):
        reconcile_r2_r15_receipts(**data)
    n = b'["not-an-object"]'
    data = args()
    data["native_receipt_bytes"] = n
    data["native_expected_sha256"] = "sha256:" + hashlib.sha256(n).hexdigest()
    with pytest.raises(ValueError, match="native-receipt-not-object"):
        reconcile_r2_r15_receipts(**data)
