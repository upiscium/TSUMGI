"""Offline-only Bonsai synthetic canary fixtures, not a GPU test."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from bonsai_12k_spica_fixtures import (
    describe_request,
    make_request,
    request_bytes,
)

_EXPECTED = {
    "long_safe": {
        "sha": "adb452d00e8a612c21a37a2dc45a10345d1ec8218b542c1b1293fc9d1db5e18c",
        "bytes": 46441,
        "tokens": 10011,
        "repetitions": 909,
    },
    "long": {
        "sha": "af877ad92bed850b1a11ed57bb4d7908eaaec3d84f5d7f9f0955c4be0bd42eab",
        "bytes": 47461,
        "tokens": 10231,
        "repetitions": 929,
    },
    "tool": {
        "sha": "c1fc1d2a9ba8ad77a275905a4bb8161a355ca66fd0e50781dfa7b0f750db176e",
        "bytes": 35483,
        "tokens": 7842,
        "repetitions": 687,
    },
}


@pytest.mark.parametrize("mode", sorted(_EXPECTED))
def test_exact_reproducible_native_8k_tokenizer_request(mode):
    raw = request_bytes(mode)
    expected = _EXPECTED[mode]
    assert len(raw) == expected["bytes"] <= 65536
    assert hashlib.sha256(raw).hexdigest() == expected["sha"]
    decoded = json.loads(raw)
    assert decoded == make_request(mode)
    assert decoded["model"] == "astrumweaver"
    assert decoded["messages"][0]["role"] == "user"
    assert len(decoded["messages"][0]["content"]) > 30000
    receipt = describe_request(mode)
    assert receipt["request_bytes"] == expected["bytes"]
    assert receipt["request_sha256"] == expected["sha"]
    assert receipt["observed_8k_native_input_tokens_not_12k_proof"] == expected["tokens"]
    assert receipt["repetitions"] == expected["repetitions"]
    assert receipt["not_native_prefill_or_completion"] is True
    assert receipt["not_production_qualified"] is True


def test_opt_in_tool_probe_contains_explicit_tool_choice_without_side_effect():
    req = make_request("tool")
    assert req["max_tokens"] == 32
    assert req["tool_choice"]["function"]["name"] == "synthetic_echo"
    assert len(req["tools"]) == 1
    assert req["tools"][0]["type"] == "function"
    assert req["tools"][0]["function"]["name"] == "synthetic_echo"
    assert make_request("long")["max_tokens"] == 16
    assert "tools" not in make_request("long")


@pytest.mark.parametrize("value", ("", "wrong", "Long", None, 0, []))
def test_unknown_modes_are_refused_before_output(value):
    with pytest.raises(ValueError, match="unsupported"):
        make_request(value)


@pytest.mark.parametrize("alias", ("", "a" * 129, 1, None))
def test_invalid_model_alias_is_rejected(alias):
    with pytest.raises(ValueError, match="invalid model alias"):
        request_bytes("long", model=alias)


def test_model_alias_needs_separate_site_requalification():
    custom = describe_request("long", model="unqualified-model")
    assert custom["request_sha256"] != _EXPECTED["long"]["sha"]
    assert custom["not_production_qualified"] is True


@pytest.mark.parametrize("mode", sorted(_EXPECTED))
def test_cli_receipt_is_small_and_no_request_is_printed_by_default(mode, tmp_path):
    cli = ROOT / "tools" / "bonsai_12k_spica_fixtures.py"
    proc = subprocess.run(
        [sys.executable, str(cli), "--mode", mode],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert len(proc.stdout) < 500
    assert len(proc.stderr) == 0
    assert json.loads(proc.stdout)["request_sha256"] == _EXPECTED[mode]["sha"]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("mode", sorted(_EXPECTED))
def test_cli_full_request_requires_explicit_opt_in(mode, tmp_path):
    cli = ROOT / "tools" / "bonsai_12k_spica_fixtures.py"
    proc = subprocess.run(
        [sys.executable, str(cli), "--mode", mode, "--emit-request"],
        cwd=tmp_path,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == request_bytes(mode) + b"\n"
    assert list(tmp_path.iterdir()) == []
