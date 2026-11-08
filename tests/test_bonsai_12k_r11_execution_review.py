"""R11 path-neutral, review-only Spica 12K execution claim contracts."""
from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from bonsai_12k_r11_execution_review import (
    SCHEMA,
    build_unattested_execution_review,
)
from bonsai_12k_spica_candidate import (
    PROFILE_12K,
    build_spica_unqualified_candidate,
)
from test_bonsai_12k_spica_candidate import make_inputs

from astrumweaver.runtime.providers.llama_cpp import LLAMA_CPP_BONSAI_MODEL_SHA256


def fake_report():
    return {
        "runtime": "sha256:" + "1" * 64,
        "adapter": "sha256:" + "2" * 64,
        "model": "sha256:" + LLAMA_CPP_BONSAI_MODEL_SHA256,
        "tokenizer": None,
        "template": "sha256:" + "3" * 64,
    }


def candidate():
    return build_spica_unqualified_candidate(**make_inputs())


def review(c=None, artifacts=None):
    return build_unattested_execution_review(
        unqualified_candidate=c if c is not None else candidate(),
        reported_artifact_hashes=artifacts if artifacts is not None else fake_report(),
    )


def test_synthetic_review_has_no_publishable_execution_or_evidence_identity():
    c, hashes = candidate(), fake_report()
    orig_c, orig_h = deepcopy(c), deepcopy(hashes)
    result = review(c, hashes)
    assert c == orig_c and hashes == orig_h
    assert result["schema_version"] == SCHEMA
    assert result["status"] == "BLOCKED_UNATTESTED_EXECUTION_AND_MANAGED_WORKER"
    assert len(result["review_fingerprint_sha256_not_a_deployment_identity"]) == 64
    record = result["record_only"]
    assert record["profile_id"] == PROFILE_12K
    assert record["parallel_slots"] == 1
    assert record["limits"] == {
        "input_tokens": 10240, "output_tokens": 2048,
        "total_tokens": 12288, "request_bytes": 65536,
    }
    assert record["gpu_uuid"] == c["source_gpu_uuids"]["spica_worker"]
    assert record["reported_artifact_sha256"] == hashes
    assert record["effective_provider_options"]["context_size"] == 12288
    assert record["effective_provider_options"]["runtime_flavor"] == "bonsai2-pq2"
    assert all(result[key] is False for key in (
        "reported_hashes_independently_verified",
        "host_and_gpu_origin_authenticated",
        "actual_native_argv_observed",
        "managed_worker_12k_tested",
        "serving_revision_issued",
        "new_runtime_instance_epoch_allocated",
        "control_catalog_updated",
        "canary_activation_authorized",
    ))
    assert not any("proposal" in key or "serving" in key and key != "serving_revision_issued"
                   for key in result)


def test_review_fingerprint_is_path_neutral_not_path_dependent_manifest_hash():
    c, hashes = candidate(), fake_report()
    baseline = review(c, hashes)
    other = deepcopy(c)
    runtime = other["runtime_UNQUALIFIED_draft"]
    # Same claimed executable hash but a different path resolution.
    runtime["provider_config"]["executable"] = "llama-server"
    runtime["provider_config"]["base_url"] = "http://127.0.0.1:9235"
    runtime["setup_intent"]["configuration"]["base_url"] = "http://127.0.0.1:9235"
    original = runtime["setup_intent"]["model_ref"]
    assert original.endswith(".gguf")
    other_path = "/another-reviewed-location/" + original.rsplit("/", 1)[1]
    runtime["demand"]["model"]["model_ref"] = other_path
    runtime["setup_intent"]["model_ref"] = other_path
    runtime["setup_intent"]["configuration"]["model_ref"] = other_path
    shifted = review(other, hashes)
    assert shifted["record_only"] == baseline["record_only"]
    assert shifted["review_fingerprint_sha256_not_a_deployment_identity"] == (
        baseline["review_fingerprint_sha256_not_a_deployment_identity"]
    )
    raw = json.dumps(shifted)
    assert "/synthetic/models/" not in raw
    assert "/another-reviewed-location/" not in raw
    assert "9235" not in raw
    assert "llama-server" not in raw


def test_full_pinned_source_split_mode_none_and_setup_none_are_equivalent():
    c = candidate()
    runtime = c["runtime_UNQUALIFIED_draft"]
    runtime["provider_config"]["split_mode"] = None
    runtime["setup_intent"]["configuration"]["split_mode"] = "none"
    assert review(c)["record_only"]["effective_provider_options"]["split_mode"] == "none"
    assert review(c)["review_fingerprint_sha256_not_a_deployment_identity"] == (
        review()["review_fingerprint_sha256_not_a_deployment_identity"]
    )


@pytest.mark.parametrize(("role", "bad"), [
    ("model", "sha256:" + "9" * 64), ("runtime", "PENDING"),
    ("adapter", "sha256:" + "X" * 64), ("model", None),
    ("template", ""), ("tokenizer", "a" * 64), ("runtime", None),
])
def test_untrusted_or_invalid_artifact_values_fail_closed(role, bad):
    hashes = fake_report()
    hashes[role] = bad
    with pytest.raises(ValueError, match="artifact-record|model-pinned"):
        review(artifacts=hashes)


@pytest.mark.parametrize("extra", ["private_key", "executor_password"])
def test_extra_artifact_roles_are_refused(extra):
    hashes = fake_report()
    hashes[extra] = "secret"
    with pytest.raises(ValueError, match="artifact-record-shape"):
        review(artifacts=hashes)


@pytest.mark.parametrize(("where", "field", "value", "error"), [
    ("provider", "context_size", 8192, "runtime-required-flavor"),
    ("setup", "context_size", 8192, "setup-context"),
    ("provider", "runtime_flavor", "upstream", "runtime-required"),
    ("provider", "offline", False, "runtime-required"),
    ("provider", "jinja", False, "runtime-required"),
    ("provider", "fit", True, "runtime-required"),
    ("provider", "gpu_layers", 0, "runtime-required"),
    ("setup", "gpu_uuids", ["GPU-8bcf8ba8-4610-d9bb-3862-a03b5ea7d931"], "setup-context"),
    ("provider", "unreviewed_runtime_arg", "--dangerous", "unreviewed-options"),
    ("setup", "unreviewed_runtime_arg", "x", "unreviewed-options"),
    ("setup", "model_alias", "other", "setup-chat-identity"),
    ("setup", "capabilities", ["audio.synthesize"], "setup-chat-identity"),
])
def test_mismatched_or_extra_effective_source_options_fail_closed(
    where, field, value, error,
):
    c = candidate()
    r = c["runtime_UNQUALIFIED_draft"]
    obj = (r["provider_config"] if where == "provider"
           else r["setup_intent"]["configuration"])
    obj[field] = value
    with pytest.raises(ValueError, match=error):
        review(c)


def test_bad_spica_gpu_reviewer_identity_and_malformed_source_fail_closed():
    c = candidate()
    c["source_gpu_uuids"]["spica_worker"] = c["source_gpu_uuids"]["deneb_worker"]
    with pytest.raises(ValueError, match="source-gpu-identity-invalid"):
        review(c)
    c = candidate()
    c["source_gpu_uuids"]["spica_worker"] = "GPU-" + "-" * 36
    with pytest.raises(ValueError, match="source-gpu-identity-invalid"):
        review(c)
    c = candidate()
    c["runtime_UNQUALIFIED_draft"]["setup_intent"]["configuration"]["gpu_uuids"] = [
        c["source_gpu_uuids"]["deneb_worker"]
    ]
    with pytest.raises(ValueError, match="setup-context-or-spica-gpu-mismatch"):
        review(c)


def test_source_side_manifests_stay_pending_and_cannot_be_used_as_deployment():
    from astrumweaver.gateway.chat import ChatGatewayError, ChatProfileCatalog
    from astrumweaver.serving import ServingContractError, ServingDeploymentDeclaration

    c = candidate()
    review(c)
    with pytest.raises(ServingContractError):
        ServingDeploymentDeclaration.from_dict(c["serving_INVALID_draft"])
    with pytest.raises(ChatGatewayError):
        ChatProfileCatalog.from_dict(c["control_INVALID_draft"])


def test_bad_provider_number_and_option_divergence_fail_closed():
    c = candidate()
    c["runtime_UNQUALIFIED_draft"]["provider_config"]["startup_timeout_seconds"] = float("nan")
    with pytest.raises(ValueError, match="nonfinite-effective-provider-option"):
        review(c)
    c = candidate()
    c["runtime_UNQUALIFIED_draft"]["setup_intent"]["configuration"]["jinja"] = False
    with pytest.raises(ValueError, match="setup-provider-diverged-jinja"):
        review(c)
    c = candidate()
    c["runtime_UNQUALIFIED_draft"]["provider_config"]["gpu_layers"] = True
    with pytest.raises(ValueError, match="runtime-required-flavor-or-options-mismatch"):
        review(c)


def test_fingerprint_changes_on_artifact_or_semantic_option_drift():
    c = candidate()
    base = review(c)["review_fingerprint_sha256_not_a_deployment_identity"]
    hashes = fake_report()
    hashes["adapter"] = "sha256:" + "f" * 64
    assert review(c, hashes)["review_fingerprint_sha256_not_a_deployment_identity"] != base
    other = candidate()
    other["runtime_UNQUALIFIED_draft"]["provider_config"]["request_timeout_seconds"] = 500.0
    assert review(other)["review_fingerprint_sha256_not_a_deployment_identity"] != base


def test_full_source_shape_all_provider_fields_and_setup_split_mode_none():
    """Mirror read-only Spica's observed 23 provider / 24 setup keys."""
    from dataclasses import fields

    from astrumweaver.runtime.providers.llama_cpp import LlamaCppProviderConfig

    c = candidate()
    runtime = c["runtime_UNQUALIFIED_draft"]
    p = runtime["provider_config"]
    s = runtime["setup_intent"]["configuration"]
    resolved = LlamaCppProviderConfig(**p)
    for field in fields(LlamaCppProviderConfig):
        if field.name not in p:
            value = getattr(resolved, field.name)
            if isinstance(value, tuple):
                value = list(value)
            p[field.name] = value
    assert len(p) == 23
    # The live configuration adds every shared provider option in setup,
    # but its split_mode=None is spelled "none" in SetupIntent.
    for key, value in p.items():
        if key not in {"package_reference", "request_timeout_seconds",
                       "startup_timeout_seconds"} and key not in s:
            s[key] = "none" if key == "split_mode" and value is None else value
    s["capabilities"] = ["llm.chat", "text.generate"]
    assert len(s) == 24
    assert review(c)["review_fingerprint_sha256_not_a_deployment_identity"] == (
        review()["review_fingerprint_sha256_not_a_deployment_identity"]
    )


@pytest.mark.parametrize(("field", "bad"), [
    ("package_reference", "llama-cpp"),
    ("tensor_split", [1.0]),
    ("fit_target_mb", [100]),
    ("cpu_moe", True),
    ("chat_template_file", "/tmp/alternate.jinja"),
    ("split_mode", "row"),
    ("main_gpu", 1),
])
def test_unapproved_alternate_bonsai_placement_or_template_options_fail(field, bad):
    c = candidate()
    c["runtime_UNQUALIFIED_draft"]["provider_config"][field] = bad
    with pytest.raises(ValueError, match="runtime-required-flavor-or-options-mismatch"):
        review(c)


def test_missing_artifact_role_and_unauthorized_profile_fail_closed():
    hashes = fake_report()
    del hashes["runtime"]
    with pytest.raises(ValueError, match="artifact-record-shape-invalid"):
        review(artifacts=hashes)
    c = candidate()
    c["opt_in_model_id"] = "bonsai2-27b"
    with pytest.raises(ValueError, match="unqualified-spica-candidate-required"):
        review(c)
