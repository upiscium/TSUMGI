"""Disposable review-file apply/rollback; never install/start a Worker or engine."""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tomllib

import pytest

from astrumweaver.contracts import ResourceShape, WorkerSpec
from astrumweaver.runtime.contracts import RuntimeHostFacts
from astrumweaver.runtime.deployment import managed_runtime_from_deployment
from astrumweaver.runtime.providers import voicevox_release as release
from astrumweaver.runtime.providers.voicevox_manifest import ARCHIVE_SHA256, VoicevoxProviderConfig
from astrumweaver.setup import (
    ActionReceipt, DeploymentPath, SetupActionKind, SetupActionResultStatus,
    SetupApproval, SetupApprovalError, SetupHostSnapshot, SetupPlan, SetupPlanGoal,
    SystemdSetupDriver, apply_plan, dry_run_plan,
)
from astrumweaver.setup.voicevox_staging import (
    VoicevoxStagingDriver, VoicevoxStagingError, VoicevoxStagingRollbackError,
    build_voicevox_staging_plan,
)
from astrumweaver.worker import daemon


def _sha(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


@pytest.fixture
def request_data(tmp_path, monkeypatch):
    engine = tmp_path / "assets/engine"
    (engine / "model").mkdir(parents=True)
    (engine / "run").write_bytes(b"synthetic engine; never execute")
    (engine / "run").chmod(0o700)
    (engine / "model/0.vvm").write_bytes(b"synthetic VVM")
    (engine / "dependency.so").write_bytes(b"synthetic shared dependency")
    monkeypatch.setattr(release, "EXECUTABLE_SHA256", _sha((engine / "run").read_bytes()))
    monkeypatch.setattr(release, "MODEL_SHA256", _sha((engine / "model/0.vvm").read_bytes()))
    config = VoicevoxProviderConfig(
        enabled=True, engine_root=str(engine), private_home="/var/lib/astrumweaver-audio/staged/home",
        port=19001, archive_sha256=ARCHIVE_SHA256,
        rights_review_sha256="sha256:" + "a" * 64,
        validation_evidence_sha256="sha256:" + "b" * 64,
    )
    receipt = tmp_path / "assets/bundle.json"
    release.record_bundle(engine, receipt, rights_review_sha256=config.rights_review_sha256)
    return {
        "enabled": True, "config": config, "serving": config.serving_declaration(),
        "worker": WorkerSpec("synthetic-audio", "cpu-audio", ResourceShape(),
                             capabilities=frozenset({"audio.synthesize"})),
        "snapshot": SetupHostSnapshot(
            RuntimeHostFacts(cpu_count=2, host_ram_mb=16384, architecture="x86_64"),
            DeploymentPath.SYSTEMD, "debian", service_manager="systemd", package_manager="nix",
        ),
        "service_user": "audio-stage", "service_group": "audio-stage",
        "environment_file": "/etc/astrumweaver/audio-voicevox.env",
        "control_url": "http://127.0.0.1:19000",
        "worker_executable": "/reviewed/bin/astrumweaver-worker",
        "bundle_receipt_path": str(receipt),
    }


def _root(tmp_path):
    root = tmp_path / "staging"
    root.mkdir(mode=0o700)
    return root


def _approval(plan):
    return SetupApproval(plan.digest, allow_confirmation_actions=True)


def _files(root):
    return {str(p.relative_to(root)): (p.read_bytes(), stat.S_IMODE(p.stat().st_mode))
            for p in root.rglob("*") if p.is_file()}


def test_disabled_plan_never_inspects_inputs():
    class Bomb:
        def __getattribute__(self, _):
            pytest.fail("disabled plan inspected an input")
    assert build_voicevox_staging_plan(config=Bomb(), serving=Bomb(), snapshot=Bomb()) is None
    with pytest.raises(VoicevoxStagingError):
        build_voicevox_staging_plan(enabled="true")


@pytest.mark.parametrize("deployment", [DeploymentPath.SYSTEMD, DeploymentPath.NIXOS])
def test_apply_roundtrip_is_offline_idempotent_and_never_activates(
    request_data, tmp_path, monkeypatch, deployment,
):
    data = dict(request_data)
    data["snapshot"] = replace(data["snapshot"], deployment_path=deployment,
                               os_id="nixos" if deployment is DeploymentPath.NIXOS else "debian")
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("process started"))
    assets_before = _files(tmp_path / "assets")
    plan = build_voicevox_staging_plan(**data)
    assert plan == build_voicevox_staging_plan(**data)
    assert SetupPlan.from_json(plan.to_json()).digest == plan.digest
    assert plan.goal is SetupPlanGoal.STAGE
    assert not plan.requires_network and not plan.requires_privilege
    assert plan.requires_confirmation and plan.metadata["activation_allowed"] is False
    assert {action.kind for action in plan.actions} == {
        SetupActionKind.VERIFY_AUDIO_BUNDLE, SetupActionKind.STAGE_AUDIO_CONFIG,
    }
    root = _root(tmp_path)
    driver = VoicevoxStagingDriver(plan, root=root)
    assert not dry_run_plan(plan, driver).blocked
    assert list(root.iterdir()) == []
    with pytest.raises(SetupApprovalError):
        apply_plan(plan, driver, approval=SetupApproval(plan.digest))
    with pytest.raises(SetupApprovalError):
        apply_plan(plan, driver, approval=SetupApproval("0" * 64, allow_confirmation_actions=True))
    assert list(root.iterdir()) == []
    result = apply_plan(plan, driver, approval=_approval(plan))
    assert result.succeeded and sum(action.changed for action in result.actions) == 4
    first = _files(root)
    second = apply_plan(plan, driver, approval=_approval(plan))
    assert second.succeeded and not any(action.changed for action in second.actions)
    assert _files(root) == first and _files(tmp_path / "assets") == assets_before
    staged = root / "voicevox"
    worker_settings = tomllib.loads((staged / "worker.toml").read_text())
    runtime = daemon._load_runtime_deployment(str(staged / "runtime-deployment.json"))
    serving = daemon._load_serving_deployment(str(staged / "serving-deployment.json"))
    worker = daemon._build_spec(worker_settings["worker"])
    assert worker == data["worker"]
    assert worker_settings["runtime"]["manifest"].endswith("/voicevox/runtime-deployment.json")
    if os.geteuid() != 0:
        managed = managed_runtime_from_deployment(
            runtime, worker=worker, host=data["snapshot"].runtime_host, serving=serving,
        )
        assert managed.provider_id == "voicevox"
    if deployment is DeploymentPath.NIXOS:
        options = json.loads((staged / "nixos-worker-options.json").read_text())
        assert options["enable"] is False
        assert options["runtime"]["providerConfig"] == runtime.to_dict()["provider_config"]
        assert options["serving"]["declaration"] == serving.to_dict()
        assert options["runtime"]["packages"] == []
    else:
        unit = (staged / "worker.service").read_text()
        for field in (
            "User=audio-stage", "Group=audio-stage", "UMask=0077", "Restart=no",
            "KillMode=control-group", "ProtectSystem=strict", "ProtectHome=true",
            "EnvironmentFile=/etc/astrumweaver/audio-voicevox.env",
        ):
            assert unit.count(field) == 1
        assert "StateDirectory=" not in unit and "WantedBy=" not in unit
    assert "etc" not in {p.name for p in root.iterdir()}


@pytest.mark.parametrize(("field", "value"), [
    ("service_user", "0"), ("service_user", "root"), ("service_user", "astrumweaver"),
    ("service_group", "0"), ("service_group", "root"),
    ("environment_file", "/etc/../tmp/environment"), ("environment_file", "/tmp/environment"),
    ("worker_executable", "/reviewed/bin/worker --unsafe"),
    ("control_url", "http://0.0.0.0:19000"), ("control_url", "http://127.0.0.1:65536"),
])
def test_plan_rejects_unsafe_declared_configuration(request_data, field, value):
    with pytest.raises(VoicevoxStagingError):
        build_voicevox_staging_plan(**{**request_data, field: value})


def test_plan_rejects_manifest_and_worker_mismatches(request_data):
    config = request_data["config"]
    with pytest.raises(VoicevoxStagingError):
        build_voicevox_staging_plan(**{**request_data, "serving": replace(config, port=19002).serving_declaration()})
    with pytest.raises(VoicevoxStagingError):
        build_voicevox_staging_plan(**{
            **request_data, "worker": replace(request_data["worker"], capabilities={"audio.transcribe"}),
        })


def test_corrupt_bundle_blocks_all_staging(request_data, tmp_path):
    plan = build_voicevox_staging_plan(**request_data)
    driver = VoicevoxStagingDriver(plan, root=_root(tmp_path))
    (Path(request_data["config"].engine_root) / "dependency.so").write_bytes(b"drift")
    assert dry_run_plan(plan, driver).blocked
    result = apply_plan(plan, driver, approval=_approval(plan))
    assert not result.succeeded and not _files(driver.root)


@pytest.mark.parametrize("failure_index", [1, 2, 3, 4])
def test_failure_rolls_back_prior_and_new_files(request_data, tmp_path, failure_index):
    plan = build_voicevox_staging_plan(**request_data)
    root = _root(tmp_path)
    (root / "voicevox").mkdir()
    prior = root / "voicevox/runtime-deployment.json"
    prior.write_text("previous reviewed candidate\n")
    prior.chmod(0o640)
    before, assets_before = _files(root), _files(tmp_path / "assets")

    class FailingDriver(VoicevoxStagingDriver):
        def apply(self, action):
            if action == plan.actions[failure_index]:
                raise RuntimeError("synthetic failure")
            return super().apply(action)

    result = apply_plan(plan, FailingDriver(plan, root=root), approval=_approval(plan))
    assert not result.succeeded
    assert all(r.status is SetupActionResultStatus.ROLLED_BACK for r in result.rollback_actions)
    assert _files(root) == before
    assert _files(tmp_path / "assets") == assets_before


def test_post_replace_failure_recovers_the_current_action(request_data, tmp_path, monkeypatch):
    plan = build_voicevox_staging_plan(**request_data)
    root = _root(tmp_path)
    (root / "voicevox").mkdir()
    path = root / "voicevox/serving-deployment.json"
    path.write_text("previous serving candidate\n")
    before = _files(root)
    driver = VoicevoxStagingDriver(plan, root=root)
    write = driver.filesystem.write_text

    def failed_write(target, content, **kwargs):
        write(target, content, **kwargs)
        if target == path and content == plan.actions[2].payload["content"]:
            raise OSError("synthetic fsync failure after replacement")

    monkeypatch.setattr(driver.filesystem, "write_text", failed_write)
    result = apply_plan(plan, driver, approval=_approval(plan))
    assert not result.succeeded
    assert _files(root) == before


def test_rollback_preserves_intervening_operator_edit(request_data, tmp_path):
    plan = build_voicevox_staging_plan(**request_data)
    root = _root(tmp_path)

    class DriftDriver(VoicevoxStagingDriver):
        def apply(self, action):
            if action == plan.actions[-1]:
                (root / "voicevox/runtime-deployment.json").write_text("operator change\n")
                raise RuntimeError("synthetic failure")
            return super().apply(action)

    result = apply_plan(plan, DriftDriver(plan, root=root), approval=_approval(plan))
    assert not result.succeeded
    assert any(r.status is SetupActionResultStatus.ROLLBACK_FAILED for r in result.rollback_actions)
    assert _files(root) == {"voicevox/runtime-deployment.json": (b"operator change\n", 0o600)}


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "directory_link"])
def test_unsafe_staged_target_is_not_overwritten(request_data, tmp_path, kind):
    plan = build_voicevox_staging_plan(**request_data)
    root = _root(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    original = outside / "runtime-deployment.json"
    original.write_text("unrelated\n")
    if kind == "directory_link":
        (root / "voicevox").symlink_to(outside, target_is_directory=True)
    else:
        (root / "voicevox").mkdir()
        target = root / "voicevox/runtime-deployment.json"
        if kind == "symlink":
            target.symlink_to(original)
        else:
            os.link(original, target)
    driver = VoicevoxStagingDriver(plan, root=root)
    assert not apply_plan(plan, driver, approval=_approval(plan)).succeeded
    assert original.read_text() == "unrelated\n"


def test_driver_rejects_live_or_linked_root_and_foreign_actions(request_data, tmp_path):
    plan = build_voicevox_staging_plan(**request_data)
    with pytest.raises(VoicevoxStagingError):
        VoicevoxStagingDriver(plan, root=Path("/"))
    root = _root(tmp_path)
    link = tmp_path / "linked"
    link.symlink_to(root, target_is_directory=True)
    with pytest.raises(VoicevoxStagingError):
        VoicevoxStagingDriver(plan, root=link)
    driver = VoicevoxStagingDriver(plan, root=root)
    with pytest.raises(VoicevoxStagingError):
        driver.apply(plan.actions[1])  # No successful verification action.
    with pytest.raises(VoicevoxStagingError):
        driver.inspect(replace(plan.actions[1], payload={"filename": "../outside", "content": "bad"}))
    with pytest.raises(VoicevoxStagingRollbackError):
        driver.rollback(plan.actions[1], ActionReceipt(True))
    with pytest.raises(VoicevoxStagingError):
        VoicevoxStagingDriver(replace(plan, goal=SetupPlanGoal.ACTIVATE), root=root)
    assert not _files(root)


def test_generic_systemd_driver_never_accepts_staging_actions(request_data, tmp_path):
    plan = build_voicevox_staging_plan(**request_data)
    root = _root(tmp_path)
    result = apply_plan(plan, SystemdSetupDriver(root=root), approval=_approval(plan))
    assert not result.succeeded and not _files(root)
    assert result.actions[0].status is SetupActionResultStatus.BLOCKED


def test_staging_root_cannot_be_the_input_bundle(request_data):
    plan = build_voicevox_staging_plan(**request_data)
    engine = Path(request_data["config"].engine_root)
    engine.chmod(0o700)
    before = _files(engine)
    with pytest.raises(VoicevoxStagingError):
        VoicevoxStagingDriver(plan, root=engine)
    assert _files(engine) == before


def test_stage_root_permissions_are_never_repaired(request_data, tmp_path):
    plan = build_voicevox_staging_plan(**request_data)
    root = tmp_path / "wrong-mode"
    root.mkdir(mode=0o755)
    with pytest.raises(VoicevoxStagingError):
        VoicevoxStagingDriver(plan, root=root)
    assert stat.S_IMODE(root.stat().st_mode) == 0o755
    assert not list(root.iterdir())
