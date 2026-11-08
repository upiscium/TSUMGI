"""#232: offline UNQUALIFIED Spica Bonsai 12K opt-in preview.

Reads an exact current 8K Spica/Deneb Runtime+Serving and Control catalog.
Never reads GPUs, contacts services, writes production files or registers Jobs.
Native 12K evidence and new execution/revision hashes are deliberately INVALID
until a separately approved Spica canary and independent provenance review.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tomllib
from collections.abc import Mapping
from contextlib import suppress
from copy import deepcopy
from pathlib import Path
from typing import Any

from astrumweaver.gateway.chat import ChatGatewayError, ChatProfileCatalog
from astrumweaver.runtime import GPUTopology, RuntimeDeploymentSpec
from astrumweaver.runtime.providers.llama_cpp import (
    LLAMA_CPP_BONSAI_MODEL_NAME,
    LLAMA_CPP_BONSAI_MODEL_SHA256,
    LlamaCppProviderConfig,
)
from astrumweaver.serving import ServingContractError, ServingDeploymentDeclaration

PROFILE_8K = "bonsai2-27b"
PROFILE_12K = "bonsai2-27b-12k"
CONTEXT_8K = 8192
CONTEXT_12K = 12288
LIMITS_8K = {
    "input_tokens": 6144, "output_tokens": 2048,
    "total_tokens": 8192, "request_bytes": 65536,
}
LIMITS_12K = {
    "input_tokens": 10240, "output_tokens": 2048,
    "total_tokens": 12288, "request_bytes": 65536,
}
PENDING_EXECUTION = "PENDING_BONSAI_12K_REVIEWED_EXECUTION_CONFIG"
PENDING_EVIDENCE = "PENDING_BONSAI_12K_REAL_NATIVE_CANARY_EVIDENCE"
PENDING_REVISION = "PENDING_BONSAI_12K_REVISION_REVIEW"
DRAFT_SCHEMA = "bonsai-spica-12k-unqualified-draft-v1"
_GPU = re.compile(r"GPU-[0-9a-fA-F-]{36}\Z")
_INPUT_NAMES = (
    "spica_runtime", "spica_serving", "spica_worker",
    "deneb_runtime", "deneb_serving", "deneb_worker", "control_catalog",
)
_MAX_JSON_BYTES = 1_048_576
_MAX_TOML_BYTES = 16_384
_KV_INCREASE_ESTIMATE_MIB = 1024
_VRAM_FLOOR_MIB = 2048


def _require(value: bool, code: str) -> None:
    if not value:
        raise ValueError(code)


def _digest(value: object) -> str:
    raw = json.dumps(
        value, ensure_ascii=True, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _validate_worker(raw: Mapping[str, Any], *, worker_id: str) -> str:
    _require(type(raw) is dict and isinstance(raw.get("worker"), dict),
             "worker-config-not-object")
    worker = raw["worker"]
    _require(worker.get("id") == worker_id, "worker-id-mismatch")
    uuids = worker.get("gpu_uuids")
    _require(
        type(uuids) is list and len(uuids) == 1
        and type(uuids[0]) is str and _GPU.fullmatch(uuids[0]) is not None,
        "worker-gpu-uuid-invalid",
    )
    _require(worker.get("gpu_count") == 1 and worker.get("max_concurrency") == 1,
             "worker-topology-or-concurrency-invalid")
    runtime = raw.get("runtime")
    serving = raw.get("serving")
    _require(type(runtime) is dict and type(serving) is dict,
             "worker-manifests-missing")
    _require(
        runtime.get("manifest") == "/etc/astrumweaver/bonsai-runtime-deployment.json"
        and serving.get("manifest") == "/etc/astrumweaver/bonsai-serving-deployment.json",
        "worker-not-running-bonsai-manifests",
    )
    return uuids[0]


def _validate_runtime(
    runtime_raw: Mapping[str, Any], *,
    expected_gpu: str, legacy_deneb_gpu: str,
) -> tuple[RuntimeDeploymentSpec, bool]:
    deployment = RuntimeDeploymentSpec.from_dict(runtime_raw)
    _require(deployment.provider_id == "llama-cpp", "runtime-provider-invalid")
    _require(
        deployment.demand.gpu_topology == GPUTopology.SINGLE_GPU
        and deployment.demand.effective_min_gpu_count == 1,
        "runtime-not-single-gpu",
    )
    intent = deployment.setup_intent
    _require(intent is not None, "runtime-setup-intent-missing")
    assert intent is not None
    _require(
        deployment.demand.model.model_ref == intent.model_ref
        and intent.model_ref.endswith("/" + LLAMA_CPP_BONSAI_MODEL_NAME),
        "runtime-model-path-mismatch",
    )
    _require(
        type(deployment.provider_config.get("context_size")) is int
        and deployment.provider_config["context_size"] == CONTEXT_8K
        and type(intent.configuration.get("context_size")) is int
        and intent.configuration["context_size"] == CONTEXT_8K,
        "runtime-8k-context-mismatch",
    )
    _require(
        intent.configuration.get("model_ref") == intent.model_ref,
        "runtime-setup-model-ref-mismatch",
    )
    _require(
        intent.configuration.get("gpu_uuids") in (
            [expected_gpu], [legacy_deneb_gpu],
        ),
        "runtime-setup-gpu-uuid-unrecognized",
    )
    LlamaCppProviderConfig(**deployment.provider_config)
    return deployment, intent.configuration["gpu_uuids"] != [expected_gpu]


def build_spica_unqualified_candidate(
    *,
    spica_runtime: Mapping[str, Any],
    spica_serving: Mapping[str, Any],
    spica_worker: Mapping[str, Any],
    deneb_runtime: Mapping[str, Any],
    deneb_serving: Mapping[str, Any],
    deneb_worker: Mapping[str, Any],
    control_catalog: Mapping[str, Any],
    observed_spica_free_mib: int | None = None,
) -> dict[str, Any]:
    """Reversible preview. No attested execution SHA, GPU release or activation.

    Does not trust user-provided baseline source authenticity; independently
    compare source bytes with the relevant live hosts before any real cutover.
    """
    # Copy everything; never modify caller's objects or original 8K entries.
    spica = deepcopy(dict(spica_runtime))
    serving = deepcopy(dict(spica_serving))
    catalog = deepcopy(dict(control_catalog))
    spica_uuid = _validate_worker(spica_worker, worker_id="bonsai-spica")
    deneb_uuid = _validate_worker(deneb_worker, worker_id="bonsai-deneb")
    _require(spica_uuid != deneb_uuid, "spica-deneb-share-gpu-uuid")
    current, needs_gpu_correction = _validate_runtime(
        spica_runtime, expected_gpu=spica_uuid, legacy_deneb_gpu=deneb_uuid,
    )
    _validate_runtime(
        deneb_runtime, expected_gpu=deneb_uuid, legacy_deneb_gpu=deneb_uuid,
    )
    _require(
        spica_runtime == deneb_runtime and spica_serving == deneb_serving,
        "spica-deneb-baseline-differs-requires-independent-review",
    )
    # Deliberately expose the observed legacy cross-host config drift.
    if needs_gpu_correction:
        _require(
            current.setup_intent is not None
            and current.setup_intent.configuration["gpu_uuids"] == [deneb_uuid],
            "spica-legacy-gpu-identity-invalid",
        )

    old_serving = ServingDeploymentDeclaration.from_dict(spica_serving)
    _require(
        old_serving.deployment.provider_id == current.provider_id
        and old_serving.deployment.model_artifact_sha256
        == "sha256:" + LLAMA_CPP_BONSAI_MODEL_SHA256,
        "serving-model-or-provider-mismatch",
    )
    _require(
        len(old_serving.contracts) == 1
        and old_serving.contracts[0].capability == "llm.chat"
        and "tools" in old_serving.contracts[0].features
        and dict(old_serving.contracts[0].limits) == LIMITS_8K,
        "serving-baseline-contract-not-8k",
    )
    existing = ChatProfileCatalog.from_dict(control_catalog)
    _require(
        len(existing.profiles) == 2
        and {p.profile_id for p in existing.profiles}
        == {"qwen3.8-27b", PROFILE_8K},
        "control-catalog-unexpected-profiles",
    )
    original = existing.get(PROFILE_8K)
    _require(
        original.resolved.deployment == old_serving.deployment
        and original.resolved.contract == old_serving.contracts[0]
        and dict(original.resolved.effective_limits) == LIMITS_8K
        and "tools" in original.resolved.profile.required_features,
        "control-existing-bonsai-identity-mismatch",
    )

    # Stage only Spica: Deneb's 8K content and the existing Control rows remain.
    spica["provider_config"]["context_size"] = CONTEXT_12K
    configuration = spica["setup_intent"]["configuration"]
    configuration["context_size"] = CONTEXT_12K
    configuration["gpu_uuids"] = [spica_uuid]
    RuntimeDeploymentSpec.from_dict(spica)
    LlamaCppProviderConfig(**spica["provider_config"])
    _require(
        spica["setup_intent"]["configuration"]["gpu_uuids"] == [spica_uuid],
        "spica-gpu-rebinding-failed",
    )

    serving["deployment"]["execution_config_sha256"] = PENDING_EXECUTION
    row = serving["contracts"][0]
    row["contract"]["deployment_revision"] = PENDING_REVISION
    row["contract"]["validation_evidence_sha256"] = PENDING_EVIDENCE
    row["contract"]["limits"] = dict(LIMITS_12K)
    row["serving_contract_revision"] = PENDING_REVISION

    selected_rows = [
        v for v in catalog["profiles"]
        if v["profile"]["profile_id"] == PROFILE_8K
    ]
    _require(len(selected_rows) == 1, "bonsai-old-profile-not-unique")
    opt_in = deepcopy(selected_rows[0])
    opt_in["profile"]["profile_id"] = PROFILE_12K
    opt_in["deployment"] = deepcopy(serving["deployment"])
    opt_in["contract"] = deepcopy(row["contract"])
    opt_in["profile"]["deployment_revision"] = PENDING_REVISION
    opt_in["profile"]["serving_contract_revision"] = PENDING_REVISION
    for key in ("input_tokens", "total_tokens"):
        if key in opt_in["profile"]["limits"]:
            opt_in["profile"]["limits"][key] = LIMITS_12K[key]
    catalog["profiles"].append(opt_in)

    # An UNQUALIFIED Serving/Control file must *not* be loadable by TSUMGI.
    try:
        ServingDeploymentDeclaration.from_dict(serving)
    except ServingContractError:
        pass
    else:
        raise AssertionError("unqualified Serving draft became loadable")
    try:
        ChatProfileCatalog.from_dict(catalog)
    except ChatGatewayError:
        pass
    else:
        raise AssertionError("unqualified Control draft became loadable")
    _require(catalog["profiles"][:2] == control_catalog["profiles"],
             "original-control-rows-drift")
    _require(deneb_runtime == spica_runtime and deneb_serving == spica_serving,
             "baseline-sources-mutated")

    estimated_free = None
    if observed_spica_free_mib is not None:
        _require(
            type(observed_spica_free_mib) is int
            and 0 <= observed_spica_free_mib <= 11264,
            "invalid-spica-free-vram-input",
        )
        estimated_free = observed_spica_free_mib - _KV_INCREASE_ESTIMATE_MIB
    return {
        "schema_version": DRAFT_SCHEMA,
        "admission": "DENIED_PENDING_REAL_CANARY_AND_REVIEW",
        "spica_worker_id": "bonsai-spica",
        "deneb_reference_worker_id": "bonsai-deneb",
        "opt_in_model_id": PROFILE_12K,
        "existing_model_id_unchanged": PROFILE_8K,
        "source_digests": {
            name: _digest(value) for name, value in (
                ("spica_runtime", spica_runtime), ("spica_serving", spica_serving),
                ("spica_worker", spica_worker), ("deneb_runtime", deneb_runtime),
                ("deneb_serving", deneb_serving), ("deneb_worker", deneb_worker),
                ("control_catalog", control_catalog),
            )
        },
        "source_gpu_uuids": {
            "spica_worker": spica_uuid, "deneb_worker": deneb_uuid,
            "spica_runtime_source": (
                spica_runtime["setup_intent"]["configuration"]["gpu_uuids"][0]
            ),
        },
        "legacy_spica_gpu_uuid_corrected_in_draft": needs_gpu_correction,
        "estimated_12k_idle_free_mib_not_measured": estimated_free,
        "mandatory_peak_free_mib": _VRAM_FLOOR_MIB,
        "runtime_UNQUALIFIED_draft": spica,
        "serving_INVALID_draft": serving,
        "control_INVALID_draft": catalog,
        "remaining_gates": [
            "independently verify pinned Bonsai model bytes and old source hashes",
            "review Spica source legacy Deneb UUID and exact proposed correction",
            "review path-neutral 12K executable/options execution_config_sha256",
            "separate approved Spica stop/start maintenance with 8K rollback",
            "prove old native/descendants/GPU owners gone before replacement",
            "measure Spica 12K peak VRAM during >7843 and near-10240 token prefill",
            "require minimum 2048 MiB physically free throughout peak",
            "qualify real 12K Chat Tool Streaming cancellation and limit rejection",
            "review new evidence/deployment/contract/profile SHA and runtime epoch",
            "publish opt-in bonsai2-27b-12k without changing existing Bonsai 8K",
            "preserve Deneb 8K and verify explicit no-fallback routing",
        ],
    }


def _read_bounded(path: Path, *, toml: bool = False) -> dict[str, Any]:
    raw = path.read_bytes()
    maximum = _MAX_TOML_BYTES if toml else _MAX_JSON_BYTES
    _require(0 < len(raw) <= maximum, "source-too-large-or-empty")
    result = tomllib.loads(raw.decode()) if toml else json.loads(raw)
    _require(type(result) is dict, "source-not-object")
    return result


def _write_private_draft(folder: Path, result: Mapping[str, Any]) -> None:
    # No arbitrary paths, symlink substitution, or existing output locations.
    _require(
        folder.is_absolute() and folder.parent == Path("/tmp")
        and folder.name.startswith("tsumgi-bonsai12k-")
        and not Path("/tmp").is_symlink()
        and not folder.exists() and not folder.is_symlink(),
        "destination-must-be-new-direct-tmp-child",
    )
    os.mkdir(folder, 0o700)
    fd: int | None = None
    items = (
        # Not a RuntimeDeploymentSpec: direct install would fail *before*
        # starting Native, even though the nested reviewed shape is valid.
        ("spica-runtime.REVIEW_WRAPPER.json", {
            "schema_version": "bonsai-runtime-review-wrapper-v1",
            "admission": result["admission"],
            "runtime_UNQUALIFIED_draft": result["runtime_UNQUALIFIED_draft"],
        }),
        ("spica-serving.INVALID.json", result["serving_INVALID_draft"]),
        ("control.INVALID.json", result["control_INVALID_draft"]),
        ("review-gates.json", {
            k: v for k, v in result.items()
            if k not in (
                "runtime_UNQUALIFIED_draft",
                "serving_INVALID_draft",
                "control_INVALID_draft",
            )
        }),
    )
    try:
        fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        os.fchmod(fd, 0o700)
        for name, obj in items:
            file_fd = os.open(
                name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                0o600, dir_fd=fd,
            )
            with os.fdopen(file_fd, "w", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), 0o600)
                json.dump(obj, handle, ensure_ascii=True, sort_keys=True, indent=2)
                handle.write("\n")
    except BaseException:
        if fd is not None:
            for name, _ in items:
                with suppress(FileNotFoundError):
                    os.unlink(name, dir_fd=fd)
        with suppress(OSError):
            os.rmdir(folder)
        raise
    finally:
        if fd is not None:
            os.close(fd)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in _INPUT_NAMES:
        parser.add_argument("--" + name.replace("_", "-"), required=True, type=Path)
    parser.add_argument("--spica-free-mib", type=int)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    try:
        data = {
            name: _read_bounded(
                getattr(args, name), toml=name.endswith("_worker"),
            )
            for name in _INPUT_NAMES
        }
        result = build_spica_unqualified_candidate(
            **data, observed_spica_free_mib=args.spica_free_mib,
        )
        if args.output_dir is not None:
            _write_private_draft(args.output_dir, result)
    except (ValueError, KeyError, TypeError, OSError, UnicodeError) as exc:
        print(json.dumps({
            "status": "BLOCKED", "error_class": type(exc).__name__,
        }))
        return 2
    print(json.dumps({
        "status": result["admission"],
        "opt_in_model_id": result["opt_in_model_id"],
        "context_size": CONTEXT_12K,
        "limits": LIMITS_12K,
        "legacy_spica_gpu_uuid_corrected_in_draft":
            result["legacy_spica_gpu_uuid_corrected_in_draft"],
        "estimated_idle_free_mib_not_measured":
            result["estimated_12k_idle_free_mib_not_measured"],
        "private_draft_written": args.output_dir is not None,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
