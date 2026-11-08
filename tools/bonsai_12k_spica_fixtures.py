"""Offline synthetic Bonsai 12K canary request recipes (#232).

This module constructs JSON only. Import/CLI never calls a model, contacts
a Worker/Gateway, starts a process or writes a file. Native tokenizer counts
recorded at 8K are observations, NOT 12K inference/VRAM qualification.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from typing import Any

_FRAGMENT = "zero one two three four five six seven eight nine. "
_MODES = ("long_safe", "long", "tool")
_REPETITIONS = {"long_safe": 909, "long": 929, "tool": 687}
_OBSERVED_8K_NATIVE_TOKENS = {
    "long_safe": 10011, "long": 10231, "tool": 7842,
}
_MODEL_ALIAS = "astrumweaver"


def make_request(mode: str, *, model: str = _MODEL_ALIAS) -> dict[str, Any]:
    """Build the exact reproducible synthetic tokenizer-only probe.

    A later *separately authorized* Native canary may reuse these requests
    as real completions after verifying model alias and tokenizer identity.
    """
    if mode not in _MODES:
        raise ValueError("unsupported synthetic Bonsai probe mode")
    if not isinstance(model, str) or not model or len(model) > 128:
        raise ValueError("invalid model alias")
    if mode in ("long_safe", "long"):
        return {
            "model": model,
            "messages": [{
                "role": "user", "content": _FRAGMENT * _REPETITIONS[mode],
            }],
            "max_tokens": 16,
        }
    return {
        "model": model,
        "messages": [{
            "role": "user",
            "content": (
                "For this synthetic tool-use request, call synthetic_echo "
                "only when answering. " + _FRAGMENT * 687
            ),
        }],
        "max_tokens": 32,
        "tools": [{
            "type": "function",
            "function": {
                "name": "synthetic_echo",
                "description": "Synthetic bounded tool verification only",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            },
        }],
        "tool_choice": {"type": "function", "function": {"name": "synthetic_echo"}},
    }


def request_bytes(mode: str, *, model: str = _MODEL_ALIAS) -> bytes:
    request = make_request(mode, model=model)
    data = json.dumps(request, separators=(",", ":"), ensure_ascii=True).encode()
    if len(data) > 65536:
        raise ValueError("synthetic request exceeds original Bonsai request byte cap")
    return data


def describe_request(mode: str, *, model: str = _MODEL_ALIAS) -> dict[str, object]:
    raw = request_bytes(mode, model=model)
    return {
        "schema": "bonsai-spica-synthetic-tokenizer-recipe-v1",
        "mode": mode,
        "request_bytes": len(raw),
        "request_sha256": hashlib.sha256(raw).hexdigest(),
        "repetitions": _REPETITIONS[mode],
        "observed_8k_native_input_tokens_not_12k_proof": (
            _OBSERVED_8K_NATIVE_TOKENS[mode]
        ),
        "not_native_prefill_or_completion": True,
        "not_production_qualified": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=_MODES, required=True)
    parser.add_argument(
        "--emit-request", action="store_true",
        help="Opt in to full synthetic JSON output. Default is a small hash receipt.",
    )
    args = parser.parse_args()
    if args.emit_request:
        print(request_bytes(args.mode).decode())
    else:
        print(json.dumps(describe_request(args.mode), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
