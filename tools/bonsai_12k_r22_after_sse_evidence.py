"""#232 R22: source-only gate for *after-first-SSE-content* cancellation.

The R21 real prefill TCP disconnect was accepted physically, but no generated
SSE content chunk was observed before the kill. A R22 future canary must
supply actual first content bytes, a claimed running Job, GPU Native busy
observation, then the same durable CANCELLED Job and idle/Ready readback.

Pure checks only: this module does not open sockets, process files, run GPUs,
authenticate the sender or mint Serving/Control identities.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from bonsai_12k_r20_cancel_gateway import verify_cancellation_claim

SCHEMA = "bonsai-spica-r22-after-first-sse-cancel-review-only-v1"
STATUS = "AFTER_FIRST_CONTENT_CANCELLATION_STRUCTURALLY_CONSISTENT_ONLY"
LAB_MODEL = "bonsai2-27b-12k-lab"
_REQ_MAX = 65536
_FRAME_MAX = 32768


def require(value: bool, code: str) -> None:
    if not value:
        raise ValueError(code)


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for k, v in pairs:
        require(k not in value, "sse-duplicate-json-key")
        value[k] = v
    return value


def first_content_frame(*, raw_sse_data_line: bytes) -> dict[str, Any]:
    """Validate an observed *whole* SSE line with non-empty generated content.

    An SSE header, role delta, reasoning-only delta, Tool Call, [DONE],
    usage-only event, or whitespace-only content is insufficient.
    """
    require(type(raw_sse_data_line) is bytes
            and 0 < len(raw_sse_data_line) <= _FRAME_MAX
            and raw_sse_data_line.startswith(b"data: ")
            and raw_sse_data_line.endswith(b"\n")
            and raw_sse_data_line not in (b"data: [DONE]\n", b"data: [DONE]\r\n"),
            "sse-content-frame-byte-shape-invalid")
    payload = raw_sse_data_line[6:].strip()
    try:
        event = json.loads(
            payload, object_pairs_hook=_unique_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(
                ValueError("sse-nonfinite-json")
            ),
        )
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError("sse-content-frame-json-invalid") from exc
    require(type(event) is dict
            and type(event.get("choices")) is list
            and len(event["choices"]) == 1,
            "sse-content-choice-shape-invalid")
    choice = event["choices"][0]
    require(type(choice) is dict
            and type(choice.get("index")) is int
            and choice["index"] == 0
            and "finish_reason" in choice
            and choice["finish_reason"] is None,
            "sse-content-choice-not-active")
    delta = choice.get("delta")
    require(type(delta) is dict and not delta.get("tool_calls"),
            "sse-content-delta-invalid")
    text = delta.get("content")
    require(type(text) is str and len(text) <= 16384 and text.strip() != "",
            "sse-first-generated-text-not-seen")
    return {
        "frame_sha256": "sha256:" + hashlib.sha256(raw_sse_data_line).hexdigest(),
        "frame_byte_length": len(raw_sse_data_line),
        "nonempty_generated_content_seen": True,
        "generated_content_chars": len(text),
        "terminal_sse_frame": False,
    }


def inspect_after_first_content_cancel(
    *,
    sender_body: bytes,
    first_content_line: bytes,
    worker_before: dict[str, Any],
    job_before: dict[str, Any],
    native_before: list[dict[str, Any]],
    worker_after: dict[str, Any],
    job_after: dict[str, Any],
    native_after: list[dict[str, Any]],
    health_after: dict[str, Any],
    stable_worker: dict[str, Any],
    stable_job: dict[str, Any],
    stable_native: list[dict[str, Any]],
    stable_health: dict[str, Any],
    staged_deployment: str,
    staged_contract: str,
    client_killed_after_first_content: bool,
    http_status: int,
) -> dict[str, Any]:
    """Verify caller-reported sequence without pretending to attest timing.

    A later production verdict additionally needs trusted time-bound source/
    process/host/GPU provenance and a physical TCP disconnect observation.
    """
    require(type(sender_body) is bytes and 0 < len(sender_body) <= _REQ_MAX,
            "request-body-invalid")
    try:
        request = json.loads(
            sender_body, object_pairs_hook=_unique_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(
                ValueError("request-nonfinite-json")
            ),
        )
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError("request-json-invalid") from exc
    require(type(request) is dict
            and set(request) <= {
                "model", "stream", "messages", "max_tokens",
                "stream_options",
            }
            and request.get("model") == LAB_MODEL
            and request.get("stream") is True
            and type(request.get("stream_options")) is dict
            and set(request["stream_options"]) == {"include_usage"}
            and request["stream_options"]["include_usage"] is True
            and type(request.get("messages")) is list
            and len(request["messages"]) == 1
            and type(request["messages"][0]) is dict
            and set(request["messages"][0]) == {"role", "content"}
            and request["messages"][0]["role"] == "user"
            and type(request["messages"][0]["content"]) is str
            and 16 <= len(request["messages"][0]["content"]) <= 16384
            and bool(request["messages"][0]["content"].strip())
            and type(request.get("max_tokens")) is int
            and 128 <= request["max_tokens"] <= 2048,
            "request-after-first-sse-recipe-unqualified")
    require(type(http_status) is int and http_status == 200
            and client_killed_after_first_content is True,
            "sender-not-killed-after-http200-and-first-content")
    first = first_content_frame(raw_sse_data_line=first_content_line)
    require(type(worker_before) is dict
            and worker_before.get("state") == "online"
            and worker_before.get("active_jobs") == 1
            and type(worker_before.get("active_jobs")) is int
            and type(native_before) is list and len(native_before) == 1
            and type(native_before[0]) is dict
            and type(native_before[0].get("n_ctx")) is int
            and native_before[0]["n_ctx"] == 12288
            and native_before[0].get("is_processing") is True,
            "first-content-not-observed-during-claimed-native-processing")
    require(type(native_after) is list and len(native_after) == 1
            and type(stable_native) is list and len(stable_native) == 1
            and all(
                type(row) is dict and type(row.get("n_ctx")) is int
                and row["n_ctx"] == 12288
                and row.get("is_processing") is False
                for row in (native_after[0], stable_native[0])
            )
            and all(
                type(row) is dict
                and row.get("ready") is True
                and row.get("active_job_id") is None
                for row in (health_after, stable_health)
            ),
            "native-or-worker-not-stably-idle-after-disconnect")
    require(type(worker_after) is dict and type(stable_worker) is dict
            and type(job_after) is dict and type(stable_job) is dict
            and worker_after.get("runtime_instance_epoch")
               == stable_worker.get("runtime_instance_epoch")
            and job_after.get("job_id") == stable_job.get("job_id"),
            "cancel-stable-job-or-worker-changed")
    claim = verify_cancellation_claim(
        before_job=job_before, after_job=job_after, worker=worker_after,
        staged_deployment=staged_deployment, staged_contract=staged_contract,
    )
    verify_cancellation_claim(
        before_job=job_before, after_job=stable_job, worker=stable_worker,
        staged_deployment=staged_deployment, staged_contract=staged_contract,
    )
    require(worker_before.get("worker_id") == worker_after.get("worker_id")
            and worker_before.get("deployment_revision") == staged_deployment
            and worker_before.get("runtime_instance_epoch")
               == worker_after.get("runtime_instance_epoch"),
            "pre-disconnect-worker-identity-drift")
    return {
        "schema_version": SCHEMA,
        "status": STATUS,
        "sender_body_sha256": "sha256:" + hashlib.sha256(sender_body).hexdigest(),
        "sender_body_byte_length": len(sender_body),
        "first_content_frame": first,
        "reported_terminal_claim": claim,
        "first_generated_text_observed_only_from_caller": True,
        "native_processing_and_worker_claim_consistent_only": True,
        "cancelled_and_stably_idle_consistent_only": True,
        "actual_tcp_close_independently_verified": False,
        "ordering_and_timestamps_independently_attested": False,
        "source_and_GPU_origin_authenticated": False,
        "complete_sender_wire_bytes_authenticated": False,
        "after_first_sse_physical_acceptance_issued": False,
        "production_12k_serving_qualified": False,
        "control_catalog_modified": False,
    }
