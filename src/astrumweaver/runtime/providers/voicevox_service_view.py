"""Offline selected-content view, not an installer or an execution attestation.

Standalone stdlib only: no extractor, subprocess, native code, or input mutation.
The original engine remains separate. Failed new outputs are retained, not
deleted, repaired or reused. Pins cannot be overridden through the CLI.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import time

_ARCHIVE = ("libc6_2.35-0ubuntu3.15_amd64.deb", 3234380,
            "79e35256227e16a607c154cdeb8d76ff12d20e31de286ea7fd9ad3b96fe0452d")
_LIBC = {
    "ld-linux-x86-64.so.2": (240936, "41daf7836f7fce21a40ca63a00c5caf21b05a35a76ccc075d86f1716212cdf01"),
    "libc.so.6": (2220400, "b2cf6c33b74d2f22543b7a469a75b538911e690f769d0b238843a49465b83793"),
    "libdl.so.2": (14432, "3eaaa4aefd9c28f3867c96a427c3cbec9191cb61c4c9d0a2cb4a765d66064499"),
    "libm.so.6": (940560, "3dd5511ae94785c9f921429b0f2b2f7aabb461b6f0e6de6dfdbef15f24bdfee6"),
    "libpthread.so.0": (21448, "fbed9505adacada2abb37c6b43be2df6de81e742712c6025694ba48de62c7e78"),
    "librt.so.1": (14664, "5f20d34fb3bb4d84b4edeb6ca11fa5b48c705207253741619acab7cc758185fb"),
    "libutil.so.1": (14432, "ee98a316fe33727cf23cc81d3a7f9628350d6ef75e905c7b713e5f5e68e177dc"),
}
_MVEC = (1031720, "44e31b0cd6dcb6f8147ffc820f51e7140c429a6e0e7724dd00ad0ea4d18f4f51")
_ZLIB = (108936, "64c206f0146cc58bbddc4f22054436f4ff278f5a554aa3ce6921ddf7e9133370")
_COPYRIGHT = (26462, "d3c95b56fa33e28b57860580f0baf4e4f4de2a268a2b80f1d031a5191bade265")
_RUN = (9315016, "acf74848282c0ff6f0aaf8514427c5f44b0992c26b48448d76b35a97dbe81a25")
_MAX_BYTES = 32 * 1024 * 1024
_MAX_FILE_BYTES = 16 * 1024 * 1024
_MAX_RECEIPT_BYTES = 64 * 1024
_MAX_ENTRIES = 64
_MAX_DEPTH = 8
_DEADLINE_SECONDS = 120
_RECEIPT = "share/astrumweaver/voicevox-service-view.json"
_LOADER = "ld-linux-x86-64.so.2"
_LIBDIR = "lib/x86_64-linux-gnu"
_COPYRIGHT_PATH = "usr/share/doc/libc6/copyright"
_LINK_PATH = "rootfs/lib64/ld-linux-x86-64.so.2"
_LINK = "../lib/x86_64-linux-gnu/ld-linux-x86-64.so.2"
_DIRS = ("rootfs", "rootfs/lib", "rootfs/lib/x86_64-linux-gnu",
         "rootfs/lib64", "rootfs/usr", "rootfs/usr/share",
         "rootfs/usr/share/doc", "rootfs/usr/share/doc/libc6",
         "rootfs/etc", "rootfs/dev", "rootfs/proc", "rootfs/sys", "rootfs/run",
         "rootfs/tmp", "rootfs/nix", "rootfs/nix/store",
         "share", "share/astrumweaver")


class VoicevoxServiceViewError(ValueError):
    def __init__(self) -> None:
        super().__init__("VOICEVOX service view verification failed")


def _fail() -> None:
    raise VoicevoxServiceViewError()


def _canon(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def _identity(info: os.stat_result) -> tuple:
    return (info.st_dev, info.st_ino, info.st_uid, info.st_gid, info.st_mode,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _before(deadline: float) -> None:
    if time.monotonic() >= deadline:
        _fail()


def _lexical(path: Path) -> None:
    value = str(path)
    if (not path.is_absolute() or value == "/" or value.startswith("//")
            or value != os.path.normpath(value) or ".." in path.parts
            or len(value.encode("utf-8")) > 4096
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        _fail()


def _protected(info: os.stat_result, *, link: bool = False) -> None:
    if (info.st_uid not in (0, os.geteuid())
            or info.st_mode & (stat.S_ISUID | stat.S_ISGID)
            or (not link and info.st_mode & 0o022)):
        _fail()


def _sticky_nix_store(path: Path, info: os.stat_result) -> bool:
    # Multi-user Nix lets build users create new store entries. The root-owned
    # sticky directory protects entries belonging to other users; each selected
    # entry must still pass the ordinary owner/mode/pin checks. This exception
    # is ONLY the canonical store directory, never an input subtree or file.
    return (path == Path("/nix/store") and stat.S_ISDIR(info.st_mode)
            and info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o1775)


def _check_path(path: Path, *, directory: bool = False) -> os.stat_result:
    _lexical(path)
    info = path.lstat()
    if not _sticky_nix_store(path, info):
        _protected(info)
    if stat.S_ISLNK(info.st_mode) or (directory and not stat.S_ISDIR(info.st_mode)):
        _fail()
    for parent in path.parents:
        observed = parent.lstat()
        if not stat.S_ISDIR(observed.st_mode):
            _fail()
        # Only the conventional root-owned sticky temporary ancestors get an
        # exception. A sticky arbitrary input/output directory is not trusted.
        sticky_tmp = (str(parent) in ("/tmp", "/var/tmp")
                      and observed.st_uid == 0 and observed.st_mode & stat.S_ISVTX)
        if not sticky_tmp and not _sticky_nix_store(parent, observed):
            _protected(observed)
    return info


def _immutable_store_file(path: Path, info: os.stat_result) -> bool:
    # Nix store optimisation may hardlink root-owned, read-only regular files.
    return (path.is_relative_to("/nix/store") and info.st_uid == 0
            and not info.st_mode & 0o222)


def _read(path: Path, pin: tuple[int, str], deadline: float, *, capture: bool = False):
    """No-follow, bounded read, with identities checked before/open/after."""
    _before(deadline)
    size, digest = pin
    info = _check_path(path)
    if (not 0 <= size <= _MAX_FILE_BYTES or not stat.S_ISREG(info.st_mode)
            or info.st_size != size
            or (info.st_nlink != 1 and not _immutable_store_file(path, info))):
        _fail()
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    chunks = bytearray() if capture else None
    hashed = hashlib.sha256()
    try:
        if _identity(os.fstat(fd)) != _identity(info):
            _fail()
        remaining = size
        while remaining:
            _before(deadline)
            block = os.read(fd, min(remaining, 1024 * 1024))
            if not block:
                _fail()
            hashed.update(block)
            remaining -= len(block)
            if chunks is not None:
                chunks.extend(block)
        if (os.read(fd, 1) or _identity(os.fstat(fd)) != _identity(info)
                or _identity(path.lstat()) != _identity(info)
                or hashed.hexdigest() != digest):
            _fail()
    finally:
        os.close(fd)
    return chunks, info


def _remember(identities: dict, path: Path, info: os.stat_result) -> None:
    identity = _identity(info)
    if identities.setdefault(path, identity) != identity:
        _fail()


def _unchanged(identities: dict, deadline: float) -> None:
    for path, identity in identities.items():
        _before(deadline)
        if _identity(path.lstat()) != identity:
            _fail()


def _overlap(left: Path, right: Path) -> bool:
    return left.is_relative_to(right) or right.is_relative_to(left)


def _paths(archive, libc_root, engine_root, output):
    archive, libc_root, engine_root, output = map(Path, (archive, libc_root, engine_root, output))
    _check_path(archive)
    _check_path(libc_root, directory=True)
    _check_path(engine_root, directory=True)
    _lexical(output)
    _check_path(output.parent, directory=True)
    if (os.path.lexists(output) or _overlap(libc_root, engine_root)
            or any(_overlap(output, source) for source in (archive, libc_root, engine_root))):
        _fail()
    return archive, libc_root, engine_root, output


def _sources(archive: Path, libc_root: Path, engine_root: Path, deadline: float):
    """Capture copy bytes once; retain identities of every selected input."""
    specs = [(libc_root, f"{_LIBDIR}/{name}", pin, f"rootfs/{_LIBDIR}/{name}")
             for name, pin in _LIBC.items()]
    specs += [
        (libc_root, _COPYRIGHT_PATH, _COPYRIGHT, f"rootfs/{_COPYRIGHT_PATH}"),
        (engine_root, "engine_internal/libz.so.1", _ZLIB, f"rootfs/{_LIBDIR}/libz.so.1"),
        (libc_root, f"{_LIBDIR}/libmvec.so.1", _MVEC, None),
        (engine_root, "engine_internal/libmvec.so.1", _MVEC, None),
        (engine_root, "run", _RUN, None),
    ]
    if _ARCHIVE[1] + sum(pin[0] for _, _, pin, _ in specs) > _MAX_BYTES:
        _fail()
    identities: dict = {}
    _, info = _read(archive, _ARCHIVE[1:], deadline)
    _remember(identities, archive, info)
    data = {}
    for root, relative, pin, destination in specs:
        _before(deadline)
        root_info = _check_path(root, directory=True)
        _remember(identities, root, root_info)
        path = root / relative
        for directory in reversed(path.parent.parents):
            if directory.is_relative_to(root):
                observed = _check_path(directory, directory=True)
                if observed.st_dev != root_info.st_dev:
                    _fail()
                _remember(identities, directory, observed)
        observed = _check_path(path.parent, directory=True)
        if observed.st_dev != root_info.st_dev:
            _fail()
        _remember(identities, path.parent, observed)
        content, observed = _read(path, pin, deadline, capture=destination is not None)
        if observed.st_dev != root_info.st_dev:
            _fail()
        _remember(identities, path, observed)
        if destination is not None:
            data[destination] = content
    _unchanged(identities, deadline)
    return data, identities


def _file_record(path: str, pin: tuple[int, str], mode: int = 0o444) -> dict:
    return {"path": path, "kind": "file", "size": pin[0],
            "sha256": "sha256:" + pin[1], "mode": mode}


def _witness(path: str, pin: tuple[int, str]) -> dict:
    # Input modes are protected, not prescribed (inert extraction uses 0600).
    return {"path": path, "size": pin[0], "sha256": "sha256:" + pin[1]}


def _receipt() -> dict:
    records = [{"path": path, "kind": "directory", "mode": 0o555} for path in _DIRS]
    records += [_file_record(f"rootfs/{_LIBDIR}/{name}", pin,
                             0o555 if name == _LOADER else 0o444)
                for name, pin in _LIBC.items()]
    records += [
        _file_record(f"rootfs/{_LIBDIR}/libz.so.1", _ZLIB),
        _file_record(f"rootfs/{_COPYRIGHT_PATH}", _COPYRIGHT),
        {"path": _LINK_PATH, "kind": "symlink", "target": _LINK},
    ]
    records.sort(key=lambda record: record["path"])
    receipt = {
        "schema_version": "voicevox-service-view-v1",
        "libc_package": {"filename": _ARCHIVE[0], "size": _ARCHIVE[1],
                         "sha256": "sha256:" + _ARCHIVE[2]},
        "input_witnesses": [
            _witness(f"libc/{_LIBDIR}/libmvec.so.1", _MVEC),
            _witness("engine/engine_internal/libmvec.so.1", _MVEC),
            _witness("engine/run", _RUN),
        ],
        "inventory": records,
        "tree_sha256": "sha256:" + hashlib.sha256(_canon(records)).hexdigest(),
        "native_execution_qualified": False,
        "elf_loader_resolution_proven": False,
        "firstexec_loaded_mapping": "UNKNOWN",
        "activation_supported": False,
        "serving_binding_supported": False,
        "full_engine_tree_verified": False,
        "signed_archive_chain_verified": False,
        "security_status": "superseded-abi-investigation-only",
    }
    if len(_canon(receipt)) + 1 > _MAX_RECEIPT_BYTES:
        _fail()
    return receipt


def _write_new(path: Path, data: bytes | bytearray, mode: int) -> None:
    _check_path(path.parent, directory=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fchmod(stream.fileno(), mode)
        os.fsync(stream.fileno())


def build_view(libc_archive: Path, libc_root: Path, engine_root: Path, output: Path) -> dict:
    """Build a new, disjoint candidate. Never repair or overwrite an output."""
    try:
        deadline = time.monotonic() + _DEADLINE_SECONDS
        archive, libc_root, engine_root, output = _paths(libc_archive, libc_root, engine_root, output)
        data, identities = _sources(archive, libc_root, engine_root, deadline)
        receipt = _receipt()
        _before(deadline)
        # Every input pin, including copyright, has passed before any writing.
        output.mkdir(mode=0o700)
        for relative in _DIRS:
            _before(deadline)
            (output / relative).mkdir(mode=0o700)
        for record in receipt["inventory"]:
            _before(deadline)
            if record["kind"] == "file":
                _write_new(output / record["path"], data[record["path"]], record["mode"])
        os.symlink(_LINK, output / _LINK_PATH)
        _unchanged(identities, deadline)
        _write_new(output / _RECEIPT, _canon(receipt) + b"\n", 0o444)
        for relative in reversed(_DIRS):
            os.chmod(output / relative, 0o555, follow_symlinks=False)
        os.chmod(output, 0o555, follow_symlinks=False)
        return _verify(output, deadline)
    except (OSError, ValueError, RuntimeError, TypeError):
        raise VoicevoxServiceViewError() from None


def _verify(output: Path, deadline: float) -> dict:
    root_info = _check_path(output, directory=True)
    if stat.S_IMODE(root_info.st_mode) != 0o555:
        _fail()
    receipt = _receipt()
    encoded = _canon(receipt) + b"\n"
    expected = {record["path"]: record for record in receipt["inventory"]}
    expected[_RECEIPT] = _file_record(
        _RECEIPT, (len(encoded), hashlib.sha256(encoded).hexdigest()),
    )
    if sum(record.get("size", 0) for record in expected.values()) > _MAX_BYTES:
        _fail()
    seen: set[str] = set()
    pending = [(output, 0)]
    identities = {output: _identity(root_info)}
    while pending:
        directory, depth = pending.pop()
        _before(deadline)
        if depth > _MAX_DEPTH:
            _fail()
        with os.scandir(directory) as entries:
            for entry in entries:
                _before(deadline)
                path = Path(entry.path)
                relative = path.relative_to(output).as_posix()
                if relative not in expected or len(seen) >= _MAX_ENTRIES:
                    _fail()
                seen.add(relative)
                info = entry.stat(follow_symlinks=False)
                _protected(info, link=stat.S_ISLNK(info.st_mode))
                if info.st_dev != root_info.st_dev:
                    _fail()
                record = expected[relative]
                if record["kind"] == "symlink":
                    if (not stat.S_ISLNK(info.st_mode) or os.readlink(path) != _LINK
                            or path.resolve(strict=True) != output / f"rootfs/{_LIBDIR}/{_LOADER}"):
                        _fail()
                elif record["kind"] == "directory":
                    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != record["mode"]:
                        _fail()
                    pending.append((path, depth + 1))
                else:
                    _, observed = _read(
                        path, (record["size"], record["sha256"].removeprefix("sha256:")), deadline,
                    )
                    if (_identity(info) != _identity(observed)
                            or stat.S_IMODE(info.st_mode) != record["mode"]):
                        _fail()
                _remember(identities, path, info)
    if seen != set(expected):
        _fail()
    _unchanged(identities, deadline)
    # Exact bytes of the deterministic receipt were checked without an
    # unbounded read or parsing attacker-controlled JSON (including duplicates).
    return receipt


def verify_view(output: Path) -> dict:
    """Rehash the exact read-only layout; not evidence of its native behavior."""
    try:
        return _verify(Path(output), time.monotonic() + _DEADLINE_SECONDS)
    except (OSError, ValueError, RuntimeError, TypeError):
        raise VoicevoxServiceViewError() from None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    build = subcommands.add_parser("build")
    for argument in ("libc-archive", "libc-root", "engine-root", "output"):
        build.add_argument("--" + argument, required=True, type=Path)
    verify = subcommands.add_parser("verify")
    verify.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command == "build":
            build_view(args.libc_archive, args.libc_root, args.engine_root, args.output)
        else:
            verify_view(args.output)
        print("voicevox_service_view=VERIFIED(content-only; activation unsupported)")
    except VoicevoxServiceViewError as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
