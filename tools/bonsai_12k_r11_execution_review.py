"""#232 R11: path-neutral, UNATTESTED Spica 12K execution review outline.

Pure data normalization only: no filesystem, subprocess, network, GPU, or
production Serving revision output. A digest of operator CLAIMS is never
proof of executable/model/adapter/template bytes or running process state.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import fields
from typing import Any

from bonsai_12k_spica_candidate import (
    CONTEXT_12K,
    DRAFT_SCHEMA,
    LIMITS_12K,
    PROFILE_12K,
)

from astrumweaver.runtime.providers.llama_cpp import (
    LLAMA_CPP_BONSAI_MODEL_NAME,
    LLAMA_CPP_BONSAI_MODEL_SHA256,
    LlamaCppProviderConfig,
)

SCHEMA = "bonsai-spica-r11-execution-review-only-v1"
_BLOCKED = "BLOCKED_UNATTESTED_EXECUTION_AND_MANAGED_WORKER"
_SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")
_GPU = re.compile(r"GPU-[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")
# All provider options are accounted for. Path/transport fields are validated
# against the runtime source, but replaced by claimed artifact hashes and
# excluded from the path-neutral execution fingerprint.
_NON_IDENTITY = {"base_url", "executable", "chat_template_file"}
_OPTION_FIELDS = (
    "context_size", "gpu_layers", "split_mode", "tensor_split", "main_gpu",
    "fit", "fit_target_mb", "cpu_moe", "n_cpu_moe", "n_cpu_ffn",
    "offline", "no_webui", "jinja", "embeddings", "pooling", "decision",
    "runtime_flavor", "startup_timeout_seconds", "request_timeout_seconds",
    "package_reference",
)
_SETUP_EXTRAS = {"gpu_uuids", "model_ref", "model_alias", "capabilities"}
_SETUP_ALLOWED = (set(_OPTION_FIELDS) - {
    "package_reference", "startup_timeout_seconds", "request_timeout_seconds",
}) | _NON_IDENTITY | _SETUP_EXTRAS
_ARTIFACTS = {"runtime", "adapter", "model", "tokenizer", "template"}
_PROVIDER_ALLOWED = {item.name for item in fields(LlamaCppProviderConfig)}
if _PROVIDER_ALLOWED != set(_OPTION_FIELDS) | _NON_IDENTITY:
    # Stop source-only fingerprint generation if a provider option is added
    # without an explicit identity/path-neutrality decision in this module.
    raise RuntimeError("unreviewed-llama-provider-field-set")


def require(condition: bool, code: str) -> None:
    if not condition:
        raise ValueError(code)


def _normal(value: Any, key: str) -> Any:
    if key == "split_mode":
        return str(value) if value is not None else "none"
    if isinstance(value, (list, tuple)):
        return [_normal(item, "") for item in value]
    if type(value) is float:
        require(math.isfinite(value), "nonfinite-effective-provider-option")
    if value is None or type(value) in (bool, int, float, str):
        return value
    raise ValueError("unsupported-provider-option-value")


def build_unattested_execution_review(
    *, unqualified_candidate: dict[str, Any],
    reported_artifact_hashes: dict[str, str | None],
) -> dict[str, object]:
    """Check a bounded review shape, then hash only nonsecret pathless claims.

    This digest MUST NOT be copied into DeploymentIdentity.execution_config_sha256.
    Nothing here verifies actual file contents, signer identity, physical GPU,
    process arguments, Worker readiness, or the R6B canary provenance.
    """
    c = unqualified_candidate
    require(type(c) is dict and c.get("schema_version") == DRAFT_SCHEMA
            and c.get("admission") == "DENIED_PENDING_REAL_CANARY_AND_REVIEW"
            and c.get("spica_worker_id") == "bonsai-spica"
            and c.get("opt_in_model_id") == PROFILE_12K
            and c.get("existing_model_id_unchanged") == "bonsai2-27b",
            "unqualified-spica-candidate-required")
    gpus = c.get("source_gpu_uuids")
    require(type(gpus) is dict
            and set(gpus) == {
                "spica_worker", "deneb_worker", "spica_runtime_source",
            }, "source-gpu-shape-invalid")
    gpu = gpus["spica_worker"]
    require(type(gpu) is str and _GPU.fullmatch(gpu) is not None
            and type(gpus["deneb_worker"]) is str
            and _GPU.fullmatch(gpus["deneb_worker"]) is not None
            and gpu != gpus["deneb_worker"], "source-gpu-identity-invalid")
    raw = c.get("runtime_UNQUALIFIED_draft")
    require(type(raw) is dict and raw.get("provider_id") == "llama-cpp",
            "runtime-review-shape-invalid")
    provider = raw.get("provider_config")
    setup = raw.get("setup_intent")
    require(type(provider) is dict and set(provider) <= _PROVIDER_ALLOWED
            and type(setup) is dict and type(setup.get("configuration")) is dict,
            "provider-or-setup-unreviewed-options")
    configuration = setup["configuration"]
    require(set(configuration) <= _SETUP_ALLOWED,
            "setup-unreviewed-options")
    # The pinned Bonsai adapter may *normalize* some caller-supplied options
    # (notably jinja=False -> True). Reject unreviewed source intent BEFORE
    # normalization so the review fingerprint cannot hide that divergence.
    require(provider.get("context_size") == CONTEXT_12K
            and provider.get("runtime_flavor") == "bonsai2-pq2"
            and provider.get("offline") is True
            and provider.get("no_webui") is True
            and provider.get("jinja") is True
            and provider.get("fit") is False
            and provider.get("gpu_layers") == "all"
            and provider.get("package_reference") == "bonsai-llama-cpp"
            and provider.get("main_gpu", 0) == 0
            and provider.get("tensor_split") is None
            and provider.get("fit_target_mb") is None
            and provider.get("cpu_moe", False) is False
            and provider.get("n_cpu_moe") is None
            and provider.get("n_cpu_ffn") is None
            and provider.get("embeddings", False) is False
            and provider.get("decision", False) is False
            and provider.get("pooling") is None
            and provider.get("chat_template_file") is None
            and provider.get("split_mode") in (None, "none"),
            "runtime-required-flavor-or-options-mismatch")
    try:
        resolved = LlamaCppProviderConfig(**provider)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError("provider-configuration-invalid") from exc
    require(resolved.runtime_flavor == "bonsai2-pq2"
            and resolved.context_size == CONTEXT_12K
            and resolved.offline is True
            and resolved.no_webui is True
            and resolved.jinja is True
            and resolved.embeddings is False
            and resolved.decision is False
            and resolved.fit is False
            and resolved.gpu_layers == "all"
            and resolved.main_gpu == 0,
            "runtime-required-flavor-or-options-mismatch")
    require(configuration.get("context_size") == CONTEXT_12K
            and configuration.get("gpu_uuids") == [gpu],
            "setup-context-or-spica-gpu-mismatch")
    demand = raw.get("demand")
    model = demand.get("model") if type(demand) is dict else None
    require(type(model) is dict
            and type(model.get("model_ref")) is str
            and model["model_ref"].endswith("/" + LLAMA_CPP_BONSAI_MODEL_NAME)
            and setup.get("model_ref") == model["model_ref"]
            and configuration.get("model_ref") == model["model_ref"]
            and demand.get("gpu_topology") == "single_gpu",
            "model-ref-or-topology-mismatch")
    # Check the common setup/provider values. The existing pinned source has
    # split_mode=None in provider and split_mode="none" in setup intent.
    for key in set(provider) & set(configuration):
        if key == "split_mode":
            equal = _normal(provider[key], key) == _normal(configuration[key], key)
        else:
            equal = provider[key] == configuration[key]
        require(equal, "setup-provider-diverged-" + key)
    require(configuration.get("model_alias", "astrumweaver") == "astrumweaver"
            and (configuration.get("capabilities") is None or
                 configuration["capabilities"] == ["llm.chat", "text.generate"]),
            "setup-chat-identity-mismatch")
    require(type(reported_artifact_hashes) is dict
            and set(reported_artifact_hashes) == _ARTIFACTS,
            "artifact-record-shape-invalid")
    for key in _ARTIFACTS:
        value = reported_artifact_hashes[key]
        require((value is None and key in {"tokenizer", "template"})
                or (type(value) is str and _SHA.fullmatch(value) is not None),
                "artifact-record-digest-invalid")
    require(reported_artifact_hashes["model"]
            == "sha256:" + LLAMA_CPP_BONSAI_MODEL_SHA256,
            "model-pinned-artifact-digest-mismatch")
    effective = {key: _normal(getattr(resolved, key), key)
                 for key in _OPTION_FIELDS}
    # A future record may carry externally measured hashes; these are merely
    # values supplied by a caller, not authenticated observations here.
    claim = {
        "schema_version": SCHEMA,
        "profile_id": PROFILE_12K,
        "worker_id": "bonsai-spica",
        "gpu_uuid": gpu,
        "parallel_slots": 1,
        "limits": dict(LIMITS_12K),
        "reported_artifact_sha256": dict(reported_artifact_hashes),
        "effective_provider_options": effective,
        "setup_model_alias": "astrumweaver",
    }
    encoded = json.dumps(
        claim, sort_keys=True, separators=(",", ":"),
        ensure_ascii=True, allow_nan=False,
    ).encode("utf-8")
    require(0 < len(encoded) <= 16_384,
            "execution-review-canonical-budget-exceeded")
    return {
        "schema_version": SCHEMA,
        "status": _BLOCKED,
        "record_only": claim,
        "review_fingerprint_sha256_not_a_deployment_identity": (
            hashlib.sha256(encoded).hexdigest()
        ),
        "reported_hashes_independently_verified": False,
        "host_and_gpu_origin_authenticated": False,
        "actual_native_argv_observed": False,
        "managed_worker_12k_tested": False,
        "serving_revision_issued": False,
        "new_runtime_instance_epoch_allocated": False,
        "control_catalog_updated": False,
        "canary_activation_authorized": False,
    }
