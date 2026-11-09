"""#232 R12 synthetic-only prospective Native receipt binding (no site I/O)."""
from __future__ import annotations

import copy
import hashlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from bonsai_12k_r6_native_probes import canonical_bytes
from bonsai_12k_r12_capture_binding import (
    PHASES,
    SCHEMA,
    STATUS,
    inspect_prospective_capture,
)
from test_bonsai_12k_r8_evidence_plan import _sse_final, _sse_tool
from test_bonsai_12k_r11_execution_review import review


def case():
    tool = _sse_tool()
    requests = {}
    for phase in PHASES:
        requests[phase] = canonical_bytes(
            phase, **({"tool_call_trace": tool}
                       if phase == "tool_result_stream" else {})
        )
    r11 = review()
    gpu = r11["record_only"]["gpu_uuid"]
    stages = []
    samples = []
    for n, phase in enumerate(PHASES):
        start = 1000 + n * 60000
        end = start + (50000 if phase == "cold_long" else
                       12000 if phase == "cancel_stream" else 10000)
        common = {
            "stage": phase, "start_ms": start, "end_ms": end,
            "request_sha256": hashlib.sha256(requests[phase]).hexdigest(),
            "native_context": 12288,
        }
        if phase == "cold_long":
            common.update(
                http_status=200, native_prompt_tokens=10231,
                no_cache_prompt=True, native_prompt_processing_ms=45768.32,
            )
        elif phase == "cancel_stream":
            common.update(
                http_status=None, client_disconnected=True,
                processing_before_disconnect=True,
                slot_idle_after_disconnect=True, slot_quiescence_ms=8545,
                disconnect_at_ms=start + 500, slot_idle_at_ms=start + 9045,
            )
        else:
            common["http_status"] = 200
        stages.append(common)
        for i in range((end - start) // 500 + 1):
            samples.append({
                "time_ms": start + i * 500,
                "stage": phase,
                "gpu_uuid": gpu,
                "free_mib": 3360 if n == 0 and i == 12 else 3600,
            })
    return {
        "r11_review": r11, "raw_http_requests": requests,
        "tool_sse": tool, "final_sse": _sse_final(),
        "cancel_sse": b"", "stages": stages, "gpu_samples": samples,
    }


def inspect(**kwargs):
    c = case()
    c.update(kwargs)
    return inspect_prospective_capture(**c)


def test_four_phase_source_only_consistency_never_claims_native_or_managed_admission():
    c = case()
    before = copy.deepcopy(c)
    got = inspect_prospective_capture(**c)
    assert c == before
    assert got["schema_version"] == SCHEMA
    assert got["status"] == STATUS
    assert got["sample_count_from_caller"] == 168
    assert got["sampled_min_free_mib_from_caller"] == 3360
    assert got["reported_spica_gpu_uuid"] == c["r11_review"]["record_only"]["gpu_uuid"]
    assert got["request_body_digests"] == {
        p: hashlib.sha256(c["raw_http_requests"][p]).hexdigest() for p in PHASES
    }
    assert got["request_bodies_match_synthetic_recipes_only"] is True
    assert got["structured_sse_consistent_only"] is True
    assert got["gpu_sample_tags_and_stage_windows_consistent_only"] is True
    for field in (
        "actual_request_bodies_independently_captured",
        "native_process_and_host_authenticated",
        "gpu_uuid_cryptographically_authenticated",
        "managed_worker_12k_qualified",
        "independent_provenance_verified",
        "spica_8k_live_restore_reverified",
        "new_runtime_epoch_issued", "serving_revision_issued",
        "control_catalog_modified", "canary_activation_authorized",
        "ready_or_merge_authorized",
    ):
        assert got[field] is False
    assert not {"runtime_manifest", "serving_manifest", "catalog_manifest"} & got.keys()


@pytest.mark.parametrize(("field", "bad"), [
    ("status", "QUALIFIED"), ("reported_hashes_independently_verified", True),
    ("host_and_gpu_origin_authenticated", True), ("managed_worker_12k_tested", True),
    ("serving_revision_issued", True), ("canary_activation_authorized", True),
])
def test_r11_review_cannot_claim_nonexistent_source_authority(field, bad):
    c = case()
    c["r11_review"][field] = bad
    with pytest.raises(ValueError, match="unqualified-r11-review-required"):
        inspect_prospective_capture(**c)


def test_wrong_r11_gpu_or_fingerprint_fails():
    c = case()
    c["r11_review"]["record_only"]["gpu_uuid"] = "GPU-" + "-" * 36
    with pytest.raises(ValueError, match="r11-gpu-identity-invalid"):
        inspect_prospective_capture(**c)
    c = case()
    c["r11_review"]["review_fingerprint_sha256_not_a_deployment_identity"] = "PENDING"
    with pytest.raises(ValueError, match="r11-review-fingerprint-invalid"):
        inspect_prospective_capture(**c)
    c = case()
    c["r11_review"]["record_only"]["profile_id"] = "bonsai2-27b"
    with pytest.raises(ValueError, match="unqualified-r11-profile-mismatch"):
        inspect_prospective_capture(**c)


@pytest.mark.parametrize("phase", PHASES)
def test_caller_submitted_raw_request_must_equal_exact_recipe(phase):
    c = case()
    c["raw_http_requests"][phase] += b" "
    with pytest.raises(ValueError, match="raw-request-must-match-approved-synthetic-recipe"):
        inspect_prospective_capture(**c)


def test_missing_or_oversize_request_bytes_fail_before_any_acceptance():
    c = case()
    del c["raw_http_requests"]["cold_long"]
    with pytest.raises(ValueError, match="request-phase-set-invalid"):
        inspect_prospective_capture(**c)
    c = case()
    c["raw_http_requests"]["cold_long"] = b"x" * 65537
    with pytest.raises(ValueError, match="request-bytes-invalid"):
        inspect_prospective_capture(**c)


@pytest.mark.parametrize(("stage_index", "key", "value", "error"), [
    (0, "native_context", 8192, "stage-request-or-runtime"),
    (0, "native_prompt_tokens", 8192, "cold-prefill"),
    (0, "no_cache_prompt", False, "cold-prefill"),
    (0, "native_prompt_processing_ms", 60000, "cold-prefill"),
    (0, "http_status", 503, "cold-prefill"),
    (1, "http_status", 503, "http-stage"),
    (2, "request_sha256", "0" * 64, "stage-request-or-runtime"),
    (3, "client_disconnected", False, "cancel-observations"),
    (3, "processing_before_disconnect", False, "cancel-observations"),
    (3, "slot_idle_after_disconnect", False, "cancel-observations"),
    (3, "slot_quiescence_ms", 40001, "cancel-observations"),
    (3, "slot_idle_at_ms", 0, "cancel-observations"),
])
def test_reported_stage_missing_or_inconsistent_fields_fail(
    stage_index, key, value, error,
):
    c = case()
    c["stages"][stage_index][key] = value
    with pytest.raises(ValueError, match=error):
        inspect_prospective_capture(**c)


def test_out_of_order_or_overlapping_phases_fail():
    c = case()
    c["stages"][0], c["stages"][1] = c["stages"][1], c["stages"][0]
    with pytest.raises(ValueError, match="stage-count-order-invalid"):
        inspect_prospective_capture(**c)
    c = case()
    c["stages"][1]["start_ms"] = c["stages"][0]["end_ms"]
    with pytest.raises(ValueError, match="stage-intervals-overlap-or-invalid"):
        inspect_prospective_capture(**c)


@pytest.mark.parametrize(("index", "field", "value", "error"), [
    (0, "free_mib", 2047, "sample-device-time-or-vram-floor"),
    (0, "gpu_uuid", "GPU-8bcf8ba8-4610-d9bb-3862-a03b5ea7d931",
     "sample-device-time-or-vram-floor"),
    (0, "time_ms", -1, "sample-device-time-or-vram-floor"),
    (20, "time_ms", 999999, "sample-outside-reported-stage"),
    (0, "stage", "tool_stream", "sample-outside-reported-stage"),
])
def test_memory_sample_gpu_uuid_floor_time_and_stage_binding(
    index, field, value, error,
):
    c = case()
    c["gpu_samples"][index][field] = value
    with pytest.raises(ValueError, match=error):
        inspect_prospective_capture(**c)


def test_sampling_gaps_undersampling_and_duplicate_times_fail_closed():
    c = case()
    c["gpu_samples"] = c["gpu_samples"][:15]
    with pytest.raises(ValueError, match="sample-count-unqualified"):
        inspect_prospective_capture(**c)
    c = case()
    # Cold phase has 101 synthetic samples; leave 15, total >=64.
    c["gpu_samples"] = c["gpu_samples"][86:]
    with pytest.raises(ValueError, match="stage-sampling-insufficient"):
        inspect_prospective_capture(**c)
    c = case()
    # Force one sample outside its 50-second cold stage window.
    c["gpu_samples"][100]["time_ms"] += 1200
    with pytest.raises(ValueError, match="sample-outside-reported-stage"):
        inspect_prospective_capture(**c)
    c = case()
    c["gpu_samples"][12]["time_ms"] = c["gpu_samples"][11]["time_ms"]
    with pytest.raises(ValueError, match="sample-device-time-or-vram-floor"):
        inspect_prospective_capture(**c)


def test_sse_fragments_remain_structural_not_authenticated():
    c = case()
    c["tool_sse"] = b"data: [DONE]\n\n"
    with pytest.raises(ValueError):
        inspect_prospective_capture(**c)
    c = case()
    c["final_sse"] = b""
    with pytest.raises(ValueError):
        inspect_prospective_capture(**c)
    c = case()
    c["cancel_sse"] = b"data: [DONE]\n\n"
    with pytest.raises(ValueError):
        inspect_prospective_capture(**c)


def test_r11_review_record_fingerprint_mismatch_fails_closed():
    c = case()
    c["r11_review"]["record_only"]["gpu_uuid"] = (
        "GPU-12345678-1234-1234-1234-123456789012"
    )
    # Consistent synthetic GPU samples would still not authenticate origin,
    # but an unmodified R11 fingerprint must detect a changed record.
    with pytest.raises(ValueError, match="r11-record-fingerprint-mismatch"):
        inspect_prospective_capture(**c)


def test_first_sample_gap_cannot_be_misread_as_cold_prefill_coverage():
    c = case()
    start = c["stages"][0]["start_ms"]
    # Monotonic, inside the window, but cold stage begins 1.1 seconds
    # before its first NVML sample; spread 101 samples over 48 seconds.
    for i in range(101):
        c["gpu_samples"][i]["time_ms"] = start + 1100 + i * 480
    with pytest.raises(ValueError, match="stage-sampling-gap-unqualified"):
        inspect_prospective_capture(**c)


def test_sample_boolean_and_duplicate_cancellation_report_key_fail_closed():
    c = case()
    c["gpu_samples"][0]["free_mib"] = True
    with pytest.raises(ValueError, match="sample-device-time-or-vram-floor"):
        inspect_prospective_capture(**c)
    c = case()
    c["stages"][3]["untrusted_extra"] = "fake_proof"
    with pytest.raises(ValueError, match="stage-fields-invalid"):
        inspect_prospective_capture(**c)
