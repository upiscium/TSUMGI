"""#232 R21 bounded offline correction tests. No GPU, Control or site I/O."""
from __future__ import annotations

import hashlib
import sys
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from bonsai_12k_r21_operator_patch import generate_r21_operator


def r17():
    return (ROOT / "tools/bonsai_12k_r17_operator.py").read_bytes()


def helper():
    return (ROOT / "tools/bonsai_12k_r20_cancel_gateway.py").read_bytes()


def generated():
    return generate_r21_operator(r17(), helper())


def namespace():
    result = {"__name__": "r21_offline_test"}
    exec(compile(generated(), "<R21 synthetic operator>", "exec"), result)  # noqa: S102
    # The execute() script is not main and imports are I/O free.
    return result


def test_exact_source_only_generation_compiles_is_reproducible_and_isolates_r20():
    a = generated()
    assert a == generated()
    assert len(hashlib.sha256(a).hexdigest()) == 64
    compile(a, "<R21 operator>", "exec")
    text = a.decode()
    for value in (
        "/root/tsumgi-r21-operator.py",
        "/root/tsumgi-bonsai12k-serving-r21-run",
        "/etc/astrumweaver/bonsai-serving-r21",
        "tsumgi-bonsai12k-spica-r21-control.service",
        "tsumgi-bonsai12k-spica-r21-worker.service",
        "tsumgi-bonsai12k-spica-r21-recovery.timer",
        "SPICA_R21_ISOLATED_REAL_TCP_CANCEL_8K_RESTORE",
        "ISOLATED_LAB_REAL_TCP_DISCONNECT_CANCELLED_8K_RESTORED",
        "def _read_r21_native_slots",
        "def _read_r21_lab_status",
    ):
        assert value in text
    assert "tsumgi-bonsai12k-spica-r20-worker.service" not in text
    assert "/root/tsumgi-bonsai12k-serving-r20-run" not in text


def test_8k_watchdog_and_bounded_control_survive_r21_generation():
    src = generated().decode()
    for value in (
        "arm_watchdog()", "native.wait_clear(timeout=115)",
        "native.wait_for_8k(timeout=165)",
        "original-worker-ownership-not-proven",
        "disarm_timer_after_verified_8k()",
        "--on-active=780s",
        "minimum_seconds=1000",
        "runtime_us == 1200 * 1000000",
        '"gateway_cancel_stream": 300',
        "with bounded_stage_walltime(stage):",
        "read_worker_health=lambda: ready(19100, \"/health\")",
        "actual-sender-http-body.bin",
        'STAGE / "bonsai_12k_r20_cancel_gateway.py"',
        "for f, expected in audit[\"staged_hashes\"].items()",
    ):
        assert value in src


def test_native_503_becomes_unknown_not_processing_or_idle_proof():
    ns = namespace()
    seen = []
    ns["_read_r21_native_slots"].__globals__["ready"] = lambda _port, _url: (
        (_ for _ in ()).throw(urllib.error.HTTPError(
            url="http://127.0.0.1:18085/slots", code=503, msg="busy",
            hdrs=None, fp=None,
        ))
    )
    ns["_read_r21_native_slots"].__globals__["note"] = (
        lambda name, **kwargs: seen.append((name, kwargs))
    )
    assert ns["_read_r21_native_slots"]() == []
    assert seen == [("R21_NATIVE_SLOTS_TRANSIENT_HTTP503", {"code": 503})]


def test_native_non503_error_is_not_suppressed():
    ns = namespace()
    ns["_read_r21_native_slots"].__globals__["ready"] = lambda _port, _url: (
        (_ for _ in ()).throw(urllib.error.HTTPError(
            url="http://127.0.0.1:18085/slots", code=500, msg="failure",
            hdrs=None, fp=None,
        ))
    )
    with pytest.raises(urllib.error.HTTPError) as error:
        ns["_read_r21_native_slots"]()
    assert error.value.code == 500


def test_actual_slots_200_is_still_required_for_processing_proof():
    ns = namespace()
    expected = [{"n_ctx": 12288, "is_processing": True}]
    ns["_read_r21_native_slots"].__globals__["ready"] = lambda port, endpoint: (
        expected if (port, endpoint) == (18085, "/slots") else []
    )
    assert ns["_read_r21_native_slots"]() == expected


def test_isolated_control_503_never_becomes_successful_empty_lab():
    ns = namespace()
    errors = []
    ns["_read_r21_lab_status"].__globals__["_lab_query"] = lambda _url: (
        (_ for _ in ()).throw(urllib.error.HTTPError(
            url="http://127.0.0.1:19090/__lab/status",
            code=503, msg="unavailable", hdrs=None, fp=None,
        ))
    )
    ns["_read_r21_lab_status"].__globals__["note"] = (
        lambda name, **kwargs: errors.append((name, kwargs))
    )
    with pytest.raises(urllib.error.HTTPError) as result:
        ns["_read_r21_lab_status"]()
    assert result.value.code == 503
    assert errors == [("R21_ISOLATED_CONTROL_STATUS_HTTP_ERROR", {"code": 503})]


@pytest.mark.parametrize("change", [b"extra", b"\n", b" "])
def test_source_sha_drift_is_rejected(change):
    with pytest.raises(ValueError, match="R20-base-reviewed-R17-source-sha-mismatch"):
        generate_r21_operator(r17() + change, helper())


@pytest.mark.parametrize("change", [b"extra", b"\n", b" "])
def test_helper_sha_drift_is_rejected(change):
    with pytest.raises(ValueError, match="R20-helper-reviewed-source-sha-mismatch"):
        generate_r21_operator(r17(), helper() + change)


def test_reviewed_final_operator_sha_is_exact_not_just_deterministic():
    from bonsai_12k_r21_operator_patch import PINNED_R21_GENERATED_SHA256

    assert hashlib.sha256(generated()).hexdigest() == PINNED_R21_GENERATED_SHA256
    assert len(PINNED_R21_GENERATED_SHA256) == 64


def test_root_lab_http_open_uses_direct_no_proxy_and_blocks_foreign_targets():
    import urllib.request

    ns = namespace()
    opener = ns["_lab_opener"]
    proxies = [h.proxies for h in opener.handlers
               if isinstance(h, urllib.request.ProxyHandler)]
    # urllib omits an empty ProxyHandler on some Python versions.
    assert proxies in ([], [{}])
    assert any(type(h).__name__ == "_NoLabRedirect" for h in opener.handlers)
    routes = []

    class FakeOpener:
        def open(self, req, timeout):
            routes.append((req, timeout))
            return "safe-local-placeholder"

    ns["_direct_lab_http_open"].__globals__["_lab_opener"] = FakeOpener()
    direct = ns["_direct_lab_http_open"]
    assert direct("http://127.0.0.1:19090/__lab/status", timeout=4) == (
        "safe-local-placeholder"
    )
    assert routes == [("http://127.0.0.1:19090/__lab/status", 4)]
    for url in (
        "http://example.com:19090/__lab/status",
        "http://127.0.0.2:19090/__lab/status",
        "http://127.0.0.1:19090/other",
        "https://127.0.0.1:19090/__lab/status",
        "http://user:password@127.0.0.1:19090/__lab/status",
        "http://127.0.0.1:19090/__lab/status?redirect=http://example.com",
    ):
        with pytest.raises(ns["Blocked"], match="lab-http-not-reviewed-loopback-endpoint"):
            direct(url, timeout=4)


def test_child_client_has_independent_direct_http_opener_no_redirect():
    from bonsai_12k_r20_cancel_gateway import _CLIENT

    assert "urllib.request.ProxyHandler({})" in _CLIENT
    assert "NoRedirect" in _CLIENT
    assert "cancel-client-target-must-be-exact-loopback" in _CLIENT
    assert "with opener.open(req, timeout=120)" in _CLIENT
    assert "urllib.request.urlopen(req" not in _CLIENT


def test_cancellation_acceptance_rechecks_actual_native_and_worker_idle():
    import inspect

    from bonsai_12k_r20_cancel_gateway import exercise_bounded_real_disconnect

    src = inspect.getsource(exercise_bounded_real_disconnect)
    assert "stable_slots = read_native_slots()" in src
    assert "stable_health = read_worker_health()" in src
    assert 'stable_slots[0].get("is_processing") is False' in src
    assert "cancel-final-native-or-worker-not-idle" in src
