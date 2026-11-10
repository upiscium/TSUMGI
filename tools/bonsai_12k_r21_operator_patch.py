"""#232 R21: source-only correction of the bounded R20 cancellation probe.

R20 physical run yielded an HTTP503 during observation and correctly
restored Spica8K. Which read callback returned that 503 was not captured.
R21 queries the Worker's /health endpoint (HTTP200 with ready flag)
rather than /ready (which legitimately returns HTTP503 when not ready);
only a temporary Native /slots HTTP503 becomes a bounded *retry*
instead of immediate abort. Never infer processing/idle from a 503.
No production rights are created. No I/O in this module.
"""
from __future__ import annotations

import hashlib

from bonsai_12k_r20_operator_patch import (
    generate_r20_operator,
    replace_exact,
)

PINNED_R20_GENERATED_SHA256 = (
    "66c10b07ee228fce3dacb7cf25470942b7e252d907217404798e3819116376f5"
)
# Final complete root-only operator bytes, not just its R20 intermediate.
PINNED_R21_GENERATED_SHA256 = (
    "56286b2c1ccc91a4c2207b76739057d2fba0215e8bf72ba6eb9704d51e0aa988"
)


def generate_r21_operator(r17_source: bytes, r20_helper_source: bytes) -> bytes:
    data = generate_r20_operator(r17_source, r20_helper_source)
    if hashlib.sha256(data).hexdigest() != PINNED_R20_GENERATED_SHA256:
        raise ValueError("R21-unreviewed-R20-controller-source")

    s = data.decode("utf-8")
    items = [
        ("/root/tsumgi-bonsai12k-serving-r20-run",
         "/root/tsumgi-bonsai12k-serving-r21-run", 1),
        ("/root/tsumgi-r20-operator.py",
         "/root/tsumgi-r21-operator.py", 1),
        ("/etc/astrumweaver/bonsai-serving-r20",
         "/etc/astrumweaver/bonsai-serving-r21", 2),
        ("tsumgi-bonsai12k-spica-r20-worker",
         "tsumgi-bonsai12k-spica-r21-worker", 2),
        ("tsumgi-bonsai12k-spica-r20-control",
         "tsumgi-bonsai12k-spica-r21-control", 1),
        ("tsumgi-bonsai12k-spica-r20-recovery",
         "tsumgi-bonsai12k-spica-r21-recovery", 2),
        ("TSUMGI_R20_OPERATOR_APPROVED",
         "TSUMGI_R21_OPERATOR_APPROVED", 1),
        ("SPICA_R20_ISOLATED_REAL_TCP_CANCEL_8K_RESTORE",
         "SPICA_R21_ISOLATED_REAL_TCP_CANCEL_8K_RESTORE", 1),
        ("tsumgi-r20-spica-isolated-managed12k-real-tcp-cancel-v1",
         "tsumgi-r21-spica-isolated-managed12k-real-tcp-cancel-v1", 1),
    ]
    for old, new, count in items:
        s = replace_exact(s, old, new, count=count)

    s = replace_exact(s, "import urllib.request\n",
                      "import urllib.error\nimport urllib.request\n")
    read_slots = '''
def _read_r21_native_slots():
    # A Native /slots HTTP503 is a *temporary unknown* state, never proof
    # of either processing or quiescence. Require a later actual HTTP200.
    try:
        return ready(18085, "/slots")
    except urllib.error.HTTPError as exc:
        if exc.code != 503:
            raise
        note("R21_NATIVE_SLOTS_TRANSIENT_HTTP503", code=503)
        return []


def _read_r21_lab_status():
    # Any isolated Control outage is not a legitimate GPU/Native busy flag.
    try:
        return _lab_query("/__lab/status")
    except urllib.error.HTTPError as exc:
        note("R21_ISOLATED_CONTROL_STATUS_HTTP_ERROR", code=exc.code)
        raise


'''
    s = replace_exact(s, "\ndef _lab_cancel(monitor):\n",
                      "\n" + read_slots + "def _lab_cancel(monitor):\n")
    s = replace_exact(
        s,
        '        read_lab_status=lambda: _lab_query("/__lab/status"),\n'
        '        read_native_slots=lambda: ready(18085, "/slots"),\n'
        '        read_worker_health=lambda: ready(19100, "/ready"),\n',
        '        read_lab_status=_read_r21_lab_status,\n'
        '        read_native_slots=_read_r21_native_slots,\n'
        '        read_worker_health=lambda: ready(19100, "/health"),\n',
    )
    for old, new in (
        ("R20_ISOLATED_ACTUAL_TCP_CANCEL_PASS", "R21_ISOLATED_ACTUAL_TCP_CANCEL_PASS"),
        ("R20_LAB_ACTUAL_CANCEL_CONFIRMED", "R21_LAB_ACTUAL_CANCEL_CONFIRMED"),
        ("R20_TEST_BEGIN", "R21_TEST_BEGIN"),
        ("R20_TEST_PASSED", "R21_TEST_PASSED"),
        ("R20_ABORT", "R21_ABORT"),
        ("R20_FINAL", "R21_FINAL"),
    ):
        s = replace_exact(s, old, new)
    for required in (
        "arm_watchdog()",
        "native.wait_for_8k(timeout=165)",
        "native.wait_clear(timeout=115)",
        "original-worker-ownership-not-proven",
        "disarm_timer_after_verified_8k()",
        "minimum_seconds=1000",
        '"gateway_cancel_stream": 300',
        "with bounded_stage_walltime(stage):",
        "read_worker_health=lambda: ready(19100, \"/health\")",
        '"isolated_lab_real_tcp_cancellation_verified": passed',
        "actual-sender-http-body.bin",
        'except urllib.error.HTTPError as exc:',
    ):
        if required not in s:
            raise ValueError("R21-reviewed-contract-missing")
    if any(name in s for name in (
        "/root/tsumgi-bonsai12k-serving-r20-run",
        "tsumgi-bonsai12k-spica-r20-worker.service",
        "TSUMGI_R20_OPERATOR_APPROVED",
    )):
        raise ValueError("R21-reused-R20-test-resources")
    generated = s.encode("utf-8")
    if hashlib.sha256(generated).hexdigest() != PINNED_R21_GENERATED_SHA256:
        raise ValueError("R21-final-generated-source-sha-mismatch")
    return generated
