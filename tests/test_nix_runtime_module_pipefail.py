"""Source regression guard: Nix runtime-module-eval must not SIGPIPE grep -q."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_runtime_module_eval_consumes_all_printf_pipeline_bytes():
    content = (ROOT / "flake.nix").read_text()
    block = content.split("runtime-module-eval = pkgs.runCommand", 1)[1].split(
        "gpu-isolation-requires-gpu =", 1
    )[0]
    checks = [
        line.strip()
        for line in block.splitlines()
        if "printf " in line and "| grep " in line
    ]
    assert len(checks) == 12
    # With bash pipefail, grep -q can stop reading immediately on a match:
    # the printf then receives SIGPIPE, which incorrectly fails the build.
    for check in checks:
        assert re.search(r"\|\s*grep\s+(-[A-Za-z]*q\b)", check) is None
        assert check.endswith(">/dev/null")
    # Keep negative checks, and preserve both regex and literal match checks.
    assert any("grep -E " in check for check in checks)
    assert any("grep " in check and "grep -E " not in check for check in checks)


def test_drain_based_grep_preserves_positive_and_negative_results():
    payload = "CUDA_VISIBLE_DEVICES=GPU-example-smoke\n" + ("unused-field\n" * 50_000)
    for needle, expected_rc in (
        ("CUDA_VISIBLE_DEVICES=GPU-example-smoke", 0),
        ("MISSING_DELIBERATELY", 1),
    ):
        result = subprocess.run(
            [
                "bash",
                "-o",
                "pipefail",
                "-c",
                'payload="$(cat)"; printf "%s" "$payload" | grep "$1" >/dev/null',
                "bash",
                needle,
            ],
            check=False,
            capture_output=True,
            text=True,
            input=payload,
            timeout=8,
        )
        assert result.returncode == expected_rc, result.stderr
        assert result.stdout == ""


def test_drain_based_extended_regex_keeps_strict_match_semantics():
    payload = "+/nix/store/" + ("a" * 32) + "-astrumweaver-preflight\n"
    payload += "unrelated-line\n" * 45_000
    for pattern, expected_rc in (
        (r"^\+/nix/store/[a-z0-9]{32}-astrumweaver-preflight$", 0),
        (r"^\+/nix/store/[a-z0-9]{32}-unknown-preflight$", 1),
    ):
        result = subprocess.run(
            [
                "bash",
                "-o",
                "pipefail",
                "-c",
                'payload="$(cat)"; printf "%s" "$payload" | grep -E "$1" >/dev/null',
                "bash",
                pattern,
            ],
            input=payload,
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
        assert result.returncode == expected_rc, result.stderr
        assert result.stdout == ""
