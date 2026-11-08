"""#232 R6: pure, OFFLINE synthetic Native 12K acceptance probe recipes.

This module NEVER contacts a server, starts a process, writes files, changes
the Worker, or evaluates GPU admission. Native cold-prefill, Tool Result,
stream and disconnect tests need a SEPARATELY APPROVED Spica maintenance
window, with the original 8K service restored before declaring completion.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from copy import deepcopy
from typing import Any

from bonsai_12k_spica_fixtures import make_request

_MAX_REQUEST_BYTES = 65536
_EXPECTED_COLD_SHA256 = "af877ad92bed850b1a11ed57bb4d7908eaaec3d84f5d7f9f0955c4be0bd42eab"
_CALL_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_SYNTHETIC_TOOL = "synthetic_echo"
_PHASES = ("cold_long", "tool_stream", "tool_result_stream", "cancel_stream")


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise ValueError(reason)


def make_probe(
    phase: str, *,
    tool_call_trace: bytes | None = None,
) -> dict[str, Any]:
    """Build ONLY the request JSON; no network or lifecycle side effects."""
    _require(phase in _PHASES, "unsupported-phase")
    if phase == "cold_long":
        _require(tool_call_trace is None, "unused-tool-stream")
        request = make_request("long")
        payload = json.dumps(request, separators=(",", ":")).encode()
        _require(hashlib.sha256(payload).hexdigest() == _EXPECTED_COLD_SHA256,
                 "cold-fixture-identity-drift")
        return request

    if phase == "cancel_stream":
        _require(tool_call_trace is None, "unused-tool-stream")
        request = make_request("long_safe")
        request["stream"] = True
        request["stream_options"] = {"include_usage": True}
        request["max_tokens"] = 512
        return request

    # Use a SHORT prompt for Tool Result verification so it is independent
    # of the original 7,842-token tool-bearing capacity test.
    old = make_request("tool")
    request = {
        "model": "astrumweaver",
        "messages": [{
            "role": "user",
            "content": "Call synthetic_echo with text equal to R6, then summarize the tool result.",
        }],
        "tools": deepcopy(old["tools"]),
        "tool_choice": deepcopy(old["tool_choice"]),
        "max_tokens": 128,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if phase == "tool_stream":
        _require(tool_call_trace is None, "unused-tool-stream")
        return request
    _require(type(tool_call_trace) is bytes, "tool-stream-required")
    # Do not accept a separately asserted call ID/argument. Derive both from
    # a full, structurally validated Tool Call SSE capture supplied by a
    # future operator. This still does NOT attest that the capture is genuine.
    parsed_trace = validate_sse_trace(tool_call_trace, phase="tool_stream")
    observed = parsed_trace["observed_tool_call"]
    _require(type(observed) is dict, "observed-tool-call-missing")
    call_id = observed["id"]
    tool_arguments = observed["arguments"]
    arguments = json.loads(tool_arguments)
    request["messages"].extend([
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": call_id, "type": "function",
                "function": {
                    "name": _SYNTHETIC_TOOL,
                    "arguments": tool_arguments,
                },
            }],
        },
        {
            "role": "tool", "tool_call_id": call_id,
            "content": "synthetic_echo result: " + arguments["text"],
        },
    ])
    # Avoid forcing a second Tool Call after the Tool Result.
    request["tool_choice"] = "none"
    return request


def canonical_bytes(phase: str, **kwargs: Any) -> bytes:
    raw = json.dumps(
        make_probe(phase, **kwargs),
        separators=(",", ":"), ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    _require(len(raw) <= _MAX_REQUEST_BYTES, "request-byte-cap-exceeded")
    return raw


def preview() -> dict[str, Any]:
    """Expose design gates; NEVER grants live or production authority."""
    cold = canonical_bytes("cold_long")
    tool = canonical_bytes("tool_stream")
    cancel = canonical_bytes("cancel_stream")
    return {
        "schema": "bonsai-spica-12k-r6-source-only-plan-v1",
        "status": "UNQUALIFIED_NO_NATIVE_OR_GATEWAY_ADMISSION",
        "spica_worker_stop_authorized": False,
        "canary_activation_authorized": False,
        "gateway_profile_promotion_authorized": False,
        "original_8k_restore_required": True,
        "cached_cold_prefill_claim_prohibited": True,
        "native_context_total": 12288,
        "input_cap": 10240,
        "output_cap": 2048,
        "min_sampled_free_vram_mib": 2048,
        "stages": [
            {
                "id": "cold_long",
                "must_be_first_in_fresh_native": True,
                "requires_no_cache_prompt_on_fresh_native": True,
                "prior_10k_prompt_reuse_forbidden": True,
                "request_sha256": hashlib.sha256(cold).hexdigest(),
                "native_expected_tokenizer_input": 10231,
                "must_record_uncached_prompt_tokens_and_timing": True,
            },
            {
                "id": "tool_stream",
                "request_sha256": hashlib.sha256(tool).hexdigest(),
                "require_stream_done": True,
                "require_structured_tool_call": True,
            },
            {
                "id": "tool_result_stream",
                "only_after_actual_tool_call_id_and_arguments": True,
                "require_correlated_tool_result_message": True,
                "require_followup_stream_done": True,
            },
            {
                "id": "cancel_stream",
                "request_sha256": hashlib.sha256(cancel).hexdigest(),
                "require_explicit_client_disconnect": True,
                "require_native_slot_quiescent_after_disconnect": True,
                "do_not_equate_disconnect_with_cancellation_proof": True,
            },
        ],
        "gateway_overlimit_rejection_deferred_until_profile_promotion": True,
        "native_tokenizer_count_is_not_gateway_admission": True,
        "cold_cache_proof_requires_new_process_or_verified_cache_erase": True,
        "sampling_is_not_continuous_vram_guarantee": True,
        "requires_independent_operator_approval_for_any_live_stop": True,
    }


def validate_sse_trace(raw: bytes, *, phase: str) -> dict[str, object]:
    """Inspect synthetic/provided SSE bytes, never attest a live outcome.

    Full stream requires [DONE] and meaningful Tool Call or final content.
    Cancel stream requires absence of [DONE] but does *not* prove that
    Native stopped computing after an HTTP client disconnection.
    """
    _require(phase in ("tool_stream", "tool_result_stream", "cancel_stream"),
             "unsupported-trace-phase")
    _require(type(raw) is bytes and len(raw) <= 1_048_576,
             "invalid-stream")
    # Disconnect may precede the *first* Native event. An empty capture is
    # valid as a partial trace only, and never proves Native cancellation.
    if phase == "cancel_stream" and not raw:
        return {
            "status": "SSE_STRUCTURE_ONLY_NO_NATIVE_ACCEPTANCE",
            "phase": phase, "frames": 0, "done": False,
            "terminal_finish_reason_seen": False,
            "observed_tool_call": None,
            "tool_call_fragment_seen": False,
            "structured_tool_call_complete": False,
            "content_fragment_seen": False,
            "native_cancellation_proven": False,
            "canary_activation_authorized": False,
        }
    _require(len(raw) > 0, "empty-stream")
    data = raw.replace(b"\r\n", b"\n")
    _require(data.endswith(b"\n\n"), "unterminated-frame")
    segments = data.split(b"\n\n")[:-1]
    _require(all(segments), "empty-sse-frame")
    frames: list[dict[str, Any]] = []
    done = False
    call_id = None
    call_name = None
    argument_fragments: list[str] = []
    tool_seen = False
    function_type_seen = False
    content_seen = False
    terminal_reasons: set[str] = set()
    for i, segment in enumerate(segments):
        _require(segment.startswith(b"data: "), "invalid-frame-prefix")
        _require(b"\n" not in segment, "unexpected-multiline-event")
        value = segment[len(b"data: "):]
        if value == b"[DONE]":
            _require(not done and i == len(segments) - 1, "done-not-terminal")
            done = True
            continue
        _require(not done, "done-not-terminal")
        try:
            decoded = json.loads(value)
        except (UnicodeError, ValueError) as exc:
            raise ValueError("invalid-frame-json") from exc
        _require(type(decoded) is dict and type(decoded.get("choices")) is list,
                 "choices-invalid")
        _require(len(decoded["choices"]) <= 1, "multiple-choices-unqualified")
        if decoded["choices"]:
            _require(not terminal_reasons, "post-terminal-choice-frame")
        frames.append(decoded)
        for choice in decoded["choices"]:
            _require(type(choice) is dict, "choice-invalid")
            _require(type(choice.get("index")) is int
                     and choice["index"] == 0, "choice-index-invalid")
            delta = choice.get("delta")
            _require(type(delta) is dict, "delta-invalid")
            reason = choice.get("finish_reason")
            _require(reason is None or reason in ("stop", "length", "tool_calls"),
                     "unsupported-finish-reason")
            if reason is not None:
                terminal_reasons.add(reason)
            if delta.get("content") is not None:
                _require(type(delta["content"]) is str, "content-invalid")
                if delta["content"]:
                    content_seen = True
            tool_calls = delta.get("tool_calls", [])
            _require(type(tool_calls) is list, "tool-call-chunk-invalid")
            for call in tool_calls:
                _require(
                    type(call) is dict
                    and type(call.get("index")) is int
                    and call["index"] == 0,
                    "tool-call-index-invalid",
                )
                tool_seen = True
                if call.get("type") is not None:
                    _require(call["type"] == "function", "tool-call-type-invalid")
                    function_type_seen = True
                if call.get("id") is not None:
                    _require(type(call["id"]) is str, "call-id-invalid")
                    _require(call_id is None or call_id == call["id"],
                             "call-id-conflict")
                    call_id = call["id"]
                fn = call.get("function")
                _require(type(fn) is dict, "tool-function-invalid")
                if fn.get("name") is not None:
                    _require(type(fn["name"]) is str, "tool-name-invalid")
                    _require(call_name is None or call_name == fn["name"],
                             "tool-name-conflict")
                    call_name = fn["name"]
                if fn.get("arguments") is not None:
                    _require(type(fn["arguments"]) is str,
                             "tool-argument-fragment-invalid")
                    argument_fragments.append(fn["arguments"])
    if phase == "cancel_stream":
        _require(not done and not terminal_reasons,
                 "cancel-must-not-be-completed")
    else:
        _require(frames and (tool_seen or content_seen), "no-content-or-tool")
        _require(done, "missing-done")
        _require(terminal_reasons, "missing-terminal-finish-reason")
    completed_call = False
    if tool_seen:
        _require(function_type_seen, "missing-function-call-type")
        _require(type(call_id) is str and _CALL_ID.fullmatch(call_id) is not None,
                 "tool-call-id-invalid")
        _require(call_name == _SYNTHETIC_TOOL, "unexpected-tool-name")
        arguments = "".join(argument_fragments)
        _require(0 < len(arguments) <= 2048, "tool-argument-total-invalid")
        try:
            parsed = json.loads(arguments)
        except (ValueError, UnicodeError) as exc:
            raise ValueError("tool-argument-json-incomplete") from exc
        _require(type(parsed) is dict and set(parsed) == {"text"}
                 and type(parsed["text"]) is str and len(parsed["text"]) <= 256,
                 "tool-arguments-invalid")
        completed_call = True
    if phase == "tool_stream":
        _require(completed_call, "missing-structured-tool-call")
        _require(terminal_reasons == {"tool_calls"},
                 "tool-call-finish-reason-mismatch")
    if phase == "tool_result_stream":
        _require(content_seen and not tool_seen, "missing-final-content")
        _require(terminal_reasons == {"stop"},
                 "tool-result-finish-reason-mismatch")
    return {
        "status": "SSE_STRUCTURE_ONLY_NO_NATIVE_ACCEPTANCE",
        "phase": phase,
        "frames": len(frames),
        "done": done,
        "terminal_finish_reason_seen": bool(terminal_reasons),
        "observed_tool_call": (
            {"id": call_id, "name": call_name, "arguments": arguments}
            if completed_call else None
        ),
        "tool_call_fragment_seen": tool_seen,
        "structured_tool_call_complete": completed_call,
        "content_fragment_seen": content_seen,
        "native_cancellation_proven": False,
        "canary_activation_authorized": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--phase", choices=(*_PHASES, "plan"), default="plan",
    )
    parser.add_argument("--emit-request", action="store_true")
    args = parser.parse_args()
    if args.phase == "plan":
        _require(not args.emit_request, "plan-has-no-request")
        print(json.dumps(preview(), sort_keys=True))
        return 0
    _require(args.phase != "tool_result_stream",
             "tool-result-needs-actual-call-id-and-arguments")
    raw = canonical_bytes(args.phase)
    if args.emit_request:
        print(raw.decode())
    else:
        print(json.dumps({
            "status": "SYNTHETIC_REQUEST_ONLY_NO_NETWORK",
            "phase": args.phase,
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "canary_activation_authorized": False,
        }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
