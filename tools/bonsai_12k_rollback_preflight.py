"""#232: read-only, fail-closed Bonsai 12K rollback asset preflight.

Only compares protected 8K snapshots with existing live files and pinned
Nix profiles. Does NOT check the current database, live Worker, GPU ownership,
12K model residency, or cross-host atomicity. A PASS never authorizes a stop.
No shell, subprocess, network, file writing, or service control is used.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tomllib
from pathlib import Path
from typing import Any

SNAPSHOT = "/var/lib/astrumweaver/acceptance/bonsai12k-8k-fallback-20261108-r0"
RECEIPT = "snapshot-receipt.json"
SCHEMA = "bonsai-12k-precanary-host-snapshot-v1"
MODEL_SHA256 = "3907dc1658db1f78a9826bf8d5bcb8dc65db0d466388937af57f2294fae62ec1"
SPICA_UUID = "GPU-f78bcab4-245f-b3ff-5570-ed629b239331"
LEGACY_DENEB_UUID = "GPU-8bcf8ba8-4610-d9bb-3862-a03b5ea7d931"
SHA_RE = re.compile(r"[0-9a-f]{64}\Z")
SPICA_FILES = {
    "bonsai-worker.toml": "/etc/astrumweaver/bonsai-worker.toml",
    "bonsai-runtime.json": "/etc/astrumweaver/bonsai-runtime-deployment.json",
    "bonsai-serving.json": "/etc/astrumweaver/bonsai-serving-deployment.json",
    "bonsai-model.sha256": "/etc/astrumweaver/bonsai-model.sha256",
    "gpu-uuids": "/etc/astrumweaver/gpu-uuids",
    "worker.env": "/etc/astrumweaver/worker.env",
    "worker.service": "/etc/systemd/system/astrumweaver-worker.service",
    "20-nvidia-driver-only.conf": "/etc/systemd/system/astrumweaver-worker.service.d/20-nvidia-driver-only.conf",
    "50-bonsai-coding.conf": "/etc/systemd/system/astrumweaver-worker.service.d/50-bonsai-coding.conf",
}
CONTROL_FILES = {
    "control.service": "/etc/systemd/system/astrumweaver-control.service",
    "control.toml": "/etc/astrumweaver/control.toml",
    "control.env": "/etc/astrumweaver/control.env",
    "chat-catalog.json": "/etc/astrumweaver/tsumgi-qwen38-bonsai-chat-catalog.json",
}
SPICA_PROFILES = {
    "bonsai_runtime": (
        "/nix/var/nix/gcroots/tsumgi-bonsai-spica-8k-pre12k-20261108",
        "/nix/var/nix/profiles/astrumweaver-runtime-bonsai",
    ),
    "worker_installer": (
        "/nix/var/nix/gcroots/tsumgi-bonsai-spica-worker-pre12k-20261108",
        "/nix/var/nix/profiles/tsumgi-v012-ga-installer",
    ),
}
CONTROL_ROOT = "/nix/var/nix/gcroots/tsumgi-bonsai-control-pre12k-20261108"
_CONTROL_MODELS = {"qwen3.8-27b": 12288, "bonsai2-27b": 8192}


class PreflightBlock(ValueError):
    """Redacted, machine-readable failure category."""


def require(condition: bool, code: str) -> None:
    if not condition:
        raise PreflightBlock(code)


def mapped(root: Path, absolute: str) -> Path:
    require(absolute.startswith("/") and ".." not in Path(absolute).parts, "invalid_source_path")
    return root / absolute.removeprefix("/")


def regular_file(path: Path, mode: int, *, max_bytes: int = 1_048_576) -> bytes:
    try:
        info = path.lstat()
        require(stat.S_ISREG(info.st_mode), "nonregular_or_symlink")
        require(stat.S_IMODE(info.st_mode) == mode, "file_mode_mismatch")
        require(0 < info.st_size <= max_bytes, "file_size_invalid")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            handle = os.fstat(fd)
            require(stat.S_ISREG(handle.st_mode), "fd_nonregular")
            require(stat.S_IMODE(handle.st_mode) == mode, "fd_mode_mismatch")
            require(handle.st_ino == info.st_ino and handle.st_dev == info.st_dev,
                    "file_replaced_during_read")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                data = stream.read(max_bytes + 1)
            require(len(data) == handle.st_size and len(data) <= max_bytes,
                    "file_length_changed")
            return data
        finally:
            os.close(fd)
    except (OSError, UnicodeError) as exc:
        raise PreflightBlock("file_unreadable") from exc


def json_dict(raw: bytes) -> dict[str, Any]:
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise PreflightBlock("invalid_json") from exc
    require(type(result) is dict, "not_object")
    return result


def verify_file_pair(
    root: Path,
    snapshot: Path,
    name: str,
    source: str,
    record: Any,
    *,
    source_key: str,
) -> None:
    require(type(record) is dict and record.get(source_key) == source,
            "receipt_source_mismatch")
    require(type(record.get("size")) is int and 0 < record["size"] < 1_048_576,
            "receipt_size_invalid")
    require(type(record.get("mode")) is int and 0 <= record["mode"] <= 0o777,
            "receipt_mode_invalid")
    require(type(record.get("sha256")) is str and SHA_RE.fullmatch(record["sha256"]) is not None,
            "receipt_digest_invalid")
    old = regular_file(snapshot / name, 0o400)
    live_path = mapped(root, source)
    live = regular_file(live_path, record["mode"])
    require(
        type(record.get("uid")) is int and type(record.get("gid")) is int
        and record["uid"] >= 0 and record["gid"] >= 0,
        "receipt_owner_invalid",
    )
    info = live_path.lstat()
    require(info.st_uid == record["uid"] and info.st_gid == record["gid"],
            "source_owner_drift")
    if root == Path("/"):
        require((snapshot / name).lstat().st_uid == 0,
                "backup_not_root_owned")
    for raw in (old, live):
        require(len(raw) == record["size"], "snapshot_size_mismatch")
        require(hashlib.sha256(raw).hexdigest() == record["sha256"],
                "source_or_snapshot_drift")
    require(old == live, "bytes_differ")


def verify_root(
    root: Path, label: str, root_path: str, profile: str, store: str
) -> None:
    require(
        type(store) is str
        and re.fullmatch(r"/nix/store/[0-9a-z]{32}-[A-Za-z0-9.+_-]+", store)
        is not None,
        "invalid_store_reference",
    )
    root_link = mapped(root, root_path)
    profile_link = mapped(root, profile)
    store_path = mapped(root, store)
    require(root_link.is_symlink() and profile_link.is_symlink(),
            "missing_nix_symlink")
    require(store_path.is_dir(), "missing_nix_store_closure")
    try:
        root_resolved = root_link.resolve(strict=True)
        profile_resolved = profile_link.resolve(strict=True)
        store_resolved = store_path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise PreflightBlock("nix_link_target_unavailable") from exc
    require(root_resolved == store_resolved, "gcroot_target_mismatch")
    require(profile_resolved == store_resolved, "active_nix_profile_drift")


def check_host(site: str, *, root: Path = Path("/")) -> dict[str, Any]:
    require(site in ("spica", "control"), "unknown_site")
    require(root.is_absolute() and root.is_dir() and not root.is_symlink(),
            "invalid_root")
    # --root is only a synthetic test fixture selector. It must not escape
    # the fixture via lexical traversal or symlinked ancestor directories.
    require(".." not in root.parts and root.resolve(strict=True) == root,
            "noncanonical_root")
    snap = mapped(root, SNAPSHOT)
    try:
        snap_stat = snap.lstat()
    except OSError as exc:
        raise PreflightBlock("snapshot_missing") from exc
    require(stat.S_ISDIR(snap_stat.st_mode) and stat.S_IMODE(snap_stat.st_mode) == 0o700,
            "snapshot_directory_not_private")
    if root == Path("/"):
        require(snap_stat.st_uid == 0, "snapshot_not_root_owned")
    receipt = json_dict(regular_file(snap / RECEIPT, 0o600))
    require(receipt.get("schema") == SCHEMA, "receipt_schema_mismatch")
    require(receipt.get("host") == ("spica" if site == "spica" else "tsumgi-control"),
            "receipt_host_mismatch")
    mode = (
        "CURRENT_8K_HOST_LOCAL_BACKUP_ONLY" if site == "spica"
        else "CURRENT_QWEN12K_BONSAI8K_HOST_LOCAL_BACKUP_ONLY"
    )
    require(receipt.get("mode") == mode, "receipt_purpose_mismatch")
    for field in ("read_only_restore_rehearsed",):
        require(receipt.get(field) is True, "restore_rehearsal_not_recorded")
    for field in ("services_changed", "cross_host_atomic", "live_rollback_verified"):
        require(receipt.get(field) is False, "receipt_invalid_lifecycle_claim")
    if site == "spica":
        require(receipt.get("external_offhost_backup") is False
                and receipt.get("model_weights_copied") is False,
                "unexpected_backup_claim")
        require(receipt.get("model_bytes_sha256_verified_prior") is True,
                "model_hash_receipt_missing")
        require(receipt.get("model_artifact_sha256") == "sha256:" + MODEL_SHA256,
                "model_pin_mismatch")
        require(receipt.get("spica_setup_uuid_matches_worker") is False,
                "unexpected_legacy_uuid_claim")
        originals = receipt.get("items")
        require(type(originals) is dict and set(originals) == set(SPICA_FILES),
                "file_set_mismatch")
        for name, source in SPICA_FILES.items():
            verify_file_pair(root, snap, name, source, originals[name], source_key="source")
        pins = receipt.get("nix_roots")
        require(type(pins) is dict and set(pins) == set(SPICA_PROFILES),
                "root_set_mismatch")
        for name, (link, profile) in SPICA_PROFILES.items():
            entry = pins[name]
            require(
                type(entry) is dict and entry.get("root") == link
                and entry.get("profile") == profile,
                "root_locator_mismatch",
            )
            verify_root(root, name, link, profile, entry.get("store"))
        worker = tomllib.loads((snap / "bonsai-worker.toml").read_text())
        runtime = json_dict((snap / "bonsai-runtime.json").read_bytes())
        require(
            worker["worker"]["id"] == "bonsai-spica"
            and worker["worker"]["gpu_uuids"] == [SPICA_UUID],
            "spica_worker_binding_changed",
        )
        require(
            runtime["provider_config"]["context_size"] == 8192
            and runtime["setup_intent"]["configuration"]["context_size"] == 8192
            and runtime["setup_intent"]["configuration"]["gpu_uuids"]
            == [LEGACY_DENEB_UUID],
            "spica_old_8k_runtime_changed",
        )
        require(
            (snap / "bonsai-model.sha256").read_text().startswith(MODEL_SHA256 + "  "),
            "model_checksum_file_changed",
        )
        files, roots = len(SPICA_FILES), len(SPICA_PROFILES)
    else:
        require(receipt.get("offhost_backup") is False, "unexpected_offhost_claim")
        originals = receipt.get("source")
        require(type(originals) is dict and set(originals) == set(CONTROL_FILES),
                "file_set_mismatch")
        for name, source in CONTROL_FILES.items():
            verify_file_pair(root, snap, name, source, originals[name], source_key="path")
        store = receipt.get("nix_control_profile")
        verify_root(
            root, "control", CONTROL_ROOT,
            "/nix/var/nix/profiles/tsumgi-v012-ga-control", store,
        )
        catalog = json_dict((snap / "chat-catalog.json").read_bytes())
        require(receipt.get("source_catalog_limits") == _CONTROL_MODELS,
                "receipt_model_limits_changed")
        try:
            limits = {
                row["profile"]["profile_id"]: row["contract"]["limits"]["total_tokens"]
                for row in catalog["profiles"]
            }
        except (KeyError, TypeError) as exc:
            raise PreflightBlock("catalog_shape_invalid") from exc
        require(limits == _CONTROL_MODELS and len(catalog["profiles"]) == 2,
                "baseline_catalog_changed")
        files, roots = len(CONTROL_FILES), 1

    return {
        "status": "HOST_LOCAL_BACKUP_BYTES_MATCH_NO_LIVE_ADMISSION",
        "site": site,
        "files_checked": files,
        "nix_roots_checked": roots,
        "synthetic_root": root != Path("/"),
        "live_rollback_verified": False,
        "cross_host_atomic": False,
        "offhost_backup": False,
        "model_full_hash_verified_this_run": False,
        "canary_activation_authorized": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", choices=("spica", "control"), required=True)
    parser.add_argument(
        "--root", type=Path, default=Path("/"),
        help="Only for synthetic tests; / is required for real host checks.",
    )
    args = parser.parse_args()
    try:
        result = check_host(args.site, root=args.root)
    except (PreflightBlock, OSError, ValueError, KeyError, TypeError) as exc:
        code = str(exc) if isinstance(exc, PreflightBlock) else "unverified-input"
        print(json.dumps({"status": "BLOCKED", "reason": code}))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
