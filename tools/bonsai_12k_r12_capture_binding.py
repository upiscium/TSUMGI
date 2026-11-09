"""#232 R12: pure, source-only prospective Native evidence consistency gate.

Checks supplied in-memory bytes and stage-tagged samples. It cannot prove
the bytes were actually sent to a Native process or that NVML rows came from
the claimed GPU. Never writes, connects, runs Native, or mints Serving IDs.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from itertools import pairwise
from typing import Any

from bonsai_12k_r6_native_probes import canonical_bytes, validate_sse_trace
from bonsai_12k_r11_execution_review import SCHEMA as R11_SCHEMA

SCHEMA = "bonsai-spica-r12-prospective-capture-review-v1"
STATUS = "BLOCKED_UNAUTHENTICATED_NATIVE_AND_MANAGED_ADMISSION"
PHASES = ("cold_long", "tool_stream", "tool_result_stream", "cancel_stream")
_STAGE_COMMON = {"stage", "start_ms", "end_ms", "request_sha256", "native_context"}
_COLD_KEYS = _STAGE_COMMON | {
    "http_status", "native_prompt_tokens", "no_cache_prompt",
    "native_prompt_processing_ms",
}
_STREAM_KEYS = _STAGE_COMMON | {"http_status"}
_CANCEL_KEYS = _STAGE_COMMON | {
    "http_status", "client_disconnected", "processing_before_disconnect",
    "slot_idle_after_disconnect", "slot_quiescence_ms",
    "disconnect_at_ms", "slot_idle_at_ms",
}
_SAMPLE_KEYS = {"time_ms", "stage", "gpu_uuid", "free_mib"}
_GPU = re.compile(r"GPU-[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_MIN_SAMPLES_PER_STAGE = 16
_MAX_SAMPLING_GAP_MS = 1000


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise ValueError(reason)


def _hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _uint(value: object, *, minimum: int = 0, maximum: int = 2**63 - 1) -> bool:
    return type(value) is int and minimum <= value <= maximum


def _r11_identity(r11: dict[str, Any]) -> tuple[str, str]:
    require(type(r11) is dict and r11.get("schema_version") == R11_SCHEMA
            and r11.get("status") == "BLOCKED_UNATTESTED_EXECUTION_AND_MANAGED_WORKER"
            and r11.get("reported_hashes_independently_verified") is False
            and r11.get("host_and_gpu_origin_authenticated") is False
            and r11.get("managed_worker_12k_tested") is False
            and r11.get("serving_revision_issued") is False
            and r11.get("canary_activation_authorized") is False,
            "unqualified-r11-review-required")
    record = r11.get("record_only")
    fp = r11.get("review_fingerprint_sha256_not_a_deployment_identity")
    require(type(record) is dict
            and record.get("profile_id") == "bonsai2-27b-12k"
            and record.get("worker_id") == "bonsai-spica"
            and record.get("parallel_slots") == 1
            and record.get("limits") == {
                "input_tokens": 10240, "output_tokens": 2048,
                "total_tokens": 12288, "request_bytes": 65536,
            }, "unqualified-r11-profile-mismatch")
    gpu = record.get("gpu_uuid")
    require(type(gpu) is str and _GPU.fullmatch(gpu) is not None,
            "r11-gpu-identity-invalid")
    require(type(fp) is str and _SHA.fullmatch(fp) is not None,
            "r11-review-fingerprint-invalid")
    try:
        encoded = json.dumps(
            record, sort_keys=True, separators=(",", ":"),
            ensure_ascii=True, allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, OverflowError, RecursionError) as exc:
        raise ValueError("r11-record-not-canonical") from exc
    require(0 < len(encoded) <= 16_384
            and _hash(encoded) == fp,
            "r11-record-fingerprint-mismatch")
    return gpu, fp


def _body_bytes(
    requests: dict[str, bytes], tool_sse: bytes,
) -> dict[str, str]:
    require(type(requests) is dict and set(requests) == set(PHASES),
            "request-phase-set-invalid")
    digests: dict[str, str] = {}
    for stage in PHASES:
        raw = requests[stage]
        require(type(raw) is bytes and 0 < len(raw) <= 65536,
                "request-bytes-invalid")
        expected = canonical_bytes(
            stage, **({"tool_call_trace": tool_sse}
                       if stage == "tool_result_stream" else {})
        )
        require(raw == expected, "raw-request-must-match-approved-synthetic-recipe")
        digests[stage] = _hash(raw)
    return digests


def inspect_prospective_capture(
    *, r11_review: dict[str, Any],
    raw_http_requests: dict[str, bytes],
    tool_sse: bytes, final_sse: bytes, cancel_sse: bytes,
    stages: list[dict[str, Any]],
    gpu_samples: list[dict[str, Any]],
) -> dict[str, object]:
    """Review internal consistency ONLY; no authentic Origin or Native proof.

    All asserted HTTP, Native, client disconnect, and GPU sample facts are
    caller-controlled. A coherent forged fixture will pass this *structural*
    check; real attestation is an independent later requirement.
    """
    gpu, fingerprint = _r11_identity(r11_review)
    # Structural stream verification never proves the SSE came from Spica.
    tool = validate_sse_trace(tool_sse, phase="tool_stream")
    final = validate_sse_trace(final_sse, phase="tool_result_stream")
    cancel = validate_sse_trace(cancel_sse, phase="cancel_stream")
    require(tool["structured_tool_call_complete"] is True
            and final["content_fragment_seen"] is True
            and tool["done"] is True and final["done"] is True
            and cancel["done"] is False,
            "stream-structure-incomplete")
    request_digests = _body_bytes(raw_http_requests, tool_sse)

    require(type(stages) is list and len(stages) == len(PHASES)
            and all(type(s) is dict for s in stages)
            and tuple(s.get("stage") for s in stages) == PHASES,
            "stage-count-order-invalid")
    previous_end = -1
    stage_ranges: dict[str, tuple[int, int]] = {}
    for stage, entry in zip(PHASES, stages, strict=True):
        required = (_COLD_KEYS if stage == "cold_long" else
                    _CANCEL_KEYS if stage == "cancel_stream" else
                    _STREAM_KEYS)
        require(set(entry) == required, "stage-fields-invalid")
        start, end = entry["start_ms"], entry["end_ms"]
        require(_uint(start) and _uint(end) and start < end
                and start > previous_end, "stage-intervals-overlap-or-invalid")
        require(end - start >= _MIN_SAMPLES_PER_STAGE - 1,
                "stage-duration-too-short")
        previous_end = end
        stage_ranges[stage] = (start, end)
        require(entry["request_sha256"] == request_digests[stage]
                and type(entry["native_context"]) is int
                and entry["native_context"] == 12288,
                "stage-request-or-runtime-identity-mismatch")
        if stage == "cold_long":
            require(entry["http_status"] == 200
                    and type(entry["http_status"]) is int
                    and type(entry["native_prompt_tokens"]) is int
                    and entry["native_prompt_tokens"] == 10231
                    and entry["no_cache_prompt"] is True
                    and type(entry["native_prompt_processing_ms"]) in (int, float)
                    and math.isfinite(entry["native_prompt_processing_ms"])
                    and 0 < entry["native_prompt_processing_ms"]
                    <= (end - start) + 1000,
                    "cold-prefill-shape-invalid")
        elif stage == "cancel_stream":
            require(entry["http_status"] is None
                    and entry["client_disconnected"] is True
                    and entry["processing_before_disconnect"] is True
                    and entry["slot_idle_after_disconnect"] is True
                    and _uint(entry["slot_quiescence_ms"], minimum=1, maximum=40000)
                    and _uint(entry["disconnect_at_ms"])
                    and _uint(entry["slot_idle_at_ms"])
                    and start <= entry["disconnect_at_ms"]
                    < entry["slot_idle_at_ms"] <= end
                    and (entry["slot_idle_at_ms"] - entry["disconnect_at_ms"]
                         == entry["slot_quiescence_ms"]),
                    "cancel-observations-inconsistent")
        else:
            require(type(entry["http_status"]) is int
                    and entry["http_status"] == 200,
                    "http-stage-not-successful")

    require(type(gpu_samples) is list
            and 64 <= len(gpu_samples) <= 5000,
            "sample-count-unqualified")
    previous_time = -1
    grouped: dict[str, list[tuple[int, int]]] = {stage: [] for stage in PHASES}
    for row in gpu_samples:
        require(type(row) is dict and set(row) == _SAMPLE_KEYS,
                "sample-fields-invalid")
        stage = row["stage"]
        stamp = row["time_ms"]
        free = row["free_mib"]
        require(type(stage) is str and stage in stage_ranges
                and row["gpu_uuid"] == gpu
                and type(row["gpu_uuid"]) is str
                and _uint(stamp) and stamp > previous_time
                and _uint(free, minimum=2048, maximum=11264),
                "sample-device-time-or-vram-floor-invalid")
        begin, end = stage_ranges[stage]
        require(begin <= stamp <= end, "sample-outside-reported-stage")
        previous_time = stamp
        grouped[stage].append((stamp, free))

    for stage, samples in grouped.items():
        begin, end = stage_ranges[stage]
        require(len(samples) >= _MIN_SAMPLES_PER_STAGE,
                "stage-sampling-insufficient")
        stamps = [stamp for stamp, _ in samples]
        require(stamps[0] - begin <= _MAX_SAMPLING_GAP_MS
                and end - stamps[-1] <= _MAX_SAMPLING_GAP_MS
                and all(b - a <= _MAX_SAMPLING_GAP_MS
                        for a, b in pairwise(stamps)),
                "stage-sampling-gap-unqualified")
    minimum = min(row["free_mib"] for row in gpu_samples)
    return {
        "schema_version": SCHEMA,
        "status": STATUS,
        "r11_claim_fingerprint_not_execution_identity": fingerprint,
        "requested_model_id": "bonsai2-27b-12k",
        "reported_spica_gpu_uuid": gpu,
        "request_body_digests": request_digests,
        "request_bodies_match_synthetic_recipes_only": True,
        "structured_sse_consistent_only": True,
        "gpu_sample_tags_and_stage_windows_consistent_only": True,
        "sampled_min_free_mib_from_caller": minimum,
        "sample_count_from_caller": len(gpu_samples),
        "actual_request_bodies_independently_captured": False,
        "native_process_and_host_authenticated": False,
        "gpu_uuid_cryptographically_authenticated": False,
        "managed_worker_12k_qualified": False,
        "independent_provenance_verified": False,
        "spica_8k_live_restore_reverified": False,
        "new_runtime_epoch_issued": False,
        "serving_revision_issued": False,
        "control_catalog_modified": False,
        "canary_activation_authorized": False,
        "ready_or_merge_authorized": False,
    }
