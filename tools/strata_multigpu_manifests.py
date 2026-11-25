"""Generate the Strata multi-GPU chat Worker files for one host (operator tool, not packaged).

Run it on the Worker host, as root, with the installed tsumgi Python (for example
``/nix/var/nix/profiles/tsumgi-<release>-installer/bin/python``). It reads a prepared Strata run config
and writes ``runtime.json``, ``serving.json``, ``catalog-entry.json``, ``execution.json``,
``model-shards.json``, ``worker.toml`` and ``gpu-uuids`` into the output directory.

The Worker owns every GPU the host shows (``gpu-preflight`` runs in ``exact-visible`` mode), so the host
must expose exactly the GPUs of the lane, and this tool refuses any other count than two. Both GGUF shards
of the model are hashed (a 55 GB model takes several minutes); the same shard list belongs in the
``sha256sum --check`` file the Worker unit runs before it starts.

See docs/runtime-strata.md, section "Multi-GPU Workers".
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

from tsumgi import ResourceShape, WorkerSpec
from tsumgi.gateway.chat import CHAT_OPERATION_SCHEMA, STRATA_CHAT_ADAPTER, ChatGatewayProfile
from tsumgi.runtime import (
    ExecutionDemand,
    GPUTopology,
    ModelDemand,
    ModelTopology,
    ResidencyPolicy,
    RuntimeCompatibilityContext,
)
from tsumgi.runtime.deployment import build_runtime_deployment_spec, discover_runtime_host_facts
from tsumgi.runtime.providers.strata import StrataProvider, StrataProviderConfig, StrataRunConfig
from tsumgi.serving import (
    DeploymentIdentity,
    LogicalServingProfile,
    ServingContract,
    ServingDeploymentDeclaration,
    resolve_profile,
)


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("out_dir")
    parser.add_argument("worker_id")
    parser.add_argument("control_url")
    parser.add_argument("strata_root", help="holds bin/strata, home/ and data/ of the pinned Strata install")
    parser.add_argument("run_config", help="the root-owned copy of strata-<model>.json (fit_max_tokens false)")
    parser.add_argument("evidence", help="validation evidence file; its SHA-256 is bound into the contract")
    parser.add_argument("profile_id")
    parser.add_argument("--quantization", default="IQ1_M")
    parser.add_argument("--output-tokens", type=int, default=8192,
                        help="output limit; thinking models spend it on reasoning first")
    parser.add_argument("--request-timeout", type=int, default=900)
    parser.add_argument("--health-port", type=int, default=9100)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    # TSUMGI v1 WorkerRuntime requires max_concurrency=1, so Strata runs one request at a time.
    parallel = 1

    rc = StrataRunConfig.load(args.run_config)
    raw_rc = json.loads(Path(args.run_config).read_text())
    limits = {
        "input_tokens": rc.max_context,
        "output_tokens": args.output_tokens,
        "total_tokens": rc.max_context,
        "request_bytes": 4 * 1024 * 1024,
    }

    smi = [
        line.split(", ")
        for line in subprocess.run(
            ["nvidia-smi", "--query-gpu=uuid,memory.total,compute_cap,name", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, check=True,
        ).stdout.strip().splitlines()
    ]
    if len(smi) != 2:
        raise SystemExit(f"expected exactly two visible GPUs, found {len(smi)}")
    uuids = [row[0] for row in smi]
    vram = int(smi[0][1])
    compute_cap = smi[0][2].strip()
    device = smi[0][3].strip()

    root = args.strata_root
    config = StrataProviderConfig(
        executable=f"{root}/bin/strata", run_config=args.run_config, home=f"{root}/home", data_dir=f"{root}/data",
        served_model_name=rc.model_name, context_tokens=rc.max_context, enable_thinking=True,
        parallel=parallel, request_timeout_seconds=float(args.request_timeout),
    )
    worker = WorkerSpec(
        worker_id=args.worker_id, worker_class="gpu-multi",
        resources=ResourceShape(gpu_count=2, total_vram_mb=2 * vram, max_single_gpu_vram_mb=vram),
        gpu_uuids=tuple(uuids), capabilities=frozenset({"llm.chat"}),
    )

    shards = sorted(Path(rc.model_path).parent.glob("*.gguf"))
    shard_sha = {}
    for path in shards:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 24), b""):
                digest.update(block)
        shard_sha[path.name] = digest.hexdigest()
    model_sha = hashlib.sha256(canonical(shard_sha)).hexdigest()

    demand = ExecutionDemand(
        model=ModelDemand(
            model_ref=rc.model_path, model_format="gguf", topology=ModelTopology.MOE,
            estimated_size_mb=sum(p.stat().st_size for p in shards) // (1 << 20), metadata={"sha256": model_sha},
        ),
        residency_policy=ResidencyPolicy.CPU_GPU_HYBRID, gpu_topology=GPUTopology.MULTI_GPU,
        min_gpu_count=2, min_host_ram_mb=28000, preferred_host_ram_mb=28000,
    )
    provider = StrataProvider(config)
    context = RuntimeCompatibilityContext(worker=worker, host=discover_runtime_host_facts(), demand=demand)
    report = provider.compatibility(context)
    if not report.compatible:
        raise SystemExit("incompatible: " + "; ".join(r.code for r in report.reasons if r.blocking))
    runtime_spec = build_runtime_deployment_spec(provider, context)

    engine_dir = Path(f"{root}/home/.local/share/strata/components/engine")
    engines = sorted(p.name for p in engine_dir.iterdir()) if engine_dir.is_dir() else []
    terminal = sorted(
        p.name for p in Path(f"{root}/data/terminal").iterdir() if p.is_dir() and p.name[0].isdigit()
    )[-1]
    paths = {str(p) for p in shards} | {rc.model_path}
    # Path-neutral behaviour. The engine variant (for example cuda12 or cuda13) is part of the identity, so
    # hosts with different GPU generations become different deployments and need different profile ids.
    execution = {
        "provider": "strata", "terminal": terminal, "engines": engines, "cuda": rc.cuda,
        "engine_args": [a for a in raw_rc.get("args", []) if a not in paths and not a.startswith(root)],
        "gpu_count": 2, "layer_split": raw_rc.get("layer_split"),
        "fit_max_tokens": rc.fit_max_tokens, "parallel": config.parallel,
        "enable_thinking": config.enable_thinking, "tools_enabled": config.tools_enabled,
        "served_model_name": rc.model_name,
    }
    adapter_source = Path(sys.modules[StrataProvider.__module__].__file__).read_bytes()
    deployment = DeploymentIdentity(
        provider_id="strata", runtime_artifact_sha256=sha(canonical({"terminal": terminal, "engines": engines})),
        adapter_artifact_sha256=sha(adapter_source), model_artifact_sha256="sha256:" + model_sha,
        execution_config_sha256=sha(canonical(execution)), quantization=args.quantization,
        tokenizer_artifact_sha256="sha256:" + model_sha, template_artifact_sha256="sha256:" + model_sha,
    )
    contract = ServingContract(
        deployment_revision=deployment.revision, capability="llm.chat", operation_schema=CHAT_OPERATION_SCHEMA,
        validation_evidence_sha256=sha(Path(args.evidence).read_bytes()),
        features=frozenset({"tools"}), limits=limits,
    )
    profile = LogicalServingProfile(
        profile_id=args.profile_id, deployment_revision=deployment.revision,
        serving_contract_revision=contract.revision, capability="llm.chat",
        operation_schema=CHAT_OPERATION_SCHEMA, required_features=frozenset({"tools"}), limits={},
    )
    gateway_profile = ChatGatewayProfile(
        resolved=resolve_profile(profile, contract, deployment), adapter_id=STRATA_CHAT_ADAPTER,
        request_timeout_seconds=args.request_timeout, max_attempts=1, owned_by="tsumgi", created=0,
    )
    entry = {
        "adapter_id": gateway_profile.adapter_id,
        "request_timeout_seconds": gateway_profile.request_timeout_seconds,
        "max_attempts": gateway_profile.max_attempts, "owned_by": gateway_profile.owned_by,
        "created": gateway_profile.created, "deployment": deployment.to_dict(),
        "contract": contract.to_dict(), "profile": profile.to_dict(),
    }

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    def write(name: str, value) -> None:
        (out / name).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")

    write("runtime.json", runtime_spec.to_dict())
    write("serving.json", ServingDeploymentDeclaration(deployment=deployment, contracts=(contract,)).to_dict())
    write("catalog-entry.json", entry)
    write("execution.json", execution)
    write("model-shards.json", shard_sha)
    accelerators = "".join(
        f'\n[[worker.accelerators]]\nuuid = "{u}"\nmemory_mb = {vram}\n'
        f'compute_capability = "{compute_cap}"\ndevice_class = "{device}"\n'
        for u in uuids
    )
    (out / "worker.toml").write_text(f'''[worker]
id = "{args.worker_id}"
class = "gpu-multi"
control_url = "{args.control_url}"
capabilities = ["llm.chat"]
gpu_uuids = [{", ".join('"%s"' % u for u in uuids)}]
gpu_count = 2
total_vram_mb = {2 * vram}
max_single_gpu_vram_mb = {vram}
max_concurrency = {parallel}
gpu_preflight = true
poll_interval_seconds = 0.25
heartbeat_interval_seconds = 5
health_host = "127.0.0.1"
health_port = {args.health_port}

[worker.labels]
"gpu.compute_capability.min" = "{compute_cap}"
"serving.role" = "coding"
{accelerators}
[runtime]
manifest = "{out / 'runtime.json'}"
startup_timeout_seconds = 900
shutdown_timeout_seconds = 60

[serving]
manifest = "{out / 'serving.json'}"
''')
    (out / "gpu-uuids").write_text("\n".join(uuids) + "\n")
    print(json.dumps({
        "deployment_revision": deployment.revision,
        "profile_revision": gateway_profile.resolved.profile_revision,
        "serving_contract_revision": contract.revision,
        "gpu_uuids": uuids, "engines": engines, "terminal": terminal, "model_sha": model_sha,
    }, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
