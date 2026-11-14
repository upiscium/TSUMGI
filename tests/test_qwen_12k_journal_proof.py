"""#230 R13 — synthetic journal identity, sequence and corruption rejection."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from qwen_12k_journal_proof import (
    NativeJournalProofError,
    inspect_journal_projection,
)


def fixture_records():
    boot = "a" * 32
    invocation = "b" * 32
    start = 1793938597097451
    sequence = [
        (1, "systemd", "init.scope", "", "Started tsumgi-qwen12k-canary.service."),
        (2, "python3", "tsumgi-qwen12k-canary.service", invocation,
         "load_model: initializing, n_slots = 1, n_ctx_slot = 12288"),
        (3, "python3", "tsumgi-qwen12k-canary.service", invocation,
         "llama_server: model loaded"),
        (4, "python3", "tsumgi-qwen12k-canary.service", invocation,
         "prompt eval time = 9350.68 ms / 7841 tokens"),
        (5, "python3", "tsumgi-qwen12k-canary.service", invocation,
         "prompt eval time = 11821.31 ms / 10052 tokens"),
        (6, "python3", "tsumgi-qwen12k-canary.service", invocation,
         "slot release: 10052"),
        (7, "python3", "tsumgi-qwen12k-canary.service", invocation,
         "slot release: chat"),
        (8, "python3", "tsumgi-qwen12k-canary.service", invocation,
         "slot release: tools"),
        (9, "python3", "tsumgi-qwen12k-canary.service", invocation,
         "slot release: stream"),
        (10, "systemd", "init.scope", "",
         "Stopping tsumgi-qwen12k-canary.service."),
        (11, "systemd", "init.scope", "",
         "tsumgi-qwen12k-canary.service: Failed with result 'exit-code'."),
        (12, "systemd", "init.scope", "",
         "Stopped tsumgi-qwen12k-canary.service."),
    ]
    return [
        {
            "__REALTIME_TIMESTAMP": str(start + i * 1_000_000),
            "_BOOT_ID": boot,
            "_SYSTEMD_UNIT": unit,
            "_SYSTEMD_INVOCATION_ID": inv,
            "SYSLOG_IDENTIFIER": identifier,
            "_PID": "1" if identifier == "systemd" else "12345",
            "MESSAGE": message,
        }
        for i, identifier, unit, inv, message in sequence
    ]


def canonical(events):
    return json.dumps(
        events, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8")


def inspect(events):
    raw = canonical(events)
    return inspect_journal_projection(
        raw, expected_sha256=hashlib.sha256(raw).hexdigest(),
    )


def test_valid_journal_still_denies_release_and_gateway_readiness():
    proof = inspect(fixture_records())
    assert proof["status"] == "NATIVE_LOG_CONFIRMED_NOT_SERVING_QUALIFIED"
    assert proof["context_size_logged"] == 12288
    assert proof["uncached_7841_prompt_logged"] is True
    assert proof["10052_prompt_logged"] is True
    assert proof["unit_nonzero_exit_observed"] is True
    assert proof["source_authenticated"] is False
    assert proof["independent_nvml_receipt_verified"] is False
    assert proof["gpu_release_proven"] is False
    assert proof["gateway_serving_qualified"] is False
    assert proof["native_pid"] == "12345"
    assert len(proof["journal_projection_sha256"]) == 71


@pytest.mark.parametrize(("index", "key", "replacement"), [
    (1, "_BOOT_ID", "c" * 32),
    (4, "_SYSTEMD_INVOCATION_ID", "c" * 32),
    (4, "_PID", "67890"),
    (4, "_SYSTEMD_UNIT", "other.service"),
    (4, "SYSLOG_IDENTIFIER", "systemd"),
    (4, "__REALTIME_TIMESTAMP", "not-time"),
    (3, "MESSAGE", "prompt eval time = 100 ms / 5000 tokens"),
    (4, "MESSAGE", "prompt eval time = 100 ms / 7841 tokens"),
    (1, "MESSAGE", "load_model: n_ctx_slot = 8192"),
    (2, "MESSAGE", "llama_server: not loaded"),
    (11, "MESSAGE", "Unit still running"),
])
def test_missing_or_inconsistent_journal_fails(index, key, replacement):
    data = fixture_records()
    data[index][key] = replacement
    with pytest.raises(NativeJournalProofError):
        inspect(data)


def test_reordered_timeline_fails():
    data = fixture_records()
    data[4]["__REALTIME_TIMESTAMP"] = data[1]["__REALTIME_TIMESTAMP"]
    with pytest.raises(NativeJournalProofError):
        inspect(data)


def test_duplicate_proof_event_fails():
    data = fixture_records()
    data[5]["MESSAGE"] = data[4]["MESSAGE"]
    with pytest.raises(NativeJournalProofError):
        inspect(data)


def test_missing_native_invocation_fails():
    data = fixture_records()
    data[4]["_SYSTEMD_INVOCATION_ID"] = ""
    with pytest.raises(NativeJournalProofError):
        inspect(data)


def test_source_digest_mismatch_rejected_even_if_events_valid():
    source = canonical(fixture_records())
    with pytest.raises(NativeJournalProofError, match="digest"):
        inspect_journal_projection(source, expected_sha256="0" * 64)


def test_noncanonical_and_oversized_projection_rejected():
    records = fixture_records()
    noncanonical = json.dumps(records, indent=2).encode()
    with pytest.raises(NativeJournalProofError, match="noncanonical"):
        inspect_journal_projection(noncanonical)
    with pytest.raises(NativeJournalProofError):
        inspect_journal_projection(b"[" * (256 * 1024 + 1))
    with pytest.raises(NativeJournalProofError):
        inspect_journal_projection(b"{}")


def test_source_records_remain_unchanged():
    records = fixture_records()
    before = deepcopy(records)
    a = inspect(records)
    b = inspect(records)
    assert a == b
    assert records == before


def test_cli_requires_digest_no_file_side_effects(tmp_path):
    src = tmp_path / "fixture.json"
    src.write_bytes(canonical(fixture_records()))
    sha = hashlib.sha256(src.read_bytes()).hexdigest()
    command = [
        sys.executable,
        str(ROOT / "tools/qwen_12k_journal_proof.py"),
        "--journal-json", str(src),
        "--expected-sha256", sha,
    ]
    before = set(tmp_path.iterdir())
    result = subprocess.run(
        command, text=True, capture_output=True, timeout=12, check=False,
    )
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["source_authenticated"] is False
    assert payload["gateway_serving_qualified"] is False
    assert set(tmp_path.iterdir()) == before
    rejected = subprocess.run(
        [*command[:-1], "0" * 64], text=True, capture_output=True,
        timeout=12, check=False,
    )
    assert rejected.returncode == 2
    assert json.loads(rejected.stdout) == {"status": "BLOCKED_JOURNAL_EVIDENCE"}
