"""Offline, explicitly UNQUALIFIED Stage-A Qwen 12K manifest preview.

Consumes the existing reviewed 8K Runtime, Serving and Control artifacts.
No native process, GPU, gateway, database or network access is performed.
Drafts intentionally contain invalid PENDING_* revisions/evidence and MUST
NOT be used for deployment. See docs/qwen-12k-candidate.md.
"""
from __future__ import annotations

import argparse
from contextlib import suppress
from copy import deepcopy
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import sys
from typing import Any, Mapping

from astrumweaver.gateway.chat import (
    ChatGatewayError, ChatProfileCatalog, LLAMA_CPP_CHAT_ADAPTER,
)
from astrumweaver.runtime import GPUTopology, RuntimeDeploymentSpec
from astrumweaver.runtime.providers.llama_cpp import LlamaCppProviderConfig
from astrumweaver.serving import (
    ServingContractError, ServingDeploymentDeclaration,
)

BASE_LIMITS = {
    "input_tokens": 6144,
    "output_tokens": 2048,
    "total_tokens": 8192,
    "request_bytes": 65536,
}
TARGET_LIMITS = {
    "input_tokens": 10240,
    "output_tokens": 2048,
    "total_tokens": 12288,
    "request_bytes": 65536,
}
BASE_CONTEXT = 8192
TARGET_CONTEXT = 12288
PROFILE_ID = "qwen3.8-27b"
PENDING_EXECUTION = "PENDING_INDEPENDENT_EXECUTION_CONFIG_REVIEW"
PENDING_VALIDATION = "PENDING_REAL_12K_CANARY_EVIDENCE"
PENDING_REVISION = "PENDING_AFTER_EVIDENCE"
DRAFT_SCHEMA = "qwen-12k-unqualified-offline-draft-v1"
TRUSTED_PIN_SCHEMA = "reviewed-qwen-baseline-pin-v1"


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise ValueError(reason)


def _canonical_digest(data: object) -> str:
    payload = json.dumps(
        data, sort_keys=True, separators=(",", ":"),
        ensure_ascii=True, allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _check_base(
    runtime_raw: dict[str, Any],
    serving_raw: dict[str, Any],
    catalog_raw: dict[str, Any],
    reviewed_pin: Mapping[str, Any],
) -> tuple[int, str]:
    """Validate inputs via the real TSUMGI domain types before editing drafts."""
    runtime = RuntimeDeploymentSpec.from_dict(runtime_raw)
    serving = ServingDeploymentDeclaration.from_dict(serving_raw)
    catalog = ChatProfileCatalog.from_dict(catalog_raw)

    # Trusted baseline identity must come from an independently reviewed
    # artifact register, not be auto-derived from these source manifests.
    expected_keys = {
        "schema_version", "profile_id", "model_artifact_sha256",
        "model_ref_sha256", "deployment_revision",
    }
    _require(isinstance(reviewed_pin, Mapping)
             and set(reviewed_pin) == expected_keys,
             "reviewed-model-pin-invalid")
    _require(reviewed_pin["schema_version"] == TRUSTED_PIN_SCHEMA
             and reviewed_pin["profile_id"] == PROFILE_ID,
             "reviewed-model-pin-scope-mismatch")
    for key in ("model_artifact_sha256", "model_ref_sha256", "deployment_revision"):
        _require(type(reviewed_pin[key]) is str and
                 re.fullmatch(r"sha256:[0-9a-f]{64}", reviewed_pin[key]) is not None,
                 "reviewed-model-pin-digest-invalid")
    # DeploymentIdentity binds the model by model_artifact_sha256, not by
    # path (it has no model_ref field). Bind the independent reviewed
    # runtime model_ref SHA below, then require setup_intent agreement.
    _require(serving.deployment.model_artifact_sha256 ==
             reviewed_pin["model_artifact_sha256"],
             "reviewed-model-artifact-mismatch")
    _require(serving.deployment.revision == reviewed_pin["deployment_revision"],
             "reviewed-deployment-revision-mismatch")
    reference_digest = "sha256:" + hashlib.sha256(
        runtime.demand.model.model_ref.encode("utf-8")
    ).hexdigest()
    _require(reference_digest == reviewed_pin["model_ref_sha256"],
             "reviewed-model-ref-mismatch")

    _require(runtime.provider_id == "llama-cpp", "runtime-provider-mismatch")
    _require(runtime.demand.gpu_topology == GPUTopology.MULTI_GPU
             and runtime.demand.effective_min_gpu_count >= 2,
             "runtime-not-multi-gpu")
    _require(runtime.setup_intent is not None, "setup-intent-missing")
    assert runtime.setup_intent is not None
    _require(runtime.demand.model.model_ref == runtime.setup_intent.model_ref,
             "model-ref-mismatch")
    _require(type(runtime.provider_config.get("context_size")) is int
             and runtime.provider_config["context_size"] == BASE_CONTEXT,
             "runtime-base-context-mismatch")
    _require(type(runtime.setup_intent.configuration.get("context_size")) is int
             and runtime.setup_intent.configuration["context_size"] == BASE_CONTEXT,
             "setup-base-context-mismatch")
    # Generic RuntimeDeploymentSpec alone does not validate the llama.cpp
    # provider-specific fields. Reuse its pinned runtime provider validator.
    LlamaCppProviderConfig(**runtime.provider_config)

    _require(serving.deployment.provider_id == runtime.provider_id,
             "serving-provider-mismatch")
    _require(len(serving.contracts) == 1 and
             serving.contracts[0].capability == "llm.chat",
             "serving-contract-scope-mismatch")
    contract = serving.contracts[0]
    _require("tools" in contract.features, "structured-tools-unsupported")
    _require(dict(contract.limits) == BASE_LIMITS, "serving-base-limits-mismatch")
    _require(len(catalog.profiles) >= 1, "empty-chat-catalog")

    matches = [
        (i, profile) for i, profile in enumerate(catalog.profiles)
        if profile.profile_id == PROFILE_ID
    ]
    _require(len(matches) == 1, "qwen-profile-not-unique")
    index, selected = matches[0]
    _require(selected.adapter_id == LLAMA_CPP_CHAT_ADAPTER, "adapter-mismatch")
    _require(selected.resolved.deployment == serving.deployment
             and selected.resolved.contract == contract,
             "gateway-worker-serving-mismatch")
    _require(dict(selected.resolved.effective_limits) == BASE_LIMITS,
             "gateway-effective-limits-mismatch")
    _require(selected.resolved.profile.capability == "llm.chat",
             "not-a-chat-profile")
    # No other profile may share the old Qwen deployment. An operator must
    # explicitly review any multi-profile routing before a 12K replacement.
    _require(not any(
        p.profile_id != PROFILE_ID and
        p.resolved.deployment.revision == serving.deployment.revision
        for p in catalog.profiles
    ), "shared-deployment-must-be-reviewed")
    return index, serving.deployment.revision


def build_unqualified_candidate(
    runtime_raw: Mapping[str, Any],
    serving_raw: Mapping[str, Any],
    catalog_raw: Mapping[str, Any],
    reviewed_pin: Mapping[str, Any],
) -> dict[str, Any]:
    """Return a draft ONLY; none of its pending Serving records can be loaded."""
    inputs = (deepcopy(dict(runtime_raw)), deepcopy(dict(serving_raw)),
              deepcopy(dict(catalog_raw)))
    runtime, serving, catalog = inputs
    selected_index, original_revision = _check_base(runtime, serving, catalog, reviewed_pin)

    runtime["provider_config"]["context_size"] = TARGET_CONTEXT
    runtime["setup_intent"]["configuration"]["context_size"] = TARGET_CONTEXT
    RuntimeDeploymentSpec.from_dict(runtime)
    LlamaCppProviderConfig(**runtime["provider_config"])

    # Do NOT invent execution_config_sha256 from a local path or a config
    # excerpt. The real reviewed digest MUST be furnished after canary.
    serving["deployment"]["execution_config_sha256"] = PENDING_EXECUTION
    wrapped = serving["contracts"][0]
    contract = wrapped["contract"]
    contract["limits"] = dict(TARGET_LIMITS)
    contract["deployment_revision"] = PENDING_REVISION
    contract["validation_evidence_sha256"] = PENDING_VALIDATION
    wrapped["serving_contract_revision"] = PENDING_REVISION

    selected = catalog["profiles"][selected_index]
    selected["deployment"] = deepcopy(serving["deployment"])
    selected["contract"] = deepcopy(contract)
    selected["profile"]["deployment_revision"] = PENDING_REVISION
    selected["profile"]["serving_contract_revision"] = PENDING_REVISION
    # Preserve explicit per-profile limits on otherwise unchanged operations,
    # but do not leave the old 6,144 input cap accidentally effective.
    profile_limits = selected["profile"].setdefault("limits", {})
    for key in ("input_tokens", "total_tokens"):
        if key in profile_limits:
            profile_limits[key] = TARGET_LIMITS[key]
    # The pending strings are intentionally INVALID TSUMGI digests. Even
    # accidental deployment of the drafts must fail closed.
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

    # The Runtime draft parses, but is not an accepted Serving deployment.
    return {
        "schema_version": DRAFT_SCHEMA,
        "admission": "DENIED_PENDING_REVIEW_AND_REAL_CANARY",
        "profile_id": PROFILE_ID,
        "source_digest": {
            "runtime": _canonical_digest(runtime_raw),
            "serving": _canonical_digest(serving_raw),
            "catalog": _canonical_digest(catalog_raw),
            "reviewed_pin": _canonical_digest(reviewed_pin),
        },
        "previous_deployment_revision": original_revision,
        "target_limits": dict(TARGET_LIMITS),
        "requested_native_context": TARGET_CONTEXT,
        "runtime_draft": runtime,
        "serving_UNLOADABLE_draft": serving,
        "control_UNLOADABLE_draft": catalog,
        "remaining_gates": [
            "verify_actual_peak_vram_on_each_gpu_with_at_least_2048_mib_free",
            "qualify_native_12k_chat_tool_oversize_and_tokenizer",
            "review_execution_config_sha256_excluding_paths_and_secrets",
            "produce_new_validation_evidence_and_provenance",
            "recompute_deployment_contract_and_profile_revisions",
            "drain_jobs_and_approve_reversible_maintenance_cutover",
            "align_opencode_limits_only_after_gateway_serving_is_ready",
            "verify_exact_old_8k_rollback_without_fallback",
        ],
    }


def _load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    _require(isinstance(data, dict), "input-not-json-object")
    return data


def _emit_private_draft(output_dir: Path, draft: dict[str, Any]) -> None:
    # Permit only a fresh direct child of /tmp, never production config paths.
    # Lexically require a *direct* /tmp child, without following any
    # intermediate path or directory symlinks. mkdir is exclusive below.
    _require(output_dir.is_absolute() and output_dir.parent == Path("/tmp")
             and Path("/tmp").is_dir() and not Path("/tmp").is_symlink(),
             "output-must-be-new-direct-tmp-child")
    _require(output_dir.name.startswith("tsumgi-qwen12k-"), "output-prefix-invalid")
    _require(not output_dir.exists() and not output_dir.is_symlink(),
             "output-must-not-exist")
    # Atomic exclusive creation cannot follow an inserted final symlink.
    # Retain a NOFOLLOW directory FD for ALL child creation so a later
    # same-user rename/replacement cannot redirect file writes.
    os.mkdir(output_dir, mode=0o700)
    folder_fd: int | None = None
    sections = (
        ("runtime.UNQUALIFIED.json", draft["runtime_draft"]),
        ("serving.INVALID.json", draft["serving_UNLOADABLE_draft"]),
        ("control.INVALID.json", draft["control_UNLOADABLE_draft"]),
        ("review-gates.json", {k: v for k, v in draft.items()
                              if not k.endswith("_draft")}),
    )
    try:
        folder_fd = os.open(
            output_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        os.fchmod(folder_fd, 0o700)
        for name, section in sections:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            descriptor = os.open(name, flags, 0o600, dir_fd=folder_fd)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(section, stream, ensure_ascii=True,
                          sort_keys=True, indent=2)
                stream.write("\n")
    except BaseException:
        if folder_fd is not None:
            for name, _ in sections:
                with suppress(FileNotFoundError):
                    os.unlink(name, dir_fd=folder_fd)
        # rmdir does not follow a symlink to a relocated directory.
        with suppress(OSError):
            os.rmdir(output_dir)
        raise
    finally:
        if folder_fd is not None:
            os.close(folder_fd)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--serving", type=Path, required=True)
    parser.add_argument("--control", type=Path, required=True)
    parser.add_argument("--reviewed-pin", type=Path, required=True,
                        help="Independent reviewed Qwen model/ref/revision pin JSON")
    parser.add_argument("--output-dir", type=Path,
                        help="Optional new /tmp/tsumgi-qwen12k-* directory")
    args = parser.parse_args()
    try:
        draft = build_unqualified_candidate(
            _load_json(args.runtime), _load_json(args.serving),
            _load_json(args.control), _load_json(args.reviewed_pin),
        )
        if args.output_dir is not None:
            _emit_private_draft(args.output_dir, draft)
    except (ValueError, TypeError, KeyError, OSError) as error:
        # No private paths, model IDs or source JSON values in errors.
        print(json.dumps({"status": "BLOCKED", "error_class": type(error).__name__}))
        return 2
    print(json.dumps({
        "status": draft["admission"],
        "profile_id": draft["profile_id"],
        "target_context": draft["requested_native_context"],
        "target_limits": draft["target_limits"],
        "review_gates_count": len(draft["remaining_gates"]),
        "private_draft_written": args.output_dir is not None,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
