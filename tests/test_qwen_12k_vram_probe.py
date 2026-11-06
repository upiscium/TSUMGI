"""Offline, fake-NVML regressions for #230 Stage-A 12K VRAM observation."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from unittest import mock

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "tools/qwen_12k_vram_probe.py"
SPEC = importlib.util.spec_from_file_location("qwen_12k_vram_probe", SOURCE)
assert SPEC is not None and SPEC.loader is not None
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)

UUIDS = [
    "GPU-00000000-0000-0000-0000-000000000001",
    "GPU-00000000-0000-0000-0000-000000000002",
]


def sample(a: int = 4350, b: int = 3349, *, reverse: bool = False) -> bytes:
    values = [
        f"{UUIDS[0]}, 12288, {a}, {12288 - a - 377}",
        f"{UUIDS[1]}, 12288, {b}, {12288 - b - 378}",
    ]
    if reverse:
        values.reverse()
    return ("\n".join(values) + "\n").encode("ascii")


def test_real_sirius_nvml_reserves_377_and_378_mib_outside_free_and_used():
    # Read-only physical probe on 2026-11-06 during approved 12K canary.
    # The old "used + free == total +/- 16" parser rejected these real values.
    raw = (
        f"{UUIDS[0]}, 12288, 4212, 7699\n"
        f"{UUIDS[1]}, 12288, 3209, 8701\n"
    ).encode("ascii")
    assert module.parse_sample(raw, UUIDS) == {
        UUIDS[0]: 4212, UUIDS[1]: 3209,
    }


@pytest.mark.parametrize("used", [1000, 10000])
def test_invalid_large_or_negative_driver_reservation_fails_closed(used):
    raw = sample().replace(b"7561", str(used).encode("ascii"))
    with pytest.raises(ValueError, match="inconsistent-memory-counts"):
        module.parse_sample(raw, UUIDS)


def test_crlf_is_accepted_without_normalizing_malformed_records():
    result = module.parse_sample(sample().replace(b"\n", b"\r\n"), UUIDS)
    assert result[UUIDS[0]] == 4350
    assert result[UUIDS[1]] == 3349


def test_parse_exact_two_expected_gpus_with_stable_uuid_order():
    assert list(module.parse_sample(sample(reverse=True), UUIDS).values()) == [3349, 4350]
    assert module.parse_sample(sample(reverse=True), UUIDS) == {
        UUIDS[0]: 4350, UUIDS[1]: 3349,
    }


@pytest.mark.parametrize("raw", [
    b"",
    b"nonsense\n",
    sample() + b"\n",
    sample().replace(b"\n", b"\r"),
    sample() + b"GPU-00000000-0000-0000-0000-000000000003, 12288, 3000, 8912\n",
    sample().replace(UUIDS[1].encode(), UUIDS[0].encode()),
    sample().replace(UUIDS[1].encode(), b"GPU-00000000-0000-0000-0000-000000000003"),
    sample().replace(b"12288", b"12288.0"),
    sample().replace(b"4350", b"-1"),
    sample().replace(b"4350", b"20000"),
    sample().replace(b"4350", b"2000, 5"),
    sample().replace(b"4350", b"n/a"),
    sample().replace(b"4350", b"\xff"),
    sample().replace(b"7561", b"3"),
    b"x" * 9000,
])
def test_parse_untrusted_or_incomplete_samples_fail_closed(raw):
    with pytest.raises(ValueError):
        module.parse_sample(raw, UUIDS)


@pytest.mark.parametrize("uuids", [
    [], [UUIDS[0]], [UUIDS[0], UUIDS[0]], [UUIDS[0], "GPU-notreal"],
    [UUIDS[0], UUIDS[1], "GPU-00000000-0000-0000-0000-000000000003"],
])
def test_refuse_missing_duplicate_or_malformed_reviewed_gpu(uuids):
    with pytest.raises(ValueError):
        module.parse_sample(sample(), uuids)


def test_observation_uses_minimum_across_every_sample_and_both_gpus():
    rows = [
        sample(4350, 3349),
        sample(4150, 3000),
        sample(2400, 2048),
        sample(2600, 2800),
        sample(3900, 3100),
    ]
    sleeps = []
    result = module.observe(
        UUIDS, phase="prefill", samples=5, interval_ms=200,
        query=lambda: rows.pop(0), sleep=sleeps.append,
    )
    assert result["observed_vram_gate"] == "PASS"
    assert result["min_free_mib_per_reviewed_gpu"] == [2400, 2048]
    assert result["sample_count"] == 5
    assert sleeps == [0.2] * 4
    assert result["production_qualified"] is False
    assert result["prefill_overlap_independently_verified"] is False
    assert result["actual_12k_context_verified"] is False
    assert UUIDS[0] not in json.dumps(result)


def test_peak_below_floor_denied_despite_other_gpu_headroom():
    result = module.observe(
        UUIDS, phase="prefill", samples=5, interval_ms=100,
        query=lambda: sample(3800, 2047), sleep=lambda _: None,
    )
    assert result["observed_vram_gate"] == "FAIL"
    assert result["production_qualified"] is False


def test_fail_fast_does_not_keep_sampling_after_first_unsafe_gpu():
    rows = [sample(), sample(4100, 2047)]
    result = module.observe(
        UUIDS, phase="prefill", samples=30, interval_ms=100,
        query=lambda: rows.pop(0), sleep=lambda _: None,
    )
    assert result["observed_vram_gate"] == "FAIL"
    assert result["sample_count"] == 2
    assert result["requested_sample_count"] == 30
    assert result["stopped_at_first_floor_violation"] is True


def test_idle_below_floor_is_failure_not_a_usable_baseline():
    result = module.observe(
        UUIDS, phase="idle", samples=5, interval_ms=100,
        query=lambda: sample(1900, 3300), sleep=lambda _: None,
    )
    assert result["observed_vram_gate"] == "FAIL"
    assert result["sample_count"] == 1


def test_slow_nvml_cannot_extend_total_observation_without_bound():
    fake_elapsed = [0.0]
    def slow_query():
        fake_elapsed[0] += 75.0
        return sample()
    with pytest.raises(ValueError, match="sampling-wall-time-exceeded"):
        module.observe(
            UUIDS, phase="prefill", samples=5, interval_ms=100,
            query=slow_query, sleep=lambda _: None,
            clock=lambda: fake_elapsed[0],
        )


def test_slow_sleep_cannot_extend_total_observation_without_bound():
    fake_elapsed = [0.0]
    def slow_sleep(_):
        fake_elapsed[0] += 181.0
    with pytest.raises(ValueError, match="sampling-wall-time-exceeded"):
        module.observe(
            UUIDS, phase="prefill", samples=5, interval_ms=100,
            query=lambda: sample(), sleep=slow_sleep,
            clock=lambda: fake_elapsed[0],
        )


def test_idle_only_never_reports_peak_pass():
    result = module.observe(
        UUIDS, phase="idle", samples=5, interval_ms=100,
        query=lambda: sample(), sleep=lambda _: None,
    )
    assert result["observed_vram_gate"] == "NOT_APPLICABLE_IDLE_ONLY"


@pytest.mark.parametrize(("phase", "samples", "interval"), [
    ("unknown", 5, 100), ("prefill", 4, 100), ("prefill", 601, 100),
    ("prefill", 5, 99), ("prefill", 5, 1001), ("prefill", 600, 1000),
])
def test_unbounded_or_invalid_observation_is_rejected_before_nvml(phase, samples, interval):
    with pytest.raises(ValueError):
        module.observe(
            UUIDS, phase=phase, samples=samples, interval_ms=interval,
            query=lambda: pytest.fail("NVML must not run"), sleep=lambda _: None,
        )


def test_mid_sample_failure_cannot_report_pass():
    rows = [sample(), sample(), b"not-cuda", sample(), sample()]
    with pytest.raises(ValueError):
        module.observe(
            UUIDS, phase="prefill", samples=5, interval_ms=100,
            query=lambda: rows.pop(0), sleep=lambda _: None,
        )


def test_fixed_read_only_command_is_used_and_process_reaped():
    original = subprocess.Popen
    calls = []
    def fake_popen(command, **kwargs):
        calls.append((command, kwargs))
        return original(
            [sys.executable, "-c", f"import sys; sys.stdout.buffer.write({sample()!r})"],
            **kwargs,
        )
    with mock.patch.object(module.subprocess, "Popen", side_effect=fake_popen):
        assert module.query_nvidia_smi() == sample()
    assert calls[0][0] == [
        "/usr/bin/nvidia-smi", "--query-gpu=uuid,memory.total,memory.free,memory.used",
        "--format=csv,noheader,nounits",
    ]
    assert calls[0][1]["stdin"] == subprocess.DEVNULL
    assert calls[0][1]["stderr"] == subprocess.DEVNULL


def test_read_only_command_oversize_output_is_blocked():
    original = subprocess.Popen
    def fake_popen(command, **kwargs):
        return original(
            [sys.executable, "-c", "import sys;sys.stdout.write('A'*16384)"],
            **kwargs,
        )
    with mock.patch.object(module.subprocess, "Popen", side_effect=fake_popen):
        with pytest.raises(ValueError, match="smi-output-too-large"):
            module.query_nvidia_smi()


def test_cli_peak_failure_returns_distinct_exit_code_and_no_raw_uuid(capsys):
    with mock.patch.object(sys, "argv", [
        "probe", "--read-only", "--expected-gpu-uuid", UUIDS[0],
        "--expected-gpu-uuid", UUIDS[1], "--phase", "prefill",
    ]), mock.patch.object(module, "observe", return_value={
        "status": "VRAM_OBSERVATION_ONLY_NOT_12K_QUALIFIED",
        "observed_vram_gate": "FAIL",
        "production_qualified": False,
    }):
        assert module.main() == 3
    report = json.loads(capsys.readouterr().out)
    assert report["observed_vram_gate"] == "FAIL"
    assert UUIDS[0] not in json.dumps(report)


def test_cli_idle_only_has_nonzero_not_applicable_exit(capsys):
    with mock.patch.object(sys, "argv", [
        "probe", "--read-only", "--expected-gpu-uuid", UUIDS[0],
        "--expected-gpu-uuid", UUIDS[1], "--phase", "idle",
    ]), mock.patch.object(module, "observe", return_value={
        "status": "VRAM_OBSERVATION_ONLY_NOT_12K_QUALIFIED",
        "observed_vram_gate": "NOT_APPLICABLE_IDLE_ONLY",
        "production_qualified": False,
    }):
        assert module.main() == 4
    result = json.loads(capsys.readouterr().out)
    assert result["observed_vram_gate"] == "NOT_APPLICABLE_IDLE_ONLY"
    assert result["production_qualified"] is False


def test_cli_unknown_observation_result_fails_closed(capsys):
    with mock.patch.object(sys, "argv", [
        "probe", "--read-only", "--expected-gpu-uuid", UUIDS[0],
        "--expected-gpu-uuid", UUIDS[1], "--phase", "prefill",
    ]), mock.patch.object(module, "observe", return_value={
        "observed_vram_gate": "UNEXPECTED",
        "production_qualified": False,
    }):
        assert module.main() == 2
    assert json.loads(capsys.readouterr().out)["production_qualified"] is False


def test_cli_native_failure_suppresses_sensitive_diagnostics(capsys):
    with mock.patch.object(sys, "argv", [
        "probe", "--read-only", "--expected-gpu-uuid", UUIDS[0],
        "--expected-gpu-uuid", UUIDS[1], "--phase", "prefill",
    ]), mock.patch.object(module, "observe", side_effect=ValueError("private-uuid-or-path")):
        assert module.main() == 2
    assert json.loads(capsys.readouterr().out) == {
        "status": "BLOCKED", "reason": "read-only-vram-probe-failed",
    }
