"""Read-only Nix Store closure check for the CURRENT Sirius Qwen 8K Runtime.

This does not create a GC root, change profiles, run GC or authenticate a
rollback package. A live 12K cutover remains a separate human decision.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import selectors
import subprocess
import time
import sys
from typing import Callable, Iterable

PROFILE = Path("/nix/var/nix/profiles/astrumweaver-runtime-qwen38")
INDEPENDENT_ROOT = Path(
    "/nix/var/nix/gcroots/tsumgi-qwen38-8k-pre12k-20261105"
)
# Independently accepted live 8K generation-3 output, not the mutable
# runtime profile after a future 12K switch. Source: #157 and #230 R4.
ACCEPTED_8K_STORE = Path(
    "/nix/store/lk6r07rifys2zvbsz2ah9wm6skay92q2-profile"
)
STORE = Path("/nix/store")
MAX_OUTPUT_BYTES = 2_000_000
STORE_NAME = re.compile(r"[0-9a-df-np-sv-z]{32}-[A-Za-z0-9+._?=-]{1,240}\Z")


def _store_path(text: str) -> bool:
    path = Path(text)
    return (
        path.parent == STORE and
        STORE_NAME.fullmatch(path.name) is not None and
        str(path) == text
    )


def summarize(
    profile_link: str,
    resolved_target: str,
    requisite_lines: Iterable[str],
    root_lines: Iterable[str],
    *,
    exists: Callable[[str], bool],
    require_independent_root: bool = False,
) -> dict[str, object]:
    """Verify closure now; independent root requires separately checked state."""
    if profile_link != str(PROFILE):
        raise ValueError("unexpected-production-profile")
    if require_independent_root and resolved_target != str(ACCEPTED_8K_STORE):
        raise ValueError("pinned-8k-target-mismatch")
    if not _store_path(resolved_target) or not exists(resolved_target):
        raise ValueError("missing-or-unexpected-profile-target")
    requisites = list(requisite_lines)
    roots = list(root_lines)
    if not requisites or len(requisites) > 20000:
        raise ValueError("unbounded-or-empty-closure")
    if any(not _store_path(path) for path in requisites):
        raise ValueError("malformed-closure-reference")
    if len(requisites) != len(set(requisites)):
        raise ValueError("duplicated-requisite")
    if resolved_target not in requisites:
        raise ValueError("profile-target-missing-from-closure")
    if any(not exists(path) for path in requisites):
        raise ValueError("missing-closure-path")
    if len(roots) > 10000:
        raise ValueError("unbounded-root-list")
    expected_record = f"{INDEPENDENT_ROOT} -> {resolved_target}"
    for record in roots:
        entry, separator, target = record.partition(" -> ")
        if not separator or not entry.startswith("/nix/var/nix/"):
            raise ValueError("malformed-root-record")
        if not _store_path(target) or target != resolved_target:
            raise ValueError("unexpected-root-target")
    if require_independent_root and roots.count(expected_record) != 1:
        raise ValueError("independent-root-missing-from-nix-records")
    return {
        "status": (
            "PINNED_8K_STORE_CLOSURE_PRESENT_READ_ONLY"
            if require_independent_root
            else "CURRENT_8K_STORE_CLOSURE_PRESENT_READ_ONLY"
        ),
        "current_closure_paths": len(requisites),
        "current_referencing_roots": len(roots),
        "target_is_store_path": True,
        "independent_gcroot_verified": bool(require_independent_root),
        "old_generation_rollback_verified": False,
        "live_service_restore_verified": False,
    }


def query_nix(command: list[str]) -> list[str]:
    # Never buffer unlimited subprocess stdout or stderr in RAM.
    # The CLI itself supplies only two fixed, read-only nix-store queries.
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    chunks: list[bytes] = []
    size = 0
    deadline = time.monotonic() + 25
    try:
        if process.stdout is None:
            raise ValueError("nix-query-no-output-pipe")
        with selectors.DefaultSelector() as selected:
            selected.register(process.stdout, selectors.EVENT_READ)
            while True:
                timeout = deadline - time.monotonic()
                if timeout <= 0:
                    raise subprocess.TimeoutExpired(command, 25)
                events = selected.select(timeout)
                if not events:
                    raise subprocess.TimeoutExpired(command, 25)
                part = os.read(process.stdout.fileno(), 8192)
                if not part:
                    break
                size += len(part)
                if size > MAX_OUTPUT_BYTES:
                    raise ValueError("nix-query-output-unbounded")
                chunks.append(part)
        process.wait(timeout=max(0.01, deadline - time.monotonic()))
        if process.returncode != 0:
            raise ValueError("nix-query-nonzero-exit")
    except BaseException:
        if process.poll() is None:
            process.kill()
        process.wait()
        raise
    finally:
        if process.stdout is not None:
            process.stdout.close()
    raw = b"".join(chunks)
    if not raw:
        return []
    if raw.endswith(b"\n"):
        raw = raw[:-1]
    lines = raw.split(b"\n")
    if len(lines) > 20000 or any(not row or len(row) > 4096 for row in lines):
        raise ValueError("nix-query-invalid-record")
    # No CRLF conversion, trimming or silent blank-record deletion.
    try:
        decoded = [row.decode("utf-8", "strict") for row in lines]
    except UnicodeDecodeError as exc:
        raise ValueError("nix-query-invalid-encoding") from exc
    if any(text != text.strip() for text in decoded):
        raise ValueError("nix-query-whitespace-in-record")
    return decoded


def probe(*, require_independent_root: bool = False) -> dict[str, object]:
    if not PROFILE.is_symlink():
        raise ValueError("profile-is-not-a-symlink")
    current = str(PROFILE.resolve(strict=True))
    if not _store_path(current):
        raise ValueError("profile-outside-nix-store")
    target = current
    if require_independent_root:
        # Even after PROFILE switches to 12K, protect/check the *fixed*
        # original 8K output rather than following any mutable link.
        target = str(ACCEPTED_8K_STORE)
        if not ACCEPTED_8K_STORE.is_dir():
            raise ValueError("accepted-8k-store-target-missing")
        if not INDEPENDENT_ROOT.is_symlink():
            raise ValueError("independent-root-not-a-symlink")
        if INDEPENDENT_ROOT.lstat().st_uid != 0:
            raise ValueError("independent-root-not-root-owned")
        # Path.resolve() is insufficient: it would follow an indirect
        # GCroot->mutable-profile->Store chain and falsely pass.
        if str(INDEPENDENT_ROOT.readlink()) != target:
            raise ValueError("independent-root-not-direct-to-pinned-8k")
        parent = INDEPENDENT_ROOT.parent
        if parent.stat().st_uid != 0 or parent.stat().st_mode & 0o022:
            raise ValueError("root-parent-not-trusted")
    query_target = target if require_independent_root else str(PROFILE)
    requisites = query_nix(
        ["nix-store", "--query", "--requisites", query_target],
    )
    roots = query_nix(
        ["nix-store", "--query", "--roots", query_target],
    )
    report = summarize(
        str(PROFILE), target, requisites, roots,
        exists=lambda p: Path(p).exists(),
        require_independent_root=require_independent_root,
    )
    report["current_profile_points_to_accepted_8k"] = current == str(ACCEPTED_8K_STORE)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--read-only", action="store_true", required=True)
    parser.add_argument("--require-independent-root", action="store_true")
    args = parser.parse_args()
    try:
        result = probe(require_independent_root=args.require_independent_root)
    except (ValueError, OSError, subprocess.SubprocessError):
        print(json.dumps({"status": "BLOCKED", "reason": "non-sensitive-preflight-failure"}))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
