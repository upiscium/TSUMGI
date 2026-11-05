"""No Nix host mutations; fake-closure tests for Qwen 8K preflight."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest import mock

import pytest


SOURCE = Path(__file__).resolve().parents[1] / "tools/qwen_8k_nix_preflight.py"
SPEC = importlib.util.spec_from_file_location("qwen_nix_preflight", SOURCE)
assert SPEC is not None and SPEC.loader is not None
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def store(label: str) -> str:
    return "/nix/store/" + "a" * 32 + "-" + label


def test_current_closure_only_is_not_an_independent_rollback_root():
    target = store("current-profile")
    runtime = store("llama-cpp-runtime")
    report = module.summarize(
        str(module.PROFILE), target, [target, runtime],
        ["/nix/var/nix/profiles/astrumweaver-runtime-qwen38"],
        exists=lambda key: key in {target, runtime},
    )
    assert report["status"] == "CURRENT_8K_STORE_CLOSURE_PRESENT_READ_ONLY"
    assert report["current_closure_paths"] == 2
    assert report["current_referencing_roots"] == 1
    assert report["independent_gcroot_verified"] is False
    assert report["old_generation_rollback_verified"] is False
    assert report["live_service_restore_verified"] is False
    assert "/nix/store/" not in json.dumps(report)


@pytest.mark.parametrize(("target", "refs", "roots", "existing"), [
    (store("current"), [], [], True),
    (store("current"), [store("other")], [], True),
    (store("current"), [store("current"), store("current")], [], True),
    (store("current"), [store("current"), "/tmp/fake"], [], True),
    ("/tmp/not-nix", ["/tmp/not-nix"], [], True),
    (store("current"), [store("current")], [], False),
])
def test_fail_closed_invalid_or_missing_closure(target, refs, roots, existing):
    with pytest.raises(ValueError):
        module.summarize(
            str(module.PROFILE), target, refs, roots,
            exists=lambda _key: existing,
        )


def test_fail_closed_other_profile_path():
    with pytest.raises(ValueError, match="unexpected-production-profile"):
        module.summarize(
            "/nix/var/nix/profiles/another-model", store("current"),
            [store("current")], [], exists=lambda _: True,
        )


def test_maximum_bound_and_no_duplicate_requisites():
    with pytest.raises(ValueError, match="unbounded-or-empty-closure"):
        module.summarize(
            str(module.PROFILE), store("current"),
            [store("current")] * 20001, [], exists=lambda _: True,
        )


def test_read_only_nix_subprocess_shape():
    calls = []

    def fake_query(argv):
        calls.append(argv)
        return [store("current")] if "--requisites" in argv else []

    with mock.patch.object(module.PROFILE.__class__, "is_symlink", return_value=True), \
         mock.patch.object(module.PROFILE.__class__, "resolve", return_value=Path(store("current"))), \
         mock.patch.object(module, "query_nix", side_effect=fake_query), \
         mock.patch.object(module.Path, "exists", return_value=True):
        result = module.probe()
    assert result["current_closure_paths"] == 1
    assert calls == [
        ["nix-store", "--query", "--requisites", str(module.PROFILE)],
        ["nix-store", "--query", "--roots", str(module.PROFILE)],
    ]
    assert all(not any(x in argv for x in (
        "--gc", "--add-root", "--realise", "--import", "--delete",
        "--switch", "--profile",
    )) for argv in calls)


@pytest.mark.parametrize("stdout", [
    " /nix/store/" + "a" * 32 + "-valid\n",
    "/nix/store/" + "a" * 32 + "-valid \n",
    "/nix/store/" + "a" * 32 + "-valid\n\n",
    "\n/nix/store/" + "a" * 32 + "-valid\n",
    "/nix/store/" + "a" * 32 + "-valid\r\n",
])
def test_query_nix_rejects_whitespace_or_blank_records(stdout):
    with pytest.raises(ValueError):
        module.query_nix([sys.executable, "-c",
                          "import sys;sys.stdout.write(" + repr(stdout) + ")"])


def test_query_nix_handles_exact_output_and_stderr_without_exposing_it():
    path = store("current")
    got = module.query_nix(
        [sys.executable, "-c", (
            "import sys;"
            "sys.stderr.write('PRIVATE_SECRET' * 4000);"
            "sys.stdout.write(" + repr(path + "\n") + ")"
        )]
    )
    assert got == [path]


def test_query_nix_stops_oversize_subprocess_before_unbounded_buffering():
    with pytest.raises(ValueError, match="nix-query-output-unbounded"):
        module.query_nix(
            [sys.executable, "-c",
             "import sys;sys.stdout.buffer.write(b'x' * 2100000)"]
        )


def test_query_nix_fails_nonzero_without_raw_stderr():
    with pytest.raises(ValueError, match="nix-query-nonzero-exit"):
        module.query_nix(
            [sys.executable, "-c",
             "import sys;sys.stderr.write('PRIVATE_SECRET');sys.exit(7)"]
        )


def test_cli_without_read_only_flag_exits_before_touching_nix():
    result = subprocess.run(
        [sys.executable, str(SOURCE)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode != 0
    assert "required" in result.stderr
