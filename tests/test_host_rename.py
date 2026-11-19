from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tsumgi.setup.host_rename import (
    CommandResult,
    HostMigration,
    MigrationError,
    main,
    rename_text,
)

STORE = "/nix/store/" + "a" * 32 + "-astrumweaver-d1-decision-cuda-sm61-runtime"


class Recorder:
    def __init__(self, enabled=True, fail_on=None):
        self.calls: list[list[str]] = []
        self.enabled = enabled
        self.fail_on = fail_on

    def __call__(self, argv):
        self.calls.append(list(argv))
        if self.fail_on and argv[:2] == list(self.fail_on):
            return CommandResult(1)
        if argv[:2] == ["systemctl", "is-enabled"]:
            return CommandResult(0 if self.enabled else 1)
        return CommandResult(0)


def write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def make_worker(root: Path) -> None:
    write(root, "etc/passwd", "root:x:0:0::/root:/bin/sh\nastrumweaver:x:999:999::/var/lib/astrumweaver:/usr/sbin/nologin\n")
    write(root, "etc/group", "root:x:0:\nvideo:x:44:astrumweaver\nastrumweaver:x:999:\nastrumweaver-config:x:998:astrumweaver\n")
    write(root, "etc/systemd/system/astrumweaver-worker.service",
          "[Service]\nUser=astrumweaver\nStateDirectory=astrumweaver\n"
          "EnvironmentFile=-/etc/astrumweaver/worker.env\n")
    write(root, "etc/systemd/system/astrumweaver-worker.service.d/70-strata.conf",
          "[Service]\nExecStartPre=+/usr/local/libexec/astrumweaver/gpu-preflight /etc/astrumweaver/gpu-uuids\n"
          "ExecStart=\nExecStart=/nix/var/nix/profiles/tsumgi-v021-installer/bin/astrumweaver-worker --config /etc/astrumweaver/p/worker.toml\n")
    write(root, "etc/astrumweaver/worker.env", "ASTRUMWEAVER_WORKER_TOKEN=s3cret-value\n")
    write(root, "etc/astrumweaver/p/worker.toml", '[worker]\nid = "spica"\ncontrol_url = "https://example.invalid"\n[runtime]\nmanifest = "/etc/astrumweaver/p/runtime.json"\n')
    write(root, "etc/astrumweaver/p/runtime.json", json.dumps({"bin": "/opt/astrumweaver/strata/bin/strata", "run": "/run/astrumweaver-worker"}))
    write(root, "etc/astrumweaver/gpu-uuids", "GPU-0000\n")
    write(root, "var/lib/astrumweaver/models/x.gguf", "weights")
    write(root, "opt/astrumweaver/strata/bin/strata", "binary")
    write(root, "usr/local/libexec/astrumweaver/gpu-preflight", "old helper ASTRUMWEAVER_NVIDIA_SMI\n")
    helpers = root.parent / (root.name + "-helpers")
    write(helpers, "gpu-preflight", "new helper TSUMGI_NVIDIA_SMI\n")


@pytest.fixture
def host(tmp_path):
    root = tmp_path / "host"
    root.mkdir()
    make_worker(root)
    return root


def migration(root, **kw):
    return HostMigration(root, "worker", "tsumgi-v030-installer",
                         helpers_from=root.parent / (root.name + "-helpers"), **kw)


def test_rename_text_renames_names_but_never_store_paths():
    text = f"ASTRUMWEAVER_X=1 /etc/astrumweaver AstrumWeaver astrumweaver-worker {STORE}/bin/astrumweaver-llama-server"
    out = rename_text(text)
    assert out.startswith("TSUMGI_X=1 /etc/tsumgi TSUMGI tsumgi-worker ")
    assert STORE + "/bin/astrumweaver-llama-server" in out


def test_plan_changes_nothing_and_lists_the_work(host):
    before = sorted(str(p) for p in host.rglob("*"))
    plan = migration(host).plan()
    assert sorted(str(p) for p in host.rglob("*")) == before
    assert plan.units == ["astrumweaver-worker.service"]
    assert plan.users == [("astrumweaver", "tsumgi")]
    assert ("astrumweaver-config", "tsumgi-config") in plan.groups
    assert {a.relative_to(host).as_posix() for a, _ in plan.moves} >= {"etc/astrumweaver", "var/lib/astrumweaver", "opt/astrumweaver", "usr/local/libexec/astrumweaver"}
    assert not plan.blockers


def test_apply_renames_everything_and_keeps_data(host, tmp_path):
    run = Recorder()
    plan = migration(host, runner=run).apply(tmp_path / "backup")
    assert not (host / "etc/astrumweaver").exists() and (host / "etc/tsumgi/gpu-uuids").read_text() == "GPU-0000\n"
    assert (host / "var/lib/tsumgi/models/x.gguf").read_text() == "weights"
    assert (host / "opt/tsumgi/strata/bin/strata").read_text() == "binary"
    # secrets keep their values; only the variable name changes
    assert (host / "etc/tsumgi/worker.env").read_text() == "TSUMGI_WORKER_TOKEN=s3cret-value\n"
    assert '"/opt/tsumgi/strata/bin/strata"' in (host / "etc/tsumgi/p/runtime.json").read_text()
    assert 'manifest = "/etc/tsumgi/p/runtime.json"' in (host / "etc/tsumgi/p/worker.toml").read_text()
    # helpers are replaced, not edited
    assert (host / "usr/local/libexec/tsumgi/gpu-preflight").read_text() == "new helper TSUMGI_NVIDIA_SMI\n"
    unit = (host / "etc/systemd/system/tsumgi-worker.service").read_text()
    assert "User=tsumgi" in unit and "StateDirectory=tsumgi" in unit and "/etc/tsumgi/worker.env" in unit
    dropin = (host / "etc/systemd/system/tsumgi-worker.service.d/70-strata.conf").read_text()
    assert "ExecStart=/nix/var/nix/profiles/tsumgi-v030-installer/bin/tsumgi-worker --config /etc/tsumgi/p/worker.toml" in dropin
    assert "/usr/local/libexec/tsumgi/gpu-preflight /etc/tsumgi/gpu-uuids" in dropin
    assert not (host / "etc/systemd/system/astrumweaver-worker.service").exists()
    assert not plan.warnings
    calls = [" ".join(c) for c in run.calls]
    assert calls.index("systemctl stop astrumweaver-worker.service") < calls.index("groupmod -n tsumgi astrumweaver")
    assert "usermod -l tsumgi astrumweaver" in calls and "usermod -d /var/lib/tsumgi tsumgi" in calls
    assert calls[-2:] == ["systemctl enable tsumgi-worker.service", "systemctl start tsumgi-worker.service"]
    assert calls.index("systemctl daemon-reload") < calls.index("systemctl start tsumgi-worker.service")


def test_rollback_script_exists_before_changes_and_restores_the_files(host, tmp_path):
    backup = tmp_path / "backup"
    original = {p.relative_to(host).as_posix(): p.read_text() for p in host.rglob("*") if p.is_file()}
    migration(host, runner=Recorder()).apply(backup)
    script = (backup / "rollback.sh").read_text()
    for needle in ("systemctl stop tsumgi-worker.service", "usermod -l astrumweaver tsumgi",
                   "groupmod -n astrumweaver tsumgi", "systemctl start astrumweaver-worker.service"):
        assert needle in script
    assert os.access(backup / "rollback.sh", os.X_OK)
    # The rollback restores contents from the backup copies (the host-level
    # commands are covered by the script text above).
    for rel in ("etc/systemd/system/astrumweaver-worker.service", "etc/astrumweaver/p/worker.toml"):
        assert (backup / "files" / rel).read_text() == original[rel]
    assert (backup / "libexec-old/gpu-preflight").read_text() == original["usr/local/libexec/astrumweaver/gpu-preflight"]


def test_a_disabled_unit_stays_disabled(host, tmp_path):
    run = Recorder(enabled=False)
    migration(host, runner=run).apply(tmp_path / "b")
    calls = [" ".join(c) for c in run.calls]
    assert not any(c.startswith("systemctl enable") or c.startswith("systemctl disable") for c in calls)


def test_no_start_leaves_the_new_unit_stopped(host, tmp_path):
    run = Recorder()
    migration(host, runner=run).apply(tmp_path / "b", start=False)
    assert not any(c[:2] == ["systemctl", "start"] for c in run.calls)


def test_pinned_identities_block_the_migration_without_touching_anything(host, tmp_path):
    write(host, "etc/astrumweaver/runtime-deployment.json", json.dumps({"executable": STORE + "/bin/astrumweaver-llama-server"}))
    plan = migration(host).plan()
    assert plan.repin and any("re-pin" in b for b in plan.blockers)
    run = Recorder()
    with pytest.raises(MigrationError, match="re-pin"):
        migration(host, runner=run).apply(tmp_path / "b")
    assert run.calls == [] and (host / "etc/astrumweaver").exists()
    write(host, "etc/astrumweaver/runtime-deployment.json", "{}")
    write(host, "etc/astrumweaver/serving-deployment.json", '{"operation_schema": "astrumweaver-decisions-v1"}')
    with pytest.raises(MigrationError, match="re-pin"):
        migration(host, runner=run).apply(tmp_path / "b2")


def test_existing_targets_block_the_migration(host, tmp_path):
    (host / "etc/tsumgi").mkdir()
    with pytest.raises(MigrationError, match="already exists"):
        migration(host, runner=Recorder()).apply(tmp_path / "b")
    (host / "etc/tsumgi").rmdir()
    write(host, "etc/passwd", (host / "etc/passwd").read_text() + "tsumgi:x:1000:1000::/home/t:/bin/sh\n")
    with pytest.raises(MigrationError, match="already exists"):
        migration(host, runner=Recorder()).apply(tmp_path / "b2")


def test_helpers_are_required_when_old_helpers_exist(host, tmp_path):
    with pytest.raises(MigrationError, match="helpers-from"):
        HostMigration(host, "worker", "tsumgi-v030-installer", runner=Recorder()).apply(tmp_path / "b")


def test_a_failing_command_points_at_the_rollback(host, tmp_path):
    run = Recorder(fail_on=("usermod", "-l"))
    with pytest.raises(MigrationError, match="rollback.sh"):
        migration(host, runner=run).apply(tmp_path / "b")
    assert (tmp_path / "b" / "rollback.sh").is_file()


def test_apply_refuses_a_used_backup_directory(host, tmp_path):
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "x").write_text("old")
    with pytest.raises(MigrationError, match="not empty"):
        migration(host, runner=Recorder()).apply(tmp_path / "b")


def test_a_host_without_the_role_unit_is_refused(tmp_path):
    with pytest.raises(MigrationError, match="no worker unit"):
        HostMigration(tmp_path, "worker", "tsumgi-v030-installer").plan()


def test_invalid_role_and_profile_are_refused(host):
    with pytest.raises(MigrationError):
        HostMigration(host, "database", "tsumgi-v030-installer")
    for bad in ("", "../x", "a/b", "name with space"):
        with pytest.raises(MigrationError):
            HostMigration(host, "worker", bad)


def test_control_role_migrates_the_control_unit_and_state(tmp_path):
    root = tmp_path / "c"
    write(root, "etc/passwd", "astrumweaver-control:x:996:996::/var/lib/astrumweaver-control:/bin/false\n")
    write(root, "etc/group", "astrumweaver-control:x:996:\nastrumweaver-config:x:995:\n")
    write(root, "etc/systemd/system/astrumweaver-control.service", "[Service]\nUser=astrumweaver-control\nStateDirectory=astrumweaver-control\n")
    write(root, "etc/systemd/system/astrumweaver-control.service.d/71.conf",
          "[Service]\nExecStart=/nix/var/nix/profiles/tsumgi-v021-control/bin/astrumweaver-control --config /etc/astrumweaver/control.toml\n")
    write(root, "etc/astrumweaver/control.env", "ASTRUMWEAVER_DATABASE_URL=postgresql://u:p@db/astrumweaver\n")
    write(root, "var/lib/astrumweaver-control/admin-credentials.json", '{"username": "ops"}')
    run = Recorder()
    HostMigration(root, "control", "tsumgi-v030-control", runner=run).apply(tmp_path / "b")
    assert (root / "var/lib/tsumgi-control/admin-credentials.json").is_file()
    assert (root / "etc/tsumgi/control.env").read_text() == "TSUMGI_DATABASE_URL=postgresql://u:p@db/tsumgi\n"
    assert "tsumgi-v030-control/bin/tsumgi-control --config /etc/tsumgi/control.toml" in (
        root / "etc/systemd/system/tsumgi-control.service.d/71.conf").read_text()
    assert ["usermod", "-l", "tsumgi-control", "astrumweaver-control"] in run.calls


def test_scripts_in_usr_local_sbin_are_reported_not_edited(host):
    write(host, "usr/local/sbin/tsumgi-gpu-mode", "systemctl stop astrumweaver-worker\n")
    plan = migration(host).plan()
    assert any("tsumgi-gpu-mode" in w for w in plan.warnings)
    assert (host / "usr/local/sbin/tsumgi-gpu-mode").read_text() == "systemctl stop astrumweaver-worker\n"


def test_cli_plan_and_blocked_exit_codes(host, capsys):
    helpers = host.parent / (host.name + "-helpers")
    args = ["plan", "--role", "worker", "--profile", "tsumgi-v030-installer", "--root", str(host), "--helpers-from", str(helpers)]
    assert main(args) == 0 and "rewrite" in capsys.readouterr().out
    write(host, "etc/astrumweaver/serving-deployment.json", '{"operation_schema": "astrumweaver-decisions-v1"}')
    assert main(args) == 3
    assert main(["rollback", "--backup-dir", str(host.parent / "none")]) == 2


def test_cli_apply_on_a_rehearsal_root_does_not_run_system_commands(host, tmp_path, capsys):
    helpers = host.parent / (host.name + "-helpers")
    code = main(["apply", "--role", "worker", "--profile", "tsumgi-v030-installer", "--root", str(host),
                 "--helpers-from", str(helpers), "--backup-dir", str(tmp_path / "bk")])
    err = capsys.readouterr().err
    assert code == 0 and "[skipped, --root is not /] systemctl stop" in err
    assert (host / "etc/tsumgi/gpu-uuids").is_file()


def _snapshot(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): p.read_text() for p in sorted(root.rglob("*")) if p.is_file()}


def test_rollback_script_restores_the_exact_tree(host, tmp_path):
    import subprocess

    bin_dir = tmp_path / "stubs"
    bin_dir.mkdir()
    log = tmp_path / "stub.log"
    for name in ("systemctl", "usermod", "groupmod", "id", "getent"):
        stub = bin_dir / name
        stub.write_text(f'#!/bin/sh\necho "{name} $*" >> {log}\nexit 0\n')
        stub.chmod(0o755)
    before = _snapshot(host)
    backup = tmp_path / "backup"
    migration(host, runner=Recorder()).apply(backup)
    assert _snapshot(host) != before
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    done = subprocess.run(["sh", str(backup / "rollback.sh")], env=env, capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    assert _snapshot(host) == before
    assert not (host / "etc/tsumgi").exists() and not (host / "etc/systemd/system/tsumgi-worker.service").exists()
    stubbed = log.read_text()
    assert "usermod -l astrumweaver tsumgi" in stubbed and "systemctl start astrumweaver-worker.service" in stubbed
    # Running it a second time changes nothing.
    again = subprocess.run(["sh", str(backup / "rollback.sh")], env=env, capture_output=True, text=True, check=False)
    assert again.returncode == 0 and _snapshot(host) == before
