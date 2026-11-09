"""R17 independent Control TTL safety gate — synthetic tests, no systemd I/O."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import bonsai_12k_r17_operator as operator


@pytest.mark.parametrize(("text", "expected"), [
    ("20min", 1200000000),
    ("15min", 900000000),
    ("900s", 900000000),
    ("1200s", 1200000000),
    ("1h", 3600000000),
    ("1000ms", 1000000),
    ("150000us", 150000),
])
def test_systemd_lifetime_units_are_bounded_and_unambiguous(text, expected):
    assert operator.parse_control_runtime_max_usec(text) == expected


@pytest.mark.parametrize("value", [
    "", "infinity", "0s", "0min", "20 min", "1h 30min",
    "99999999999999s", "20seconds", "3d", "15min\nBAD",
    "4h", "-2min", "nan", None, True,
])
def test_indefinite_malformed_and_oversized_runtime_max_are_denied(value):
    with pytest.raises(operator.Blocked):
        operator.parse_control_runtime_max_usec(value)


def test_remaining_lifetime_uses_monotonic_absolute_activation_not_ttl_only():
    assert operator.control_lifetime_remaining_usec(
        runtime_max_usec=1200000000,
        active_since_usec=3000000000,
        now_usec=3060000000,
    ) == 1140000000
    assert operator.control_lifetime_remaining_usec(
        runtime_max_usec=900000000,
        active_since_usec=3000000000,
        now_usec=3900000000,
    ) == 0


@pytest.mark.parametrize("max_us,start,now", [
    (True, 3000000000, 4000000000),
    (1200000000, 0, 4000000000),
    (1200000000, 4000000000, 3999999999),
    (-1, 3000000000, 4000000000),
    (1200000000, 3000000000, 2.0),
])
def test_invalid_monotonic_deadline_fields_fail_closed(max_us, start, now):
    with pytest.raises(operator.Blocked, match="lab-control-lifetime-clock-invalid"):
        operator.control_lifetime_remaining_usec(
            runtime_max_usec=max_us, active_since_usec=start, now_usec=now,
        )


def make_mock(monkeypatch, *, duration="20min", elapsed_seconds=60, active=True):
    now_us = 4000000000
    start_us = now_us - elapsed_seconds * 1000000
    expected = {
        "RuntimeMaxUSec": duration,
        "ActiveEnterTimestampMonotonic": str(start_us),
    }
    calls = []

    def fake_run(argv, timeout=30, *, tolerate=False):
        assert argv[:4] == ["systemctl", "show", "-P", argv[3]]
        key = argv[3]
        assert key in expected
        assert argv[-1] == operator.LAB_CONTROL_UNIT
        calls.append(key)
        return SimpleNamespace(stdout=expected[key] + "\n", returncode=0)

    monkeypatch.setattr(operator, "run", fake_run)
    monkeypatch.setattr(operator, "native", SimpleNamespace(
        is_active=lambda unit: unit == operator.LAB_CONTROL_UNIT and active,
    ))
    monkeypatch.setattr(operator.time, "monotonic_ns", lambda: now_us * 1000)
    result = []
    monkeypatch.setattr(operator, "note", lambda name, **kwargs: result.append((name, kwargs)))
    return calls, result


def test_fresh_20min_control_with_adequate_time_is_accepted(monkeypatch):
    calls, events = make_mock(monkeypatch, duration="20min", elapsed_seconds=60)
    operator.verify_lab_control_lifetime(minimum_seconds=1000)
    assert calls == ["RuntimeMaxUSec", "ActiveEnterTimestampMonotonic"]
    assert events == [
        ("R17_CONTROL_LIFETIME_GATE_PASS", {
            "remaining_seconds": 1140,
            "required_seconds": 1000,
        }),
    ]


@pytest.mark.parametrize(("limit", "elapsed", "required", "error"), [
    ("15min", 0, 1000, "lab-control-runtime-cap-not-exactly-20min"),
    ("30min", 0, 1000, "lab-control-runtime-cap-not-exactly-20min"),
    ("1h", 0, 1000, "lab-control-runtime-cap-not-exactly-20min"),
    ("20min", 201, 1000, "lab-control-insufficient-remaining-lifetime"),
    ("20min", 950, 260, "lab-control-insufficient-remaining-lifetime"),
    ("infinity", 0, 1000, "lab-control-runtime-limit-invalid"),
])
def test_expired_or_short_lived_control_fails_before_worker_stop(
    monkeypatch, limit, elapsed, required, error,
):
    make_mock(monkeypatch, duration=limit, elapsed_seconds=elapsed)
    with pytest.raises(operator.Blocked, match=error):
        operator.verify_lab_control_lifetime(minimum_seconds=required)


def test_stale_or_inactive_isolated_control_is_denied(monkeypatch):
    make_mock(monkeypatch, duration="20min", elapsed_seconds=60, active=False)
    with pytest.raises(operator.Blocked, match="lab-control-no-longer-active"):
        operator.verify_lab_control_lifetime(minimum_seconds=1000)


@pytest.mark.parametrize("required", [0, 10, 1001, True, 500.0])
def test_caller_cannot_weaken_or_overstate_ttl_floor(monkeypatch, required):
    make_mock(monkeypatch)
    with pytest.raises(operator.Blocked, match="lab-control-minimum-ttl-invalid"):
        operator.verify_lab_control_lifetime(minimum_seconds=required)


@pytest.mark.parametrize(("stage", "minimum"), [
    ("gateway_small_chat", 300),
    ("gateway_stream", 300),
    ("gateway_10k_long", 300),
    ("gateway_tool_result", 520),
])
def test_stage_ttl_reserves_complete_sequential_call_budget(stage, minimum):
    assert operator.control_stage_minimum_seconds(stage) == minimum


@pytest.mark.parametrize("stage", ["", "cancel", "production", None, True, 1])
def test_unreviewed_or_invalid_stage_cannot_reduce_control_ttl(stage):
    with pytest.raises(operator.Blocked, match="lab-control-unreviewed-stage"):
        operator.control_stage_minimum_seconds(stage)


def test_tool_stage_rejects_240sec_remaining_even_if_one_call_might_fit(monkeypatch):
    make_mock(monkeypatch, duration="20min", elapsed_seconds=920)
    with pytest.raises(operator.Blocked, match="lab-control-insufficient-remaining-lifetime"):
        operator.verify_lab_control_lifetime(
            minimum_seconds=operator.control_stage_minimum_seconds(
                "gateway_tool_result"
            ),
        )


def test_stage_wall_time_is_stricter_than_remaining_control_ttl_floor():
    assert operator.stage_wall_clock_limit_seconds("gateway_tool_result") == 480
    assert operator.stage_wall_clock_limit_seconds("gateway_10k_long") == 260
    assert operator.stage_wall_clock_limit_seconds("gateway_stream") == 260


def test_entire_stage_has_independent_wall_clock_alarm_and_restores_prior_handler(
    monkeypatch,
):
    armed = []
    previous = object()
    handler = []

    def fake_signal(_sig, callback):
        handler.append(callback)

    def fake_itimer(_kind, duration):
        armed.append(duration)

    monkeypatch.setattr(operator.signal, "getsignal", lambda _sig: previous)
    monkeypatch.setattr(operator.signal, "getitimer", lambda _kind: (0.0, 0.0))
    monkeypatch.setattr(operator.signal, "signal", fake_signal)
    monkeypatch.setattr(operator.signal, "setitimer", fake_itimer)
    with operator.bounded_stage_walltime("gateway_tool_result"):
        assert armed == [480]
        assert callable(handler[-1])
        with pytest.raises(operator.Blocked, match="lab-stage-walltime-expired"):
            handler[-1](operator.signal.SIGALRM, None)
    assert armed == [480, 0]
    assert handler[-1] is previous


def test_existing_unrelated_alarm_is_not_overwritten(monkeypatch):
    monkeypatch.setattr(operator.signal, "getsignal", lambda _sig: object())
    monkeypatch.setattr(operator.signal, "getitimer", lambda _kind: (12.0, 0.0))
    with (
        pytest.raises(operator.Blocked, match="unexpected-operator-alarm-already-armed"),
        operator.bounded_stage_walltime("gateway_stream"),
    ):
        raise AssertionError("must not enter stage")
