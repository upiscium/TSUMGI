"""Offline exact-byte 8K rollback snapshot and disposable restoration rehearsal.

This tool NEVER restores production files, alters services, or pins Nix roots.
Snapshots contain private paths/secrets: keep owner-only and delete manually.
The snapshot receipt is an integrity check, NOT a cryptographic signature.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
from typing import Mapping

from qwen_12k_candidate import _check_base, BASE_CONTEXT, PROFILE_ID

SCHEMA = "qwen8k-rollback-snapshot-v2"
PREFIX = "tsumgi-qwen8k-rollback-"
MAX_BYTES = 512 * 1024
REQUIRED = (
    "runtime.json", "serving.json", "catalog.json", "reviewed-pin.json",
    "worker.toml", "worker.service",
    "30-coding-runtime.conf", "40-qwen38-identity.conf",
    "control.toml",
)
GATES = (
    "pin_real_8k_nix_runtime_store_closure_against_garbage_collection",
    "retain_exact_8k_model_file_and_sha256_attestation",
    "inventory_all_live_systemd_dropins_and_protect_environment_separately",
    "verify_exact_current_service_unit_worker_control_config_permissions",
    "ensure_no_old_revision_jobs_remain_nonterminal_before_cutover",
    "authorize_service_maintenance_and_ordered_rollback_separately",
    "rehearse_actual_service_restore_in_authorized_window",
)


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _read_regular(path: Path) -> bytes:
    # O_NOFOLLOW on the final component rejects a symlink to a private file.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            raise ValueError("source-not-bounded-regular-file")
        data = b""
        while len(data) <= MAX_BYTES:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            data += chunk
        if len(data) > MAX_BYTES:
            raise ValueError("source-file-too-large")
        return data
    finally:
        os.close(fd)


def _load_json(raw: bytes) -> dict:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("expected-json-object")
    return value


def validate(data: Mapping[str, bytes]) -> str:
    if set(data) != set(REQUIRED):
        raise ValueError("snapshot-file-set-mismatch")
    # Real 8K serving/profile schema and independently reviewed model pin.
    runtime = _load_json(data["runtime.json"])
    serving = _load_json(data["serving.json"])
    catalog = _load_json(data["catalog.json"])
    reviewed = _load_json(data["reviewed-pin.json"])
    _, revision = _check_base(runtime, serving, catalog, reviewed)
    if runtime["provider_config"]["context_size"] != BASE_CONTEXT:
        raise ValueError("not-current-8k")
    # Other configuration inputs are backed up as opaque exact bytes.
    # They are NOT parsed as authority for a production restoration.
    if not all(data[k] for k in (
        "worker.toml", "worker.service",
        "30-coding-runtime.conf", "40-qwen38-identity.conf", "control.toml",
    )):
        raise ValueError("empty-service-config")
    return revision


def receipt_for(data: Mapping[str, bytes], revision: str) -> dict:
    return {
        "schema_version": SCHEMA,
        "mode": "BACKUP_ONLY_NOT_RESTORED",
        "profile_id": PROFILE_ID,
        "original_context": BASE_CONTEXT,
        "source_files": {
            k: {"size": len(data[k]), "sha256": digest(data[k])}
            for k in sorted(REQUIRED)
        },
        "old_deployment_revision": revision,
        "remaining_gates": list(GATES),
        "signed_or_authenticated": False,
        "nix_runtime_closure_backed_up": False,
    }


def _private_dirfd(path: Path) -> int:
    if (
        not path.is_absolute() or path.parent != Path("/tmp")
        or not path.name.startswith(PREFIX)
        or path.is_symlink() or not path.is_dir()
    ):
        raise ValueError("not-private-direct-tmp-child")
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077):
            raise ValueError("snapshot-directory-not-owner-only")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _write_fd(dirfd: int, name: str, content: bytes) -> None:
    if "/" in name or name not in REQUIRED + ("receipt.json",):
        raise ValueError("invalid-snapshot-member")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(name, flags, 0o600, dir_fd=dirfd)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=False) as output:
            output.write(content)
            output.flush()
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_fd(dirfd: int, name: str) -> bytes:
    if "/" in name or name not in REQUIRED + ("receipt.json",):
        raise ValueError("invalid-snapshot-member")
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dirfd)
    try:
        st = os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid()
                or st.st_mode & 0o077):
            raise ValueError("snapshot-file-not-owner-only")
        if st.st_size > MAX_BYTES:
            raise ValueError("snapshot-file-too-large")
        raw = b""
        while len(raw) <= MAX_BYTES:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            raw += chunk
        if len(raw) > MAX_BYTES:
            raise ValueError("snapshot-file-too-large")
        return raw
    finally:
        os.close(fd)


def capture(sources: Mapping[str, Path]) -> Path:
    if set(sources) != set(REQUIRED):
        raise ValueError("source-arguments-incomplete")
    data = {key: _read_regular(sources[key]) for key in REQUIRED}
    rev = validate(data)
    receipt = receipt_for(data, rev)
    directory = Path(tempfile.mkdtemp(prefix=PREFIX, dir="/tmp"))
    fd = None
    try:
        fd = _private_dirfd(directory)
        for key in REQUIRED:
            _write_fd(fd, key, data[key])
        _write_fd(fd, "receipt.json", (
            json.dumps(receipt, sort_keys=True, indent=2) + "\n"
        ).encode())
        os.fsync(fd)
        # Input files can change during capture; refuse to claim an exact
        # snapshot if any changed between the initial and final reads.
        if any(_read_regular(sources[k]) != data[k] for k in REQUIRED):
            raise ValueError("source-changed-during-snapshot")
        if _verify_open_fd(fd)["files_verified"] != len(REQUIRED):
            raise AssertionError("snapshot-verification-incomplete")
        return directory
    except BaseException:
        # Only our own fresh, owner-only temporary directory is removed.
        shutil.rmtree(directory)
        raise
    finally:
        if fd is not None:
            os.close(fd)


def _verify_open_fd(fd: int) -> dict:
    report = _load_json(_read_fd(fd, "receipt.json"))
    if set(report) != {
        "schema_version", "mode", "profile_id", "original_context",
        "source_files", "old_deployment_revision", "remaining_gates",
        "signed_or_authenticated", "nix_runtime_closure_backed_up",
    } or (
        report["schema_version"] != SCHEMA
        or report["mode"] != "BACKUP_ONLY_NOT_RESTORED"
        or report["profile_id"] != PROFILE_ID
        or report["original_context"] != BASE_CONTEXT
        or report["signed_or_authenticated"] is not False
        or report["nix_runtime_closure_backed_up"] is not False
        or report["remaining_gates"] != list(GATES)
    ):
        raise ValueError("invalid-receipt")
    records = report["source_files"]
    if set(records) != set(REQUIRED):
        raise ValueError("receipt-source-set-invalid")
    names = set(os.listdir(fd))
    if names != set(REQUIRED) | {"receipt.json"}:
        raise ValueError("unexpected-snapshot-files")
    data = {key: _read_fd(fd, key) for key in REQUIRED}
    for key in REQUIRED:
        expected = records[key]
        if (set(expected) != {"size", "sha256"} or
            expected["size"] != len(data[key]) or
            expected["sha256"] != digest(data[key])):
            raise ValueError("snapshot-file-hash-mismatch")
    original_revision = validate(data)
    if original_revision != report["old_deployment_revision"]:
        raise ValueError("snapshot-deployment-revision-mismatch")
    return {"files_verified": len(REQUIRED), "deployment_revision": original_revision,
            "raw_data": data}


def verify(directory: Path, *, rehearse: bool = False) -> dict:
    fd = _private_dirfd(directory)
    try:
        result = _verify_open_fd(fd)
        # This "restore" intentionally has NO option to target /etc,
        # /nix, a Control host or a running Worker. Temporary scratch only.
        if rehearse:
            with tempfile.TemporaryDirectory(prefix="tsumgi-qwen8k-rehearsal-") as sandbox:
                dest = Path(sandbox)
                for key, raw in result["raw_data"].items():
                    copy_fd = os.open(
                        dest / key,
                        os.O_WRONLY | os.O_EXCL | os.O_CREAT | os.O_NOFOLLOW,
                        0o600,
                    )
                    with os.fdopen(copy_fd, "wb") as file:
                        file.write(raw)
                    if _read_regular(dest / key) != raw:
                        raise ValueError("disposable-restore-byte-mismatch")
                restored = {key: _read_regular(dest / key) for key in REQUIRED}
                if validate(restored) != result["deployment_revision"]:
                    raise ValueError("disposable-restore-profile-mismatch")
        return {
            "status": "EXACT_8K_BYTES_VERIFIED_IN_DISPOSABLE_SANDBOX"
                if rehearse else "EXACT_8K_BYTES_VERIFIED",
            "files_verified": result["files_verified"],
            "nix_closure_preserved": False,
            "live_restore_exercised": False,
            "remaining_gates": len(GATES),
        }
    finally:
        os.close(fd)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "capture", "verify", "rehearse"))
    arguments = {
        "runtime.json": "--runtime",
        "serving.json": "--serving",
        "catalog.json": "--catalog",
        "reviewed-pin.json": "--reviewed-pin",
        "worker.toml": "--worker-toml",
        "worker.service": "--worker-service",
        "30-coding-runtime.conf": "--worker-dropin-runtime",
        "40-qwen38-identity.conf": "--worker-dropin-identity",
        "control.toml": "--control-toml",
    }
    for name, flag in arguments.items():
        parser.add_argument(flag, type=Path)
    parser.add_argument("--snapshot-dir", type=Path)
    args = parser.parse_args()
    try:
        names = {
            key: getattr(args, arguments[key].removeprefix("--").replace("-", "_"))
            for key in REQUIRED
        }
        if args.action in ("check", "capture"):
            if args.snapshot_dir is not None or any(value is None for value in names.values()):
                raise ValueError("source-arguments-incomplete")
            data = {name: _read_regular(path) for name, path in names.items()}
            validate(data)
            if args.action == "check":
                result = {"status": "READ_ONLY_8K_BASELINE_VALIDATED",
                          "files_checked": len(REQUIRED), "nix_closure_preserved": False}
            else:
                output = capture(names)
                result = {"status": "PRIVATE_8K_SNAPSHOT_CREATED",
                          "snapshot_dir": str(output), "files_copied": len(REQUIRED),
                          "nix_closure_preserved": False}
        else:
            if args.snapshot_dir is None or any(value is not None for value in names.values()):
                raise ValueError("expected-only-snapshot-directory")
            result = verify(args.snapshot_dir, rehearse=args.action == "rehearse")
        print(json.dumps(result, sort_keys=True))
        return 0
    except (ValueError, TypeError, OSError, KeyError, json.JSONDecodeError):
        print(json.dumps({"status": "BLOCKED", "details": "non-sensitive validation failure"}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
