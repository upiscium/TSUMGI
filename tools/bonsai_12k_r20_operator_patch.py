"""#232 R20: reviewed, deterministic R17 -> Spica-only cancel lab operator.

PURE source transformation. Never installs, executes or stops a Worker.
Input MUST be the exact already-reviewed R17 operator source. The generated
operator preserves R17's independent systemd 8K rollback logic unchanged,
with unique R20 site paths and a single explicit real TCP disconnect stage.
"""
from __future__ import annotations

import hashlib

SOURCE_R17_SHA256 = "1b3e21be8136844a75cfcec5cb875dc205710855e0db42a80c2bf1496cb02261"
SOURCE_HELPER_SHA256 = "dfe4fab2b251106c7002b77486e685edf5fa7b6dfc369ecdbdb30c4dae0b426d"
ROOT_R20 = "/root/tsumgi-bonsai12k-serving-r20-run"
OPERATOR_R20 = "/root/tsumgi-r20-operator.py"
STAGE_R20 = "/etc/astrumweaver/bonsai-serving-r20"


def replace_exact(source: str, old: str, new: str, *, count: int = 1) -> str:
    if source.count(old) != count:
        raise ValueError("R20-reviewed-patch-anchor-mismatch:" + old[:54])
    return source.replace(old, new)


def generate_r20_operator(base_source: bytes, helper_source: bytes) -> bytes:
    """Generate ONLY the new lab operator; never touch R17/R19 or prod files."""
    if type(base_source) is not bytes or (
        hashlib.sha256(base_source).hexdigest() != SOURCE_R17_SHA256
    ):
        raise ValueError("R20-base-reviewed-R17-source-sha-mismatch")
    if type(helper_source) is not bytes or (
        hashlib.sha256(helper_source).hexdigest() != SOURCE_HELPER_SHA256
    ):
        raise ValueError("R20-helper-reviewed-source-sha-mismatch")

    text = base_source.decode("utf-8")
    substitutions = [
        ("/root/tsumgi-bonsai12k-serving-r17-run", ROOT_R20, 1),
        ("/root/tsumgi-r17-operator.py", OPERATOR_R20, 1),
        ("/etc/astrumweaver/bonsai-serving-r17", STAGE_R20, 2),
        ("tsumgi-bonsai12k-spica-r17-worker", "tsumgi-bonsai12k-spica-r20-worker", 2),
        ("tsumgi-bonsai12k-spica-r17-control", "tsumgi-bonsai12k-spica-r20-control", 1),
        ("tsumgi-bonsai12k-spica-r17-recovery", "tsumgi-bonsai12k-spica-r20-recovery", 2),
        ("TSUMGI_R17_OPERATOR_APPROVED", "TSUMGI_R20_OPERATOR_APPROVED", 1),
        ("SPICA_R17_ISOLATED_MANAGED12K_JOB_CANARY_8K_RESTORE",
         "SPICA_R20_ISOLATED_REAL_TCP_CANCEL_8K_RESTORE", 1),
    ]
    for old, new, n in substitutions:
        text = replace_exact(text, old, new, count=n)

    # All lab Control/Gateway/Native/health HTTP must be direct loopback.
    # urllib.urlopen honors ambient proxy variables and redirect handlers;
    # both can send a private lab bearer to the wrong destination.
    direct_http = '''\nclass _NoLabRedirect(urllib.request.HTTPRedirectHandler):\n    def redirect_request(self, req, fp, code, msg, headers, newurl):\n        raise ValueError("lab-redirect-denied")\n\n\n_lab_opener = urllib.request.build_opener(\n    urllib.request.ProxyHandler({}), _NoLabRedirect,\n)\n\n\ndef _direct_lab_http_open(req, *, timeout):\n    from urllib.parse import urlsplit\n\n    address = req.full_url if isinstance(req, urllib.request.Request) else req\n    parsed = urlsplit(address)\n    check(parsed.scheme == "http" and parsed.hostname == "127.0.0.1"\n          and parsed.username is None and parsed.password is None\n          and parsed.query == "" and parsed.fragment == ""\n          and (parsed.port, parsed.path) in {\n              (19090, "/__lab/status"),\n              (19090, "/v1/chat/completions"),\n              (18085, "/slots"),\n              (19100, "/ready"),\n              (19100, "/health"),\n          }, "lab-http-not-reviewed-loopback-endpoint")\n    return _lab_opener.open(req, timeout=timeout)\n\n\n'''
    text = replace_exact(text, "\ndef _lab_secret() -> str:\n",
                         "\n" + direct_http + "def _lab_secret() -> str:\n")
    text = replace_exact(text, "urllib.request.urlopen(",
                         "_direct_lab_http_open(", count=3)

    # Staged helper hash is covered by the root-owned prepare receipt and
    # rechecked *before* imported code is allowed to execute in a live run.
    text = replace_exact(
        text,
        '        STAGE / "control.env", STAGE / "worker.env",\n',
        '        STAGE / "control.env", STAGE / "worker.env",\n'
        '        STAGE / "bonsai_12k_r20_cancel_gateway.py",\n',
    )
    text = replace_exact(
        text,
        '        "gateway_10k_long": 300,\n',
        '        "gateway_10k_long": 300,\n'
        '        "gateway_cancel_stream": 300,\n',
    )
    cancel_method = '''
def _lab_cancel(monitor):
    # This stage is run ONLY after an independent 8K recovery timer is armed.
    helper = STAGE / "bonsai_12k_r20_cancel_gateway.py"
    info = helper.lstat()
    check(stat.S_ISREG(info.st_mode) and info.st_uid == 0
          and not (stat.S_IMODE(info.st_mode) & 0o022)
          and hash_of(helper) == "''' + SOURCE_HELPER_SHA256 + '''",
          "r20-cancel-helper-source-or-ownership-mismatch")
    if str(STAGE) not in sys.path:
        sys.path.insert(0, str(STAGE))
    from bonsai_12k_r20_cancel_gateway import exercise_bounded_real_disconnect
    from bonsai_12k_spica_fixtures import make_request

    staged = json.loads(STAGED_SERVING.read_text())
    contract = staged["contracts"][0]
    result = exercise_bounded_real_disconnect(
        make_request=make_request,
        read_lab_status=lambda: _lab_query("/__lab/status"),
        read_native_slots=lambda: ready(18085, "/slots"),
        read_worker_health=lambda: ready(19100, "/ready"),
        assert_vram_healthy=monitor.assert_healthy,
        client_bearer=_lab_secret(),
        staged_deployment=contract["contract"]["deployment_revision"],
        staged_contract=contract["serving_contract_revision"],
        record_sender_bytes=lambda raw: _owned_write(
            ROOT / "actual-sender-http-body.bin", raw, group=0, mode=0o600,
        ),
    )
    note(
        "R20_ISOLATED_ACTUAL_TCP_CANCEL_PASS",
        status=result["status"],
        request_bytes=result["request_sender_byte_length"],
        request_sha256=result["request_sender_sha256"],
        native_slot_idle=result["native_slot_idle_and_worker_ready"],
    )
    return result


'''
    text = replace_exact(text, "\ndef execute() -> int:\n",
                         "\n" + cancel_method + "def execute() -> int:\n")

    text = replace_exact(
        text,
        '    lab_summary = None\n    arm_watchdog()\n',
        '    lab_summary = None\n    cancel_summary = None\n    arm_watchdog()\n',
    )
    text = replace_exact(
        text,
        '        for stage, method in (\n'
        '            ("gateway_small_chat", lambda: _lab_plain(monitor)),\n'
        '            ("gateway_stream", lambda: _lab_stream(monitor)),\n'
        '            ("gateway_10k_long", lambda: _lab_long(monitor)),\n'
        '            ("gateway_tool_result", lambda: _lab_tool(monitor)),\n'
        '        ):\n',
        '        for stage, method in (\n'
        '            ("gateway_cancel_stream", lambda: _lab_cancel(monitor)),\n'
        '        ):\n',
    )
    text = replace_exact(
        text,
        '            with bounded_stage_walltime(stage):\n'
        '                method()\n',
        '            with bounded_stage_walltime(stage):\n'
        '                cancel_summary = method()\n',
    )
    old_final = '''        check(lab_summary.get("production_eligible") is False
              and type(lab_summary.get("workers")) is list
              and len(lab_summary["workers"]) == 1
              and type(lab_summary.get("jobs")) is list
              and len(lab_summary["jobs"]) >= 5,
              "lab-final-claim-or-job-state-invalid")
        expected = json.loads(STAGED_SERVING.read_text())
        contract = expected["contracts"][0]["serving_contract_revision"]
        deployment = expected["contracts"][0]["contract"]["deployment_revision"]
        for completed in lab_summary["jobs"]:
            validate_terminal_lab_claim(
                completed, lab_summary["workers"][0],
                expected_contract_revision=contract,
                expected_deployment_revision=deployment,
            )
        note("R17_LAB_JOB_CLAIMS_ACCEPTED", jobs=len(lab_summary["jobs"]),
             terminal_assignment_released=True)
'''
    new_final = '''        check(lab_summary.get("production_eligible") is False
              and type(lab_summary.get("workers")) is list
              and len(lab_summary["workers"]) == 1
              and type(lab_summary.get("jobs")) is list
              and len(lab_summary["jobs"]) == 1
              and lab_summary["jobs"][0].get("status") == "cancelled"
              and lab_summary["jobs"][0].get("assigned_worker_id") is None
              and lab_summary["workers"][0].get("active_jobs") == 0
              and type(cancel_summary) is dict
              and cancel_summary.get("status") ==
                  "ISOLATED_LAB_REAL_TCP_DISCONNECT_CANCELLED",
              "r20-real-tcp-cancel-final-job-release-unverified")
        note("R20_LAB_ACTUAL_CANCEL_CONFIRMED", jobs=1,
             terminal_assignment_released=True)
'''
    text = replace_exact(text, old_final, new_final)

    text = replace_exact(
        text,
        '        and stages == [\n'
        '            "gateway_small_chat", "gateway_stream",\n'
        '            "gateway_10k_long", "gateway_tool_result",\n'
        '        ]\n',
        '        and stages == ["gateway_cancel_stream"]\n'
        '        and cancel_summary is not None\n',
    )
    text = replace_exact(
        text,
        '"tsumgi-r17-spica-isolated-managed12k-bounded-canary-v1"',
        '"tsumgi-r20-spica-isolated-managed12k-real-tcp-cancel-v1"',
    )
    text = replace_exact(
        text,
        '"ISOLATED_LAB_MANAGED_JOB_AND_GATEWAY_SMOKE_8K_RESTORED" if passed',
        '"ISOLATED_LAB_REAL_TCP_DISCONNECT_CANCELLED_8K_RESTORED" if passed',
    )
    text = replace_exact(
        text,
        '        "cancellation_e2e_qualified": False,\n',
        '        "cancellation_e2e_qualified": False,\n'
        '        "isolated_lab_real_tcp_cancellation_verified": passed,\n'
        '        "actual_sender_http_body_sha256": (\n'
        '            None if cancel_summary is None else\n'
        '            cancel_summary["request_sender_sha256"]\n'
        '        ),\n'
        '        "cancel_source_untrusted_not_production": True,\n',
    )
    text = replace_exact(text, 'note("R17_FINAL", status=record["status"]',
                         'note("R20_FINAL", status=record["status"]')
    text = replace_exact(text, 'note("R17_TEST_BEGIN", name=stage)',
                         'note("R20_TEST_BEGIN", name=stage)')
    text = replace_exact(text, 'note("R17_TEST_PASSED", name=stage)',
                         'note("R20_TEST_PASSED", name=stage)')
    text = replace_exact(text, 'note("R17_ABORT", reason=failed',
                         'note("R20_ABORT", reason=failed')

    # Guard every practical stop/start or recovery mechanism. This must not
    # be bypassable by a random, unreviewed source transform.
    for invariant in (
        "def recover(*, from_watchdog: bool = False)",
        "arm_watchdog()",
        "native.wait_clear(timeout=115)",
        "native.wait_for_8k(timeout=165)",
        "disarm_timer_after_verified_8k()",
        "original-worker-ownership-not-proven",
        "RuntimeMaxUSec",
        'minimum_seconds=1000',
        'bounded_stage_walltime(stage)',
        '"gateway_cancel_stream"',
        '"isolated_lab_real_tcp_cancellation_verified": passed',
        SOURCE_HELPER_SHA256,
    ):
        if invariant not in text:
            raise ValueError("R20-operator-required-recovery-or-gate-missing")
    if "TSUMGI_R17_OPERATOR_APPROVED" in text or "tsumgi-bonsai12k-spica-r17-worker.service" in text:
        raise ValueError("R20-unreviewed-old-unit-identity")
    return text.encode("utf-8")
