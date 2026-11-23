"""Offline, byte-preserving VOICEVOX bundle verification; never execute code.

This stdlib-only file also runs directly inside the Nix builder. The three
release pins are regression-checked against voicevox_manifest: importing that
module here would add Worker application dependencies to the build utility.
Receipts describe content, not rights grants, signatures or executable ABI
qualification. Only the packaging recipe ties extraction to verify_archive().
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import time

VERSION = "0.26.2"
ARCHIVE_SHA256 = "sha256:483633de02700cd918731538543a22ce895877aeb09450a990ff38af6e4c26c0"
EXECUTABLE_SHA256 = "sha256:acf74848282c0ff6f0aaf8514427c5f44b0992c26b48448d76b35a97dbe81a25"
MODEL_SHA256 = "sha256:ecd35374d4182cd883cba5040376f7f888cc6ba248b1c2f4cea07cdb34bb1318"
_SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ARCHIVE_BYTES = 8 * 1024**3
_FILE_BYTES = 4 * 1024**3
_TOTAL_BYTES = 16 * 1024**3
_MAX_ENTRIES = 100_000
_MAX_DEPTH = 32
_MAX_RECEIPT_BYTES = 16 * 1024**2
_SCAN_SECONDS = 120.0
_FILE_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK


class VoicevoxBundleError(ValueError):
    def __init__(self) -> None:
        super().__init__("VOICEVOX bundle verification failed")


def _fail() -> None:
    raise VoicevoxBundleError()


def _identity(info: os.stat_result) -> tuple:
    return (
        info.st_dev, info.st_ino, info.st_uid, info.st_gid, info.st_mode,
        info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
    )


def _protected(info: os.stat_result, *, link: bool = False) -> None:
    if (info.st_uid not in (0, os.geteuid())
            or info.st_mode & (stat.S_ISUID | stat.S_ISGID)
            or (not link and info.st_mode & 0o022)):
        _fail()


def _absolute(path: Path) -> None:
    value = str(path)
    if (not path.is_absolute() or value in ("", "/") or value.startswith("//")
            or value != os.path.normpath(value) or len(value.encode("utf-8")) > 4096
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
            or path.resolve(strict=True) != path):
        _fail()
    _protected(path.lstat())
    for parent in path.parents:
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode):
            _fail()
        sticky_tmp = (str(parent) in ("/tmp", "/var/tmp") and info.st_uid == 0
                      and info.st_mode & stat.S_ISVTX)
        if not sticky_tmp:
            _protected(info)


def _before_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        _fail()


def _immutable_store_file(path: Path, info: os.stat_result) -> bool:
    # Nix store optimisation deduplicates read-only root-owned files with
    # hardlinks. Mutable staging/archive inputs do not get this exception.
    return (path.is_relative_to("/nix/store") and info.st_uid == 0
            and not info.st_mode & 0o222)


def _file_bytes(path: Path, maximum: int, deadline: float, *, capture: bool = False):
    """Bounded no-follow read; reject growth, truncation, relinking or replacement."""
    _before_deadline(deadline)
    before = path.lstat()
    _protected(before)
    if (not stat.S_ISREG(before.st_mode)
            or (before.st_nlink != 1 and not _immutable_store_file(path, before))
            or not 0 <= before.st_size <= maximum):
        _fail()
    digest = hashlib.sha256()
    content = bytearray() if capture else None
    fd = os.open(path, _FILE_FLAGS)
    try:
        if _identity(os.fstat(fd)) != _identity(before):
            _fail()
        remaining = before.st_size
        while remaining:
            _before_deadline(deadline)
            block = os.read(fd, min(1024 * 1024, remaining))
            if not block:
                _fail()
            remaining -= len(block)
            digest.update(block)
            if content is not None:
                content.extend(block)
        if (os.read(fd, 1) or _identity(os.fstat(fd)) != _identity(before)
                or _identity(path.lstat()) != _identity(before)):
            _fail()
    finally:
        os.close(fd)
    return "sha256:" + digest.hexdigest(), before, content


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def verify_archive(archive: Path) -> None:
    """Verify the local release archive before the external extractor is called."""
    try:
        archive = Path(archive)
        _absolute(archive)
        digest, _, _ = _file_bytes(
            archive, _ARCHIVE_BYTES, time.monotonic() + _SCAN_SECONDS,
        )
        if digest != ARCHIVE_SHA256:
            _fail()
    except (OSError, ValueError, RuntimeError):
        raise VoicevoxBundleError() from None


def _inventory(root: Path) -> tuple[list[dict], int]:
    _absolute(root)
    root_info = root.lstat()
    if not stat.S_ISDIR(root_info.st_mode):
        _fail()
    deadline = time.monotonic() + _SCAN_SECONDS
    pending = [(root, 0)]
    identities = [(root, _identity(root_info))]
    records: list[dict] = []
    total = 0
    metadata_bytes = 0
    pins: dict[str, str] = {}
    while pending:
        directory, depth = pending.pop()
        _before_deadline(deadline)
        if depth > _MAX_DEPTH:
            _fail()
        before = directory.lstat()
        _protected(before)
        if not stat.S_ISDIR(before.st_mode) or before.st_dev != root_info.st_dev:
            _fail()
        with os.scandir(directory) as entries:
            for entry in entries:
                _before_deadline(deadline)
                if len(records) >= _MAX_ENTRIES:
                    _fail()
                path = Path(entry.path)
                relative = path.relative_to(root).as_posix()
                if (len(relative.encode("utf-8")) > 4096 or "\\" in relative
                        or any(ord(c) < 32 or ord(c) == 127 for c in relative)):
                    _fail()
                info = entry.stat(follow_symlinks=False)
                link = stat.S_ISLNK(info.st_mode)
                _protected(info, link=link)
                if info.st_dev != root_info.st_dev:
                    _fail()
                if link:
                    target = os.readlink(path)
                    lexical = Path(os.path.normpath(path.parent / target))
                    resolved = path.resolve(strict=True)
                    if (Path(target).is_absolute() or not lexical.is_relative_to(root)
                            or not resolved.is_relative_to(root)
                            or resolved.stat().st_dev != root_info.st_dev
                            or any(ord(c) < 32 or ord(c) == 127 for c in target)):
                        _fail()
                    record = {"path": relative, "kind": "symlink", "target": target}
                elif stat.S_ISDIR(info.st_mode):
                    pending.append((path, depth + 1))
                    record = {"path": relative, "kind": "directory"}
                elif stat.S_ISREG(info.st_mode):
                    digest, observed, _ = _file_bytes(
                        path, min(_FILE_BYTES, _TOTAL_BYTES - total), deadline,
                    )
                    if _identity(info) != _identity(observed):
                        _fail()
                    total += observed.st_size
                    record = {
                        "path": relative, "kind": "file", "size": observed.st_size,
                        "sha256": digest, "executable": bool(observed.st_mode & 0o111),
                    }
                    if relative in ("run", "model/0.vvm"):
                        pins[relative] = digest
                    if relative == "run" and not record["executable"]:
                        _fail()
                else:
                    _fail()
                metadata_bytes += len(_canonical(record)) + 1
                if metadata_bytes + 4096 > _MAX_RECEIPT_BYTES:
                    _fail()
                records.append(record)
                identities.append((path, _identity(info)))
        if _identity(directory.lstat()) != _identity(before):
            _fail()
    if pins != {"run": EXECUTABLE_SHA256, "model/0.vvm": MODEL_SHA256}:
        _fail()
    # Catch a file changing after its hash was taken while later entries scan.
    for path, expected in identities:
        _before_deadline(deadline)
        if _identity(path.lstat()) != expected:
            _fail()
    return sorted(records, key=lambda record: record["path"]), total


def _receipt(root: Path, rights_review_sha256: str) -> dict:
    if type(rights_review_sha256) is not str or _SHA.fullmatch(rights_review_sha256) is None:
        _fail()
    inventory, total = _inventory(root)
    receipt = {
        "schema_version": "voicevox-cpu-bundle-v1",
        "provider_id": "voicevox",
        "version": VERSION,
        "target": "linux-cpu-x64",
        "archive_sha256": ARCHIVE_SHA256,
        "executable_sha256": EXECUTABLE_SHA256,
        "model_sha256": MODEL_SHA256,
        "rights_review_sha256": rights_review_sha256,
        "native_execution_qualified": False,
        "tree_sha256": "sha256:" + hashlib.sha256(_canonical(inventory)).hexdigest(),
        "entry_count": len(inventory),
        "total_bytes": total,
        "inventory": inventory,
    }
    if len(_canonical(receipt)) + 1 > _MAX_RECEIPT_BYTES:
        _fail()
    return receipt


def record_bundle(root: Path, receipt_path: Path, *, rights_review_sha256: str) -> dict:
    """Write a new content receipt outside the tree; never overwrite an input."""
    try:
        root, receipt_path = Path(root), Path(receipt_path)
        _absolute(receipt_path.parent)
        if (receipt_path.is_relative_to(root) or receipt_path.name in ("", ".", "..")
                or receipt_path != Path(os.path.normpath(receipt_path))):
            _fail()
        receipt = _receipt(root, rights_review_sha256)
        fd = os.open(receipt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                     | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644)
        with os.fdopen(fd, "wb") as stream:
            stream.write(_canonical(receipt) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        return receipt
    except (OSError, ValueError, RuntimeError):
        raise VoicevoxBundleError() from None


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail()
        result[key] = value
    return result


def _constant(_value):
    _fail()


def verify_bundle(root: Path, receipt_path: Path, *, rights_review_sha256: str) -> dict:
    """Rehash a protected bundle and compare its complete bounded content receipt."""
    try:
        root, receipt_path = Path(root), Path(receipt_path)
        _absolute(receipt_path)
        if receipt_path.is_relative_to(root):
            _fail()
        _, _, raw = _file_bytes(
            receipt_path, _MAX_RECEIPT_BYTES, time.monotonic() + _SCAN_SECONDS, capture=True,
        )
        supplied = json.loads(raw, object_pairs_hook=_object, parse_constant=_constant)
        expected = _receipt(root, rights_review_sha256)
        if _canonical(supplied) != _canonical(expected):
            _fail()
        return expected
    except (OSError, ValueError, RuntimeError, RecursionError):
        raise VoicevoxBundleError() from None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    archive = subcommands.add_parser("verify-archive")
    archive.add_argument("archive", type=Path)
    record = subcommands.add_parser("record")
    record.add_argument("root", type=Path)
    record.add_argument("receipt", type=Path)
    record.add_argument("--rights-review-sha256", required=True)
    args = parser.parse_args()
    try:
        if args.command == "verify-archive":
            verify_archive(args.archive)
            print("voicevox_archive=VERIFIED")
        else:
            record_bundle(args.root, args.receipt, rights_review_sha256=args.rights_review_sha256)
            print("voicevox_bundle=RECORDED(content-only)")
    except VoicevoxBundleError as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
