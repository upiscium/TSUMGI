"""#232 R8: pure review-only Bonsai12K provenance binding, NO deployment.

Validates consistency of a recorded R6B Native-only canary and the existing
source-only R0 12K proposal. It does NOT authenticate the host, signs nothing,
creates no deployable Serving Identity, grants no GPU access/admission, and
never touches a file, network, DB, Worker or Control.
"""
from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from bonsai_12k_r6_native_probes import canonical_bytes, validate_sse_trace

# Keep the site evidence checker independent from installed astrumweaver:
# only R6 offline recipe helpers and the Python standard library are needed.
# Tests explicitly compare these constants with the R0 candidate module.
CONTEXT_12K = 12288
DRAFT_SCHEMA = "bonsai-spica-12k-unqualified-draft-v1"
LIMITS_12K = {
    "input_tokens": 10240, "output_tokens": 2048,
    "total_tokens": 12288, "request_bytes": 65536,
}
PENDING_EVIDENCE = "PENDING_BONSAI_12K_REAL_NATIVE_CANARY_EVIDENCE"
PENDING_EXECUTION = "PENDING_BONSAI_12K_REVIEWED_EXECUTION_CONFIG"
PENDING_REVISION = "PENDING_BONSAI_12K_REVISION_REVIEW"
PROFILE_12K = "bonsai2-27b-12k"

SCHEMA = "bonsai-r8-unattested-native-evidence-review-v1"
BUNDLE_SCHEMA = "bonsai-r8-spica-review-only-not-a-deployment-v1"
_EXPECTED_STAGES = ("cold_long", "tool_stream", "tool_result_stream", "cancel_stream")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_RESTORE = "RESTORED_ORIGINAL_8K_READY"
_RESULT = "R6_NATIVE_COLD_TOOL_SSE_CANCEL_PASS_THEN_RESTORE_8K"
_MAX_CAPTURE = 1_048_576
_MAX_SAMPLE_BYTES = 128_000
_MAX_EVIDENCE = 64_000


_SOURCE_KEYS = frozenset((
    "spica_runtime", "spica_serving", "spica_worker",
    "deneb_runtime", "deneb_serving", "deneb_worker",
    "control_catalog",
))
_GPU = re.compile(r"GPU-[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")


def _require(check: bool, code: str) -> None:
    if not check:
        raise ValueError(code)


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _is_sha(value: object) -> bool:
    return type(value) is str and _SHA.fullmatch(value) is not None


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    for key, value in pairs:
        _require(key not in obj, "duplicate-receipt-key")
        obj[key] = value
    return obj


def _finite_positive_number(value: object) -> bool:
    return type(value) in (int, float) and value > 0 and value < 1e9


def verify_native_r6b_receipt(
    *,
    evidence_json_bytes: bytes,
    tool_sse: bytes,
    result_sse: bytes,
    nvml_csv: bytes,
    canary_result: str,
    restore_status: str,
) -> dict[str, object]:
    """Check actual-record-shaped bytes; outputs observational NOT authoritative."""
    _require(
        type(evidence_json_bytes) is bytes
        and 0 < len(evidence_json_bytes) <= _MAX_EVIDENCE,
        "invalid-evidence-size",
    )
    _require(
        type(tool_sse) is bytes and 0 < len(tool_sse) <= _MAX_CAPTURE
        and type(result_sse) is bytes and 0 < len(result_sse) <= _MAX_CAPTURE,
        "invalid-sse-bytes",
    )
    _require(
        type(nvml_csv) is bytes and 0 < len(nvml_csv) <= _MAX_SAMPLE_BYTES,
        "invalid-nvml-size",
    )
    _require(
        type(canary_result) is str and canary_result.strip() == _RESULT
        and type(restore_status) is str and restore_status.strip() == _RESTORE,
        "canary-or-8k-restore-not-attested",
    )
    try:
        observations = json.loads(evidence_json_bytes, object_pairs_hook=_unique)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError("invalid-r6-native-receipt-json") from exc
    _require(type(observations) is list and len(observations) == 4,
             "r6-stage-count-invalid")
    _require(
        all(type(item) is dict for item in observations)
        and tuple(item.get("stage") for item in observations) == _EXPECTED_STAGES,
        "r6-stage-order-invalid",
    )
    cold, tool, answer, cancel = observations
    _require(
        cold.get("request_sha256") == _sha(canonical_bytes("cold_long"))
        and cold.get("native_tokenizer_input") == 10231
        and cold.get("usage_prompt_tokens") == 10231
        and cold.get("fresh_native_and_first_inference") is True
        and cold.get("cache_flag") == "--no-cache-prompt"
        and cold.get("native_prompt_processing_timing_qualified") is True
        and _finite_positive_number(cold.get("native_prompt_processing_milliseconds"))
        and _finite_positive_number(cold.get("wall_elapsed_seconds")),
        "r6-cold-prefill-evidence-mismatch",
    )
    _require(
        cold["native_prompt_processing_milliseconds"]
        <= 1000 * cold["wall_elapsed_seconds"] + 1000,
        "r6-cold-timing-inconsistent",
    )
    parsed_tool = validate_sse_trace(tool_sse, phase="tool_stream")
    parsed_answer = validate_sse_trace(result_sse, phase="tool_result_stream")
    _require(
        parsed_tool["done"] is True
        and parsed_tool["structured_tool_call_complete"] is True
        and parsed_answer["done"] is True
        and parsed_answer["content_fragment_seen"] is True,
        "r6-stream-structural-failure",
    )
    expected_tool = _sha(canonical_bytes("tool_stream"))
    expected_result = _sha(canonical_bytes(
        "tool_result_stream", tool_call_trace=tool_sse,
    ))
    for row, raw, parsed, expected in (
        (tool, tool_sse, parsed_tool, expected_tool),
        (answer, result_sse, parsed_answer, expected_result),
    ):
        _require(
            row.get("request_sha256") == expected
            and row.get("sse_capture_sha256") == _sha(raw)
            and type(row.get("sse_bytes")) is int
            and row["sse_bytes"] == len(raw)
            and type(row.get("sse_frames")) is int
            and row["sse_frames"] == parsed["frames"]
            and row.get("single_terminal_done") is True
            and row.get("terminal_finish_seen") is True,
            "r6-stream-receipt-digest-or-shape-mismatch",
        )
    _require(
        tool.get("structured_tool_call_complete") is True
        and answer.get("content_fragment_seen") is True
        and cancel.get("request_sha256") == _sha(canonical_bytes("cancel_stream"))
        and cancel.get("native_slot_processing_true_observed_before_disconnect") is True
        and cancel.get("client_shutdown_close_performed") is True
        and cancel.get("slot_processing_false_after_disconnect_observed") is True
        and _finite_positive_number(cancel.get("slot_quiescence_delay_seconds"))
        and cancel["slot_quiescence_delay_seconds"] <= 40,
        "r6-tool-or-cancellation-evidence-incomplete",
    )
    try:
        rows = nvml_csv.decode("ascii").splitlines()
    except UnicodeError as exc:
        raise ValueError("nvml-record-not-ascii") from exc
    _require(64 <= len(rows) <= 5000, "nvml-sample-count-out-of-bounds")
    values: list[int] = []
    last_stamp: Decimal | None = None
    for line in rows:
        parts = line.split(",")
        _require(len(parts) == 2, "nvml-row-invalid")
        try:
            stamp = Decimal(parts[0])
        except InvalidOperation as exc:
            raise ValueError("nvml-timestamp-invalid") from exc
        _require(stamp.is_finite() and stamp > 0, "nvml-timestamp-invalid")
        _require(last_stamp is None or stamp > last_stamp,
                 "nvml-timestamps-not-increasing")
        last_stamp = stamp
        _require(re.fullmatch(r"[0-9]+", parts[1]) is not None,
                 "nvml-free-mib-invalid")
        value = int(parts[1])
        _require(0 <= value <= 11264, "nvml-free-mib-out-of-range")
        values.append(value)
    minimum = min(values)
    _require(minimum >= 2048, "nvml-floor-breached")
    for item in observations:
        v = item.get("min_sampled_free_mib")
        _require(
            type(v) is int and minimum <= v <= 11264 and v >= 2048,
            "r6-stage-memory-floor-invalid",
        )
    return {
        "schema": SCHEMA,
        "status": "NATIVE_EVIDENCE_INTERNAL_CONSISTENCY_ONLY",
        "native_host_authenticity_proven": False,
        "operator_signed_provenance": False,
        "managed_12k_admission_qualified": False,
        "gateway_12k_promotion_authorized": False,
        "original_spica_8k_restore_status_strings_match": True,
        "live_post_restore_worker_readback_verified_by_this_receipt": False,
        "distinct_spica_12k_native_claimed": True,
        "actual_native_http_request_bodies_captured_and_rehashed": False,
        "nvml_device_uuid_bound_to_spica_by_samples": False,
        "nvml_sample_intervals_tied_to_specific_prefill": False,
        "cold_native_input_tokens": 10231,
        "cold_native_prompt_ms": cold["native_prompt_processing_milliseconds"],
        "tool_stream_frames": parsed_tool["frames"],
        "tool_result_stream_frames": parsed_answer["frames"],
        "slot_quiescence_after_disconnect_s": cancel["slot_quiescence_delay_seconds"],
        "sampled_min_free_mib": minimum,
        "sample_count": len(values),
        "evidence_file_sha256": _sha(evidence_json_bytes),
        "tool_sse_file_sha256": _sha(tool_sse),
        "result_sse_file_sha256": _sha(result_sse),
        "nvml_csv_file_sha256": _sha(nvml_csv),
    }


def _review_json_budget(value: object) -> None:
    """Preflight a hostile candidate before canonical JSON serialization.

    This is only a *review* fingerprint; reject cycles, unexpectedly deep
    or huge structures, non-string keys and oversized scalar strings before
    constructing any potentially unbounded encoded JSON output.
    """
    max_nodes = 8192
    max_depth = 32
    max_scalar_bytes = 1_048_576
    visited: set[int] = set()
    stack: list[tuple[object, int]] = [(value, 0)]
    nodes = 0
    scalar_bytes = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        _require(
            depth <= max_depth and nodes <= max_nodes,
            "candidate-runtime-review-budget-exceeded",
        )
        if type(item) in (dict, list):
            identity = id(item)
            _require(identity not in visited,
                     "candidate-runtime-review-cyclic-or-aliased")
            visited.add(identity)
            if type(item) is dict:
                for key, child in item.items():
                    _require(type(key) is str,
                             "candidate-runtime-json-key-invalid")
                    _require(len(key) <= max_scalar_bytes,
                             "candidate-runtime-review-budget-exceeded")
                    scalar_bytes += len(key)
                    stack.append((child, depth + 1))
            else:
                for child in item:
                    stack.append((child, depth + 1))
        elif type(item) is str:
            _require(len(item) <= max_scalar_bytes,
                     "candidate-runtime-review-budget-exceeded")
            scalar_bytes += len(item)
        elif item is None or type(item) in (int, float, bool):
            pass
        else:
            raise ValueError("candidate-runtime-not-canonical-json")
        _require(scalar_bytes <= max_scalar_bytes,
                 "candidate-runtime-review-budget-exceeded")


def build_review_only_packet(
    *, unqualified_candidate: dict[str, Any],
    native_receipt: dict[str, Any],
) -> dict[str, object]:
    """Tie proposed 12K source and canary review facts, NEVER publish identity."""
    draft = unqualified_candidate
    r = native_receipt
    _require(
        type(draft) is dict
        and draft.get("schema_version") == DRAFT_SCHEMA
        and draft.get("admission") == "DENIED_PENDING_REAL_CANARY_AND_REVIEW"
        and draft.get("spica_worker_id") == "bonsai-spica"
        and draft.get("deneb_reference_worker_id") == "bonsai-deneb"
        and draft.get("opt_in_model_id") == PROFILE_12K,
        "unqualified-draft-required",
    )
    _require(
        type(r) is dict and r.get("schema") == SCHEMA
        and r.get("status") == "NATIVE_EVIDENCE_INTERNAL_CONSISTENCY_ONLY"
        and r.get("native_host_authenticity_proven") is False
        and r.get("operator_signed_provenance") is False
        and r.get("managed_12k_admission_qualified") is False
        and r.get("actual_native_http_request_bodies_captured_and_rehashed") is False
        and r.get("nvml_device_uuid_bound_to_spica_by_samples") is False
        and r.get("live_post_restore_worker_readback_verified_by_this_receipt")
        is False,
        "native-unattested-receipt-required",
    )
    source_digests = draft.get("source_digests")
    _require(
        type(source_digests) is dict
        and set(source_digests) == _SOURCE_KEYS
        and all(
            type(value) is str and value.startswith("sha256:")
            and _is_sha(value.removeprefix("sha256:"))
            for value in source_digests.values()
        ),
        "source-manifest-digests-not-qualified",
    )

    # Never trust arbitrary nested candidate shapes: report bounded,
    # redacted rejection codes rather than KeyError/IndexError/TypeError.
    proposed = draft.get("runtime_UNQUALIFIED_draft")
    serving = draft.get("serving_INVALID_draft")
    catalog = draft.get("control_INVALID_draft")
    _require(
        type(proposed) is dict and type(serving) is dict
        and type(catalog) is dict,
        "source-draft-shape-invalid",
    )
    runtime_cfg = proposed.get("provider_config")
    setup = proposed.get("setup_intent")
    _require(
        type(runtime_cfg) is dict and type(setup) is dict,
        "runtime-candidate-shape-invalid",
    )
    configuration = setup.get("configuration")
    uuids = draft.get("source_gpu_uuids")
    _require(
        type(configuration) is dict and type(uuids) is dict,
        "gpu-setup-shape-invalid",
    )
    spica_uuid = uuids.get("spica_worker")
    deneb_uuid = uuids.get("deneb_worker")
    _require(
        type(spica_uuid) is str and _GPU.fullmatch(spica_uuid) is not None
        and type(deneb_uuid) is str and _GPU.fullmatch(deneb_uuid) is not None
        and spica_uuid != deneb_uuid,
        "candidate-spica-deneb-uuid-mismatch",
    )
    _require(
        type(runtime_cfg.get("context_size")) is int
        and runtime_cfg["context_size"] == CONTEXT_12K
        and type(configuration.get("context_size")) is int
        and configuration["context_size"] == CONTEXT_12K,
        "candidate-context-not-12k",
    )
    _require(
        configuration.get("gpu_uuids") == [spica_uuid],
        "candidate-spica-deneb-uuid-mismatch",
    )
    deployment = serving.get("deployment")
    contracts = serving.get("contracts")
    _require(
        type(deployment) is dict and type(contracts) is list
        and len(contracts) == 1 and type(contracts[0]) is dict,
        "serving-candidate-shape-invalid",
    )
    contract_row = contracts[0]
    contract = contract_row.get("contract")
    _require(type(contract) is dict, "serving-contract-shape-invalid")
    catalog_profiles = catalog.get("profiles")
    _require(
        catalog.get("schema_version") == "chat-gateway-catalog-v1"
        and type(catalog_profiles) is list
        and len(catalog_profiles) == 3
        and all(type(row) is dict for row in catalog_profiles),
        "catalog-candidate-shape-invalid",
    )
    profile_ids = []
    for entry in catalog_profiles:
        profile = entry.get("profile")
        _require(type(profile) is dict, "catalog-profile-shape-invalid")
        profile_ids.append(profile.get("profile_id"))
    _require(
        profile_ids == ["qwen3.8-27b", "bonsai2-27b", PROFILE_12K],
        "catalog-candidate-profile-order-invalid",
    )
    _require(
        deployment.get("execution_config_sha256") == PENDING_EXECUTION
        and contract.get("validation_evidence_sha256") == PENDING_EVIDENCE
        and contract_row.get("serving_contract_revision") == PENDING_REVISION
        and catalog_profiles[-1]["profile"].get("serving_contract_revision")
        == PENDING_REVISION,
        "deployment-must-remain-unqualified",
    )
    _require(
        contract.get("limits") == LIMITS_12K,
        "candidate-12k-limits-mismatch",
    )
    _require(
        _finite_positive_number(r.get("cold_native_prompt_ms"))
        and type(r.get("sampled_min_free_mib")) is int
        and 2048 <= r["sampled_min_free_mib"] <= 11264,
        "native-observation-fields-invalid",
    )
    native_digest_keys = (
        "evidence_file_sha256", "tool_sse_file_sha256",
        "result_sse_file_sha256", "nvml_csv_file_sha256",
    )
    _require(all(_is_sha(r.get(k)) for k in native_digest_keys),
             "native-evidence-digests-invalid")
    _review_json_budget(proposed)
    try:
        review_raw = json.dumps(
            proposed, sort_keys=True, ensure_ascii=True,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError) as exc:
        raise ValueError("candidate-runtime-not-canonical-json") from exc
    _require(0 < len(review_raw) <= 1_048_576,
             "candidate-runtime-review-payload-too-large")
    return {
        "schema": BUNDLE_SCHEMA,
        "status": "BLOCKED_PENDING_INDEPENDENT_PROVENANCE_AND_MANAGED_WORKER_TEST",
        "source_manifest_digest_records": dict(source_digests),
        "native_evidence_digest_records": {k: r[k] for k in native_digest_keys},
        "candidate_spica_gpu_uuid": draft["source_gpu_uuids"]["spica_worker"],
        "deneb_reference_gpu_uuid": draft["source_gpu_uuids"]["deneb_worker"],
        "candidate_total_context": CONTEXT_12K,
        "candidate_limits": dict(LIMITS_12K),
        "opt_in_profile_id": PROFILE_12K,
        "candidate_path_bearing_runtime_review_hash_not_execution_identity": (
            _sha(review_raw)
        ),
        "canary_cold_prompt_ms": r["cold_native_prompt_ms"],
        "canary_sampled_min_free_mib": r["sampled_min_free_mib"],
        "source_authenticated_from_hosts": False,
        "native_host_authenticity_proven": False,
        "actual_request_bodies_verified": False,
        "nvml_gpu_uuid_binding_verified": False,
        "original_managed_8k_postcondition_verified_by_receipt": False,
        "execution_config_identity_approved": False,
        "serving_deployment_revision_issued": False,
        "serving_contract_revision_issued": False,
        "runtime_instance_epoch_allocated": False,
        "managed_worker_ready": False,
        "control_catalog_modified": False,
        "canary_activation_authorized": False,
        "ready_or_merge_authorized": False,
        "remaining_evidence_gaps": [
            "capture and rehash actual HTTP request bodies from a newly approved site run",
            "independent post-rollback 8K Worker/Native/Control/Deneb readback",
            "NVML device UUID and sample-to-prefill interval binding",
            "independent host and binary/model/configuration provenance",
            "reviewed execution config identity and Serving/Runtime epoch admission",
        ],
    }
