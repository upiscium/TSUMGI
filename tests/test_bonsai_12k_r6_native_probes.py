"""#232 R6 bounded source-only Native canary recipe contracts."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from bonsai_12k_r6_native_probes import (
    canonical_bytes,
    make_probe,
    preview,
    validate_sse_trace,
)


def test_cold_recipe_matches_previous_native_input_exactly():
    raw = canonical_bytes("cold_long")
    assert hashlib.sha256(raw).hexdigest() == (
        "af877ad92bed850b1a11ed57bb4d7908eaaec3d84f5d7f9f0955c4be0bd42eab"
    )
    assert len(raw) == 47461
    assert len(raw) < 65536
    request = json.loads(raw)
    assert request["max_tokens"] == 16
    assert len(request["messages"][0]["content"]) > 45000
    assert request["model"] == "astrumweaver"


def test_plan_fails_closed_before_external_approval_and_restoration():
    plan = preview()
    assert plan["status"] == "UNQUALIFIED_NO_NATIVE_OR_GATEWAY_ADMISSION"
    assert not plan["canary_activation_authorized"]
    assert not plan["spica_worker_stop_authorized"]
    assert not plan["gateway_profile_promotion_authorized"]
    assert plan["original_8k_restore_required"]
    assert plan["stages"][0]["id"] == "cold_long"
    assert plan["stages"][0]["must_be_first_in_fresh_native"]
    assert plan["stages"][0]["requires_no_cache_prompt_on_fresh_native"]
    assert plan["stages"][0]["prior_10k_prompt_reuse_forbidden"]
    assert plan["stages"][0]["native_expected_tokenizer_input"] == 10231
    assert plan["min_sampled_free_vram_mib"] == 2048
    assert plan["gateway_overlimit_rejection_deferred_until_profile_promotion"]
    assert plan["cold_cache_proof_requires_new_process_or_verified_cache_erase"]


def test_tool_stream_fixture_is_short_and_requests_explicit_call():
    request = make_probe("tool_stream")
    assert request["stream"] is True
    assert request["stream_options"] == {"include_usage": True}
    assert request["max_tokens"] == 128
    assert request["tool_choice"]["function"]["name"] == "synthetic_echo"
    assert len(request["messages"]) == 1
    assert len(canonical_bytes("tool_stream")) < 65536
    assert "127.0.0.1" not in canonical_bytes("tool_stream").decode()


def test_tool_result_round_trip_binds_the_observed_call_id_and_args():
    args = '{"text":"R6"}'
    first = make_probe("tool_stream")
    second = make_probe(
        "tool_result_stream", tool_call_trace=synthetic_tool_sse(),
    )
    assert first["messages"] == second["messages"][:1]
    assert second["messages"][1] == {
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "id": "call_R6",
            "type": "function",
            "function": {"name": "synthetic_echo", "arguments": args},
        }],
    }
    assert second["messages"][2] == {
        "role": "tool",
        "tool_call_id": "call_R6",
        "content": "synthetic_echo result: R6",
    }
    assert second["tool_choice"] == "none"
    assert len(canonical_bytes(
        "tool_result_stream", tool_call_trace=synthetic_tool_sse(),
    )) < 65536


@pytest.mark.parametrize(("call_id", "arguments"), [
    (None, '{"text":"R6"}'),
    ("1", '{"text":"R6"}'),
    ("call_!invalid", '{"text":"R6"}'),
    ("call_aaa", ""),
    ("call_aaa", '{"not_text":"R6"}'),
    ("call_aaa", '{"text":42}'),
    ("call_aaa", '{"text":"R6"} trailing'),
    ("call_aaa", '{"text":"R6","extra":true}'),
    ("call_aaa", '{"text":"' + "x" * 257 + '"}'),
    ("call_aaa", '{"text":"' + "x" * 2049 + '"}'),
])
def test_tool_result_must_not_invent_or_accept_unbounded_call_data(call_id, arguments):
    with pytest.raises(ValueError):
        make_probe(
            "tool_result_stream",
            tool_call_trace=synthetic_tool_sse_with(call_id, arguments),
        )


@pytest.mark.parametrize("phase", [
    "cold_long", "tool_stream", "cancel_stream",
])
def test_irrelevant_tool_identity_fails_closed(phase):
    with pytest.raises(ValueError, match="unused-tool-stream"):
        make_probe(phase, tool_call_trace=synthetic_tool_sse())


@pytest.mark.parametrize("phase", [
    "unsupported", "", None, False, 0,
])
def test_unknown_phase_fails_closed(phase):
    with pytest.raises(ValueError, match="unsupported-phase"):
        make_probe(phase)


def test_cancel_stream_recipe_is_only_a_request_not_cancellation_proof():
    req = make_probe("cancel_stream")
    assert req["stream"] is True
    assert req["max_tokens"] == 512
    assert req["messages"][0]["content"].startswith("zero one")
    assert len(req["messages"][0]["content"]) > 46000
    assert preview()["stages"][-1]["do_not_equate_disconnect_with_cancellation_proof"]


def frame(data):
    if data == "[DONE]":
        return b"data: [DONE]\n\n"
    return b"data: " + json.dumps(data, separators=(",", ":")).encode() + b"\n\n"


def synthetic_tool_sse_with(call_id, arguments, *, call_index=0):
    return b"".join([
        frame({"choices": [{"index": 0, "delta": {"tool_calls": [{
            "index": call_index, "id": call_id, "type": "function",
            "function": {"name": "synthetic_echo", "arguments": arguments},
        }]}, "finish_reason": "tool_calls"}]}),
        frame("[DONE]"),
    ])


def synthetic_tool_sse():
    return b"".join([
        frame({"choices": [{"index": 0, "delta": {"role": "assistant"},
                            "finish_reason": None}]}),
        frame({"choices": [{"index": 0, "delta": {"tool_calls": [{
            "index": 0, "id": "call_R6", "type": "function",
            "function": {"name": "synthetic_echo", "arguments": '{"text":'},
        }]}, "finish_reason": None}]}),
        frame({"choices": [{"index": 0, "delta": {"tool_calls": [{
            "index": 0, "function": {"arguments": '"R6"}'},
        }]}, "finish_reason": "tool_calls"}]}),
        frame("[DONE]"),
    ])


def test_synthetic_complete_tool_stream_trace_reports_structure_only():
    summary = validate_sse_trace(synthetic_tool_sse(), phase="tool_stream")
    assert summary["status"] == "SSE_STRUCTURE_ONLY_NO_NATIVE_ACCEPTANCE"
    assert summary["frames"] == 3
    assert summary["done"] is True
    assert summary["terminal_finish_reason_seen"] is True
    assert summary["tool_call_fragment_seen"] is True
    assert summary["structured_tool_call_complete"] is True
    assert summary["observed_tool_call"] == {
        "id": "call_R6",
        "name": "synthetic_echo",
        "arguments": '{"text":"R6"}',
    }
    assert summary["native_cancellation_proven"] is False
    assert summary["canary_activation_authorized"] is False


@pytest.mark.parametrize("reason", ["stop", "length"])
def test_incompatible_tool_call_terminal_reason_is_rejected(reason):
    trace = synthetic_tool_sse()
    original = frame({"choices": [{
        "index": 0, "delta": {"tool_calls": [{
            "index": 0, "function": {"arguments": '"R6"}'},
        }]}, "finish_reason": "tool_calls",
    }]})
    altered = frame({"choices": [{
        "index": 0, "delta": {"tool_calls": [{
            "index": 0, "function": {"arguments": '"R6"}'},
        }]}, "finish_reason": reason,
    }]})
    assert original in trace
    with pytest.raises(ValueError, match="tool-call-finish-reason-mismatch"):
        validate_sse_trace(trace.replace(original, altered), phase="tool_stream")


def test_length_truncated_tool_result_is_not_a_complete_round_trip():
    trace = frame({"choices": [{
        "index": 0, "delta": {"content": "partial"},
        "finish_reason": "length",
    }]}) + frame("[DONE]")
    with pytest.raises(ValueError, match="tool-result-finish-reason-mismatch"):
        validate_sse_trace(trace, phase="tool_result_stream")


def test_synthetic_final_stream_requires_content_and_done():
    trace = frame({"choices": [{
        "index": 0, "delta": {"content": "synthetic finished"},
        "finish_reason": "stop",
    }]}) + frame("[DONE]")
    summary = validate_sse_trace(trace, phase="tool_result_stream")
    assert summary["done"] and summary["content_fragment_seen"]
    assert summary["structured_tool_call_complete"] is False


def test_observed_tool_argument_is_echoed_in_followup_result():
    request = make_probe(
        "tool_result_stream",
        tool_call_trace=synthetic_tool_sse_with("call_other", '{"text":"other"}'),
    )
    assert request["messages"][2]["content"] == "synthetic_echo result: other"
    assert request["messages"][1]["tool_calls"][0]["id"] == "call_other"


def test_tool_result_cannot_claim_unobserved_stream_values():
    with pytest.raises(TypeError):
        make_probe(
            "tool_result_stream", call_id="call_unobserved",
            tool_arguments='{"text":"R6"}',
        )
    with pytest.raises(ValueError, match="tool-stream-required"):
        make_probe("tool_result_stream")


@pytest.mark.parametrize(("choice_index", "call_type", "message"), [
    (9, "function", "choice-index-invalid"),
    (True, "function", "choice-index-invalid"),
    (0, "notfunction", "tool-call-type-invalid"),
    (0, None, "missing-function-call-type"),
])
def test_malformed_streamed_tool_identity_fails_closed(
    choice_index, call_type, message,
):
    raw_call = {
        "index": 0,
        "id": "call_R6",
        "function": {"name": "synthetic_echo", "arguments": '{"text":"R6"}'},
    }
    if call_type is not None:
        raw_call["type"] = call_type
    trace = frame({"choices": [{
        "index": choice_index,
        "delta": {"tool_calls": [raw_call]},
        "finish_reason": "tool_calls",
    }]}) + frame("[DONE]")
    with pytest.raises(ValueError, match=message):
        validate_sse_trace(trace, phase="tool_stream")


@pytest.mark.parametrize("bad_index", [False, 0.0, 1, None])
def test_streamed_tool_call_index_requires_exact_zero_integer(bad_index):
    trace = synthetic_tool_sse_with(
        "call_R6", '{"text":"R6"}', call_index=bad_index,
    )
    with pytest.raises(ValueError, match="tool-call-index-invalid"):
        validate_sse_trace(trace, phase="tool_stream")


def test_no_choice_data_after_terminal_finish_reason():
    first = frame({"choices": [{
        "index": 0, "delta": {"content": "complete"},
        "finish_reason": "stop",
    }]})
    late = frame({"choices": [{
        "index": 0, "delta": {"content": "late"},
        "finish_reason": None,
    }]})
    with pytest.raises(ValueError, match="post-terminal-choice-frame"):
        validate_sse_trace(first + late + frame("[DONE]"),
                           phase="tool_result_stream")
    usage = frame({"choices": [], "usage": {
        "prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5,
    }})
    summary = validate_sse_trace(first + usage + frame("[DONE]"),
                                 phase="tool_result_stream")
    assert summary["done"] is True and summary["frames"] == 2


def test_disconnect_before_any_stream_event_is_not_native_cancellation_proof():
    summary = validate_sse_trace(b"", phase="cancel_stream")
    assert summary["frames"] == 0
    assert summary["done"] is False
    assert summary["native_cancellation_proven"] is False
    with pytest.raises(ValueError, match="empty-stream"):
        validate_sse_trace(b"", phase="tool_stream")


def test_disconnect_after_role_only_chunk_is_structurally_partial():
    trace = frame({"choices": [{
        "index": 0, "delta": {"role": "assistant"},
        "finish_reason": None,
    }]})
    summary = validate_sse_trace(trace, phase="cancel_stream")
    assert summary["frames"] == 1
    assert not summary["done"]
    assert not summary["native_cancellation_proven"]


def test_synthetic_cancel_partial_stream_is_not_cancellation_proof():
    trace = frame({"choices": [{
        "index": 0, "delta": {"content": "a"},
        "finish_reason": None,
    }]})
    report = validate_sse_trace(trace, phase="cancel_stream")
    assert report["done"] is False
    assert report["terminal_finish_reason_seen"] is False
    assert report["frames"] == 1
    assert report["native_cancellation_proven"] is False
    with pytest.raises(ValueError, match="cancel-must-not-be-completed"):
        validate_sse_trace(trace + frame("[DONE]"), phase="cancel_stream")


@pytest.mark.parametrize(("trace", "reason"), [
    (b"", "empty-stream"),
    (b"data: {}\n", "unterminated-frame"),
    (b"data: no-json\n\n", "invalid-frame-json"),
    (b"data: {}\n\n" + frame("[DONE]"), "choices-invalid"),
    (frame("[DONE]"), "no-content-or-tool"),
    (frame({"choices": [{"index": 0, "delta": {"content": "ok"}}]}), "missing-done"),
    (frame({"choices": [{"index": 0, "delta": {"content": "ok"}}]}) +
     frame("[DONE]") + frame("[DONE]"), "done-not-terminal"),
])
def test_bad_sse_streams_cannot_be_accepted(trace, reason):
    with pytest.raises(ValueError, match=reason):
        validate_sse_trace(trace, phase="tool_result_stream")


def test_stream_done_without_terminal_finish_reason_is_rejected():
    trace = frame({"choices": [{
        "index": 0, "delta": {"content": "partial"},
        "finish_reason": None,
    }]}) + frame("[DONE]")
    with pytest.raises(ValueError, match="missing-terminal-finish-reason"):
        validate_sse_trace(trace, phase="tool_result_stream")


def test_cancel_without_done_but_already_finished_is_not_cancellation():
    trace = frame({"choices": [{
        "index": 0, "delta": {"content": "completed"},
        "finish_reason": "stop",
    }]})
    with pytest.raises(ValueError, match="cancel-must-not-be-completed"):
        validate_sse_trace(trace, phase="cancel_stream")


def test_cli_receipts_and_plan_are_offline_and_write_nothing(tmp_path):
    cli = ROOT / "tools/bonsai_12k_r6_native_probes.py"
    for phase in ("plan", "cold_long", "tool_stream", "cancel_stream"):
        cmd = [sys.executable, str(cli), "--phase", phase]
        result = subprocess.run(
            cmd, cwd=tmp_path, capture_output=True, timeout=10, check=False,
        )
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        if phase == "plan":
            assert data["requires_independent_operator_approval_for_any_live_stop"]
        else:
            assert data["status"] == "SYNTHETIC_REQUEST_ONLY_NO_NETWORK"
            assert data["canary_activation_authorized"] is False
    assert list(tmp_path.iterdir()) == []


def test_cli_refuses_unbound_tool_result_without_receipt(tmp_path):
    cli = ROOT / "tools/bonsai_12k_r6_native_probes.py"
    proc = subprocess.run(
        [sys.executable, str(cli), "--phase", "tool_result_stream"],
        cwd=tmp_path, capture_output=True, timeout=10, check=False,
    )
    assert proc.returncode != 0
    assert b"tool-result-needs-actual-call-id-and-arguments" in proc.stderr
    assert list(tmp_path.iterdir()) == []
