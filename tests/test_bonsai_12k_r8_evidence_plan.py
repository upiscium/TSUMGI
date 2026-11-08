"""R8: pure, fail-closed Native evidence + source-only draft provenance plan."""
from __future__ import annotations

import hashlib
import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from bonsai_12k_r6_native_probes import (
    canonical_bytes,
    validate_sse_trace,
)
from bonsai_12k_r8_evidence_plan import (
    BUNDLE_SCHEMA,
    SCHEMA,
    build_review_only_packet,
    verify_native_r6b_receipt,
)
from bonsai_12k_spica_candidate import (
    build_spica_unqualified_candidate,
)
from test_bonsai_12k_spica_candidate import make_inputs


def _frame(obj) -> bytes:
    if obj == "[DONE]":
        return b"data: [DONE]\n\n"
    return b"data: " + json.dumps(obj, separators=(",", ":")).encode() + b"\n\n"


def _sse_tool() -> bytes:
    return b"".join([
        _frame({"choices": [{"index": 0, "delta": {"role": "assistant"},
                             "finish_reason": None}]}),
        _frame({"choices": [{"index": 0, "delta": {"tool_calls": [{
            "index": 0, "id": "native_opaque_token_123",
            "type": "function", "function": {
                "name": "synthetic_echo", "arguments": '{"text":"R6"}',
            },
        }]}, "finish_reason": "tool_calls"}]}),
        _frame("[DONE]"),
    ])


def _sse_final() -> bytes:
    return b"".join([
        _frame({"choices": [{"index": 0, "delta": {"content": "Echo: R6"},
                             "finish_reason": "stop"}]}),
        _frame({"choices": [], "usage": {
            "prompt_tokens": 50, "completion_tokens": 7, "total_tokens": 57,
        }}),
        _frame("[DONE]"),
    ])


def fixture(**kwargs):
    tool_raw = _sse_tool()
    final_raw = _sse_final()
    tool_detail = validate_sse_trace(tool_raw, phase="tool_stream")
    final_detail = validate_sse_trace(final_raw, phase="tool_result_stream")
    stages = [
        {
            "stage": "cold_long",
            "request_sha256": hashlib.sha256(canonical_bytes("cold_long")).hexdigest(),
            "native_tokenizer_input": 10231,
            "usage_prompt_tokens": 10231,
            "cache_flag": "--no-cache-prompt",
            "fresh_native_and_first_inference": True,
            "native_prompt_processing_timing_qualified": True,
            "native_prompt_processing_milliseconds": 45768.32,
            "wall_elapsed_seconds": 46.5,
            "min_sampled_free_mib": 3360,
        },
        {
            "stage": "tool_stream",
            "request_sha256": hashlib.sha256(canonical_bytes("tool_stream")).hexdigest(),
            "sse_capture_sha256": hashlib.sha256(tool_raw).hexdigest(),
            "sse_bytes": len(tool_raw),
            "sse_frames": tool_detail["frames"],
            "single_terminal_done": True,
            "terminal_finish_seen": True,
            "structured_tool_call_complete": True,
            "min_sampled_free_mib": 3360,
        },
        {
            "stage": "tool_result_stream",
            "request_sha256": hashlib.sha256(canonical_bytes(
                "tool_result_stream", tool_call_trace=tool_raw,
            )).hexdigest(),
            "sse_capture_sha256": hashlib.sha256(final_raw).hexdigest(),
            "sse_bytes": len(final_raw),
            "sse_frames": final_detail["frames"],
            "single_terminal_done": True,
            "terminal_finish_seen": True,
            "content_fragment_seen": True,
            "min_sampled_free_mib": 3360,
        },
        {
            "stage": "cancel_stream",
            "request_sha256": hashlib.sha256(canonical_bytes("cancel_stream")).hexdigest(),
            "client_shutdown_close_performed": True,
            "native_slot_processing_true_observed_before_disconnect": True,
            "slot_processing_false_after_disconnect_observed": True,
            "slot_quiescence_delay_seconds": 8.545,
            "min_sampled_free_mib": 3360,
        },
    ]
    # 70 strictly monotonic timestamped GPU-local memory observations
    samples = "".join(f"{1000 + i * 0.35:.3f},3360\n" for i in range(70)).encode()
    data = {
        "evidence_json_bytes": json.dumps(stages).encode(),
        "tool_sse": tool_raw,
        "result_sse": final_raw,
        "nvml_csv": samples,
        "canary_result": "R6_NATIVE_COLD_TOOL_SSE_CANCEL_PASS_THEN_RESTORE_8K\n",
        "restore_status": "RESTORED_ORIGINAL_8K_READY\n",
    }
    data.update(kwargs)
    return data


def test_r8_constants_exactly_match_r0_unqualified_source_candidate():
    import bonsai_12k_r8_evidence_plan as r8
    import bonsai_12k_spica_candidate as r0
    for key in (
        "CONTEXT_12K", "DRAFT_SCHEMA", "LIMITS_12K",
        "PENDING_EVIDENCE", "PENDING_EXECUTION",
        "PENDING_REVISION", "PROFILE_12K",
    ):
        assert getattr(r8, key) == getattr(r0, key)


def synthetic_candidate():
    return build_spica_unqualified_candidate(**make_inputs())


def _change_stage(params, index, field, value):
    stages = json.loads(params["evidence_json_bytes"])
    stages[index][field] = value
    params["evidence_json_bytes"] = json.dumps(stages).encode()


def test_valid_synthetic_native_evidence_remains_unattested():
    data = fixture()
    original = deepcopy(data)
    report = verify_native_r6b_receipt(**data)
    assert data == original
    assert report["schema"] == SCHEMA
    assert report["status"] == "NATIVE_EVIDENCE_INTERNAL_CONSISTENCY_ONLY"
    assert report["cold_native_input_tokens"] == 10231
    assert report["cold_native_prompt_ms"] == 45768.32
    assert report["sample_count"] == 70
    assert report["sampled_min_free_mib"] == 3360
    assert report["slot_quiescence_after_disconnect_s"] == 8.545
    assert report["original_spica_8k_restore_status_strings_match"] is True
    assert report["live_post_restore_worker_readback_verified_by_this_receipt"] is False
    assert report["actual_native_http_request_bodies_captured_and_rehashed"] is False
    assert report["nvml_device_uuid_bound_to_spica_by_samples"] is False
    assert report["nvml_sample_intervals_tied_to_specific_prefill"] is False
    assert report["tool_stream_frames"] == 2
    assert report["tool_result_stream_frames"] == 2
    assert all(
        report[key] is False for key in (
            "native_host_authenticity_proven",
            "operator_signed_provenance",
            "managed_12k_admission_qualified",
            "gateway_12k_promotion_authorized",
        )
    )


def test_deeply_nested_r6_evidence_json_fails_with_bounded_error():
    data = fixture()
    # Well within the 64-KB input cap, but deeper than the JSON parser
    # recursion limit. Failure must be a fixed validation error.
    data["evidence_json_bytes"] = b"[" * 10000 + b"0" + b"]" * 10000
    with pytest.raises(ValueError, match="invalid-r6-native-receipt-json"):
        verify_native_r6b_receipt(**data)


def test_source_review_packet_is_not_deployable_or_authoritative():
    native = verify_native_r6b_receipt(**fixture())
    draft = synthetic_candidate()
    before = deepcopy(draft)
    packet = build_review_only_packet(
        unqualified_candidate=draft, native_receipt=native,
    )
    assert draft == before
    assert packet["schema"] == BUNDLE_SCHEMA
    assert packet["status"] == "BLOCKED_PENDING_INDEPENDENT_PROVENANCE_AND_MANAGED_WORKER_TEST"
    assert packet["candidate_total_context"] == 12288
    assert packet["candidate_limits"]["input_tokens"] == 10240
    assert packet["candidate_limits"]["output_tokens"] == 2048
    assert packet["opt_in_profile_id"] == "bonsai2-27b-12k"
    assert packet["candidate_spica_gpu_uuid"] != packet["deneb_reference_gpu_uuid"]
    assert len(packet["source_manifest_digest_records"]) == 7
    assert packet["candidate_path_bearing_runtime_review_hash_not_execution_identity"]
    assert all(
        packet[key] is False for key in (
            "source_authenticated_from_hosts", "native_host_authenticity_proven",
            "actual_request_bodies_verified", "nvml_gpu_uuid_binding_verified",
            "original_managed_8k_postcondition_verified_by_receipt",
            "execution_config_identity_approved", "serving_deployment_revision_issued",
            "serving_contract_revision_issued", "runtime_instance_epoch_allocated",
            "managed_worker_ready", "control_catalog_modified",
            "canary_activation_authorized", "ready_or_merge_authorized",
        )
    )
    assert "deployment" not in packet and "profile" not in packet
    assert "execution_config_sha256" not in packet
    assert len(packet["remaining_evidence_gaps"]) == 5


@pytest.mark.parametrize(("index", "field", "wrong", "reason"), [
    (0, "native_tokenizer_input", 10230, "r6-cold"),
    (0, "usage_prompt_tokens", 9999, "r6-cold"),
    (0, "fresh_native_and_first_inference", False, "r6-cold"),
    (0, "cache_flag", "--cache-prompt", "r6-cold"),
    (0, "native_prompt_processing_milliseconds", 0.0, "r6-cold"),
    (0, "native_prompt_processing_milliseconds", 60000, "r6-cold-timing"),
    (1, "sse_capture_sha256", "0" * 64, "r6-stream"),
    (1, "request_sha256", "0" * 64, "r6-stream"),
    (1, "sse_frames", 99, "r6-stream"),
    (1, "single_terminal_done", False, "r6-stream"),
    (2, "request_sha256", "0" * 64, "r6-stream"),
    (2, "sse_capture_sha256", "a" * 64, "r6-stream"),
    (2, "content_fragment_seen", False, "r6-tool"),
    (3, "slot_processing_false_after_disconnect_observed", False, "r6-tool"),
    (3, "client_shutdown_close_performed", False, "r6-tool"),
    (3, "slot_quiescence_delay_seconds", 41, "r6-tool"),
])
def test_false_native_stage_evidence_is_rejected(index, field, wrong, reason):
    data = fixture()
    _change_stage(data, index, field, wrong)
    with pytest.raises(ValueError, match=reason):
        verify_native_r6b_receipt(**data)


@pytest.mark.parametrize(("change", "reason"), [
    (lambda x: x.update(canary_result="PASS"), "restore"),
    (lambda x: x.update(restore_status="UNKNOWN"), "restore"),
    (lambda x: x.update(tool_sse=b"not sse"), "unterminated|frame"),
    (lambda x: x.update(result_sse=b"not sse"), "unterminated|frame"),
    (lambda x: x.update(evidence_json_bytes=b'{"not":"array"}'), "r6-stage"),
    (lambda x: x.update(evidence_json_bytes=b'{"a":1,"a":2}'), "duplicate|invalid"),
    (lambda x: x.update(nvml_csv=b"100.0,3360\n"), "sample-count"),
    (lambda x: x.update(nvml_csv=b"garbage"), "sample-count"),
])
def test_bad_receipts_never_authorize_managed_deployment(change, reason):
    data = fixture()
    change(data)
    with pytest.raises(ValueError, match=reason):
        verify_native_r6b_receipt(**data)


@pytest.mark.parametrize(("samples", "reason"), [
    (b"".join(f"{i + 1},2000\n".encode() for i in range(70)), "floor"),
    (b"".join(f"{i + 1},4000\n".encode() for i in range(70))
     .replace(b"10,4000\n", b"10,99999\n"), "range"),
    (b"".join(f"{i + 1},4000\n".encode() for i in range(70))
     .replace(b"10,4000\n", b"8,4000\n"), "not-increasing|row-invalid"),
    (b"".join(f"{i + 1},4000\n".encode() for i in range(70))
     .replace(b"10,4000\n", b"nan,4000\n"), "timestamp|row"),
])
def test_invalid_gpu_evidence_never_passes(samples, reason):
    data = fixture(nvml_csv=samples)
    with pytest.raises(ValueError, match=reason):
        verify_native_r6b_receipt(**data)


@pytest.mark.parametrize(("where", "mutation", "reason"), [
    ("source_digests", lambda v: v.clear(), "source-manifest"),
    ("admission", lambda v: None, "unqualified"),
])
def test_invalid_candidate_cannot_be_promoted(where, mutation, reason):
    draft = synthetic_candidate()
    if where == "admission":
        draft["admission"] = "READY"
    else:
        mutation(draft[where])
    with pytest.raises(ValueError, match=reason):
        build_review_only_packet(
            unqualified_candidate=draft,
            native_receipt=verify_native_r6b_receipt(**fixture()),
        )


@pytest.mark.parametrize("falsely_promoted", [
    "actual_native_http_request_bodies_captured_and_rehashed",
    "nvml_device_uuid_bound_to_spica_by_samples",
    "live_post_restore_worker_readback_verified_by_this_receipt",
])
def test_forced_true_evidence_gap_does_not_get_promoted(falsely_promoted):
    report = verify_native_r6b_receipt(**fixture())
    report[falsely_promoted] = True
    with pytest.raises(ValueError, match="native-unattested-receipt-required"):
        build_review_only_packet(
            unqualified_candidate=synthetic_candidate(),
            native_receipt=report,
        )


@pytest.mark.parametrize(("section", "field", "wrong", "reason"), [
    ("source_digests", "spica_runtime", None, "source-manifest"),
    ("runtime_UNQUALIFIED_draft", "provider_config", None, "runtime-candidate"),
    ("runtime_UNQUALIFIED_draft", "setup_intent", [], "runtime-candidate"),
    ("serving_INVALID_draft", "contracts", [], "serving-candidate"),
    ("serving_INVALID_draft", "deployment", None, "serving-candidate"),
    ("control_INVALID_draft", "profiles", [None], "catalog-candidate"),
    ("control_INVALID_draft", "schema_version", None, "catalog-candidate"),
])
def test_malformed_nested_candidate_must_fail_with_bounded_error(
    section, field, wrong, reason,
):
    draft = synthetic_candidate()
    draft[section][field] = wrong
    with pytest.raises(ValueError, match=reason):
        build_review_only_packet(
            unqualified_candidate=draft,
            native_receipt=verify_native_r6b_receipt(**fixture()),
        )


@pytest.mark.parametrize("bad", [
    float("nan"), float("inf"), {"unjsonable"},
])
def test_malformed_extra_candidate_value_gets_bounded_serialization_error(bad):
    draft = synthetic_candidate()
    draft["runtime_UNQUALIFIED_draft"]["demand"]["metadata"]["invalid"] = bad
    with pytest.raises(ValueError, match="candidate-runtime-not-canonical-json"):
        build_review_only_packet(
            unqualified_candidate=draft,
            native_receipt=verify_native_r6b_receipt(**fixture()),
        )


def test_cyclic_runtime_candidate_review_is_bounded():
    draft = synthetic_candidate()
    x = draft["runtime_UNQUALIFIED_draft"]["demand"]["metadata"]
    x["itself"] = x
    with pytest.raises(ValueError, match="candidate-runtime-review-cyclic-or-aliased"):
        build_review_only_packet(
            unqualified_candidate=draft,
            native_receipt=verify_native_r6b_receipt(**fixture()),
        )


def test_excessively_deep_runtime_review_is_bounded_before_json_dump():
    draft = synthetic_candidate()
    metadata = draft["runtime_UNQUALIFIED_draft"]["demand"]["metadata"]
    cursor = metadata
    for _ in range(80):
        next_item = {}
        cursor["next"] = next_item
        cursor = next_item
    with pytest.raises(ValueError, match="candidate-runtime-review-budget-exceeded"):
        build_review_only_packet(
            unqualified_candidate=draft,
            native_receipt=verify_native_r6b_receipt(**fixture()),
        )


def test_oversized_runtime_review_string_is_bounded_before_serialization():
    draft = synthetic_candidate()
    draft["runtime_UNQUALIFIED_draft"]["demand"]["metadata"]["huge"] = "x" * 1048577
    with pytest.raises(ValueError, match="candidate-runtime-review-budget-exceeded"):
        build_review_only_packet(
            unqualified_candidate=draft,
            native_receipt=verify_native_r6b_receipt(**fixture()),
        )


def test_oversized_runtime_review_node_count_is_bounded_before_serialization():
    draft = synthetic_candidate()
    draft["runtime_UNQUALIFIED_draft"]["demand"]["metadata"]["nodes"] = [0] * 9000
    with pytest.raises(ValueError, match="candidate-runtime-review-budget-exceeded"):
        build_review_only_packet(
            unqualified_candidate=draft,
            native_receipt=verify_native_r6b_receipt(**fixture()),
        )


def test_wrong_seven_source_keys_are_rejected_even_with_valid_digests():
    draft = synthetic_candidate()
    value = draft["source_digests"].pop("spica_runtime")
    draft["source_digests"]["unexpected_external_source"] = value
    with pytest.raises(ValueError, match="source-manifest-digests-not-qualified"):
        build_review_only_packet(
            unqualified_candidate=draft,
            native_receipt=verify_native_r6b_receipt(**fixture()),
        )


@pytest.mark.parametrize(("field", "wrong"), [
    ("cold_native_prompt_ms", None),
    ("cold_native_prompt_ms", "45768"),
    ("sampled_min_free_mib", True),
    ("sampled_min_free_mib", 2047),
])
def test_untrusted_receipt_numeric_type_and_floor_stay_bounded(field, wrong):
    report = verify_native_r6b_receipt(**fixture())
    report[field] = wrong
    with pytest.raises(ValueError, match="native-observation-fields-invalid"):
        build_review_only_packet(
            unqualified_candidate=synthetic_candidate(),
            native_receipt=report,
        )


def test_production_digest_placeholders_must_remain_invalid():
    draft = synthetic_candidate()
    draft["serving_INVALID_draft"]["deployment"]["execution_config_sha256"] = (
        "sha256:" + "a" * 64
    )
    with pytest.raises(ValueError, match="deployment-must-remain-unqualified"):
        build_review_only_packet(
            unqualified_candidate=draft,
            native_receipt=verify_native_r6b_receipt(**fixture()),
        )


def test_provided_native_evidence_cannot_claim_authentication():
    report = verify_native_r6b_receipt(**fixture())
    report["native_host_authenticity_proven"] = True
    with pytest.raises(ValueError, match="native-unattested-receipt"):
        build_review_only_packet(
            unqualified_candidate=synthetic_candidate(),
            native_receipt=report,
        )
