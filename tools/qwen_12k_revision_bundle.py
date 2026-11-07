"""#230 R12: offline 12K Serving/Control revision preview from reviewed-shaped receipts.

This DOES NOT attest receipts, run PID1, access GPUs, or grant deployment.
It produces domain-valid proposed revisions only in memory; no output writer
or operator/Worker activation path exists here. An independent reviewer must
verify original receipts and exact runtime identity before any publication.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from qwen_12k_candidate import (
    PROFILE_ID,
    TARGET_CONTEXT,
    TARGET_LIMITS,
    build_unqualified_candidate,
)

from astrumweaver.gateway.chat import ChatProfileCatalog
from astrumweaver.serving import (
    DeploymentIdentity,
    LogicalServingProfile,
    ServingContract,
    ServingDeploymentDeclaration,
)

_EXECUTION_SCHEMA = "qwen12k-reviewed-execution-shape-v1"
_NATIVE_SCHEMA = "qwen12k-native-canary-shape-v1"
_CANDIDATE_SCHEMA = "qwen12k-review-required-revisions-v1"
_GPU_PATTERN = re.compile(r"GPU-[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,40}\Z")
_SHA_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SOURCE_PREFIX = "https://github.com/upiscium/TSUMGI/issues/230#issuecomment-"
_OPTIONS = (
    "context_size", "gpu_layers", "split_mode", "tensor_split",
    "offline", "no_webui", "fit", "jinja",
)


def _require(ok: bool, reason: str) -> None:
    if not ok:
        raise ValueError(reason)


def _digest(value: object) -> str:
    content = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    _require(len(content) <= 16_384, "review-receipt-too-large")
    return "sha256:" + hashlib.sha256(content).hexdigest()


def build_review_required_revisions(
    runtime_source: Mapping[str, Any],
    serving_source: Mapping[str, Any],
    catalog_source: Mapping[str, Any],
    reviewed_baseline_pin: Mapping[str, Any],
    *,
    execution_review: Mapping[str, Any],
    native_canary_record: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate the revision chain, not the authenticity of external evidence.

    Inputs must be independently inspected, because a caller can manufacture
    both JSON receipts and their hashes. Never install returned artifacts
    merely because TSUMGI's schema accepts them.
    """
    draft = build_unqualified_candidate(
        runtime_source, serving_source, catalog_source, reviewed_baseline_pin,
    )
    runtime = deepcopy(draft["runtime_draft"])
    raw_serving = deepcopy(draft["serving_UNLOADABLE_draft"])
    raw_catalog = deepcopy(draft["control_UNLOADABLE_draft"])
    original = ServingDeploymentDeclaration.from_dict(serving_source)

    _require(isinstance(execution_review, Mapping), "execution-review-invalid")
    required_execution = {
        "schema_version", "profile_id", "baseline_deployment_revision",
        "model_artifact_sha256", "context_size", "parallel_slots",
        "gpu_uuids", "runtime_options",
    }
    _require(set(execution_review) == required_execution,
             "execution-review-fields-mismatch")
    _require(
        execution_review["schema_version"] == _EXECUTION_SCHEMA
        and execution_review["profile_id"] == PROFILE_ID
        and execution_review["baseline_deployment_revision"]
        == original.deployment.revision
        and execution_review["model_artifact_sha256"]
        == original.deployment.model_artifact_sha256
        and type(execution_review["context_size"]) is int
        and execution_review["context_size"] == TARGET_CONTEXT
        and type(execution_review["parallel_slots"]) is int
        and execution_review["parallel_slots"] == 1,
        "execution-review-identity-or-size-mismatch",
    )
    gpus = execution_review["gpu_uuids"]
    _require(
        type(gpus) is list and len(gpus) == 2
        and all(type(gpu) is str and _GPU_PATTERN.fullmatch(gpu) for gpu in gpus)
        and len(set(gpus)) == 2,
        "execution-review-gpu-set-invalid",
    )
    options = {
        key: deepcopy(runtime["provider_config"][key])
        for key in _OPTIONS if key in runtime["provider_config"]
    }
    _require(
        type(execution_review["runtime_options"]) is dict
        and execution_review["runtime_options"] == options,
        "execution-review-provider-options-mismatch",
    )
    # Canonical record contains no local model paths, secrets or credentials.
    execution_sha = _digest(dict(execution_review))
    _require(execution_sha != original.deployment.execution_config_sha256,
             "execution-review-reuses-8k-digest")

    _require(isinstance(native_canary_record, Mapping), "canary-record-invalid")
    required_canary = {
        "schema_version", "source_run", "execution_config_sha256",
        "model_artifact_sha256", "native_context_size", "max_input_tokens",
        "sample_count", "gpu_min_free_mib", "chat_ok", "tool_ok",
        "stream_ok", "rollback8k_ok",
    }
    _require(set(native_canary_record) == required_canary,
             "canary-record-fields-mismatch")
    _require(
        native_canary_record["schema_version"] == _NATIVE_SCHEMA
        and type(native_canary_record["source_run"]) is str
        and native_canary_record["source_run"].startswith(_SOURCE_PREFIX)
        and native_canary_record["source_run"][len(_SOURCE_PREFIX):].isdigit()
        and native_canary_record["execution_config_sha256"] == execution_sha
        and native_canary_record["model_artifact_sha256"]
        == original.deployment.model_artifact_sha256
        and type(native_canary_record["native_context_size"]) is int
        and native_canary_record["native_context_size"] == TARGET_CONTEXT
        and type(native_canary_record["max_input_tokens"]) is int
        and 10_052 <= native_canary_record["max_input_tokens"] <= TARGET_LIMITS["input_tokens"]
        and type(native_canary_record["sample_count"]) is int
        and native_canary_record["sample_count"] >= 20
        and all(native_canary_record[k] is True for k in (
            "chat_ok", "tool_ok", "stream_ok", "rollback8k_ok",
        )),
        "canary-record-not-matched-to-12k-native-evidence",
    )
    free = native_canary_record["gpu_min_free_mib"]
    _require(
        type(free) is dict and set(free) == set(gpus)
        and all(type(x) is int and 2048 <= x <= 131_072 for x in free.values()),
        "canary-record-vram-floor-or-gpus-invalid",
    )
    evidence_sha = _digest(dict(native_canary_record))
    _require(evidence_sha != original.contracts[0].validation_evidence_sha256,
             "canary-evidence-reuses-8k-digest")

    identity_dict = deepcopy(raw_serving["deployment"])
    identity_dict["execution_config_sha256"] = execution_sha
    identity = DeploymentIdentity(**identity_dict)
    contract_dict = deepcopy(raw_serving["contracts"][0]["contract"])
    contract_dict["deployment_revision"] = identity.revision
    contract_dict["validation_evidence_sha256"] = evidence_sha
    contract = ServingContract(**contract_dict)
    serving = ServingDeploymentDeclaration(identity, (contract,))

    selected = [
        p for p in raw_catalog["profiles"]
        if p["profile"]["profile_id"] == PROFILE_ID
    ]
    _require(len(selected) == 1, "selected-profile-missing")
    row = selected[0]
    p_dict = deepcopy(row["profile"])
    p_dict["deployment_revision"] = identity.revision
    p_dict["serving_contract_revision"] = contract.revision
    profile = LogicalServingProfile(**p_dict)
    row["deployment"] = identity.to_dict()
    row["contract"] = contract.to_dict()
    row["profile"] = profile.to_dict()
    parsed_catalog = ChatProfileCatalog.from_dict(raw_catalog)
    selected_parsed = parsed_catalog.get(PROFILE_ID)
    _require(
        selected_parsed.resolved.deployment == identity
        and selected_parsed.resolved.contract == contract
        and dict(selected_parsed.resolved.effective_limits) == TARGET_LIMITS,
        "review-proposal-serving-catalog-not-aligned",
    )
    before_other = {
        p["profile"]["profile_id"]: p
        for p in catalog_source["profiles"]
        if p["profile"]["profile_id"] != PROFILE_ID
    }
    after_other = {
        p["profile"]["profile_id"]: p
        for p in raw_catalog["profiles"]
        if p["profile"]["profile_id"] != PROFILE_ID
    }
    _require(before_other == after_other, "other-profiles-changed")

    return {
        "schema_version": _CANDIDATE_SCHEMA,
        "admission": "REVIEW_REQUIRED_NOT_PRODUCTION_QUALIFIED",
        "reviewed_receipts_authenticated": False,
        "kernel_lifecycle_or_gateway_accepted": False,
        "baseline_deployment_revision": original.deployment.revision,
        "proposed_deployment_revision": identity.revision,
        "proposed_serving_contract_revision": contract.revision,
        "proposed_profile_revision": profile.revision,
        "execution_record_sha256": execution_sha,
        "native_canary_record_sha256": evidence_sha,
        "runtime_proposal": runtime,
        "serving_proposal": serving.to_dict(),
        "catalog_proposal": raw_catalog,
    }


# CLI is deliberately separate from the live Worker/Control deployment path.
def _read_limited_object(path: str) -> dict[str, Any]:
    from pathlib import Path
    source = Path(path)
    # Read-only untrusted-file size bound; never print source content/path.
    if source.stat().st_size > 1_048_576:
        raise ValueError("review-file-too-large")
    value = json.loads(source.read_text(encoding="utf-8"))
    _require(type(value) is dict, "review-file-must-be-object")
    return value


def _write_private_review_bundle(directory: str, proposal: Mapping[str, Any]) -> None:
    """Write only to a new, exact /tmp child; never a production path."""
    import os
    from contextlib import suppress
    from pathlib import Path

    dest = Path(directory)
    _require(
        dest.is_absolute() and dest.parent == Path("/tmp")
        and dest.name.startswith("tsumgi-qwen12k-review-")
        and not Path("/tmp").is_symlink()
        and not dest.exists() and not dest.is_symlink(),
        "review-output-must-be-fresh-private-tmp-directory",
    )
    items = (
        ("runtime.REVIEW_ONLY.json", proposal["runtime_proposal"]),
        ("serving.REVIEW_ONLY.json", proposal["serving_proposal"]),
        ("control.REVIEW_ONLY.json", proposal["catalog_proposal"]),
        ("review-gates.json", {
            k: v for k, v in proposal.items()
            if k not in ("runtime_proposal", "serving_proposal", "catalog_proposal")
        }),
    )
    os.mkdir(dest, 0o700)
    folder_fd = None
    try:
        folder_fd = os.open(dest, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        os.fchmod(folder_fd, 0o700)
        for name, data in items:
            fd = os.open(
                name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600, dir_fd=folder_fd,
            )
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(data, stream, sort_keys=True, ensure_ascii=True, indent=2)
                stream.write("\n")
    except BaseException:
        if folder_fd is not None:
            for name, _ in items:
                with suppress(FileNotFoundError):
                    os.unlink(name, dir_fd=folder_fd)
        with suppress(OSError):
            os.rmdir(dest)
        raise
    finally:
        if folder_fd is not None:
            os.close(folder_fd)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "runtime", "serving", "control", "reviewed-pin",
        "execution-review", "native-canary-record",
    ):
        parser.add_argument("--" + name, required=True)
    parser.add_argument(
        "--output-dir", help="new /tmp/tsumgi-qwen12k-review-* directory",
    )
    args = parser.parse_args()
    try:
        proposal = build_review_required_revisions(
            _read_limited_object(args.runtime),
            _read_limited_object(args.serving),
            _read_limited_object(args.control),
            _read_limited_object(args.reviewed_pin),
            execution_review=_read_limited_object(args.execution_review),
            native_canary_record=_read_limited_object(args.native_canary_record),
        )
        if args.output_dir:
            _write_private_review_bundle(args.output_dir, proposal)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        print(json.dumps({
            "status": "BLOCKED", "error_class": type(exc).__name__,
        }))
        return 2
    print(json.dumps({
        "status": proposal["admission"],
        "reviewed_receipts_authenticated": False,
        "kernel_lifecycle_or_gateway_accepted": False,
        "deployment_revision": proposal["proposed_deployment_revision"],
        "serving_contract_revision": proposal[
            "proposed_serving_contract_revision"
        ],
        "profile_revision": proposal["proposed_profile_revision"],
        "private_review_files_written": bool(args.output_dir),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
