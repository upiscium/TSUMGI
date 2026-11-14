"""No Nix host mutations; fake-closure tests for Qwen 8K preflight."""
from __future__ import annotations

import importlib.util
from contextlib import ExitStack
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
        ["/nix/var/nix/profiles/astrumweaver-runtime-qwen38-3-link -> " + target],
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


def test_independent_gcroot_requires_exact_record_and_target():
    target = str(module.ACCEPTED_8K_STORE)
    extra = store("other")
    records = [
        "/nix/var/nix/profiles/astrumweaver-runtime-qwen38-3-link -> " + target,
        str(module.INDEPENDENT_ROOT) + " -> " + target,
    ]
    result = module.summarize(
        str(module.PROFILE), target, [target, extra], records,
        exists=lambda k: k in {target, extra},
        require_independent_root=True,
    )
    assert result["status"] == "PINNED_8K_STORE_CLOSURE_PRESENT_READ_ONLY"
    assert result["independent_gcroot_verified"] is True
    assert result["old_generation_rollback_verified"] is False
    assert result["live_service_restore_verified"] is False
    assert result["current_referencing_roots"] == 2


@pytest.mark.parametrize("records", [
    [],
    ["/nix/var/nix/profiles/astrumweaver-runtime-qwen38-3-link -> "
     + str(module.ACCEPTED_8K_STORE)],
    [str(module.INDEPENDENT_ROOT) + " -> " + store("wrong")],
    [str(module.INDEPENDENT_ROOT) + " -> " + str(module.ACCEPTED_8K_STORE)] * 2,
    [str(module.INDEPENDENT_ROOT) + " -> /tmp/other"],
    [str(module.INDEPENDENT_ROOT)],
    [str(module.INDEPENDENT_ROOT) + " -> " + str(module.ACCEPTED_8K_STORE) + " "],
])
def test_missing_malformed_or_wrong_independent_root_fails_closed(records):
    target = str(module.ACCEPTED_8K_STORE)
    with pytest.raises(ValueError):
        module.summarize(
            str(module.PROFILE), target, [target], records,
            exists=lambda x: x == target,
            require_independent_root=True,
        )


def fake_fs_context(active_target: str, root_literal_target: str):
    scope = ExitStack()
    scope.enter_context(mock.patch.object(module.Path, "is_symlink", return_value=True))
    scope.enter_context(mock.patch.object(module.Path, "is_dir", return_value=True))
    scope.enter_context(mock.patch.object(
        module.Path, "resolve", return_value=Path(active_target)
    ))
    scope.enter_context(mock.patch.object(
        module.Path, "readlink", return_value=Path(root_literal_target)
    ))
    scope.enter_context(mock.patch.object(
        module.Path, "lstat", return_value=SimpleNamespace(st_uid=0)
    ))
    scope.enter_context(mock.patch.object(
        module.Path, "stat", return_value=SimpleNamespace(st_uid=0, st_mode=0o755)
    ))
    scope.enter_context(mock.patch.object(module.Path, "exists", return_value=True))
    return scope


def test_probe_confirmed_independent_root_calls_only_read_queries():
    target = str(module.ACCEPTED_8K_STORE)
    commands = []
    def fake_query(argv):
        commands.append(argv)
        if "--requisites" in argv:
            return [target]
        return [
            "/nix/var/nix/profiles/astrumweaver-runtime-qwen38-3-link -> " + target,
            str(module.INDEPENDENT_ROOT) + " -> " + target,
        ]
    with fake_fs_context(target, target), mock.patch.object(
        module, "query_nix", side_effect=fake_query
    ):
        result = module.probe(require_independent_root=True)
    assert result["independent_gcroot_verified"] is True
    assert result["current_profile_points_to_accepted_8k"] is True
    assert [c[2] for c in commands] == ["--requisites", "--roots"]
    assert all(c[-1] == target for c in commands)


def test_pinned_old_8k_root_survives_profile_switch_to_12k():
    pinned = str(module.ACCEPTED_8K_STORE)
    new_12k = store("new-12k-profile")
    calls = []
    def fake_query(argv):
        calls.append(argv)
        return ([pinned] if "--requisites" in argv else
                [str(module.INDEPENDENT_ROOT) + " -> " + pinned])
    with fake_fs_context(new_12k, pinned), mock.patch.object(
        module, "query_nix", side_effect=fake_query
    ):
        result = module.probe(require_independent_root=True)
    assert result["independent_gcroot_verified"] is True
    assert result["current_profile_points_to_accepted_8k"] is False
    assert all(c[-1] == pinned for c in calls)


def test_indirect_gcroot_via_mutable_profile_is_rejected():
    pinned = str(module.ACCEPTED_8K_STORE)
    with fake_fs_context(pinned, str(module.PROFILE)):
        with pytest.raises(ValueError, match="not-direct-to-pinned-8k"):
            module.probe(require_independent_root=True)


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
