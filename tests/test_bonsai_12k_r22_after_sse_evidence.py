"""R22 synthetic post-first-content cancellation boundary, zero live I/O."""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from bonsai_12k_r22_after_sse_evidence import (
    first_content_frame,
    inspect_after_first_content_cancel,
)

LAB_WORKER = "bonsai-spica-r17-lab"
LAB_MODEL = "bonsai2-27b-12k-lab"
DEP = "sha256:" + "a" * 64
CONTRACT = "sha256:" + "b" * 64
EPOCH = "d52ba5b4-c2b1-4f52-8661-e726a6e3a068"


def raw_line(*, content="Hello", reason=None, index=0, delta=None):
    return (
        "data: " + json.dumps({
            "choices": [{"index": index, "delta": (
                {"content": content} if delta is None else delta
            ), "finish_reason": reason}],
        }, separators=(",", ":")) + "\n"
    ).encode()


def worker(active_jobs):
    return {
        "worker_id": LAB_WORKER, "state": "online",
        "active_jobs": active_jobs, "has_serving": True,
        "deployment_revision": DEP,
        "runtime_instance_epoch": EPOCH,
    }


def job(status, assigned):
    return {
        "job_id": "synthetic-job",
        "status": status, "assigned_worker_id": assigned,
        "serving_bound": True,
        "claimed_deployment_revision": DEP,
        "claimed_serving_contract_revision": CONTRACT,
        "claimed_runtime_instance_epoch": EPOCH,
    }


def case():
    return {
        "sender_body": json.dumps({
            "model": LAB_MODEL, "stream": True,
            "messages": [{"role": "user", "content": "Describe 20 scenes."}],
            "max_tokens": 2048,
            "stream_options": {"include_usage": True},
        }).encode(),
        "first_content_line": raw_line(),
        "worker_before": worker(1), "job_before": job("running", LAB_WORKER),
        "native_before": [{"n_ctx": 12288, "is_processing": True}],
        "worker_after": worker(0), "job_after": job("cancelled", None),
        "native_after": [{"n_ctx": 12288, "is_processing": False}],
        "health_after": {"ready": True, "active_job_id": None},
        "stable_worker": worker(0), "stable_job": job("cancelled", None),
        "stable_native": [{"n_ctx": 12288, "is_processing": False}],
        "stable_health": {"ready": True, "active_job_id": None},
        "staged_deployment": DEP, "staged_contract": CONTRACT,
        "client_killed_after_first_content": True, "http_status": 200,
    }


def check(**kwargs):
    inputs = case()
    inputs.update(kwargs)
    return inspect_after_first_content_cancel(**inputs)


def test_positive_is_structural_only_never_production_or_physical_acceptance():
    data = case()
    before = copy.deepcopy(data)
    result = inspect_after_first_content_cancel(**data)
    assert data == before
    assert result["status"] == "AFTER_FIRST_CONTENT_CANCELLATION_STRUCTURALLY_CONSISTENT_ONLY"
    assert result["first_content_frame"]["nonempty_generated_content_seen"] is True
    assert result["first_content_frame"]["generated_content_chars"] == 5
    assert result["reported_terminal_claim"]["status"] == "cancelled"
    for key in (
        "actual_tcp_close_independently_verified",
        "ordering_and_timestamps_independently_attested",
        "source_and_GPU_origin_authenticated",
        "complete_sender_wire_bytes_authenticated",
        "after_first_sse_physical_acceptance_issued",
        "production_12k_serving_qualified",
        "control_catalog_modified",
    ):
        assert result[key] is False


@pytest.mark.parametrize("line", [
    b"", b"data: [DONE]\n", b"data: {}\n",
    b"event: message\n", b"data: not-json\n",
    b"data: {\"choices\":[]}\n",
    b"data: {\"choices\":[{},{}]}\n",
    b"data: {\"choices\":[{\"index\":0,\"delta\":{\"content\":\"a\"}}]}\n",
])
def test_invalid_or_noncontent_sse_frame_cannot_authorize_disconnect(line):
    with pytest.raises(ValueError):
        first_content_frame(raw_sse_data_line=line)


@pytest.mark.parametrize("delta", [
    {"role": "assistant"},
    {"content": ""},
    {"content": " \t"},
    {"reasoning_content": "thinking"},
    {"content": None},
    {"content": "a", "tool_calls": [{"index": 0}]},
    {"content": 1},
])
def test_role_reasoning_tool_or_empty_deltas_are_not_generated_text(delta):
    with pytest.raises(ValueError):
        first_content_frame(raw_sse_data_line=raw_line(delta=delta))


@pytest.mark.parametrize(("field", "value"), [
    ("reason", "stop"),
    ("reason", "length"),
    ("index", 1),
    ("index", True),
])
def test_terminal_or_wrong_index_frame_rejected(field, value):
    kwargs = {field: value}
    with pytest.raises(ValueError):
        first_content_frame(raw_sse_data_line=raw_line(**kwargs))


def test_duplicate_json_keys_in_sse_rejected():
    data = b'data: {"choices":[{"index":0,"delta":{"content":"yes","content":"no"},"finish_reason":null}]}\n'
    with pytest.raises(ValueError, match="sse-content-frame-json-invalid"):
        first_content_frame(raw_sse_data_line=data)


@pytest.mark.parametrize(("key", "value", "error"), [
    ("http_status", 503, "sender-not-killed"),
    ("http_status", True, "sender-not-killed"),
    ("client_killed_after_first_content", False, "sender-not-killed"),
    ("client_killed_after_first_content", 1, "sender-not-killed"),
    ("native_before", [{"n_ctx": 8192, "is_processing": True}], "first-content-not-observed"),
    ("native_before", [{"n_ctx": 12288, "is_processing": False}], "first-content-not-observed"),
    ("native_before", [], "first-content-not-observed"),
    ("native_after", [{"n_ctx": 12288, "is_processing": True}], "native-or-worker-not-stably-idle"),
    ("stable_native", [{"n_ctx": 8192, "is_processing": False}], "native-or-worker-not-stably-idle"),
    ("health_after", {"ready": False, "active_job_id": None}, "native-or-worker-not-stably-idle"),
    ("stable_health", {"ready": True, "active_job_id": "job"}, "native-or-worker-not-stably-idle"),
    ("job_after", {"job_id": "other", "status": "cancelled"}, "cancel-stable-job-or-worker-changed"),
])
def test_invalid_http_claim_native_lease_or_idle_fails(key, value, error):
    data = case()
    data[key] = value
    with pytest.raises(ValueError, match=error):
        inspect_after_first_content_cancel(**data)


@pytest.mark.parametrize(("where", "field", "bad"), [
    ("job_before", "status", "succeeded"),
    ("job_before", "assigned_worker_id", None),
    ("job_before", "claimed_runtime_instance_epoch", "wrong"),
    ("job_after", "status", "succeeded"),
    ("job_after", "assigned_worker_id", LAB_WORKER),
    ("job_after", "claimed_serving_contract_revision", "wrong"),
    ("stable_job", "status", "running"),
    ("worker_after", "state", "offline"),
    ("worker_after", "active_jobs", 1),
    ("stable_worker", "runtime_instance_epoch", "wrong"),
])
def test_stale_or_changed_job_claim_is_denied(where, field, bad):
    data = case()
    data[where][field] = bad
    with pytest.raises(ValueError):
        inspect_after_first_content_cancel(**data)


@pytest.mark.parametrize("mutate", [
    {"model": "bonsai2-27b"},
    {"stream": False},
    {"max_tokens": 16},
    {"max_tokens": 2049},
    {"messages": []},
])
def test_request_must_be_bounded_distinct_streaming_12k_lab(mutate):
    x = case()
    payload = json.loads(x["sender_body"])
    payload.update(mutate)
    x["sender_body"] = json.dumps(payload).encode()
    with pytest.raises(ValueError, match="request-after-first-sse-recipe-unqualified"):
        inspect_after_first_content_cancel(**x)


@pytest.mark.parametrize("bad_raw", [
    b'{"model":"bonsai2-27b-12k-lab","stream":true,"max_tokens":128,"messages":[{"role":"user","content":"Provide many words"}],"stream_options":{"include_usage":true},"temperature":NaN}',
    b'{"model":"bonsai2-27b-12k-lab","stream":true,"max_tokens":128,"messages":[{"role":"user","content":"Provide many words"}],"stream_options":{"include_usage":true},"temperature":Infinity}',
    b'{"model":"bonsai2-27b-12k-lab","model":"bonsai2-27b-12k-lab","stream":true,"max_tokens":128,"messages":[{"role":"user","content":"Provide many words"}],"stream_options":{"include_usage":true}}',
])
def test_nonfinite_or_duplicate_request_json_is_rejected(bad_raw):
    with pytest.raises(ValueError, match="request-json-invalid"):
        check(sender_body=bad_raw)


@pytest.mark.parametrize("changed", [
    {"messages": [{"role": "assistant", "content": "Generate 500 words."}]},
    {"messages": [{"role": "user", "content": ""}]},
    {"messages": [{"role": "user", "content": "short"}]},
    {"messages": [{"role": "user", "content": ["not a string"]}]},
    {"messages": [{"role": "user", "content": "Generate 500 words.", "tool_calls": []}]},
    {"messages": [{"role": "user", "content": "Generate 500 words."}, {"role": "user", "content": "More."}]},
    {"stream_options": {"include_usage": False}},
    {"stream_options": {"include_usage": 1}},
    {"stream_options": {}},
    {"tools": [{"type": "function"}]},
    {"max_tokens": True},
])
def test_message_schema_or_unreviewed_prompt_option_fails_closed(changed):
    data = case()
    x = json.loads(data["sender_body"])
    x.update(changed)
    with pytest.raises(ValueError, match="request-after-first-sse-recipe-unqualified"):
        check(sender_body=json.dumps(x).encode())
