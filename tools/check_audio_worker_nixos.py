"""Synthetic parity and offline evaluation for the audio-worker Nix module."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import sys
import tempfile


def generate_cases(directory: Path) -> list[dict]:
    from tsumgi.runtime.providers.parakeet_manifest import ParakeetManifestConfig
    from tsumgi.runtime.providers.style_bert_vits2_manifest import StyleBertManifestConfig
    from tsumgi.runtime.providers.voicevox_manifest import ARCHIVE_SHA256, VoicevoxProviderConfig
    from tsumgi.runtime.deployment import RuntimeDeploymentSpec
    from tsumgi.runtime.providers.parakeet_manifest import DEMAND as PARAKEET_DEMAND
    from tsumgi.runtime.providers.style_bert_vits2_manifest import _DEMAND as STYLE_DEMAND
    from tsumgi.runtime.providers.voicevox_manifest import _DEMAND as VOICEVOX_DEMAND

    directory.mkdir(parents=True, exist_ok=True)
    fake = "sha256:" + "a" * 64
    configs = {
        "voicevox": VoicevoxProviderConfig(
            engine_root="/var/lib/reviewed-voicevox/engine",
            private_home="/var/lib/tsumgi-audio/voicevox/home", port=19001,
            archive_sha256=ARCHIVE_SHA256, rights_review_sha256=fake,
            validation_evidence_sha256="sha256:" + "b" * 64, enabled=True),
        "style-bert-vits2": StyleBertManifestConfig(
            model_directory="/var/lib/reviewed-jvnv/jvnv-F1-jp",
            interpreter_path="/var/lib/reviewed-jvnv/venv/bin/python",
            dependency_lock_path="/var/lib/reviewed-jvnv/requirements.lock",
            dictionary_directory="/var/lib/reviewed-jvnv/dictionary",
            dictionary_sha256="sha256:" + "c" * 64,
            private_home="/var/lib/tsumgi-audio/style-bert-vits2/home",
            rights_review_sha256=fake, validation_evidence_sha256="sha256:" + "d" * 64,
            enabled=True),
        "parakeet-ja": ParakeetManifestConfig(
            model_path="/var/lib/reviewed-parakeet/model.nemo",
            interpreter_path="/var/lib/reviewed-parakeet/venv/bin/python",
            base_python_root="/var/lib/reviewed-parakeet/python",
            dependency_lock_path="/var/lib/reviewed-parakeet/requirements.lock",
            private_home="/var/lib/tsumgi-audio/parakeet-ja/home",
            gpu_uuid="GPU-12345678-1234-1234-1234-123456789abc",
            rights_review_sha256=fake, validation_evidence_sha256="sha256:" + "e" * 64,
            enabled=True),
    }
    cases = []
    demands = {"voicevox": VOICEVOX_DEMAND, "style-bert-vits2": STYLE_DEMAND,
               "parakeet-ja": PARAKEET_DEMAND}
    for provider, config in configs.items():
        runtime = RuntimeDeploymentSpec(
            provider_id=provider, provider_config=asdict(config), demand=demands[provider],
        ).to_dict()
        cases.append({
            "name": provider, "provider": provider,
            "worker": {"worker_id": "synthetic-audio", "worker_class": "audio-test",
                        "control_url": "http://127.0.0.1:19000", "user": "audio-test",
                        "group": "audio-test",
                        "capabilities": ["audio.transcribe"] if provider == "parakeet-ja"
                        else ["audio.synthesize"], "environment_file": "/run/secrets/audio.env",
                        "gpu_uuids": [config.gpu_uuid] if provider == "parakeet-ja" else [],
                        "total_vram_mb": 11264 if provider == "parakeet-ja" else 0,
                        "max_single_gpu_vram_mb": 11264 if provider == "parakeet-ja" else 0},
            "provider_config": {f: getattr(config, f) for f in config.__dataclass_fields__},
            "runtime": runtime,
            "serving": config.serving_declaration().to_dict(),
        })
    (directory / "manifest.json").write_text(
        json.dumps(cases, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return cases


def evaluate(source: Path, manifest: Path) -> subprocess.CompletedProcess[str]:
    expr = (
        "let flake = builtins.getFlake " + json.dumps("path:" + str(source))
        + "; in (import (builtins.toPath " + json.dumps(str(source / "nix/tests/audio-worker-config.nix"))
        + ")) { inherit flake; manifest = builtins.fromJSON (builtins.readFile "
        + json.dumps(str(manifest)) + "); }"
    )
    return subprocess.run(
        ["nix", "eval", "--offline", "--no-update-lock-file", "--no-write-lock-file",
         "--impure", "--json", "--option", "allow-import-from-derivation", "false",
         "--expr", expr], capture_output=True, text=True, timeout=240, check=False)


def check(source: Path, directory: Path) -> None:
    cases = generate_cases(directory)
    result = evaluate(source, directory / "manifest.json")
    if result.returncode:
        raise RuntimeError("offline audio-worker Nix evaluation failed:\n" + result.stderr)
    actual = json.loads(result.stdout)
    if len(actual) != len(cases):
        raise RuntimeError(f"expected {len(cases)} evaluated cases, got {len(actual)}")
    for case, row in zip(cases, actual, strict=True):
        if (row.get("name") != case["name"] or row.get("assertionsPassed") is not True
                or row.get("serviceContracts") is not True):
            raise RuntimeError(f"audio-worker Nix contract failed: {case['name']}: {row!r}")
        reviewed = row.get("reviewed", {})
        if reviewed.get("runtime") != case["runtime"] or reviewed.get("serving") != case["serving"]:
            raise RuntimeError(f"reviewed runtime/serving JSON differs: {case['name']}")
        # Round-trip the exact evaluated manifests through the production
        # daemon loaders and construct the managed runtime, but never start it.
        from tsumgi.worker import daemon
        from tsumgi.runtime.deployment import managed_runtime_from_deployment
        from tsumgi.runtime.contracts import RuntimeHostFacts
        reviewed_dir = directory / case["name"]
        reviewed_dir.mkdir()
        runtime_path = reviewed_dir / "runtime.json"
        serving_path = reviewed_dir / "serving.json"
        runtime_path.write_text(json.dumps(reviewed["runtime"]), encoding="utf-8")
        serving_path.write_text(json.dumps(reviewed["serving"]), encoding="utf-8")
        deployment = daemon._load_runtime_deployment(str(runtime_path))
        serving = daemon._load_serving_deployment(str(serving_path))
        worker_section = reviewed.get("settings", {}).get("worker")
        if not isinstance(worker_section, dict):
            raise RuntimeError(f"reviewed worker settings missing: {case['name']}")
        worker = daemon._build_spec(worker_section)
        managed = managed_runtime_from_deployment(
            deployment, worker=worker,
            host=RuntimeHostFacts(cpu_count=1, host_ram_mb=16384, architecture="x86_64"),
            serving=serving,
        )
        if getattr(managed, "provider_id", None) != case["provider"]:
            raise RuntimeError(f"daemon factory provider mismatch: {case['name']}")
        print(f"{case['name']}=PASS", flush=True)
    print(f"audio_worker_nixos_evaluation=PASS({len(cases)} cases)", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--generate-only", type=Path)
    args = parser.parse_args()
    source = args.source.resolve()
    sys.path.insert(0, str(source / "src"))
    try:
        if args.generate_only:
            print(f"generated_fixtures={len(generate_cases(args.generate_only.resolve()))}; nix_evaluation=NOT_RUN")
        else:
            with tempfile.TemporaryDirectory(prefix="tsumgi-audio-nixos-") as tmp:
                check(source, Path(tmp))
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
