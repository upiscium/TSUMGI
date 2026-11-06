"""Offline Qwen 8K -> 12K draft: no real deployment evidence is invented."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from uuid import uuid4

import pytest

from astrumweaver.gateway.chat import (
    CHAT_CATALOG_SCHEMA, CHAT_OPERATION_SCHEMA, LLAMA_CPP_CHAT_ADAPTER,
    ChatGatewayError, ChatProfileCatalog,
)
from astrumweaver.runtime import RuntimeDeploymentSpec
from astrumweaver.serving import (
    DeploymentIdentity, LogicalServingProfile,
    ServingContract, ServingContractError, ServingDeploymentDeclaration,
    resolve_profile,
)

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools/qwen_12k_candidate.py"
SPEC = importlib.util.spec_from_file_location("qwen_12k_candidate", TOOL)
assert SPEC is not None and SPEC.loader is not None
qwen = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(qwen)


def digest(c: str) -> str:
    return "sha256:" + c * 64


def fixtures(*, explicit_profile_limits: bool = False):
    ref = "/synthetic-not-actual/qwen.gguf"
    runtime = {
        "schema_version": "v1",
        "provider_id": "llama-cpp",
        "provider_config": {
            "base_url": "http://127.0.0.1:8080",
            "executable": "llama-server",
            "package_reference": "llama-cpp",
            "context_size": 8192,
            "gpu_layers": "all",
            "split_mode": "layer",
            "tensor_split": [0.45, 0.55],
            "offline": True,
            "no_webui": True,
        },
        "demand": {
            "model": {
                "model_ref": ref, "model_format": "gguf",
                "topology": "moe", "estimated_size_mb": 4096, "metadata": {},
            },
            "residency_policy": "vram_only",
            "gpu_topology": "multi_gpu",
            "min_gpu_count": 2,
            "min_total_vram_mb": 20000,
            "min_single_gpu_vram_mb": 10240,
            "min_host_ram_mb": 0,
            "preferred_host_ram_mb": 0,
            "metadata": {},
        },
        "setup_intent": {
            "provider_id": "llama-cpp",
            "package_references": ["llama-cpp"],
            "configuration": {"context_size": 8192, "model_ref": ref},
            "model_preparation": "reference_only",
            "model_ref": ref,
            "requires_privilege": False,
            "metadata": {},
        },
    }
    identity = DeploymentIdentity(
        provider_id="llama-cpp",
        runtime_artifact_sha256=digest("1"),
        adapter_artifact_sha256=digest("2"),
        model_artifact_sha256=digest("3"),
        execution_config_sha256=digest("4"),
        quantization="Q4_K_M",
        tokenizer_artifact_sha256=digest("5"),
        template_artifact_sha256=digest("6"),
    )
    contract = ServingContract(
        deployment_revision=identity.revision,
        capability="llm.chat",
        operation_schema=CHAT_OPERATION_SCHEMA,
        features=frozenset({"tools"}),
        limits=qwen.BASE_LIMITS,
        validation_evidence_sha256=digest("7"),
    )
    profile = LogicalServingProfile(
        profile_id=qwen.PROFILE_ID,
        deployment_revision=identity.revision,
        serving_contract_revision=contract.revision,
        capability="llm.chat",
        operation_schema=CHAT_OPERATION_SCHEMA,
        required_features=frozenset({"tools"}),
        limits=(
            {"input_tokens": 6144, "total_tokens": 8192}
            if explicit_profile_limits else {}
        ),
    )
    serving = ServingDeploymentDeclaration(identity, (contract,)).to_dict()
    catalog = {
        "schema_version": CHAT_CATALOG_SCHEMA,
        "profiles": [{
            "adapter_id": LLAMA_CPP_CHAT_ADAPTER,
            "deployment": identity.to_dict(),
            "contract": contract.to_dict(),
            "profile": profile.to_dict(),
            "request_timeout_seconds": 60.0,
            "max_attempts": 2,
            "created": 0,
            "owned_by": "astrumweaver",
        }],
    }
    assert RuntimeDeploymentSpec.from_dict(runtime).provider_id == "llama-cpp"
    assert ChatProfileCatalog.from_dict(catalog).profiles[0].profile_id == qwen.PROFILE_ID
    return runtime, serving, catalog


def reviewed_pin() -> dict:
    """Out-of-band synthetic Qwen binding independent of modified test inputs."""
    _, original_serving, _ = fixtures()
    original = ServingDeploymentDeclaration.from_dict(original_serving)
    ref = "/synthetic-not-actual/qwen.gguf"
    return {
        "schema_version": qwen.TRUSTED_PIN_SCHEMA,
        "profile_id": qwen.PROFILE_ID,
        "model_artifact_sha256": digest("3"),
        "model_ref_sha256": "sha256:" + hashlib.sha256(ref.encode()).hexdigest(),
        "deployment_revision": original.deployment.revision,
    }


def make_draft(runtime, serving, catalog, *, pin=None):
    return qwen.build_unqualified_candidate(
        runtime, serving, catalog,
        reviewed_pin() if pin is None else pin,
    )


def test_offline_draft_has_matched_12k_limits_but_cannot_be_admitted():
    runtime, serving, catalog = fixtures()
    originals = deepcopy((runtime, serving, catalog))
    draft = make_draft(runtime, serving, catalog)
    assert (runtime, serving, catalog) == originals
    assert draft["admission"] == "DENIED_PENDING_REVIEW_AND_REAL_CANARY"
    assert draft["schema_version"] == qwen.DRAFT_SCHEMA
    assert draft["requested_native_context"] == 12288
    assert draft["target_limits"] == qwen.TARGET_LIMITS
    candidate = draft["runtime_draft"]
    assert RuntimeDeploymentSpec.from_dict(candidate).provider_id == "llama-cpp"
    assert candidate["provider_config"]["context_size"] == 12288
    assert candidate["setup_intent"]["configuration"]["context_size"] == 12288
    declaration = draft["serving_UNLOADABLE_draft"]
    assert declaration["contracts"][0]["contract"]["limits"] == qwen.TARGET_LIMITS
    assert declaration["deployment"]["execution_config_sha256"] == qwen.PENDING_EXECUTION
    assert declaration["contracts"][0]["contract"]["validation_evidence_sha256"] == qwen.PENDING_VALIDATION
    with pytest.raises(ServingContractError):
        ServingDeploymentDeclaration.from_dict(declaration)
    with pytest.raises(ChatGatewayError):
        ChatProfileCatalog.from_dict(draft["control_UNLOADABLE_draft"])
    assert len(draft["remaining_gates"]) >= 6


def test_optional_profile_limits_may_be_absent_in_source_catalog():
    runtime, serving, catalog = fixtures()
    del catalog["profiles"][0]["profile"]["limits"]
    assert ChatProfileCatalog.from_dict(catalog).profiles[0].resolved.effective_limits == qwen.BASE_LIMITS
    draft = make_draft(runtime, serving, catalog)
    assert draft["control_UNLOADABLE_draft"]["profiles"][0]["profile"]["limits"] == {}


def test_explicit_profile_limits_updated_and_8k_source_not_changed():
    runtime, serving, catalog = fixtures(explicit_profile_limits=True)
    result = make_draft(runtime, serving, catalog)
    profile = result["control_UNLOADABLE_draft"]["profiles"][0]["profile"]
    assert profile["limits"] == {"input_tokens": 10240, "total_tokens": 12288}
    assert catalog["profiles"][0]["profile"]["limits"] == {
        "input_tokens": 6144, "total_tokens": 8192,
    }


def test_unrelated_catalog_profile_is_preserved_byte_for_byte():
    runtime, serving, catalog = fixtures()
    unrelated = deepcopy(catalog["profiles"][0])
    identity = DeploymentIdentity(
        provider_id="llama-cpp",
        runtime_artifact_sha256=digest("a"),
        adapter_artifact_sha256=digest("b"),
        model_artifact_sha256=digest("c"),
        execution_config_sha256=digest("d"),
        quantization="Q4_K_M",
        tokenizer_artifact_sha256=digest("e"),
        template_artifact_sha256=digest("f"),
    )
    contract = ServingContract(
        deployment_revision=identity.revision, capability="llm.chat",
        operation_schema=CHAT_OPERATION_SCHEMA,
        features=frozenset({"tools"}),
        limits=qwen.BASE_LIMITS,
        validation_evidence_sha256=digest("a"),
    )
    profile = LogicalServingProfile(
        profile_id="bonsai2-27b", deployment_revision=identity.revision,
        serving_contract_revision=contract.revision,
        capability="llm.chat", operation_schema=CHAT_OPERATION_SCHEMA,
        required_features=frozenset({"tools"}),
    )
    unrelated.update(deployment=identity.to_dict(), contract=contract.to_dict(),
                     profile=profile.to_dict())
    catalog["profiles"].append(unrelated)
    assert len(ChatProfileCatalog.from_dict(catalog).profiles) == 2
    draft = make_draft(runtime, serving, catalog)
    assert draft["control_UNLOADABLE_draft"]["profiles"][1] == unrelated


@pytest.mark.parametrize(("target", "change"), [
    ("runtime", lambda d: d["provider_config"].update(context_size=12288)),
    ("runtime", lambda d: d["setup_intent"]["configuration"].update(context_size=4096)),
    ("runtime", lambda d: d["demand"].update(gpu_topology="single_gpu", min_gpu_count=1)),
    ("runtime", lambda d: d["setup_intent"].update(model_ref="/wrong.gguf")),
    ("runtime", lambda d: d.update(provider_id="another-provider")),
    ("serving", lambda d: d["contracts"][0]["contract"]["limits"].update(input_tokens=8192)),
    ("serving", lambda d: d["deployment"].update(execution_config_sha256=digest("a"))),
    ("catalog", lambda d: d["profiles"][0]["profile"].update(profile_id="different-model")),
    ("catalog", lambda d: d["profiles"][0]["profile"]["limits"].update(input_tokens=8192)),
])
def test_rejects_incompatible_or_changed_8k_baseline(target, change):
    runtime, serving, catalog = fixtures()
    item = {"runtime": runtime, "serving": serving, "catalog": catalog}[target]
    change(item)
    with pytest.raises((ValueError, TypeError)):
        make_draft(runtime, serving, catalog)


def test_cannot_share_base_deployment_with_another_profile():
    runtime, serving, catalog = fixtures()
    second = deepcopy(catalog["profiles"][0])
    second["profile"]["profile_id"] = "unsafe-alias"
    catalog["profiles"].append(second)
    with pytest.raises(ValueError, match="shared-deployment-must-be-reviewed"):
        make_draft(runtime, serving, catalog)


def test_serving_model_identity_schema_and_separate_runtime_reference():
    # TSUMGI DeploymentIdentity binds model bytes, not a model_ref path.
    # The operator-approved runtime reference is pinned separately.
    fields = DeploymentIdentity.__dataclass_fields__
    assert "model_artifact_sha256" in fields
    assert "model_ref_sha256" not in fields
    assert "model_ref" not in fields
    runtime, serving, catalog = fixtures()
    pin = reviewed_pin()
    assert pin["model_artifact_sha256"] == serving["deployment"]["model_artifact_sha256"]
    assert pin["model_ref_sha256"] == (
        "sha256:" + hashlib.sha256(
            runtime["demand"]["model"]["model_ref"].encode()
        ).hexdigest()
    )
    assert make_draft(runtime, serving, catalog, pin=pin)["admission"].startswith("DENIED")


@pytest.mark.parametrize(("key", "value"), [
    ("model_artifact_sha256", digest("8")),
    ("deployment_revision", digest("9")),
    ("model_ref_sha256", digest("a")),
    ("profile_id", "bonsai2-27b"),
    ("schema_version", "unreviewed"),
])
def test_independent_baseline_pin_rejects_wrong_identity(key, value):
    runtime, serving, catalog = fixtures()
    pin = reviewed_pin()
    pin[key] = value
    with pytest.raises(ValueError):
        make_draft(runtime, serving, catalog, pin=pin)


def test_mutually_consistent_wrong_model_alias_is_rejected():
    runtime, serving, catalog = fixtures()
    other_ref = "/synthetic-not-actual/other.gguf"
    runtime["demand"]["model"]["model_ref"] = other_ref
    runtime["setup_intent"]["model_ref"] = other_ref
    runtime["setup_intent"]["configuration"]["model_ref"] = other_ref
    original_deployment = DeploymentIdentity(**serving["deployment"])
    changed_deployment = replace(
        original_deployment, model_artifact_sha256=digest("8"),
    )
    original_contract = ServingContract(**serving["contracts"][0]["contract"])
    changed_contract = replace(
        original_contract, deployment_revision=changed_deployment.revision,
    )
    old_profile = LogicalServingProfile(**catalog["profiles"][0]["profile"])
    new_profile = replace(
        old_profile, deployment_revision=changed_deployment.revision,
        serving_contract_revision=changed_contract.revision,
    )
    changed_serving = ServingDeploymentDeclaration(
        changed_deployment, (changed_contract,),
    ).to_dict()
    catalog["profiles"][0]["deployment"] = changed_deployment.to_dict()
    catalog["profiles"][0]["contract"] = changed_contract.to_dict()
    catalog["profiles"][0]["profile"] = new_profile.to_dict()
    # All three source manifests agree, but the model is NOT the
    # separately approved Qwen artifact or model reference.
    assert ChatProfileCatalog.from_dict(catalog).profiles[0].profile_id == qwen.PROFILE_ID
    with pytest.raises(ValueError, match="reviewed-model-artifact-mismatch"):
        make_draft(runtime, changed_serving, catalog)


def test_consistent_runtime_model_reference_swap_is_rejected_by_reviewed_pin():
    runtime, serving, catalog = fixtures()
    other_ref = "/synthetic-not-actual/wrong-model.gguf"
    runtime["demand"]["model"]["model_ref"] = other_ref
    runtime["setup_intent"]["model_ref"] = other_ref
    runtime["setup_intent"]["configuration"]["model_ref"] = other_ref
    assert RuntimeDeploymentSpec.from_dict(runtime).setup_intent.model_ref == other_ref
    with pytest.raises(ValueError, match="reviewed-model-ref-mismatch"):
        make_draft(runtime, serving, catalog)


def test_private_drafts_are_only_written_in_fresh_direct_tmp_child():
    runtime, serving, catalog = fixtures()
    proposal = make_draft(runtime, serving, catalog)
    output = Path("/tmp") / ("tsumgi-qwen12k-test-" + uuid4().hex)
    assert not output.exists()
    try:
        qwen._emit_private_draft(output, proposal)
        assert output.stat().st_mode & 0o777 == 0o700
        assert {p.name for p in output.iterdir()} == {
            "runtime.UNQUALIFIED.json", "serving.INVALID.json",
            "control.INVALID.json", "review-gates.json",
        }
        for path in output.iterdir():
            assert path.stat().st_mode & 0o777 == 0o600
        with pytest.raises(ValueError, match="output-must-not-exist"):
            qwen._emit_private_draft(output, proposal)
        with pytest.raises(ValueError, match="output-must-be-new-direct-tmp-child"):
            qwen._emit_private_draft(output / "nested", proposal)
        with pytest.raises(ValueError, match="output-must-be-new-direct-tmp-child"):
            qwen._emit_private_draft(Path("/etc/tsumgi-qwen12k-should-not-create"), proposal)
    finally:
        if output.exists():
            shutil.rmtree(output)


def test_symlinked_tmp_parent_and_target_are_rejected(tmp_path: Path):
    runtime, serving, catalog = fixtures()
    proposal = make_draft(runtime, serving, catalog)
    symlink = Path("/tmp") / ("tsumgi-qwen12k-link-" + uuid4().hex)
    escaped_name = "tsumgi-qwen12k-escaped-" + uuid4().hex
    target = tmp_path / escaped_name
    symlink.symlink_to(tmp_path, target_is_directory=True)
    try:
        with pytest.raises(ValueError, match="output-must-not-exist"):
            qwen._emit_private_draft(symlink, proposal)
        with pytest.raises(ValueError, match="output-must-be-new-direct-tmp-child"):
            qwen._emit_private_draft(symlink / escaped_name, proposal)
        assert not target.exists()
    finally:
        symlink.unlink()


def test_symlink_inserted_between_existence_check_and_atomic_mkdir(
    tmp_path: Path, monkeypatch,
):
    runtime, serving, catalog = fixtures()
    proposal = make_draft(runtime, serving, catalog)
    output = Path("/tmp") / ("tsumgi-qwen12k-race-" + uuid4().hex)
    original_mkdir = os.mkdir

    def racing_mkdir(path, mode=0o777, *, dir_fd=None):
        if os.fspath(path) == os.fspath(output):
            output.symlink_to(tmp_path, target_is_directory=True)
        return original_mkdir(path, mode=mode, dir_fd=dir_fd)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(qwen.os, "mkdir", racing_mkdir)
            with pytest.raises(FileExistsError):
                qwen._emit_private_draft(output, proposal)
        assert not (tmp_path / "runtime.UNQUALIFIED.json").exists()
    finally:
        if output.is_symlink():
            output.unlink()


def test_directory_swap_before_fd_open_does_not_follow_symlink(
    tmp_path: Path, monkeypatch,
):
    runtime, serving, catalog = fixtures()
    proposal = make_draft(runtime, serving, catalog)
    output = Path("/tmp") / ("tsumgi-qwen12k-race-" + uuid4().hex)
    displaced = Path("/tmp") / ("tsumgi-qwen12k-displaced-" + uuid4().hex)
    original_open = os.open

    def racing_open(path, flags, mode=0o777, *, dir_fd=None):
        if os.fspath(path) == os.fspath(output) and dir_fd is None:
            output.rename(displaced)
            output.symlink_to(tmp_path, target_is_directory=True)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(qwen.os, "open", racing_open)
            with pytest.raises(OSError):
                qwen._emit_private_draft(output, proposal)
        assert not (tmp_path / "runtime.UNQUALIFIED.json").exists()
        assert not (tmp_path / "serving.INVALID.json").exists()
    finally:
        if output.is_symlink():
            output.unlink()
        if displaced.is_dir():
            displaced.rmdir()


def test_cli_only_reports_shape_and_never_outputs_model_paths(tmp_path: Path):
    runtime, serving, catalog = fixtures()
    for name, data in [("runtime", runtime), ("serving", serving), ("control", catalog)]:
        (tmp_path / f"{name}.json").write_text(json.dumps(data))
    (tmp_path / "reviewed-pin.json").write_text(json.dumps(reviewed_pin()))
    # Keep the isolated test environment's read-only dependency path for
    # subprocess CLI smoke; never depend on a host-global Python install.
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(filter(None, (
        str(ROOT / "src"), os.environ.get("PYTHONPATH"),
    ))))
    result = subprocess.run(
        [sys.executable, str(TOOL), "--runtime", str(tmp_path / "runtime.json"),
         "--serving", str(tmp_path / "serving.json"),
         "--control", str(tmp_path / "control.json"),
         "--reviewed-pin", str(tmp_path / "reviewed-pin.json")],
        env=env, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert data["status"] == "DENIED_PENDING_REVIEW_AND_REAL_CANARY"
    assert not data["private_draft_written"]
    assert "/synthetic-not-actual/" not in result.stdout
    assert "/synthetic-not-actual/" not in result.stderr


# Exact-byte 8K rollback preservation is independent from 12K draft creation.
def rollback_module():
    # Only the source checkout's operator tools, never an installed runtime.
    import sys
    tools_dir = str(ROOT / "tools")
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    spec = importlib.util.spec_from_file_location(
        "qwen_8k_rollback_snapshot", ROOT / "tools/qwen_8k_rollback_snapshot.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def rollback_fixture_files(tmp_path: Path) -> dict[str, Path]:
    baseline_runtime, baseline_serving, baseline_catalog = fixtures()
    docs = {
        "runtime.json": json.dumps(baseline_runtime).encode(),
        "serving.json": json.dumps(baseline_serving).encode(),
        "catalog.json": json.dumps(baseline_catalog).encode(),
        "reviewed-pin.json": json.dumps(reviewed_pin()).encode(),
        "worker.toml": b"[worker]\nsynthetic_secret = 'DO_NOT_PRINT_SECRET'\n",
        "worker.service": b"[Service]\nExecStart=/fake/worker\n",
        "30-coding-runtime.conf": b"[Service]\nEnvironment=CODE=SAFE\n",
        "40-qwen38-identity.conf": b"[Service]\nEnvironment=IDENTITY=QWEN\n",
        "control.toml": b"[control]\nsynthetic_secret = 'DO_NOT_PRINT_SECRET'\n",
    }
    return {name: (tmp_path / name) for name in docs
            if (tmp_path / name).write_bytes(docs[name]) == len(docs[name])}


def test_exact_8k_snapshot_capture_verify_and_disposable_rehearsal(tmp_path: Path):
    rb = rollback_module()
    sources = rollback_fixture_files(tmp_path)
    contents = {k: p.read_bytes() for k, p in sources.items()}
    root = rb.capture(sources)
    try:
        assert root.is_dir() and root.parent == Path("/tmp")
        assert root.name.startswith(rb.PREFIX)
        assert root.stat().st_mode & 0o777 == 0o700
        assert set(p.name for p in root.iterdir()) == set(rb.REQUIRED) | {"receipt.json"}
        assert all(p.stat().st_mode & 0o777 == 0o600 for p in root.iterdir())
        assert rb.verify(root)["status"] == "EXACT_8K_BYTES_VERIFIED"
        proof = rb.verify(root, rehearse=True)
        assert proof["status"] == "EXACT_8K_BYTES_VERIFIED_IN_DISPOSABLE_SANDBOX"
        assert proof["files_verified"] == len(rb.REQUIRED)
        assert proof["nix_closure_preserved"] is False
        assert proof["live_restore_exercised"] is False
        assert {k: p.read_bytes() for k, p in sources.items()} == contents
        receipt = json.loads((root / "receipt.json").read_text())
        assert receipt["signed_or_authenticated"] is False
        assert receipt["nix_runtime_closure_backed_up"] is False
        assert "DO_NOT_PRINT_SECRET" not in json.dumps(receipt)
    finally:
        shutil.rmtree(root)


def test_8k_snapshot_refuses_postcapture_corruption(tmp_path: Path):
    rb = rollback_module()
    root = rb.capture(rollback_fixture_files(tmp_path))
    try:
        (root / "worker.toml").write_bytes(b"tampered")
        with pytest.raises(ValueError, match="snapshot-file-hash-mismatch"):
            rb.verify(root)
    finally:
        shutil.rmtree(root)


def test_8k_snapshot_refuses_missing_systemd_dropins(tmp_path: Path):
    rb = rollback_module()
    sources = rollback_fixture_files(tmp_path)
    for key in ("30-coding-runtime.conf", "40-qwen38-identity.conf"):
        incomplete = dict(sources)
        del incomplete[key]
        with pytest.raises(ValueError, match="source-arguments-incomplete"):
            rb.capture(incomplete)


@pytest.mark.parametrize("dropin", [
    "30-coding-runtime.conf", "40-qwen38-identity.conf",
])
def test_8k_snapshot_detects_dropin_tampering(tmp_path: Path, dropin: str):
    rb = rollback_module()
    root = rb.capture(rollback_fixture_files(tmp_path))
    try:
        (root / dropin).write_bytes(b"[Service]\\nMOCKED=1\\n")
        with pytest.raises(ValueError, match="snapshot-file-hash-mismatch"):
            rb.verify(root)
    finally:
        shutil.rmtree(root)


def test_v1_seven_member_snapshot_receipt_is_not_accepted(tmp_path: Path):
    rb = rollback_module()
    root = rb.capture(rollback_fixture_files(tmp_path))
    try:
        receipt = root / "receipt.json"
        value = json.loads(receipt.read_text())
        value["schema_version"] = "qwen8k-rollback-snapshot-v1"
        receipt.write_text(json.dumps(value))
        with pytest.raises(ValueError, match="invalid-receipt"):
            rb.verify(root)
    finally:
        shutil.rmtree(root)


def test_8k_snapshot_rejects_symlinked_source(tmp_path: Path):
    rb = rollback_module()
    sources = rollback_fixture_files(tmp_path)
    link = tmp_path / "symlink-worker.toml"
    link.symlink_to(sources["worker.toml"])
    sources["worker.toml"] = link
    with pytest.raises(OSError):
        rb.capture(sources)


def test_8k_snapshot_rejects_symlinked_member(tmp_path: Path):
    rb = rollback_module()
    root = rb.capture(rollback_fixture_files(tmp_path))
    try:
        member = root / "worker.toml"
        member.unlink()
        member.symlink_to(tmp_path / "worker.toml")
        with pytest.raises(OSError):
            rb.verify(root)
        assert (tmp_path / "worker.toml").is_file()
    finally:
        shutil.rmtree(root)


def test_8k_snapshot_rejects_relaxed_directory_permissions(tmp_path: Path):
    rb = rollback_module()
    root = rb.capture(rollback_fixture_files(tmp_path))
    try:
        root.chmod(0o755)
        with pytest.raises(ValueError, match="snapshot-directory-not-owner-only"):
            rb.verify(root)
    finally:
        root.chmod(0o700)
        shutil.rmtree(root)


def test_8k_snapshot_rejects_oversized_source(tmp_path: Path):
    rb = rollback_module()
    sources = rollback_fixture_files(tmp_path)
    sources["worker.toml"].write_bytes(b"x" * (rb.MAX_BYTES + 1))
    with pytest.raises(ValueError, match="source-not-bounded-regular-file"):
        rb.capture(sources)


def test_8k_snapshot_rejects_extra_members_and_modified_receipt(tmp_path: Path):
    rb = rollback_module()
    root = rb.capture(rollback_fixture_files(tmp_path))
    try:
        (root / "unreviewed.json").write_text("{}")
        with pytest.raises(ValueError, match="unexpected-snapshot-files"):
            rb.verify(root)
        (root / "unreviewed.json").unlink()
        receipt_path = root / "receipt.json"
        d = json.loads(receipt_path.read_text())
        d["nix_runtime_closure_backed_up"] = True
        receipt_path.write_text(json.dumps(d))
        with pytest.raises(ValueError, match="invalid-receipt"):
            rb.verify(root)
    finally:
        shutil.rmtree(root)


def test_8k_snapshot_rejects_symlinked_snapshot_root(tmp_path: Path):
    rb = rollback_module()
    root = rb.capture(rollback_fixture_files(tmp_path))
    alias = Path("/tmp") / (rb.PREFIX + "symlink-" + uuid4().hex)
    alias.symlink_to(root, target_is_directory=True)
    try:
        with pytest.raises(ValueError, match="not-private-direct-tmp-child"):
            rb.verify(alias)
    finally:
        alias.unlink()
        shutil.rmtree(root)


def test_8k_snapshot_rejects_source_changes_during_capture(tmp_path: Path, monkeypatch):
    rb = rollback_module()
    files = rollback_fixture_files(tmp_path)
    before = {p for p in Path("/tmp").glob(rb.PREFIX + "*") if p.is_dir()}
    original = rb._read_regular
    calls = 0

    def altered(path: Path) -> bytes:
        nonlocal calls
        calls += 1
        raw = original(path)
        if calls > len(rb.REQUIRED) and path == files["runtime.json"]:
            return raw + b" "
        return raw

    monkeypatch.setattr(rb, "_read_regular", altered)
    with pytest.raises(ValueError, match="source-changed-during-snapshot"):
        rb.capture(files)
    after = {p for p in Path("/tmp").glob(rb.PREFIX + "*") if p.is_dir()}
    assert before == after


def test_8k_snapshot_cli_check_capture_verify_rehearse(tmp_path: Path):
    rb = rollback_module()
    files = rollback_fixture_files(tmp_path)
    command = [sys.executable, str(ROOT / "tools/qwen_8k_rollback_snapshot.py")]
    args = []
    for name, opt in (
        ("runtime.json", "--runtime"), ("serving.json", "--serving"),
        ("catalog.json", "--catalog"), ("reviewed-pin.json", "--reviewed-pin"),
        ("worker.toml", "--worker-toml"), ("worker.service", "--worker-service"),
        ("30-coding-runtime.conf", "--worker-dropin-runtime"),
        ("40-qwen38-identity.conf", "--worker-dropin-identity"),
        ("control.toml", "--control-toml"),
    ):
        args.extend((opt, str(files[name])))
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(filter(None, (
        str(ROOT / "src"), os.environ.get("PYTHONPATH"),
    ))))

    def invoke(*parts: str):
        ret = subprocess.run(
            [*command, *parts], env=env, text=True,
            capture_output=True, check=False, timeout=15,
        )
        assert ret.returncode == 0, (ret.returncode, ret.stdout[-300:], ret.stderr[-300:])
        assert "DO_NOT_PRINT_SECRET" not in ret.stdout + ret.stderr
        assert "/synthetic-not-actual/" not in ret.stdout + ret.stderr
        return json.loads(ret.stdout)

    dry = invoke("check", *args)
    assert dry["status"] == "READ_ONLY_8K_BASELINE_VALIDATED"
    root = Path(invoke("capture", *args)["snapshot_dir"])
    try:
        assert invoke("verify", "--snapshot-dir", str(root))["files_verified"] == 9
        assert invoke("rehearse", "--snapshot-dir", str(root))["live_restore_exercised"] is False
        with pytest.raises(AssertionError):
            invoke("capture", "--snapshot-dir", str(root), *args)
    finally:
        shutil.rmtree(root)



# This is a synthetic, in-memory proof of the revision-chain shape.
# It does NOT establish the provenance of an actual 12K execution digest,
# canary validation record, GPU artifact or real serving eligibility.
def synthetic_revision_chain(draft, *, execution=None, evidence=None):
    execution = digest("8") if execution is None else execution
    evidence = digest("9") if evidence is None else evidence
    identity_raw = deepcopy(draft["serving_UNLOADABLE_draft"]["deployment"])
    identity_raw["execution_config_sha256"] = execution
    identity = DeploymentIdentity(**identity_raw)

    contract_raw = deepcopy(
        draft["serving_UNLOADABLE_draft"]["contracts"][0]["contract"]
    )
    contract_raw["deployment_revision"] = identity.revision
    contract_raw["validation_evidence_sha256"] = evidence
    contract = ServingContract(**contract_raw)
    serving = ServingDeploymentDeclaration(identity, (contract,))

    catalog_raw = deepcopy(draft["control_UNLOADABLE_draft"])
    matches = [
        row for row in catalog_raw["profiles"]
        if row["profile"]["profile_id"] == qwen.PROFILE_ID
    ]
    assert len(matches) == 1
    record = matches[0]
    profile_raw = record["profile"]
    profile_raw["deployment_revision"] = identity.revision
    profile_raw["serving_contract_revision"] = contract.revision
    profile = LogicalServingProfile(**profile_raw)

    record["deployment"] = identity.to_dict()
    record["contract"] = contract.to_dict()
    record["profile"] = profile.to_dict()
    catalog = ChatProfileCatalog.from_dict(catalog_raw)
    return serving, catalog, catalog_raw


def test_synthetic_12k_revision_chain_validates_exact_target_and_fences_8k():
    runtime, original_serving, original_catalog = fixtures(explicit_profile_limits=True)
    before = deepcopy((runtime, original_serving, original_catalog))
    draft = make_draft(runtime, original_serving, original_catalog)
    declared, catalog, catalog_raw = synthetic_revision_chain(draft)
    assert (runtime, original_serving, original_catalog) == before
    assert len(catalog.profiles) == 1
    original = ServingDeploymentDeclaration.from_dict(original_serving)
    assert declared.deployment.revision != original.deployment.revision
    assert declared.contracts[0].revision != original.contracts[0].revision
    q = catalog.get(qwen.PROFILE_ID)
    assert q.resolved.profile_revision != (
        ChatProfileCatalog.from_dict(original_catalog).get(qwen.PROFILE_ID).resolved.profile_revision
    )
    assert q.resolved.deployment == declared.deployment
    assert q.resolved.contract == declared.contracts[0]
    assert dict(q.resolved.effective_limits) == qwen.TARGET_LIMITS
    assert q.resolved.profile.profile_id == qwen.PROFILE_ID
    assert (q.resolved.profile.deployment_revision == declared.deployment.revision)
    assert q.resolved.profile.serving_contract_revision == declared.contracts[0].revision
    assert declared.deployment.execution_config_sha256 == digest("8")
    assert declared.contracts[0].validation_evidence_sha256 == digest("9")
    assert RuntimeDeploymentSpec.from_dict(draft["runtime_draft"]).provider_config[
        "context_size"
    ] == 12288
    assert ChatProfileCatalog.from_dict(catalog_raw).models_response()["data"][0][
        "x_astrumweaver"
    ]["effective_limits"]["input_tokens"] == 10240


@pytest.mark.parametrize(("request_limit","value"), [
    ("input_tokens", 10240),
    ("output_tokens", 2048),
    ("total_tokens", 12288),
    ("request_bytes", 65536),
])
def test_synthetic_12k_admission_accepts_boundary_but_rejects_one_more(request_limit, value):
    runtime, serving, catalog = fixtures()
    draft = make_draft(runtime, serving, catalog)
    _, new_catalog, _ = synthetic_revision_chain(draft)
    selected = new_catalog.get(qwen.PROFILE_ID)
    model = selected.resolved
    ok = resolve_profile(
        model.profile, model.contract, model.deployment,
        requested_usage={request_limit: value},
    )
    assert ok.profile_revision == model.profile_revision
    with pytest.raises(ServingContractError, match="limit-exceeded"):
        resolve_profile(
            model.profile, model.contract, model.deployment,
            requested_usage={request_limit: value + 1},
        )


def test_synthetic_12k_revision_chain_refuses_stale_8k_contract_and_profile():
    runtime, original_serving, original_catalog = fixtures()
    draft = make_draft(runtime, original_serving, original_catalog)
    new_serving, _, new_catalog_raw = synthetic_revision_chain(draft)
    old_id = original_serving["deployment"]
    new_id = new_serving.deployment.to_dict()
    with pytest.raises((ServingContractError, ValueError)):
        ServingDeploymentDeclaration.from_dict({
            **new_serving.to_dict(), "deployment": old_id,
        })
    original_row = original_catalog["profiles"][0]
    record = new_catalog_raw["profiles"][0]
    tamper = deepcopy(new_catalog_raw)
    tamper["profiles"][0]["profile"] = deepcopy(original_row["profile"])
    with pytest.raises(ChatGatewayError):
        ChatProfileCatalog.from_dict(tamper)
    tamper = deepcopy(new_catalog_raw)
    tamper["profiles"][0]["contract"] = deepcopy(original_row["contract"])
    with pytest.raises(ChatGatewayError):
        ChatProfileCatalog.from_dict(tamper)
    tamper = deepcopy(new_catalog_raw)
    tamper["profiles"][0]["deployment"] = deepcopy(old_id)
    with pytest.raises(ChatGatewayError):
        ChatProfileCatalog.from_dict(tamper)
    assert new_id != old_id


def test_synthetic_12k_revision_chain_does_not_modify_unrelated_catalog_profile():
    runtime, serving, catalog = fixtures()
    unrelated = deepcopy(catalog["profiles"][0])
    other = DeploymentIdentity(
        provider_id="llama-cpp",
        runtime_artifact_sha256=digest("a"),
        adapter_artifact_sha256=digest("b"),
        model_artifact_sha256=digest("c"),
        execution_config_sha256=digest("d"),
        quantization="Q4_K_M",
        tokenizer_artifact_sha256=digest("e"),
        template_artifact_sha256=digest("f"),
    )
    other_contract = ServingContract(
        deployment_revision=other.revision,
        capability="llm.chat",
        operation_schema=CHAT_OPERATION_SCHEMA,
        features=frozenset({"tools"}),
        limits=qwen.BASE_LIMITS,
        validation_evidence_sha256=digest("a"),
    )
    other_profile = LogicalServingProfile(
        profile_id="bonsai2-27b",
        deployment_revision=other.revision,
        serving_contract_revision=other_contract.revision,
        capability="llm.chat",
        operation_schema=CHAT_OPERATION_SCHEMA,
        required_features=frozenset({"tools"}),
    )
    unrelated.update(
        deployment=other.to_dict(),
        contract=other_contract.to_dict(),
        profile=other_profile.to_dict(),
    )
    catalog["profiles"].append(unrelated)
    before = deepcopy(catalog)
    _, new_catalog, new_catalog_raw = synthetic_revision_chain(
        make_draft(runtime, serving, catalog)
    )
    assert len(new_catalog.profiles) == 2
    assert new_catalog_raw["profiles"][1] == unrelated
    assert catalog == before


def test_synthetic_evidence_digests_are_not_production_attestations():
    runtime, serving, catalog = fixtures()
    draft = make_draft(runtime, serving, catalog)
    for bad in ("PENDING_REAL_12K_CANARY_EVIDENCE", "sha256:not-a-digest"):
        with pytest.raises((ServingContractError, ValueError)):
            synthetic_revision_chain(draft, evidence=bad)
    with pytest.raises((ServingContractError, ValueError)):
        synthetic_revision_chain(draft, execution="PENDING_UNREVIEWED")
    assert draft["admission"] == "DENIED_PENDING_REVIEW_AND_REAL_CANARY"
