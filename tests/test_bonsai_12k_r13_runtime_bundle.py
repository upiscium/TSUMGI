"""#232 R13: synthetic-only selected mapped runtime dependency binding.

No actual /proc, GPU, file, Nix, HTTP or cryptographic attestation I/O.
"""
from __future__ import annotations

import hashlib
import sys
from copy import deepcopy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from bonsai_12k_r13_runtime_bundle import (
    REQUIRED_MAPPED_SONAMES,
    STATUS,
    bind_r11_reported_runtime_bundle,
    compute_reported_runtime_bundle,
)
from test_bonsai_12k_r11_execution_review import fake_report, review


def digest(data: str) -> str:
    return "sha256:" + hashlib.sha256(data.encode()).hexdigest()


def inventory():
    # Sizes are selected observed live Spica 8K examples. Digests are FAKE
    # deterministic synthetic fixture hashes, NOT the actual Spica hashes.
    sizes = {
        "llama-server": 26656,
        "libggml-base.so.0.21.0": 973656,
        "libggml-cpu-cooperlake.so": 1466320,
        "libggml-cpu-haswell.so": 1310464,
        "libggml-cuda.so": 692921504,
        "libggml-rpc.so": 173912,
        "libggml.so.0.21.0": 61064,
        "libllama-common.so.0.2.0": 6713176,
        "libllama-server-impl.so": 7437400,
        "libllama.so.0.2.0": 5022720,
        "libmtmd.so.0.2.0": 1914584,
        "libcublas.so.12.9.2.10": 105186528,
        "libcublasLt.so.12.9.2.10": 749232240,
        "libcudart.so.12.9.79": 774872,
    }
    assert set(sizes) == REQUIRED_MAPPED_SONAMES
    native = {
        "sha256": digest("synthetic-actual-native-exe"),
        "size_bytes": sizes["llama-server"],
        "executable_name": "llama-server",
    }
    mapping = [
        {
            "soname": name,
            "sha256": native["sha256"] if name == "llama-server" else digest(name),
            "size_bytes": sizes[name],
        }
        for name in sorted(sizes)
    ]
    return {
        "wrapper": {
            "sha256": digest("synthetic-bash-wrapper"),
            "size_bytes": 4433,
            "entrypoint_kind": "bash-wrapper",
        },
        "native_executable": native,
        "mapped_files": mapping,
    }


def review_bundle(*, inventory_override=None):
    inv = inventory() if inventory_override is None else inventory_override
    return compute_reported_runtime_bundle(**inv)


def binding():
    bundle = review_bundle()
    hashes = fake_report()
    hashes["runtime"] = bundle["review_only_bundle_sha256"]
    return review(artifacts=hashes), bundle


def test_r13_canonical_selected_runtime_bundle_only_is_unqualified():
    inv = inventory()
    frozen = deepcopy(inv)
    result = review_bundle(inventory_override=inv)
    assert inv == frozen
    assert result["status"] == STATUS
    assert result["reported_object_count"] == 14
    assert result["reported_runtime_dependencies"]["wrapper"] == frozen["wrapper"]
    assert result["reported_runtime_dependencies"]["native_executable"] == frozen["native_executable"]
    assert result["selected_libraries_not_full_elf_closure"] is True
    assert result["review_only_bundle_sha256"].startswith("sha256:")
    assert all(result[field] is False for field in (
        "library_bytes_independently_verified", "live_pid_maps_attested",
        "gpu_runtime_artifact_authenticated", "managed_12k_qualified",
        "canary_activation_authorized",
    ))


def test_mapping_order_and_host_paths_cannot_change_claimed_byte_identity():
    inv = inventory()
    old = review_bundle(inventory_override=inv)
    changed = deepcopy(inv)
    changed["mapped_files"].reverse()
    assert review_bundle(inventory_override=changed)["review_only_bundle_sha256"] == (
        old["review_only_bundle_sha256"]
    )
    assert not any("/nix/store/" in str(row) or "/proc/" in str(row)
                   for row in old["reported_runtime_dependencies"]["mapped_sonames"])


def test_r11_runtime_claim_must_cover_whole_selected_bundle_not_just_wrapper():
    r11, bundle = binding()
    output = bind_r11_reported_runtime_bundle(r11_review=r11, bundle_review=bundle)
    assert output["status"] == STATUS
    assert output["selected_dependency_count"] == 14
    assert output["review_only_covered_runtime_dependency_sha256"] == (
        bundle["review_only_bundle_sha256"]
    )
    assert output["r11_fingerprint_bound_to_runtime_bundle_only"] == (
        r11["review_fingerprint_sha256_not_a_deployment_identity"]
    )
    assert all(output[k] is False for k in (
        "full_dynamic_library_dependency_closure_proven",
        "source_host_authenticity_proven", "managed_worker_12k_qualified",
        "serving_revision_issued", "new_runtime_instance_epoch_allocated",
        "control_catalog_modified", "canary_activation_authorized",
    ))
    # A standalone wrapper hash is not enough, despite being digest-shaped.
    hashes = fake_report()
    hashes["runtime"] = inventory()["wrapper"]["sha256"]
    with pytest.raises(ValueError, match="r11-runtime-hash-does-not-cover-selected-bundle"):
        bind_r11_reported_runtime_bundle(
            r11_review=review(artifacts=hashes), bundle_review=bundle,
        )


@pytest.mark.parametrize(("role", "field", "bad", "error"), [
    ("wrapper", "entrypoint_kind", "binary", "wrapper-byte-claim"),
    ("wrapper", "size_bytes", 0, "wrapper-byte-claim"),
    ("wrapper", "size_bytes", True, "wrapper-byte-claim"),
    ("wrapper", "sha256", "PENDING", "wrapper-byte-claim"),
    ("native_executable", "executable_name", "llama-wrapper", "native-executable-byte"),
    ("native_executable", "sha256", "sha256:" + "X" * 64, "native-executable-byte"),
    ("native_executable", "size_bytes", -1, "native-executable-byte"),
])
def test_wrong_runtime_wrapper_or_native_byte_claims_fail_closed(
    role, field, bad, error,
):
    inv = inventory()
    inv[role][field] = bad
    with pytest.raises(ValueError, match=error):
        review_bundle(inventory_override=inv)


@pytest.mark.parametrize(("name", "bad", "error"), [
    ("soname", "libunknown.so", "mapped-runtime-object-name"),
    ("size_bytes", -1, "mapped-runtime-object-byte"),
    ("sha256", "missing", "mapped-runtime-object-byte"),
    ("size_bytes", True, "mapped-runtime-object-byte"),
])
def test_malformed_or_unreviewed_mapped_library_fails_closed(name, bad, error):
    inv = inventory()
    inv["mapped_files"][0][name] = bad
    with pytest.raises(ValueError, match=error):
        review_bundle(inventory_override=inv)


def test_missing_or_duplicate_shared_library_fails_closed():
    inv = inventory()
    inv["mapped_files"].pop()
    with pytest.raises(ValueError, match="mapped-runtime-object-count"):
        review_bundle(inventory_override=inv)
    inv = inventory()
    inv["mapped_files"][2] = deepcopy(inv["mapped_files"][1])
    with pytest.raises(ValueError, match="mapped-runtime-object-name"):
        review_bundle(inventory_override=inv)


def test_observed_native_executable_must_match_mapped_runtime_process_binary():
    inv = inventory()
    native_row = next(row for row in inv["mapped_files"] if row["soname"] == "llama-server")
    native_row["sha256"] = digest("different-dynamic-executable")
    with pytest.raises(ValueError, match="native-process-executable-map-mismatch"):
        review_bundle(inventory_override=inv)


def test_every_selected_shared_library_change_changes_review_sha():
    inv = inventory()
    base = review_bundle(inventory_override=inv)["review_only_bundle_sha256"]
    for mapped_name in (
        "libggml-cuda.so", "libcublasLt.so.12.9.2.10",
        "libllama-server-impl.so", "libllama.so.0.2.0",
    ):
        modified = deepcopy(inv)
        item = next(row for row in modified["mapped_files"] if row["soname"] == mapped_name)
        item["sha256"] = digest(mapped_name + "-modified")
        assert review_bundle(inventory_override=modified)["review_only_bundle_sha256"] != base


def test_untrusted_r11_report_mutation_cannot_silently_change_runtime_claim():
    r11, bundle = binding()
    bad = deepcopy(r11)
    bad["record_only"]["reported_artifact_sha256"]["runtime"] = digest("wrapper-only")
    with pytest.raises(ValueError, match="r11-review-fingerprint-mismatch"):
        bind_r11_reported_runtime_bundle(r11_review=bad, bundle_review=bundle)
    bad = deepcopy(r11)
    bad["serving_revision_issued"] = True
    with pytest.raises(ValueError, match="r11-unqualified-source-required"):
        bind_r11_reported_runtime_bundle(r11_review=bad, bundle_review=bundle)


@pytest.mark.parametrize("field", [
    "library_bytes_independently_verified", "live_pid_maps_attested",
    "gpu_runtime_artifact_authenticated", "managed_12k_qualified",
    "canary_activation_authorized",
])
def test_false_proof_flags_cannot_be_upgraded_by_caller(field):
    r11, bundle = binding()
    forged = deepcopy(bundle)
    forged[field] = True
    with pytest.raises(ValueError, match="runtime-bundle-still-unqualified-required"):
        bind_r11_reported_runtime_bundle(r11_review=r11, bundle_review=forged)


def test_bundle_digest_mutation_and_extra_unreviewed_roles_fail_closed():
    r11, bundle = binding()
    bad = deepcopy(bundle)
    bad["review_only_bundle_sha256"] = digest("made-up")
    with pytest.raises(ValueError, match="runtime-bundle-review-digest-mismatch"):
        bind_r11_reported_runtime_bundle(r11_review=r11, bundle_review=bad)
    bad = deepcopy(bundle)
    bad["provenance_signature_verified"] = True
    with pytest.raises(ValueError, match="runtime-bundle-still-unqualified-required"):
        bind_r11_reported_runtime_bundle(r11_review=r11, bundle_review=bad)


def test_changed_pinned_model_claim_is_rejected_even_with_recomputed_review_hash():
    from bonsai_12k_r13_runtime_bundle import sha256_json

    r11, bundle = binding()
    forged = deepcopy(r11)
    forged["record_only"]["reported_artifact_sha256"]["model"] = digest("wrong-gguf")
    forged["review_fingerprint_sha256_not_a_deployment_identity"] = (
        sha256_json(forged["record_only"]).removeprefix("sha256:")
    )
    with pytest.raises(ValueError, match="r11-pinned-model-identity-invalid"):
        bind_r11_reported_runtime_bundle(r11_review=forged, bundle_review=bundle)


def test_self_asserted_r11_gpu_origin_flag_is_not_authentication():
    r11, bundle = binding()
    bad = deepcopy(r11)
    bad["host_and_gpu_origin_authenticated"] = True
    with pytest.raises(ValueError, match="r11-unqualified-source-required"):
        bind_r11_reported_runtime_bundle(r11_review=bad, bundle_review=bundle)
