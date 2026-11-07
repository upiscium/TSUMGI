"""#230 R12: proposed 12K revision-chain validation; never deploys artifacts."""

from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))

import qwen_12k_candidate as qwen
import qwen_12k_revision_bundle as bundle
from test_qwen_12k_candidate import fixtures, reviewed_pin

from astrumweaver.gateway.chat import ChatProfileCatalog
from astrumweaver.runtime import RuntimeDeploymentSpec
from astrumweaver.serving import ServingDeploymentDeclaration

GPU0 = "GPU-11111111-1111-1111-1111-111111111111"
GPU1 = "GPU-22222222-2222-2222-2222-222222222222"


def records(runtime, serving, catalog):
    draft = qwen.build_unqualified_candidate(
        runtime, serving, catalog, reviewed_pin(),
    )
    opts = draft["runtime_draft"]["provider_config"]
    execution = {
        "schema_version": "qwen12k-reviewed-execution-shape-v1",
        "profile_id": "qwen3.8-27b",
        "baseline_deployment_revision": serving["contracts"][0]["contract"][
            "deployment_revision"
        ],
        "model_artifact_sha256": serving["deployment"]["model_artifact_sha256"],
        "context_size": 12288,
        "parallel_slots": 1,
        "gpu_uuids": [GPU0, GPU1],
        "runtime_options": {
            k: deepcopy(opts[k]) for k in bundle._OPTIONS if k in opts
        },
    }
    canary = {
        "schema_version": "qwen12k-native-canary-shape-v1",
        "source_run": (
            "https://github.com/upiscium/TSUMGI/issues/230"
            "#issuecomment-6676882528"
        ),
        "execution_config_sha256": bundle._digest(execution),
        "model_artifact_sha256": serving["deployment"]["model_artifact_sha256"],
        "native_context_size": 12288,
        "max_input_tokens": 10052,
        "sample_count": 98,
        "gpu_min_free_mib": {GPU0: 4212, GPU1: 3207},
        "chat_ok": True,
        "tool_ok": True,
        "stream_ok": True,
        "rollback8k_ok": True,
    }
    return execution, canary


def build(runtime, serving, catalog, execution, canary):
    return bundle.build_review_required_revisions(
        runtime, serving, catalog, reviewed_pin(),
        execution_review=execution, native_canary_record=canary,
    )


def test_real_revision_shapes_are_consistent_but_not_qualified():
    runtime, serving, catalog = fixtures(explicit_profile_limits=True)
    before = deepcopy((runtime, serving, catalog))
    execution, canary = records(runtime, serving, catalog)
    result = build(runtime, serving, catalog, execution, canary)
    assert result["admission"] == "REVIEW_REQUIRED_NOT_PRODUCTION_QUALIFIED"
    assert result["reviewed_receipts_authenticated"] is False
    assert result["kernel_lifecycle_or_gateway_accepted"] is False
    assert (runtime, serving, catalog) == before
    assert result["proposed_deployment_revision"] != (
        result["baseline_deployment_revision"]
    )
    proposed_serving = ServingDeploymentDeclaration.from_dict(
        result["serving_proposal"]
    )
    proposed_catalog = ChatProfileCatalog.from_dict(result["catalog_proposal"])
    proposed_runtime = RuntimeDeploymentSpec.from_dict(
        result["runtime_proposal"]
    )
    assert proposed_runtime.provider_config["context_size"] == 12288
    assert proposed_runtime.setup_intent.configuration["context_size"] == 12288
    selected = proposed_catalog.get(qwen.PROFILE_ID).resolved
    assert selected.deployment == proposed_serving.deployment
    assert selected.contract == proposed_serving.contracts[0]
    assert selected.profile_revision == result["proposed_profile_revision"]
    assert dict(selected.effective_limits) == qwen.TARGET_LIMITS
    assert selected.deployment.execution_config_sha256 == (
        result["execution_record_sha256"]
    )
    assert selected.contract.validation_evidence_sha256 == (
        result["native_canary_record_sha256"]
    )
    assert result["native_canary_record_sha256"] != (
        serving["contracts"][0]["contract"]["validation_evidence_sha256"]
    )
    assert json.dumps(result, sort_keys=True)


@pytest.mark.parametrize(("key", "bad"), [
    ("parallel_slots", 2),
    ("context_size", 8192),
    ("gpu_uuids", [GPU0, GPU0]),
    ("gpu_uuids", [GPU0]),
    ("gpu_uuids", [GPU0, "not-gpu"]),
    ("model_artifact_sha256", "sha256:" + "f" * 64),
    ("baseline_deployment_revision", "sha256:" + "e" * 64),
    ("schema_version", "synthetic-only"),
])
def test_wrong_execution_review_fails_closed(key, bad):
    runtime, serving, catalog = fixtures()
    execution, canary = records(runtime, serving, catalog)
    execution[key] = bad
    with pytest.raises(ValueError):
        build(runtime, serving, catalog, execution, canary)


def test_runtime_options_tamper_fails_closed():
    runtime, serving, catalog = fixtures()
    execution, canary = records(runtime, serving, catalog)
    execution["runtime_options"]["tensor_split"] = [0.01, 0.99]
    with pytest.raises(ValueError, match="provider-options"):
        build(runtime, serving, catalog, execution, canary)


@pytest.mark.parametrize(("key", "bad"), [
    ("native_context_size", 8192),
    ("max_input_tokens", 7841),
    ("max_input_tokens", 10241),
    ("sample_count", 0),
    ("sample_count", True),
    ("chat_ok", False),
    ("tool_ok", 1),
    ("stream_ok", False),
    ("rollback8k_ok", False),
    ("execution_config_sha256", "sha256:" + "b" * 64),
    ("source_run", "https://example.com/fake"),
])
def test_wrong_native_canary_evidence_fails_closed(key, bad):
    runtime, serving, catalog = fixtures()
    execution, canary = records(runtime, serving, catalog)
    canary[key] = bad
    with pytest.raises(ValueError):
        build(runtime, serving, catalog, execution, canary)


@pytest.mark.parametrize("bad", [
    {GPU0: 4212}, {GPU0: 4212, GPU1: 2000},
    {GPU0: 4212, GPU1: False}, {GPU0: 4212, GPU1: 3207, "other": 4000},
])
def test_gpu_evidence_is_exact_and_above_floor(bad):
    runtime, serving, catalog = fixtures()
    execution, canary = records(runtime, serving, catalog)
    canary["gpu_min_free_mib"] = bad
    with pytest.raises(ValueError, match="vram"):
        build(runtime, serving, catalog, execution, canary)


def test_no_external_side_effects_or_input_rebinding(tmp_path):
    runtime, serving, catalog = fixtures()
    original = deepcopy((runtime, serving, catalog))
    execution, canary = records(runtime, serving, catalog)
    before = set(tmp_path.iterdir())
    a = build(runtime, serving, catalog, execution, canary)
    b = build(runtime, serving, catalog, execution, canary)
    assert a == b
    assert set(tmp_path.iterdir()) == before
    assert original == (runtime, serving, catalog)


def test_explicit_old_profile_cap_cannot_leak_into_new_limit():
    runtime, serving, catalog = fixtures(explicit_profile_limits=True)
    execution, canary = records(runtime, serving, catalog)
    reviewed = build(runtime, serving, catalog, execution, canary)
    profile = reviewed["catalog_proposal"]["profiles"][0]["profile"]
    assert profile["limits"]["input_tokens"] == 10240
    assert profile["limits"]["total_tokens"] == 12288


def test_operator_cli_private_review_files_and_no_published_secrets(tmp_path):
    import os
    import subprocess

    runtime, serving, catalog = fixtures()
    execution, canary = records(runtime, serving, catalog)
    sources = {
        "runtime": runtime,
        "serving": serving,
        "control": catalog,
        "reviewed-pin": reviewed_pin(),
        "execution-review": execution,
        "native-canary-record": canary,
    }
    args = []
    for name, payload in sources.items():
        p = tmp_path / (name + ".json")
        p.write_text(json.dumps(payload), encoding="utf-8")
        args.extend(("--" + name, str(p)))
    dest = Path("/tmp") / f"tsumgi-qwen12k-review-pytest-{os.getpid()}"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + ":" + str(ROOT / "tools")
    cli = [
        sys.executable, str(ROOT / "tools/qwen_12k_revision_bundle.py"), *args,
    ]
    try:
        dry = subprocess.run(cli, env=env, text=True, capture_output=True, timeout=15, check=False)
        assert dry.returncode == 0, dry.stderr
        assert json.loads(dry.stdout)["private_review_files_written"] is False
        assert not dest.exists()
        result = subprocess.run(
            [*cli, "--output-dir", str(dest)], env=env,
            text=True, capture_output=True, timeout=15, check=False,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["private_review_files_written"] is True
        assert dest.stat().st_mode & 0o777 == 0o700
        assert all(p.stat().st_mode & 0o777 == 0o600 for p in dest.iterdir())
        assert len(list(dest.iterdir())) == 4
        assert "synthetic-not-actual" not in result.stdout
        data = json.loads((dest / "review-gates.json").read_text())
        assert data["admission"] == "REVIEW_REQUIRED_NOT_PRODUCTION_QUALIFIED"
        assert data["reviewed_receipts_authenticated"] is False
        reject = subprocess.run(
            [*cli, "--output-dir", str(dest)], env=env, text=True,
            capture_output=True, timeout=15, check=False,
        )
        assert reject.returncode == 2
        assert json.loads(reject.stdout)["status"] == "BLOCKED"
    finally:
        if dest.exists():
            import shutil
            shutil.rmtree(dest)


def test_operator_cli_refuses_other_output_directories(tmp_path):
    runtime, serving, catalog = fixtures()
    execution, canary = records(runtime, serving, catalog)
    proposal = build(runtime, serving, catalog, execution, canary)
    with pytest.raises(ValueError, match="private-tmp"):
        bundle._write_private_review_bundle(str(tmp_path / "review"), proposal)
    with pytest.raises(ValueError, match="private-tmp"):
        bundle._write_private_review_bundle("/etc/astrumweaver/review", proposal)
