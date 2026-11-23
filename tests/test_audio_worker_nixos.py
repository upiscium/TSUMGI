from __future__ import annotations

import json
import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import pytest

_TOOL = Path(__file__).resolve().parents[1] / "tools" / "check_audio_worker_nixos.py"
_SPEC = importlib.util.spec_from_file_location("check_audio_worker_nixos", _TOOL)
assert _SPEC and _SPEC.loader
checker = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = checker
_SPEC.loader.exec_module(checker)


def test_generates_three_synthetic_manifest_cases(tmp_path: Path) -> None:
    cases = checker.generate_cases(tmp_path)
    first_bytes = (tmp_path / "manifest.json").read_bytes()
    other = tmp_path / "deterministic"
    checker.generate_cases(other)
    assert (other / "manifest.json").read_bytes() == first_bytes
    assert [case["provider"] for case in cases] == [
        "voicevox", "style-bert-vits2", "parakeet-ja",
    ]
    assert json.loads((tmp_path / "manifest.json").read_text()) == cases
    for case in cases:
        assert case["worker"]["user"] == "audio-test"
        assert case["worker"]["group"] == "audio-test"
        assert case["worker"]["environment_file"] == "/run/secrets/audio.env"
        assert case["serving"]["deployment"]["provider_id"] == case["provider"]
        assert case["runtime"]["provider_id"] == case["provider"]
        assert case["runtime"]["demand"]["model"]["model_ref"]
        assert all("/var/lib/reviewed-" in value
                   for value in case["provider_config"].values()
                   if isinstance(value, str) and value.startswith("/")
                   and "/tsumgi-audio/" not in value)


def test_evaluate_uses_offline_nix_and_no_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text("[]")
    observed: list[tuple[list[str], dict]] = []

    def fake_run(command, **kwargs):
        observed.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "[]", "")

    monkeypatch.setattr(checker.subprocess, "run", fake_run)
    checker.evaluate(tmp_path, manifest)
    command, options = observed[0]
    assert command[:3] == ["nix", "eval", "--offline"]
    assert "--no-update-lock-file" in command
    assert "--no-write-lock-file" in command
    assert "build" not in command
    assert options["timeout"] == 240
    assert options["capture_output"] and options["text"] and not options["check"]


def _evaluation_rows(cases: list[dict]) -> list[dict]:
    rows = []
    for case in cases:
        worker = case["worker"]
        rows.append({
            "name": case["name"], "assertionsPassed": True, "serviceContracts": True,
            "reviewed": {
                "runtime": case["runtime"], "serving": case["serving"],
                "settings": {"worker": {
                    "id": worker["worker_id"], "class": worker["worker_class"],
                    "capabilities": worker["capabilities"], "gpu_uuids": worker["gpu_uuids"],
                    "gpu_count": len(worker["gpu_uuids"]),
                    "total_vram_mb": worker["total_vram_mb"],
                    "max_single_gpu_vram_mb": worker["max_single_gpu_vram_mb"],
                }},
            },
        })
    return rows


def test_check_accepts_and_rejects_evaluated_contracts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases = checker.generate_cases(tmp_path / "generated")
    rows = _evaluation_rows(cases)
    monkeypatch.setattr(checker, "evaluate", lambda *_: subprocess.CompletedProcess(
        ["nix"], 0, json.dumps(rows), ""))
    monkeypatch.setattr(
        "tsumgi.runtime.deployment.managed_runtime_from_deployment",
        lambda deployment, **_: type("Managed", (), {"provider_id": deployment.provider_id})(),
    )
    checker.check(tmp_path, tmp_path / "accepted")

    for field, bad in (("assertionsPassed", False), ("serviceContracts", False)):
        invalid = json.loads(json.dumps(rows))
        invalid[0][field] = bad
        monkeypatch.setattr(checker, "evaluate", lambda *_, value=invalid:
                            subprocess.CompletedProcess(["nix"], 0, json.dumps(value), ""))
        with pytest.raises(RuntimeError):
            checker.check(tmp_path, tmp_path / f"bad-{field}")

    for field in ("runtime", "serving"):
        invalid = json.loads(json.dumps(rows))
        invalid[0]["reviewed"][field]["drift"] = True
        monkeypatch.setattr(checker, "evaluate", lambda *_, value=invalid:
                            subprocess.CompletedProcess(["nix"], 0, json.dumps(value), ""))
        with pytest.raises(RuntimeError):
            checker.check(tmp_path, tmp_path / f"bad-{field}")


def _daemon_factory(case: dict, root: Path):
    from tsumgi.contracts import ResourceShape, WorkerSpec
    from tsumgi.runtime.contracts import RuntimeHostFacts
    from tsumgi.runtime.deployment import managed_runtime_from_deployment
    from tsumgi.worker import daemon

    worker_data = case["worker"]
    worker = WorkerSpec(
        worker_id=worker_data["worker_id"], worker_class=worker_data["worker_class"],
        resources=ResourceShape(
            gpu_count=len(worker_data["gpu_uuids"]),
            total_vram_mb=worker_data["total_vram_mb"],
            max_single_gpu_vram_mb=worker_data["max_single_gpu_vram_mb"]),
        gpu_uuids=tuple(worker_data["gpu_uuids"]),
        capabilities=frozenset(worker_data["capabilities"]),
    )
    runtime_path, serving_path = root / "runtime.json", root / "serving.json"
    runtime_path.write_text(json.dumps(case["runtime"]))
    serving_path.write_text(json.dumps(case["serving"]))
    runtime = daemon._load_runtime_deployment(str(runtime_path))
    serving = daemon._load_serving_deployment(str(serving_path))
    return managed_runtime_from_deployment(
        runtime, worker=worker,
        host=RuntimeHostFacts(cpu_count=1, host_ram_mb=16384, architecture="x86_64"),
        serving=serving)


def test_runtime_factory_constructs_all_providers_without_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.geteuid() == 0:
        pytest.skip("owned audio providers require an unprivileged process")
    cases = checker.generate_cases(tmp_path)
    monkeypatch.setattr(
        subprocess, "Popen",
        lambda *a, **k: pytest.fail("manifest factory attempted to launch a child"),
    )
    for case in cases:
        root = tmp_path / case["name"]
        root.mkdir()
        runtime = _daemon_factory(case, root)
        assert runtime.provider_id == case["provider"]


def test_runtime_factory_rejects_disabled_or_drifted_manifest_without_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tsumgi.worker import daemon

    if os.geteuid() == 0:
        pytest.skip("owned audio providers require an unprivileged process")
    cases = checker.generate_cases(tmp_path)
    monkeypatch.setattr(
        subprocess, "Popen",
        lambda *a, **k: pytest.fail("manifest factory attempted to launch a child"),
    )
    for case in cases:
        changed = dict(case["runtime"])
        config = dict(changed["provider_config"])
        config["enabled"] = False
        changed["provider_config"] = config
        deployment_path = tmp_path / f"{case['name']}-disabled.json"
        deployment_path.write_text(json.dumps(changed))
        runtime = daemon._load_runtime_deployment(str(deployment_path))
        altered_case = {**case, "runtime": runtime.to_dict()}
        factory_root = tmp_path / f"{case['name']}-disabled"
        factory_root.mkdir()
        with pytest.raises((RuntimeError, ValueError)):
            _daemon_factory(altered_case, factory_root)

    # A changed serving identity must not bind to the otherwise valid runtime.
    altered = dict(cases[0]["serving"])
    altered["deployment"] = dict(altered["deployment"], provider_id="not-voicevox")
    serving_path = tmp_path / "serving-drift.json"
    serving_path.write_text(json.dumps(altered))
    with pytest.raises(RuntimeError):
        daemon._load_serving_deployment(str(serving_path))
    runtime_path = tmp_path / "voicevox-runtime.json"
    runtime_path.write_text(json.dumps(cases[0]["runtime"]))
    valid_serving_path = tmp_path / "voicevox-serving.json"
    valid_serving_path.write_text(json.dumps(cases[0]["serving"]))
    # A validly encoded but altered demand is rejected against the manifest
    # provider's exact demand contract before runtime construction.
    mutated = json.loads(json.dumps(cases[0]["runtime"]))
    mutated["demand"]["model"]["model_ref"] = "/synthetic/model-drift"
    demand_path = tmp_path / "voicevox-demand-drift.json"
    demand_path.write_text(json.dumps(mutated))
    drifted = daemon._load_runtime_deployment(str(demand_path))
    worker = {
        **cases[0]["worker"], "gpu_uuids": [], "total_vram_mb": 0, "max_single_gpu_vram_mb": 0,
    }
    demand_case = {**cases[0], "runtime": drifted.to_dict(), "worker": worker}
    demand_root = tmp_path / "demand-factory"
    demand_root.mkdir()
    with pytest.raises(RuntimeError):
        _daemon_factory(demand_case, demand_root)
