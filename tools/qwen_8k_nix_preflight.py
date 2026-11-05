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
) -> dict[str, object]:
    """Pure bounded verifier. Root references do NOT imply future pin safety."""
    if profile_link != str(PROFILE):
        raise ValueError("unexpected-production-profile")
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
    # nix-store --query --roots shows current reachable roots, not
    # guaranteed protection when the profile switches generations.
    # Do not claim an independent GCroot is already established.
    return {
        "status": "CURRENT_8K_STORE_CLOSURE_PRESENT_READ_ONLY",
        "current_closure_paths": len(requisites),
        "current_referencing_roots": len(roots),
        "target_is_store_path": True,
        "independent_gcroot_verified": False,
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


def probe() -> dict[str, object]:
    if not PROFILE.is_symlink():
        raise ValueError("profile-is-not-a-symlink")
    resolved = str(PROFILE.resolve(strict=True))
    if not _store_path(resolved):
        raise ValueError("profile-outside-nix-store")
    requisites = query_nix(
        ["nix-store", "--query", "--requisites", str(PROFILE)],
    )
    roots = query_nix(
        ["nix-store", "--query", "--roots", str(PROFILE)],
    )
    return summarize(
        str(PROFILE), resolved, requisites, roots,
        exists=lambda p: Path(p).exists(),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--read-only", action="store_true", required=True)
    parser.parse_args()
    try:
        result = probe()
    except (ValueError, OSError, subprocess.SubprocessError):
        print(json.dumps({"status": "BLOCKED", "reason": "non-sensitive-preflight-failure"}))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
