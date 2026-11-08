"""Synthetic-only Spica 12K opt-in preview; no live 12K admission."""

from __future__ import annotations

import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from bonsai_12k_spica_candidate import (
    CONTEXT_12K,
    LIMITS_12K,
    PROFILE_12K,
    build_spica_unqualified_candidate,
)

from astrumweaver.control import InMemoryControlRepository
from astrumweaver.control.api import create_app
from astrumweaver.gateway.api import create_chat_router
from astrumweaver.gateway.chat import (
    ChatGatewayError,
    ChatProfileCatalog,
)
from astrumweaver.runtime import RuntimeDeploymentSpec
from astrumweaver.runtime.providers.llama_cpp import (
    LLAMA_CPP_BONSAI_MODEL_NAME,
    LLAMA_CPP_BONSAI_MODEL_SHA256,
    LlamaCppProviderConfig,
)
from astrumweaver.serving import (
    DeploymentIdentity,
    LogicalServingProfile,
    ServingContract,
    ServingContractError,
    ServingDeploymentDeclaration,
)

SPICA = "GPU-f78bcab4-245f-b3ff-5570-ed629b239331"
DENEB = "GPU-8bcf8ba8-4610-d9bb-3862-a03b5ea7d931"


def digest(value: str) -> str:
    return "sha256:" + value * 64


def _make_declaration(*, model_sha: str, size: int, letter: str):
    identity = DeploymentIdentity(
        provider_id="llama-cpp",
        runtime_artifact_sha256=digest("1"),
        adapter_artifact_sha256=digest("2"),
        model_artifact_sha256=model_sha,
        execution_config_sha256=digest(letter),
        quantization="PQ2_0",
        template_artifact_sha256=digest("3"),
    )
    contract = ServingContract(
        deployment_revision=identity.revision,
        capability="llm.chat",
        operation_schema="openai-chat-completions-v1",
        validation_evidence_sha256=digest("4"),
        features=frozenset({"tools"}),
        limits={
            "input_tokens": size - 2048,
            "output_tokens": 2048,
            "total_tokens": size,
            "request_bytes": 65536,
        },
    )
    return identity, contract, ServingDeploymentDeclaration(
        deployment=identity, contracts=(contract,),
    )


def _row(model: str, identity, contract):
    profile = LogicalServingProfile(
        profile_id=model,
        deployment_revision=identity.revision,
        serving_contract_revision=contract.revision,
        capability="llm.chat",
        operation_schema="openai-chat-completions-v1",
        required_features=frozenset({"tools"}),
    )
    return {
        "adapter_id": "llama-cpp-chat-v1",
        "request_timeout_seconds": 120,
        "max_attempts": 2,
        "owned_by": "astrumweaver",
        "created": 0,
        "deployment": identity.to_dict(),
        "contract": contract.to_dict(),
        "profile": profile.to_dict(),
    }


def make_inputs(*, spica_runtime_gpu=DENEB) -> dict:
    model_ref = "/synthetic/models/" + LLAMA_CPP_BONSAI_MODEL_NAME
    runtime = {
        "schema_version": "v1",
        "provider_id": "llama-cpp",
        "demand": {
            "model": {
                "model_ref": model_ref, "model_format": "gguf",
                "topology": "dense", "estimated_size_mb": 6873,
                "metadata": {},
            },
            "residency_policy": "vram_only",
            "gpu_topology": "single_gpu",
            "min_gpu_count": 0,
            "min_total_vram_mb": 6800,
            "min_single_gpu_vram_mb": 6800,
            "min_host_ram_mb": 0,
            "preferred_host_ram_mb": 0,
            "metadata": {},
        },
        "provider_config": {
            "base_url": "http://127.0.0.1:8085",
            "context_size": 8192,
            "executable": "/nix/var/nix/profiles/astrumweaver-runtime-bonsai/bin/llama-server",
            "package_reference": "bonsai-llama-cpp",
            "gpu_layers": "all",
            "fit": False,
            "offline": True,
            "no_webui": True,
            "jinja": True,
            "runtime_flavor": "bonsai2-pq2",
        },
        "setup_intent": {
            "provider_id": "llama-cpp",
            "model_ref": model_ref,
            "model_preparation": "reference_only",
            "package_references": ["bonsai-llama-cpp"],
            "requires_privilege": True,
            "metadata": {"runtime_provider": "llama-cpp"},
            "configuration": {
                "model_ref": model_ref,
                "context_size": 8192,
                "gpu_uuids": [spica_runtime_gpu],
                "base_url": "http://127.0.0.1:8085",
                "model_alias": "astrumweaver",
            },
        },
    }
    bonsai_identity, bonsai_contract, bonsai_serving = _make_declaration(
        model_sha="sha256:" + LLAMA_CPP_BONSAI_MODEL_SHA256,
        size=8192, letter="5",
    )
    qwen_identity, qwen_contract, _ = _make_declaration(
        model_sha=digest("9"), size=12288, letter="6",
    )
    worker_common = {
        "runtime": {
            "manifest": "/etc/astrumweaver/bonsai-runtime-deployment.json",
        },
        "serving": {
            "manifest": "/etc/astrumweaver/bonsai-serving-deployment.json",
        },
    }
    worker_spica = deepcopy(worker_common)
    worker_spica["worker"] = {
        "id": "bonsai-spica", "gpu_count": 1, "max_concurrency": 1,
        "gpu_uuids": [SPICA],
    }
    worker_deneb = deepcopy(worker_common)
    worker_deneb["worker"] = {
        "id": "bonsai-deneb", "gpu_count": 1, "max_concurrency": 1,
        "gpu_uuids": [DENEB],
    }
    # The live November 8 source uses a byte-identical runtime/serving on
    # both replicas (including Spica's incorrect Deneb setup-intent UUID).
    return {
        "spica_runtime": runtime,
        "spica_serving": bonsai_serving.to_dict(),
        "spica_worker": worker_spica,
        "deneb_runtime": deepcopy(runtime),
        "deneb_serving": bonsai_serving.to_dict(),
        "deneb_worker": worker_deneb,
        "control_catalog": {
            "schema_version": "chat-gateway-catalog-v1",
            "profiles": [
                _row("qwen3.8-27b", qwen_identity, qwen_contract),
                _row("bonsai2-27b", bonsai_identity, bonsai_contract),
            ],
        },
    }


def preview(**changes):
    args = make_inputs()
    args.update(changes)
    return build_spica_unqualified_candidate(
        **args, observed_spica_free_mib=3610,
    )


def test_current_spica_legacy_deneb_uuid_is_explicitly_corrected():
    inputs = make_inputs()
    before = deepcopy(inputs)
    result = preview()
    assert result["admission"] == "DENIED_PENDING_REAL_CANARY_AND_REVIEW"
    assert result["legacy_spica_gpu_uuid_corrected_in_draft"] is True
    assert result["source_gpu_uuids"] == {
        "spica_worker": SPICA,
        "deneb_worker": DENEB,
        "spica_runtime_source": DENEB,
    }
    assert result["estimated_12k_idle_free_mib_not_measured"] == 2586
    assert result["mandatory_peak_free_mib"] == 2048
    runtime = result["runtime_UNQUALIFIED_draft"]
    assert runtime["provider_config"]["context_size"] == CONTEXT_12K
    assert runtime["setup_intent"]["configuration"]["context_size"] == CONTEXT_12K
    assert runtime["setup_intent"]["configuration"]["gpu_uuids"] == [SPICA]
    assert runtime["setup_intent"]["configuration"]["model_ref"] == (
        inputs["spica_runtime"]["setup_intent"]["configuration"]["model_ref"]
    )
    assert RuntimeDeploymentSpec.from_dict(runtime).provider_id == "llama-cpp"
    assert LlamaCppProviderConfig(**runtime["provider_config"]).context_size == 12288
    assert inputs == before


def test_already_correct_spica_uuid_does_not_qualify_12k():
    args = make_inputs(spica_runtime_gpu=SPICA)
    args["deneb_runtime"]["setup_intent"]["configuration"]["gpu_uuids"] = [DENEB]
    # A corrected baseline cannot be equal to Deneb's old deployment, and
    # must receive a separately reviewed baseline status; don't silently accept.
    with pytest.raises(ValueError, match="baseline-differs"):
        build_spica_unqualified_candidate(**args)


def test_original_bonsai_qwen_control_rows_unchanged_and_unloadable():
    args = make_inputs()
    draft = build_spica_unqualified_candidate(**args)
    cat = draft["control_INVALID_draft"]
    assert len(cat["profiles"]) == 3
    assert cat["profiles"][:2] == args["control_catalog"]["profiles"]
    assert [r["profile"]["profile_id"] for r in cat["profiles"]] == [
        "qwen3.8-27b", "bonsai2-27b", PROFILE_12K,
    ]
    new = cat["profiles"][2]
    assert new["profile"]["profile_id"] == PROFILE_12K
    assert new["contract"]["limits"] == LIMITS_12K
    assert new["deployment"]["execution_config_sha256"].startswith("PENDING_")
    assert new["contract"]["validation_evidence_sha256"].startswith("PENDING_")
    assert new["profile"]["deployment_revision"].startswith("PENDING_")
    with pytest.raises(ServingContractError):
        ServingDeploymentDeclaration.from_dict(draft["serving_INVALID_draft"])
    with pytest.raises(ChatGatewayError):
        ChatProfileCatalog.from_dict(cat)


def synthetic_only_resolve(draft):
    """Resolve test-only dummy hashes to check schema compatibility, NOT attestation."""
    serving = deepcopy(draft["serving_INVALID_draft"])
    row = deepcopy(draft["control_INVALID_draft"]["profiles"][2])
    serving["deployment"]["execution_config_sha256"] = digest("a")
    dep = DeploymentIdentity(**serving["deployment"])
    contract_raw = serving["contracts"][0]["contract"]
    contract_raw["deployment_revision"] = dep.revision
    contract_raw["validation_evidence_sha256"] = digest("b")
    contract = ServingContract(**contract_raw)
    serving_valid = ServingDeploymentDeclaration(dep, (contract,))
    assert len(serving_valid.contracts) == 1
    row["deployment"] = dep.to_dict()
    row["contract"] = contract.to_dict()
    row["profile"]["deployment_revision"] = dep.revision
    row["profile"]["serving_contract_revision"] = contract.revision
    profile = LogicalServingProfile(**row["profile"])
    row["profile"] = profile.to_dict()
    valid_catalog = deepcopy(draft["control_INVALID_draft"])
    valid_catalog["profiles"][2] = row
    return valid_catalog, serving_valid


def test_synthetic_valid_chain_only_when_dummy_sha_supplied_by_test():
    args = make_inputs()
    draft = build_spica_unqualified_candidate(**args)
    raw, serving = synthetic_only_resolve(draft)
    catalog = ChatProfileCatalog.from_dict(raw)
    assert serving.deployment.revision == catalog.get(PROFILE_12K).resolved.deployment.revision
    assert dict(catalog.get(PROFILE_12K).resolved.effective_limits) == LIMITS_12K
    assert dict(catalog.get("bonsai2-27b").resolved.effective_limits) == {
        "input_tokens": 6144,
        "output_tokens": 2048,
        "total_tokens": 8192,
        "request_bytes": 65536,
    }
    assert dict(catalog.get("qwen3.8-27b").resolved.effective_limits) == {
        "input_tokens": 10240,
        "output_tokens": 2048,
        "total_tokens": 12288,
        "request_bytes": 65536,
    }
    assert raw["profiles"][:2] == args["control_catalog"]["profiles"]


@pytest.mark.asyncio
async def test_synthetic_gateway_optin_is_exact_and_rejects_limits():
    raw, _ = synthetic_only_resolve(preview())
    catalog = ChatProfileCatalog.from_dict(raw)
    repository = InMemoryControlRepository()
    app = create_app(
        repository, client_auth="none", client_token=None,
        worker_token="synthetic-test-only-worker", maintenance_interval_seconds=60,
    )
    app.include_router(
        create_chat_router(
            repository, catalog, client_auth="none", client_token=None,
            poll_interval_seconds=0.01,
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://synthetic",
    ) as client:
        models = await client.get("/v1/models")
        assert models.status_code == 200
        names = [m["id"] for m in models.json()["data"]]
        assert names == ["qwen3.8-27b", "bonsai2-27b", PROFILE_12K]
        max_tokens = await client.post(
            "/v1/chat/completions",
            json={
                "model": PROFILE_12K,
                "messages": [{"role": "user", "content": "synthetic"}],
                "max_tokens": 2049,
            },
        )
        assert max_tokens.status_code == 400
        assert max_tokens.json()["error"]["code"] == "output_limit_exceeded"
        old_limit = await client.post(
            "/v1/chat/completions",
            json={
                "model": "bonsai2-27b",
                "messages": [{"role": "user", "content": "synthetic"}],
                "max_tokens": 2049,
            },
        )
        assert old_limit.status_code == 400
    assert repository.list_jobs() == []


@pytest.mark.parametrize(("name", "change", "reason"), [
    ("spica_worker", lambda r: r["worker"].update(id="spica"), "worker-id"),
    ("deneb_worker", lambda r: r["worker"].update(id="deneb"), "worker-id"),
    ("spica_worker", lambda r: r["worker"].update(gpu_uuids=[DENEB]), "share-gpu"),
    ("spica_worker", lambda r: r["worker"].update(gpu_uuids=[]), "uuid-invalid"),
    ("spica_worker", lambda r: r["worker"].update(max_concurrency=2), "concurrency"),
    ("spica_worker", lambda r: r["runtime"].update(manifest="/etc/other.json"), "manifests"),
    ("spica_runtime", lambda r: r["provider_config"].update(context_size=12288), "8k-context"),
    ("spica_runtime", lambda r: r["setup_intent"]["configuration"].update(context_size=4096), "8k-context"),
    ("spica_runtime", lambda r: r["setup_intent"]["configuration"].update(gpu_uuids=["GPU-00000000-0000-0000-0000-000000000000"]), "gpu-uuid"),
    ("spica_runtime", lambda r: r["setup_intent"]["configuration"].update(model_ref="/other/model.gguf"), "model-ref"),
    ("spica_serving", lambda r: r["contracts"][0]["contract"]["limits"].update(total_tokens=16384), "baseline-differs"),
    ("spica_serving", lambda r: r["deployment"].update(model_artifact_sha256=digest("8")), "baseline-differs"),
    ("control_catalog", lambda r: r["profiles"].pop(), "unexpected-profiles"),
    ("control_catalog", lambda r: r["profiles"][1]["profile"].update(profile_id=PROFILE_12K), "unexpected-profiles"),
])
def test_baseline_tampering_fails_closed(name, change, reason):
    args = make_inputs()
    change(args[name])
    with pytest.raises((ValueError, ServingContractError, ChatGatewayError), match=reason):
        build_spica_unqualified_candidate(**args)


@pytest.mark.parametrize("value", [-1, 11265, 3.5, True, "3610", None])
def test_capacity_reporting_never_authorizes_rollout(value):
    if value is None:
        draft = build_spica_unqualified_candidate(**make_inputs())
        assert draft["estimated_12k_idle_free_mib_not_measured"] is None
    else:
        with pytest.raises(ValueError, match="invalid-spica-free-vram-input"):
            build_spica_unqualified_candidate(
                **make_inputs(), observed_spica_free_mib=value,
            )


def test_estimated_idle_headroom_below_floor_is_still_report_only():
    draft = build_spica_unqualified_candidate(
        **make_inputs(), observed_spica_free_mib=2900,
    )
    assert draft["estimated_12k_idle_free_mib_not_measured"] == 1876
    assert draft["admission"] == "DENIED_PENDING_REAL_CANARY_AND_REVIEW"


def test_output_dir_must_be_new_direct_private_tmp_and_never_production(tmp_path):
    from bonsai_12k_spica_candidate import _write_private_draft

    draft = preview()
    with pytest.raises(ValueError, match="direct-tmp-child"):
        _write_private_draft(tmp_path / "tsumgi-bonsai12k-review", draft)
    out = Path("/tmp") / ("tsumgi-bonsai12k-" + uuid4().hex)
    try:
        _write_private_draft(out, draft)
        assert out.stat().st_mode & 0o777 == 0o700
        children = sorted(p.name for p in out.iterdir())
        assert children == sorted([
            "spica-runtime.REVIEW_WRAPPER.json",
            "spica-serving.INVALID.json",
            "control.INVALID.json",
            "review-gates.json",
        ])
        assert all(f.stat().st_mode & 0o777 == 0o600 for f in out.iterdir())
        review_runtime = json.loads(
            (out / "spica-runtime.REVIEW_WRAPPER.json").read_text()
        )
        assert review_runtime["admission"] == "DENIED_PENDING_REAL_CANARY_AND_REVIEW"
        with pytest.raises((KeyError, ValueError, TypeError)):
            RuntimeDeploymentSpec.from_dict(review_runtime)
        assert RuntimeDeploymentSpec.from_dict(
            review_runtime["runtime_UNQUALIFIED_draft"]
        ).provider_config["context_size"] == CONTEXT_12K
        gates = json.loads((out / "review-gates.json").read_text())
        assert gates["legacy_spica_gpu_uuid_corrected_in_draft"] is True
        assert gates["source_gpu_uuids"]["spica_runtime_source"] == DENEB
        assert gates["estimated_12k_idle_free_mib_not_measured"] == 2586
        with pytest.raises(ValueError, match="direct-tmp-child"):
            _write_private_draft(out, draft)
    finally:
        if out.exists():
            for file in out.iterdir():
                file.unlink()
            out.rmdir()


def test_cli_source_only_no_output_default_and_invalid_json(tmp_path: Path):
    args = make_inputs()
    files = {}
    for name, obj in args.items():
        dest = tmp_path / (name + (".toml" if name.endswith("_worker") else ".json"))
        if name.endswith("_worker"):
            worker = obj["worker"]
            dest.write_text(
                "\n".join([
                    "[worker]",
                    f'id = "{worker["id"]}"',
                    f'gpu_uuids = ["{worker["gpu_uuids"][0]}"]',
                    f'gpu_count = {worker["gpu_count"]}',
                    f'max_concurrency = {worker["max_concurrency"]}',
                    "[runtime]",
                    f'manifest = "{obj["runtime"]["manifest"]}"',
                    "[serving]",
                    f'manifest = "{obj["serving"]["manifest"]}"',
                ]) + "\n"
            )
        else:
            dest.write_text(json.dumps(obj))
        files[name] = dest
    command = [sys.executable, str(ROOT / "tools/bonsai_12k_spica_candidate.py")]
    for name, path in files.items():
        command.extend(["--" + name.replace("_", "-"), str(path)])
    command.extend(["--spica-free-mib", "3610"])
    before = set(tmp_path.iterdir())
    result = subprocess.run(command, capture_output=True, text=True, timeout=15, check=False)
    assert result.returncode == 0, result.stderr
    response = json.loads(result.stdout)
    assert response["status"] == "DENIED_PENDING_REAL_CANARY_AND_REVIEW"
    assert response["private_draft_written"] is False
    assert response["legacy_spica_gpu_uuid_corrected_in_draft"] is True
    assert set(tmp_path.iterdir()) == before
    files["spica_runtime"].write_text("{}")
    blocked = subprocess.run(command, capture_output=True, text=True, timeout=15, check=False)
    assert blocked.returncode == 2
    assert json.loads(blocked.stdout)["status"] == "BLOCKED"


def test_opt_in_model_must_match_its_exact_binding():
    from astrumweaver.gateway.chat import compile_chat_request

    raw, _ = synthetic_only_resolve(preview())
    catalog = ChatProfileCatalog.from_dict(raw)
    old = catalog.get("bonsai2-27b")
    new = catalog.get(PROFILE_12K)
    for profile, model in (
        (old, "bonsai2-27b"),
        (new, PROFILE_12K),
    ):
        request = {
            "model": model,
            "messages": [{"role": "user", "content": "synthetic"}],
            "max_tokens": 16,
        }
        compiled = compile_chat_request(
            request, profile, request_size_bytes=128,
        )
        assert compiled is not None
        wrong_profile = new if model == "bonsai2-27b" else old
        with pytest.raises(ChatGatewayError) as exc:
            compile_chat_request(
                request, wrong_profile, request_size_bytes=128,
            )
        assert exc.value.code == "model_mismatch"
