"""#232 R16: bounded offline reconciliation of actual Native R2 / Managed R15 receipts.

Accepts exact caller-supplied bytes and independently supplied expected SHA256
strings. Performs NO disk, network, host, Nix, GPU, Control, or model I/O.
A coherent receipt is neither a signed host observation nor Serving admission.
Do not use any result digest as a DeploymentIdentity or ServingContract digest.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any

SCHEMA = "bonsai-spica-r16-dual-canary-receipts-review-only-v1"
STATUS = "NATIVE_AND_MANAGED_SMOKE_INTERNAL_CONSISTENCY_ONLY"
NATIVE_SCHEMA = "tsumgi-spica-bonsai12k-native-canary-r1"
MANAGED_SCHEMA = "tsumgi-r15-spica-managed12k-bounded-canary-v1"
NATIVE_TESTS = ("small_chat", "stream", "long_safe", "long", "tool_and_result")
MANAGED_TESTS = ("small_chat", "stream", "long_cold", "tool_result")
_MAX_BYTES = 16_384
_MAX_DEPTH = 12
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")

_NATIVE_FIELDS = {
    "schema", "host", "worker_stop_attempted", "worker_originally_stopped",
    "old_gpu_released_before_12k", "canary_started", "canary_unit",
    "native_context_target", "performed_tests", "tests_all_pass", "vram",
    "original_8k_worker_restored", "reason", "deneb_touched",
    "control_catalog_changed", "gateway_12k_qualified",
    "live_rollback_tested", "native_canary_promoted_to_service",
    "semantic_quality_qualified", "continuous_vram_floor_proven",
}
_MANAGED_FIELDS = {
    "schema_version", "status", "reported_tests", "vram",
    "restored_original_8k", "managed_12k_source_only_no_serving_advertised",
    "control_catalog_modified", "native_request_bodies_independently_attested",
    "new_12k_serving_revision_issued", "permanent_12k_promotion", "error",
}
_VRAM_FIELDS = {
    "abort_reason", "interval_target_s", "max_sample_gap_ms",
    "max_sampled_free_mib", "min_sampled_free_mib",
    "monitor_thread_still_alive", "safety_abort",
    "sampled_not_continuous", "samples", "threshold_mib",
}


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise ValueError(reason)


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        require(key not in out, "duplicate-receipt-key")
        out[key] = value
    return out


def _reject_nonfinite(value: str) -> None:
    raise ValueError("nonfinite-receipt-number")


def _bounded_object(raw: bytes, expected_sha256: str, *, role: str) -> dict[str, Any]:
    require(type(raw) is bytes and 0 < len(raw) <= _MAX_BYTES,
            role + "-receipt-bytes-invalid")
    require(type(expected_sha256) is str and _SHA256.fullmatch(expected_sha256) is not None,
            role + "-expected-digest-invalid")
    require("sha256:" + hashlib.sha256(raw).hexdigest() == expected_sha256,
            role + "-receipt-digest-mismatch")
    try:
        obj = json.loads(
            raw, object_pairs_hook=unique_object,
            parse_constant=_reject_nonfinite,
        )
    except (ValueError, RecursionError, UnicodeError) as exc:
        raise ValueError(role + "-receipt-json-invalid") from exc
    require(type(obj) is dict, role + "-receipt-not-object")
    # Keep the historical receipt parser bounded if maliciously modified.
    visited = 0
    stack: list[tuple[Any, int]] = [(obj, 0)]
    while stack:
        item, depth = stack.pop()
        visited += 1
        require(visited <= 300 and depth <= _MAX_DEPTH, role + "-receipt-too-complex")
        if type(item) is dict:
            require(all(type(k) is str and len(k) <= 128 for k in item),
                    role + "-receipt-invalid-key")
            stack.extend((v, depth + 1) for v in item.values())
        elif type(item) is list:
            stack.extend((v, depth + 1) for v in item)
        else:
            require(item is None or type(item) in (int, float, bool, str),
                    role + "-receipt-invalid-scalar")
            if type(item) is str:
                require(len(item) <= 1024, role + "-receipt-string-too-large")
    return obj


def _number(value: object, *, minimum: float, maximum: float) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and minimum <= value <= maximum


def _vram(value: object, *, role: str) -> dict[str, int | float]:
    require(type(value) is dict and set(value) == _VRAM_FIELDS,
            role + "-vram-structure-invalid")
    assert isinstance(value, dict)
    require(
        value.get("abort_reason") is None
        and value.get("monitor_thread_still_alive") is False
        and value.get("safety_abort") is False
        and value.get("sampled_not_continuous") is True
        and type(value.get("threshold_mib")) is int
        and value["threshold_mib"] == 2048,
        role + "-vram-safety-or-sampling-claim-invalid",
    )
    require(
        type(value.get("samples")) is int and 64 <= value["samples"] <= 100_000
        and _number(value.get("interval_target_s"), minimum=0.001, maximum=1.0)
        and _number(value.get("max_sample_gap_ms"), minimum=0.001, maximum=250)
        and type(value.get("min_sampled_free_mib")) is int
        and type(value.get("max_sampled_free_mib")) is int
        and 2048 <= value["min_sampled_free_mib"] <= value["max_sampled_free_mib"] <= 11264,
        role + "-vram-observed-envelope-invalid",
    )
    return {
        "sample_count_reported": value["samples"],
        "max_sample_gap_ms_reported": value["max_sample_gap_ms"],
        "min_free_mib_reported": value["min_sampled_free_mib"],
    }


def reconcile_r2_r15_receipts(
    *, native_receipt_bytes: bytes,
    native_expected_sha256: str,
    managed_receipt_bytes: bytes,
    managed_expected_sha256: str,
) -> dict[str, object]:
    """Parse and cross-check 2 site-local receipts without promoting Serving.

    The caller must obtain and bind bytes and hashes through an independent
    trusted path. Caller-provided hashes and self-reported flags do not
    authenticate a process, Host, GPU, wire request or approved operator.
    """
    require(native_expected_sha256 != managed_expected_sha256,
            "receipt-roles-must-not-share-hash")
    native = _bounded_object(
        native_receipt_bytes, native_expected_sha256, role="native",
    )
    managed = _bounded_object(
        managed_receipt_bytes, managed_expected_sha256, role="managed",
    )
    require(set(native) == _NATIVE_FIELDS and native.get("schema") == NATIVE_SCHEMA
            and native.get("host") == "spica"
            and native.get("native_context_target") == 12288
            and type(native.get("native_context_target")) is int
            and native.get("canary_unit") ==
            "tsumgi-bonsai12k-spica-native-20261109-r2.service",
            "native-r2-schema-context-or-unit-invalid")
    for key in (
        "worker_stop_attempted", "worker_originally_stopped",
        "old_gpu_released_before_12k", "canary_started",
        "tests_all_pass", "original_8k_worker_restored", "live_rollback_tested",
    ):
        require(native[key] is True, "native-r2-acceptance-flag-invalid-" + key)
    for key in (
        "deneb_touched", "control_catalog_changed",
        "gateway_12k_qualified", "native_canary_promoted_to_service",
        "semantic_quality_qualified", "continuous_vram_floor_proven",
    ):
        require(native[key] is False, "native-r2-overclaimed-flag-invalid-" + key)
    require(native.get("reason") is None
            and type(native.get("performed_tests")) is list
            and native["performed_tests"] == list(NATIVE_TESTS),
            "native-r2-test-sequence-or-error-invalid")

    require(set(managed) == _MANAGED_FIELDS
            and managed.get("schema_version") == MANAGED_SCHEMA
            and managed.get("status") == "MANAGED_NATIVE_SMOKE_ONLY_RESTORED_8K"
            and type(managed.get("reported_tests")) is list
            and managed["reported_tests"] == list(MANAGED_TESTS),
            "managed-r15-schema-status-or-tests-invalid")
    require(managed.get("restored_original_8k") is True
            and managed.get("managed_12k_source_only_no_serving_advertised") is True
            and managed.get("error") is None,
            "managed-r15-rollback-or-failure-invalid")
    for key in (
        "control_catalog_modified", "native_request_bodies_independently_attested",
        "new_12k_serving_revision_issued", "permanent_12k_promotion",
    ):
        require(managed[key] is False, "managed-r15-overclaimed-flag-invalid-" + key)

    n_vram = _vram(native["vram"], role="native")
    m_vram = _vram(managed["vram"], role="managed")
    return {
        "schema_version": SCHEMA,
        "status": STATUS,
        "host_reported_only": "spica",
        "native_r2_receipt_sha256": native_expected_sha256,
        "managed_r15_receipt_sha256": managed_expected_sha256,
        "native_r2_reported_tests": list(NATIVE_TESTS),
        "managed_r15_reported_tests": list(MANAGED_TESTS),
        "native_r2_vram_summary_reported": n_vram,
        "managed_r15_vram_summary_reported": m_vram,
        "source_receipts_internally_consistent_only": True,
        "receipt_origins_independently_authenticated": False,
        "actual_http_request_bytes_captured_and_attested": False,
        "managed12k_serving_advertised_and_claim_tested": False,
        "gateway12k_tool_stream_cancel_e2e_qualified": False,
        "current_spica_8k_readback_verified_by_this_parser": False,
        "safe_live_managed_promotion_authorized": False,
        "serving_contract_evidence_digest_issued": False,
        "deployment_execution_digest_issued": False,
        "control_profile_published": False,
        "ready_merge_deploy_authorized": False,
    }
