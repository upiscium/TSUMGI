"""#232 R13: source-only Bonsai Native dependency-bundle review.

An observed llama-server frontend is a tiny wrapper/bridge, not the whole
CUDA inference runtime. This module only checks *caller-supplied* inventory
shapes and never reads /proc, Nix, CUDA, model files or production services.
It does NOT issue trusted evidence, a Serving revision, or an executable ID.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from bonsai_12k_r11_execution_review import SCHEMA as R11_SCHEMA

from astrumweaver.runtime.providers.llama_cpp import LLAMA_CPP_BONSAI_MODEL_SHA256

SCHEMA = "bonsai-spica-r13-native-runtime-bundle-review-v1"
BUNDLE_SCHEMA = "bonsai-spica-pinned-runtime-dependency-bundle-v1"
STATUS = "BLOCKED_UNATTESTED_RUNTIME_DEPENDENCIES_AND_MANAGED_12K"
_SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")
_GPU = re.compile(r"GPU-[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")
# Snapshot of selected Bonsai-related loaded objects observed on the active
# Spica 8K process (not all Linux ELF dependencies and not an ABI guarantee).
# Any drift must prompt an independent scope review, never silent omission.
REQUIRED_MAPPED_SONAMES = frozenset({
    "llama-server",
    "libggml-base.so.0.21.0",
    "libggml-cpu-cooperlake.so",
    "libggml-cpu-haswell.so",
    "libggml-cuda.so",
    "libggml-rpc.so",
    "libggml.so.0.21.0",
    "libllama-common.so.0.2.0",
    "libllama-server-impl.so",
    "libllama.so.0.2.0",
    "libmtmd.so.0.2.0",
    "libcublas.so.12.9.2.10",
    "libcublasLt.so.12.9.2.10",
    "libcudart.so.12.9.79",
})
_WRAPPER_FIELDS = {"sha256", "size_bytes", "entrypoint_kind"}
_NATIVE_FIELDS = {"sha256", "size_bytes", "executable_name"}
_MAPPING_FIELDS = {"soname", "sha256", "size_bytes"}


def require(test: bool, code: str) -> None:
    if not test:
        raise ValueError(code)


def sha256_json(value: object) -> str:
    try:
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"),
            ensure_ascii=True, allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError) as exc:
        raise ValueError("noncanonical-runtime-review-data") from exc
    require(0 < len(encoded) <= 16384, "runtime-review-budget-exceeded")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _valid_sha(value: object) -> bool:
    return type(value) is str and _SHA.fullmatch(value) is not None


def _valid_size(value: object) -> bool:
    return type(value) is int and 1 <= value <= 4_000_000_000


def compute_reported_runtime_bundle(
    *, wrapper: dict[str, object],
    native_executable: dict[str, object],
    mapped_files: list[dict[str, object]],
) -> dict[str, object]:
    """Canonicalize *claims*. No proof any library was really mapped."""
    require(type(wrapper) is dict and set(wrapper) == _WRAPPER_FIELDS,
            "wrapper-role-invalid")
    require(_valid_sha(wrapper["sha256"]) and _valid_size(wrapper["size_bytes"])
            and wrapper["entrypoint_kind"] == "bash-wrapper",
            "wrapper-byte-claim-invalid")
    require(type(native_executable) is dict
            and set(native_executable) == _NATIVE_FIELDS,
            "native-executable-role-invalid")
    require(_valid_sha(native_executable["sha256"])
            and _valid_size(native_executable["size_bytes"])
            and native_executable["executable_name"] == "llama-server",
            "native-executable-byte-claim-invalid")
    require(wrapper["sha256"] != native_executable["sha256"],
            "wrapper-and-native-must-be-distinct")
    require(type(mapped_files) is list and len(mapped_files) == len(REQUIRED_MAPPED_SONAMES),
            "mapped-runtime-object-count-invalid")
    by_name: dict[str, dict[str, object]] = {}
    for row in mapped_files:
        require(type(row) is dict and set(row) == _MAPPING_FIELDS,
                "mapped-runtime-object-shape-invalid")
        name = row["soname"]
        require(type(name) is str and name in REQUIRED_MAPPED_SONAMES
                and name not in by_name,
                "mapped-runtime-object-name-invalid")
        require(_valid_sha(row["sha256"]) and _valid_size(row["size_bytes"]),
                "mapped-runtime-object-byte-claim-invalid")
        by_name[name] = dict(row)
    require(set(by_name) == REQUIRED_MAPPED_SONAMES,
            "mapped-runtime-object-set-mismatch")
    require(by_name["llama-server"]["sha256"] == native_executable["sha256"]
            and by_name["llama-server"]["size_bytes"] == native_executable["size_bytes"],
            "native-process-executable-map-mismatch")
    # A stable, path-neutral identity of claimed bytes, NOT trusted provenance.
    material = {
        "schema_version": BUNDLE_SCHEMA,
        "wrapper": dict(wrapper),
        "native_executable": dict(native_executable),
        "mapped_sonames": [by_name[key] for key in sorted(by_name)],
    }
    return {
        "schema_version": BUNDLE_SCHEMA,
        "status": STATUS,
        "review_only_bundle_sha256": sha256_json(material),
        "reported_object_count": len(by_name),
        "reported_runtime_dependencies": material,
        "selected_libraries_not_full_elf_closure": True,
        "library_bytes_independently_verified": False,
        "live_pid_maps_attested": False,
        "gpu_runtime_artifact_authenticated": False,
        "managed_12k_qualified": False,
        "canary_activation_authorized": False,
    }


def bind_r11_reported_runtime_bundle(
    *, r11_review: dict[str, Any],
    bundle_review: dict[str, object],
) -> dict[str, object]:
    """Require R11's untrusted 'runtime' SHA to refer to the whole bundle.

    The resulting relationship is purely syntactic. A malicious caller can
    invent both inputs, so this NEVER qualifies physical or managed serving.
    """
    require(type(r11_review) is dict
            and r11_review.get("schema_version") == R11_SCHEMA
            and r11_review.get("status") == "BLOCKED_UNATTESTED_EXECUTION_AND_MANAGED_WORKER"
            and r11_review.get("reported_hashes_independently_verified") is False
            and r11_review.get("managed_worker_12k_tested") is False
            and r11_review.get("host_and_gpu_origin_authenticated") is False
            and r11_review.get("new_runtime_instance_epoch_allocated") is False
            and r11_review.get("control_catalog_updated") is False
            and r11_review.get("canary_activation_authorized") is False
            and r11_review.get("serving_revision_issued") is False,
            "r11-unqualified-source-required")
    record = r11_review.get("record_only")
    fp = r11_review.get("review_fingerprint_sha256_not_a_deployment_identity")
    require(type(record) is dict
            and record.get("profile_id") == "bonsai2-27b-12k"
            and record.get("worker_id") == "bonsai-spica"
            and record.get("parallel_slots") == 1
            and type(record.get("gpu_uuid")) is str
            and _GPU.fullmatch(record["gpu_uuid"]) is not None
            and record.get("limits") == {
                "input_tokens": 10240, "output_tokens": 2048,
                "total_tokens": 12288, "request_bytes": 65536,
            },
            "r11-profile-or-gpu-unqualified")
    require(type(fp) is str and re.fullmatch(r"[0-9a-f]{64}", fp) is not None
            and sha256_json(record) == "sha256:" + fp,
            "r11-review-fingerprint-mismatch")
    require(type(bundle_review) is dict
            and set(bundle_review) == {
                "schema_version", "status", "review_only_bundle_sha256",
                "reported_object_count", "reported_runtime_dependencies",
                "selected_libraries_not_full_elf_closure",
                "library_bytes_independently_verified", "live_pid_maps_attested",
                "gpu_runtime_artifact_authenticated", "managed_12k_qualified",
                "canary_activation_authorized",
            }
            and bundle_review.get("schema_version") == BUNDLE_SCHEMA
            and bundle_review.get("status") == STATUS
            and bundle_review.get("selected_libraries_not_full_elf_closure") is True
            and bundle_review.get("library_bytes_independently_verified") is False
            and bundle_review.get("live_pid_maps_attested") is False
            and bundle_review.get("gpu_runtime_artifact_authenticated") is False
            and bundle_review.get("managed_12k_qualified") is False
            and bundle_review.get("canary_activation_authorized") is False,
            "runtime-bundle-still-unqualified-required")
    # Revalidate even a caller-mutated bundle summary by regenerating from
    # the full report, rather than trusting its externally provided digest.
    dependency_record = bundle_review.get("reported_runtime_dependencies")
    require(type(dependency_record) is dict
            and set(dependency_record) == {
                "schema_version", "wrapper", "native_executable", "mapped_sonames",
            } and dependency_record["schema_version"] == BUNDLE_SCHEMA,
            "runtime-bundle-review-structure-invalid")
    canonical = compute_reported_runtime_bundle(
        wrapper=dependency_record["wrapper"],
        native_executable=dependency_record["native_executable"],
        mapped_files=dependency_record["mapped_sonames"],
    )
    require(bundle_review.get("review_only_bundle_sha256")
            == canonical["review_only_bundle_sha256"]
            and bundle_review.get("reported_object_count") == 14,
            "runtime-bundle-review-digest-mismatch")
    claimed = record.get("reported_artifact_sha256")
    require(type(claimed) is dict
            and set(claimed) == {"runtime", "adapter", "model", "tokenizer", "template"}
            and claimed.get("model") == "sha256:" + LLAMA_CPP_BONSAI_MODEL_SHA256,
            "r11-pinned-model-identity-invalid")
    require(claimed["runtime"] == canonical["review_only_bundle_sha256"],
            "r11-runtime-hash-does-not-cover-selected-bundle")
    return {
        "schema_version": SCHEMA,
        "status": STATUS,
        "review_only_covered_runtime_dependency_sha256": canonical["review_only_bundle_sha256"],
        "r11_fingerprint_bound_to_runtime_bundle_only": fp,
        "selected_dependency_count": 14,
        "wrapper_and_mapped_objects_claimed_not_authenticated": True,
        "full_dynamic_library_dependency_closure_proven": False,
        "source_host_authenticity_proven": False,
        "managed_worker_12k_qualified": False,
        "serving_revision_issued": False,
        "new_runtime_instance_epoch_allocated": False,
        "control_catalog_modified": False,
        "canary_activation_authorized": False,
    }
