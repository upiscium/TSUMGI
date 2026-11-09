"""#232 R17: OFFLINE, non-production Spica 12K isolated-Control fixture.

Constructs *syntactically valid* Serving identity/contracts for a local-only,
disposable lab. Evidence references are explicitly UNATTESTED historical R2/R15
receipt digests, not evidence that authorizes a production Serving promotion.
Pure data transformation: no file, network, Worker, GPU or Control I/O.
"""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any

from astrumweaver.gateway.chat import (
    CHAT_CATALOG_SCHEMA,
    CHAT_OPERATION_SCHEMA,
    LLAMA_CPP_CHAT_ADAPTER,
    ChatProfileCatalog,
)
from astrumweaver.runtime.deployment import RuntimeDeploymentSpec
from astrumweaver.runtime.providers.llama_cpp import LLAMA_CPP_BONSAI_MODEL_SHA256
from astrumweaver.serving import (
    DeploymentIdentity,
    LogicalServingProfile,
    ServingContract,
    ServingDeploymentDeclaration,
    resolve_profile,
)

SCHEMA = "bonsai-spica-r17-isolated-lab-only-v1"
LAB_PROFILE = "bonsai2-27b-12k-lab"
LAB_WORKER = "bonsai-spica-r17-lab"
LAB_CONTROL_URL = "http://127.0.0.1:19090"
LAB_NATIVE_URL = "http://127.0.0.1:18085"
LAB_CONTROL_PORT = 19090
LAB_NATIVE_PORT = 18085
LAB_HEALTH_PORT = 19100
GPU = "GPU-f78bcab4-245f-b3ff-5570-ed629b239331"
NATIVE_R2_SHA = "2cb28007dfb762eeaf1b09f4111c9c0cca3ef43f10350b1fe011d2376bf49cdb"
MANAGED_R15_SHA = "a54017f3674590ff03eb39eb63f11d1afff16dfd65dac462b5f3ccd98005490b"
LIMITS_8K = {"input_tokens": 6144, "output_tokens": 2048,
             "total_tokens": 8192, "request_bytes": 65536}
LIMITS_12K = {"input_tokens": 10240, "output_tokens": 2048,
              "total_tokens": 12288, "request_bytes": 65536}


def _check(expr: bool, reason: str) -> None:
    if not expr:
        raise ValueError(reason)


def _sha_json(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"),
                     allow_nan=False, ensure_ascii=True).encode("utf-8")
    _check(len(raw) <= 16384, "lab-hash-input-budget")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def render_isolated_lab(
    *, original_8k_runtime: dict[str, Any],
    original_8k_serving: dict[str, Any],
) -> dict[str, Any]:
    """Return *unattested* lab bytes, never install any production identity.

    Explicitly private, unusable outside isolated loopback Control;
    the lab executor/Control checks the resulting revision compatibility.
    """
    orig_runtime = deepcopy(original_8k_runtime)
    orig_serving = deepcopy(original_8k_serving)
    original_spec = RuntimeDeploymentSpec.from_dict(orig_runtime)
    old = ServingDeploymentDeclaration.from_dict(orig_serving)
    _check(original_spec.provider_id == "llama-cpp"
           and len(old.contracts) == 1
           and old.deployment.provider_id == "llama-cpp"
           and old.deployment.quantization == "PQ2_0"
           and old.deployment.model_artifact_sha256 ==
           "sha256:" + LLAMA_CPP_BONSAI_MODEL_SHA256
           and old.contracts[0].capability == "llm.chat"
           and old.contracts[0].operation_schema == CHAT_OPERATION_SCHEMA
           and "tools" in old.contracts[0].features
           and dict(old.contracts[0].limits) == LIMITS_8K,
           "unexpected-bonsai-8k-serving-baseline")
    provider = orig_runtime.get("provider_config")
    setup = orig_runtime.get("setup_intent", {}).get("configuration")
    _check(type(provider) is dict and type(setup) is dict
           and provider.get("context_size") == 8192
           and setup.get("context_size") == 8192
           and provider.get("runtime_flavor") == "bonsai2-pq2"
           and provider.get("offline") is True
           and provider.get("no_webui") is True
           and provider.get("jinja") is True
           and provider.get("fit") is False
           and provider.get("gpu_layers") == "all"
           and provider.get("main_gpu", 0) == 0
           and orig_runtime["demand"]["gpu_topology"] == "single_gpu",
           "unexpected-bonsai-8k-runtime-baseline")
    # R11 precedent: check the old provider/setup intent agree BEFORE
    # mutating a proposed 12K candidate. The old Spica setup-intent GPU
    # was observed to contain Deneb's UUID; accept ONLY that documented
    # historical defect or the already-corrected Spica UUID.
    historical_deneb = "GPU-8bcf8ba8-4610-d9bb-3862-a03b5ea7d931"
    _check(setup.get("gpu_uuids") in ([GPU], [historical_deneb])
           and provider.get("base_url") == setup.get("base_url")
           and setup.get("model_ref") ==
               orig_runtime["setup_intent"].get("model_ref") ==
               orig_runtime["demand"]["model"].get("model_ref"),
           "unexpected-source-setup-intent")
    # This lab builder accepts the actual complete Spica runtime source,
    # not the shortened R0 illustrative fixture. Every provider option that
    # the Setup Intent should mirror must be PRESENT; checking only shared
    # keys lets deleted offline/Jinja/GPU options silently bypass review.
    setup_mirrors = set(provider) - {
        "package_reference", "startup_timeout_seconds",
        "request_timeout_seconds",
    }
    _check(setup_mirrors <= set(setup),
           "source-setup-required-option-missing")
    for key in setup_mirrors:
        left, right = provider[key], setup[key]
        if key == "split_mode":
            left = "none" if left is None else str(left)
            right = "none" if right is None else str(right)
        _check(left == right, "source-provider-setup-diverged-" + key)
    new_runtime = deepcopy(orig_runtime)
    p = new_runtime["provider_config"]
    c = new_runtime["setup_intent"]["configuration"]
    p["context_size"] = 12288
    p["base_url"] = LAB_NATIVE_URL
    c["context_size"] = 12288
    c["base_url"] = LAB_NATIVE_URL
    c["gpu_uuids"] = [GPU]
    new_spec = RuntimeDeploymentSpec.from_dict(new_runtime)
    _check(new_spec.provider_id == "llama-cpp", "lab-runtime-invalid")
    # This digest is a path-neutral, LAB-ONLY CONFIGURATION CLAIM, not the
    # attested physical executable bytes or a vetted production revision.
    lab_options = {
        "schema": SCHEMA,
        "allowed_use": "ISOLATED_LOOPBACK_NONPRODUCTION_ONLY",
        "gpu_uuid": GPU,
        "model_sha256": old.deployment.model_artifact_sha256,
        "context_size": 12288,
        "single_slot": True,
        "runtime_flavor": p["runtime_flavor"],
        "gpu_layers": p["gpu_layers"],
        "main_gpu": p.get("main_gpu", 0),
        "offline": p["offline"],
        "no_webui": p["no_webui"],
        "jinja": p["jinja"],
        "fit": p["fit"],
        "actual_runtime_and_model_bytes_independently_attested": False,
    }
    deployed = DeploymentIdentity(
        **(old.deployment.to_dict() | {
            "execution_config_sha256": _sha_json(lab_options),
        })
    )
    lab_evidence = {
        "schema": "bonsai-spica-r17-unattested-lab-evidence-reference-only-v1",
        "allowed_use": "NONPRODUCTION_VALIDATION_ONLY",
        "native_r2_receipt_sha256": "sha256:" + NATIVE_R2_SHA,
        "managed_r15_receipt_sha256": "sha256:" + MANAGED_R15_SHA,
        "actual_sender_request_bytes_attested": False,
        "process_and_host_origin_authenticated": False,
        "managed_12k_serving_job_claim_tested": False,
    }
    old_contract = old.contracts[0]
    contract = ServingContract(
        deployment_revision=deployed.revision,
        capability="llm.chat",
        operation_schema=CHAT_OPERATION_SCHEMA,
        validation_evidence_sha256=_sha_json(lab_evidence),
        features=old_contract.features,
        limits=LIMITS_12K,
        semantic_revision=old_contract.semantic_revision,
    )
    declaration = ServingDeploymentDeclaration(deployed, (contract,))
    profile = LogicalServingProfile(
        profile_id=LAB_PROFILE,
        deployment_revision=deployed.revision,
        serving_contract_revision=contract.revision,
        capability="llm.chat",
        operation_schema=CHAT_OPERATION_SCHEMA,
        required_features=frozenset({"tools"}),
        limits={},
    )
    catalog_entry = {
        "deployment": deployed.to_dict(),
        "contract": contract.to_dict(),
        "profile": profile.to_dict(),
        "adapter_id": LLAMA_CPP_CHAT_ADAPTER,
        "request_timeout_seconds": 200.0,
        "max_attempts": 1,
        "owned_by": "tsumgi-r17-isolated-lab-only",
        "created": 0,
    }
    lab_catalog = ChatProfileCatalog.from_dict({
        "schema_version": CHAT_CATALOG_SCHEMA,
        "profiles": [catalog_entry],
    })
    _check([m["id"] for m in lab_catalog.models_response()["data"]] == [LAB_PROFILE],
           "lab-model-must-be-single-opt-in")
    _check(resolve_profile(profile, contract, deployed).deployment.revision ==
           deployed.revision, "lab-profile-revision-drift")
    _check(orig_runtime == original_8k_runtime
           and orig_serving == original_8k_serving,
           "unexpected-input-mutation")
    return {
        "schema_version": SCHEMA,
        "admission": "LAB_ONLY_UNATTESTED_SERVING_IDENTITY",
        "new_lab_runtime": new_runtime,
        "new_lab_serving": declaration.to_dict(),
        "new_lab_catalog": {
            "schema_version": CHAT_CATALOG_SCHEMA,
            "profiles": [catalog_entry],
        },
        "new_lab_profile_id": LAB_PROFILE,
        "new_lab_worker_id": LAB_WORKER,
        "lab_control_url": LAB_CONTROL_URL,
        "lab_native_url": LAB_NATIVE_URL,
        "lab_worker_health_port": LAB_HEALTH_PORT,
        "lab_execution_config_claim_sha256": deployed.execution_config_sha256,
        "lab_validation_evidence_claim_sha256": contract.validation_evidence_sha256,
        "production_serving_identity_issued": False,
        "production_control_catalog_modified": False,
        "deployment_authorized": False,
    }
