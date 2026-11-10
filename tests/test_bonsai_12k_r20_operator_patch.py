"""#232 R20: deterministic non-executing root operator patch acceptance."""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from bonsai_12k_r20_operator_patch import (
    OPERATOR_R20,
    ROOT_R20,
    SOURCE_HELPER_SHA256,
    STAGE_R20,
    generate_r20_operator,
)


def source():
    return (ROOT / "tools/bonsai_12k_r17_operator.py").read_bytes()


def helper():
    return (ROOT / "tools/bonsai_12k_r20_cancel_gateway.py").read_bytes()


def generate():
    return generate_r20_operator(source(), helper())


def test_r20_generated_operator_is_deterministic_compiles_and_cannot_be_r17():
    first = generate()
    assert first == generate()
    assert len(first) > 40000
    assert len(hashlib.sha256(first).hexdigest()) == 64
    compile(first, "<R20 purely rendered root operator>", "exec")
    doc = first.decode("utf-8")
    assert f'OPERATOR = Path("{OPERATOR_R20}")' in doc
    assert f'ROOT = Path("{ROOT_R20}")' in doc
    assert f'STAGE = Path("{STAGE_R20}")' in doc
    assert 'EXPERIMENT_UNIT = "tsumgi-bonsai12k-spica-r20-worker.service"' in doc
    assert 'LAB_CONTROL_UNIT = "tsumgi-bonsai12k-spica-r20-control.service"' in doc
    assert 'WATCHDOG_TIMER = "tsumgi-bonsai12k-spica-r20-recovery.timer"' in doc
    assert 'LAB_WORKER = "bonsai-spica-r17-lab"' in doc
    assert 'LAB_PROFILE = "bonsai2-27b-12k-lab"' in doc
    assert "SPICA_R20_ISOLATED_REAL_TCP_CANCEL_8K_RESTORE" in doc
    assert 'LAB_CONTROL = "http://127.0.0.1:19090"' in doc
    assert SOURCE_HELPER_SHA256 in doc


def test_r20_no_old_r17_service_owner_published_or_legacy_4phase_success():
    rendered = generate().decode()
    assert "tsumgi-bonsai12k-spica-r17-worker.service" not in rendered
    assert "/etc/astrumweaver/bonsai-serving-r17/" not in rendered
    assert '("gateway_cancel_stream", lambda: _lab_cancel(monitor))' in rendered
    assert 'and stages == ["gateway_cancel_stream"]' in rendered
    assert '"gateway_small_chat", "gateway_stream",' not in rendered
    assert '"gateway_10k_long", "gateway_tool_result",' not in rendered
    assert "ISOLATED_LAB_REAL_TCP_DISCONNECT_CANCELLED_8K_RESTORED" in rendered
    assert '"isolated_lab_real_tcp_cancellation_verified": passed' in rendered
    assert '"cancellation_e2e_qualified": False' in rendered
    assert '"production_control_catalog_modified": False' in rendered
    assert '"production_12k_serving_revision_issued": False' in rendered
    assert '"permanent_12k_promotion": False' in rendered


def test_required_vram_watchdog_8k_restore_preflight_are_structurally_preserved():
    text = generate().decode()
    assert "arm_watchdog()" in text
    assert "native.wait_for_8k(timeout=165)" in text
    assert "native.wait_clear(timeout=115)" in text
    assert "native.worker_owns_original_8k()" in text
    assert "check(not native.is_active(WORKER_UNIT)" in text
    assert "monitor.assert_healthy()" in text
    assert 'time.monotonic() - timer_armed_at <= 450' in text
    assert 'time.monotonic() - timer_armed_at <= 680' in text
    assert '--on-active=780s' in text
    assert 'minimum_seconds=1000' in text
    assert 'runtime_us == 1200 * 1000000' in text
    assert '"gateway_cancel_stream": 300' in text
    assert 'with bounded_stage_walltime(stage):' in text
    assert "            try:\n                recover()\n                restored = True" in text
    assert "disarm_timer_after_verified_8k()" in text


def test_r20_included_in_verified_root_owned_prepare_receipt_and_not_logged():
    text = generate().decode()
    assert 'STAGE / "bonsai_12k_r20_cancel_gateway.py",' in text
    assert "for f, expected in audit[\"staged_hashes\"].items()" in text
    assert "hash_of(helper)" in text
    assert "info.st_uid == 0" in text
    assert 'ROOT / "actual-sender-http-body.bin"' in text
    assert 'group=0, mode=0o600' in text
    assert "token=client_bearer" not in text
    assert "client_bearer=_lab_secret()" in text
    assert '"actual_sender_http_body_sha256"' in text


@pytest.mark.parametrize("mutate", [
    lambda x: x.replace(b"stage", b"staged", 1),
    lambda x: b"",
    lambda x: x[:-1],
])
def test_unreviewed_base_operator_source_drift_fails_closed(mutate):
    with pytest.raises(ValueError, match="R20-base-reviewed-R17-source-sha-mismatch"):
        generate_r20_operator(mutate(source()), helper())


@pytest.mark.parametrize("mutate", [
    lambda x: x + b"# changed\n",
    lambda x: b"",
    lambda x: x[:-1],
])
def test_unreviewed_cancel_helper_source_drift_fails_closed(mutate):
    with pytest.raises(ValueError, match="R20-helper-reviewed-source-sha-mismatch"):
        generate_r20_operator(source(), mutate(helper()))


def test_no_source_files_modified_by_generation():
    r, h = source(), helper()
    sha = (hashlib.sha256(r).hexdigest(), hashlib.sha256(h).hexdigest())
    generate()
    assert (hashlib.sha256(source()).hexdigest(), hashlib.sha256(helper()).hexdigest()) == sha
