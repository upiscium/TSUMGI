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
    write(root, "nix/var/nix/profiles/tsumgi-v030-installer/bin/tsumgi-worker", "#!/bin/sh\n")
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
    kw.setdefault("live", True)
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
    write(host, "etc/astrumweaver/q/worker.toml", "[runtime]\nmanifest = \"/etc/astrumweaver/runtime-deployment.json\"\n")
    plan = migration(host).plan()
    assert plan.repin and any("re-pin" in b for b in plan.blockers)
    run = Recorder()
    with pytest.raises(MigrationError, match="re-pin"):
        migration(host, runner=run).apply(tmp_path / "b")
    assert run.calls == [] and (host / "etc/astrumweaver").exists()
    write(host, "etc/astrumweaver/runtime-deployment.json", "{}")
    write(host, "etc/astrumweaver/serving-deployment.json", '{"operation_schema": "astrumweaver-decisions-v1"}')
    write(host, "etc/astrumweaver/q/worker.toml", "[x]\nserving = \"/etc/astrumweaver/serving-deployment.json\"\n")
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
        HostMigration(host, "worker", "tsumgi-v030-installer", runner=Recorder(), live=True).apply(tmp_path / "b")


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
    write(root, "nix/var/nix/profiles/tsumgi-v030-control/bin/tsumgi-control", "#!/bin/sh\n")
    HostMigration(root, "control", "tsumgi-v030-control", runner=run, live=True).apply(tmp_path / "b", db_renamed=True)
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
    write(host, "etc/astrumweaver/q/worker.toml", "[x]\nserving = \"/etc/astrumweaver/serving-deployment.json\"\n")
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


# ---- review follow-ups ---------------------------------------------------------------


def _stubs(tmp_path):
    bin_dir = tmp_path / "stubs"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / "stub.log"
    for name in ("systemctl", "usermod", "groupmod", "id", "getent"):
        stub = bin_dir / name
        stub.write_text(f'#!/bin/sh\necho "{name} $*" >> {log}\nexit 0\n')
        stub.chmod(0o755)
    return {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}, log


def _run_script(path: Path, env):
    import subprocess

    return subprocess.run(["sh", str(path)], env=env, capture_output=True, text=True, check=False)


class Selective(Recorder):
    def __init__(self, inactive=(), **kw):
        super().__init__(**kw)
        self.inactive = set(inactive)

    def __call__(self, argv):
        if argv[:2] == ["systemctl", "is-active"] and argv[2] in self.inactive:
            self.calls.append(list(argv))
            return CommandResult(3)
        return super().__call__(argv)


def test_a_unit_that_was_stopped_stays_stopped_in_apply_and_rollback(host, tmp_path):
    run = Selective(inactive={"astrumweaver-worker.service"})
    plan = migration(host, runner=run).apply(tmp_path / "b")
    assert not any(c[:2] == ["systemctl", "start"] for c in run.calls)
    assert any("was not running" in w for w in plan.warnings)
    assert "systemctl start" not in (tmp_path / "b" / "rollback.sh").read_text()


def test_control_is_held_until_the_database_is_renamed(tmp_path):
    root = tmp_path / "c"
    write(root, "etc/passwd", "astrumweaver-control:x:996:996::/var/lib/astrumweaver-control:/bin/false\n")
    write(root, "etc/group", "astrumweaver-control:x:996:\n")
    write(root, "etc/systemd/system/astrumweaver-control.service", "[Service]\nUser=astrumweaver-control\n")
    write(root, "etc/astrumweaver/control.env",
          "TSUMGI_DATABASE_URL=postgresql://astrumweaver:pw-astrumweaver@db/astrumweaver\n".replace("TSUMGI", "ASTRUMWEAVER"))
    write(root, "nix/var/nix/profiles/tsumgi-v030-control/bin/tsumgi-control", "#!/bin/sh\n")
    run = Recorder()
    plan = HostMigration(root, "control", "tsumgi-v030-control", runner=run, live=True).apply(tmp_path / "b")
    assert not any(c[:2] == ["systemctl", "start"] for c in run.calls)
    assert any("Control was NOT started" in w for w in plan.warnings)
    # the credentials in the URL keep their names; only the database name changes
    assert (root / "etc/tsumgi/control.env").read_text() == "TSUMGI_DATABASE_URL=postgresql://astrumweaver:pw-astrumweaver@db/tsumgi\n"


def test_rename_text_keeps_url_credentials():
    assert rename_text("postgresql://astrumweaver:p@db/astrumweaver") == "postgresql://astrumweaver:p@db/tsumgi"
    assert rename_text("see https://example.invalid/astrumweaver") == "see https://example.invalid/tsumgi"


def test_relative_paths_become_absolute_in_the_rollback_script(host, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    migration(host, runner=Recorder()).apply(Path("rel-backup"))
    script = (tmp_path / "rel-backup" / "rollback.sh").read_text()
    assert str(tmp_path / "rel-backup") in script and " rel-backup/" not in script
    assert oct((tmp_path / "rel-backup").stat().st_mode & 0o777) == "0o700"


def test_a_rehearsal_root_never_gets_system_commands_in_its_rollback(host, tmp_path):
    host2 = host
    (host / "nix/var/nix/profiles/tsumgi-v030-installer/bin/tsumgi-worker").unlink()
    helpers = host.parent / (host.name + "-helpers")
    run = Recorder()
    m = HostMigration(host2, "worker", "tsumgi-v030-installer", helpers_from=helpers, runner=run)  # live defaults to False
    plan = m.plan()
    assert any("binary is missing" in w for w in plan.warnings) and not plan.blockers
    m.apply(tmp_path / "b")
    script = (tmp_path / "b" / "rollback.sh").read_text()
    for forbidden in ("systemctl", "usermod", "groupmod", "getent", "id "):
        assert forbidden not in script.replace("rehearsal root: only files are restored; no system command is run", "")
    assert "rehearsal root" in script


def test_a_missing_new_binary_blocks_a_live_host(host):
    (host / "nix/var/nix/profiles/tsumgi-v030-installer/bin/tsumgi-worker").unlink()
    assert any("binary is missing" in b for b in migration(host).plan().blockers)


def test_files_that_cannot_be_rewritten_block_when_they_mention_the_old_name(host):
    (host / "etc/astrumweaver/blob.bin").write_bytes(b"\xff\xfe ASTRUMWEAVER \xff")
    assert any("blob.bin" in b and "cannot be rewritten" in b for b in migration(host).plan().blockers)
    (host / "etc/astrumweaver/blob.bin").write_bytes(b"\xff\xfe clean \xff")
    (host / "etc/astrumweaver/big.txt").write_text("astrumweaver " + "x" * (2 * 1024 * 1024))
    assert any("big.txt" in b for b in migration(host).plan().blockers)


def test_other_extensions_are_rewritten_too(host, tmp_path):
    write(host, "etc/astrumweaver/extra.yaml", "path: /etc/astrumweaver/x\n")
    migration(host, runner=Recorder()).apply(tmp_path / "b")
    assert (host / "etc/tsumgi/extra.yaml").read_text() == "path: /etc/tsumgi/x\n"


def test_symlinks_that_cannot_be_retargeted_are_reported(host):
    (host / "usr/local/bin").mkdir(parents=True)
    (host / "usr/local/bin/strata").symlink_to("../../../opt/astrumweaver/strata/bin/strata")  # relative
    (host / "var/lib/astrumweaver/link").symlink_to("/nix/store/" + "c" * 32 + "-astrumweaver-thing")  # store path
    plan = migration(host).plan()
    assert sum("cannot be retargeted" in w for w in plan.warnings) == 2 and not plan.symlinks


def test_the_other_role_on_the_same_host_blocks(host):
    write(host, "etc/systemd/system/astrumweaver-control.service", "[Service]\n")
    assert any("control unit is installed on this host too" in b for b in migration(host).plan().blockers)


def test_a_half_migrated_host_blocks_until_rolled_back(host):
    write(host, "etc/group", (host / "etc/group").read_text() + "tsumgi-config:x:997:\n")
    host2 = host
    (host2 / "etc/group").write_text("root:x:0:\ntsumgi-config:x:998:\n")  # old group gone, new present
    blockers = migration(host2).plan().blockers
    assert any("tsumgi-config exists but astrumweaver-config does not" in b for b in blockers)


def test_a_symlinked_or_odd_unit_is_refused(host):
    target = host / "etc/systemd/system/real.service"
    (host / "etc/systemd/system/astrumweaver-worker.service").rename(target)
    (host / "etc/systemd/system/astrumweaver-worker.service").symlink_to(target)
    assert any("is a symlink" in b for b in migration(host).plan().blockers)


def test_mount_points_block(host, monkeypatch):
    monkeypatch.setattr(os.path, "ismount", lambda p: str(p).endswith("opt/astrumweaver"))
    assert any("mount point" in b for b in migration(host).plan().blockers)


def test_unhandled_files_in_other_units_are_reported(host):
    write(host, "etc/systemd/system/backup.timer", "[Timer]\nUnit=astrumweaver-worker.service\n")
    assert any("backup.timer" in w for w in migration(host).plan().warnings)


def test_the_original_home_is_recorded_and_restored(host, tmp_path):
    write(host, "etc/passwd", "astrumweaver:x:999:999::/nonexistent:/usr/sbin/nologin\n")
    run = Recorder()
    migration(host, runner=run).apply(tmp_path / "b")
    assert not any(c[:2] == ["usermod", "-d"] for c in run.calls)
    assert "usermod -d" not in (tmp_path / "b" / "rollback.sh").read_text()


def _partial_failure_roundtrip(host, tmp_path, *, break_at):
    env, log = _stubs(tmp_path)
    helpers_dot = host.parent / (host.name + "-helpers")
    write(helpers_dot, ".hidden", "dot")
    (host / "usr/local/libexec/astrumweaver/.keep").write_text("old dotfile")
    (host / "usr/local/libexec/astrumweaver/sub").mkdir()
    (host / "usr/local/libexec/astrumweaver/sub/x").write_text("old sub")
    before = _snapshot(host)
    backup = tmp_path / "backup"
    with pytest.raises(MigrationError, match="rollback.sh"):
        break_at(migration(host, runner=Recorder(fail_on=None)), backup)
    assert (backup / "rollback.sh").is_file()
    done = _run_script(backup / "rollback.sh", env)
    assert done.returncode == 0, done.stderr
    assert _snapshot(host) == before
    again = _run_script(backup / "rollback.sh", env)
    assert again.returncode == 0 and _snapshot(host) == before


def test_rollback_after_a_failure_in_the_middle_of_the_moves(host, tmp_path, monkeypatch):
    real_rename = os.rename

    def failing(src, dst, *a, **k):
        if str(dst).endswith("var/lib/tsumgi"):
            raise OSError(18, "Invalid cross-device link")
        return real_rename(src, dst, *a, **k)

    def run(m, backup):
        monkeypatch.setattr(os, "rename", failing)
        try:
            m.apply(backup)
        finally:
            monkeypatch.setattr(os, "rename", real_rename)

    _partial_failure_roundtrip(host, tmp_path, break_at=run)


def test_rollback_after_a_failure_after_the_user_rename(host, tmp_path):
    def run(m, backup):
        m.run = Recorder(fail_on=("groupmod", "-n"))
        m.apply(backup)

    _partial_failure_roundtrip(host, tmp_path, break_at=run)


def test_rollback_after_a_failure_when_the_units_are_renamed(host, tmp_path, monkeypatch):
    def run(m, backup):
        m.run = Recorder(fail_on=("systemctl", "daemon-reload"))
        m.apply(backup)

    _partial_failure_roundtrip(host, tmp_path, break_at=run)


def test_a_broken_backup_makes_the_rollback_exit_non_zero(host, tmp_path):
    env, _ = _stubs(tmp_path)
    backup = tmp_path / "b"
    migration(host, runner=Recorder()).apply(backup)
    for item in (backup / "files").rglob("worker.env"):
        item.unlink()
    done = _run_script(backup / "rollback.sh", env)
    assert done.returncode == 1 and "could not restore" in done.stderr


def test_old_helpers_with_subdirectories_and_dotfiles_are_replaced_and_restored(host, tmp_path):
    (host / "usr/local/libexec/astrumweaver/.keep").write_text("old dotfile")
    (host / "usr/local/libexec/astrumweaver/sub").mkdir()
    (host / "usr/local/libexec/astrumweaver/sub/x").write_text("old sub")
    migration(host, runner=Recorder()).apply(tmp_path / "b")
    names = sorted(p.name for p in (host / "usr/local/libexec/tsumgi").iterdir())
    assert names == ["gpu-preflight"]


# ---- findings from the stage 5 plan on real hosts ------------------------------------------


def _full_snapshot(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if p.is_symlink():
            out[rel] = "-> " + os.readlink(p)
        elif p.is_file():
            out[rel] = p.read_text()
        else:
            out[rel] = "<dir>"
    return out


def add_strata_links(host: Path) -> None:
    write(host, "opt/astrumweaver/strata/data/terminal/0.1/bin/strata", "binary")
    (host / "opt/astrumweaver/strata/data/terminal/current").symlink_to("/opt/astrumweaver/strata/data/terminal/0.1")
    (host / "opt/astrumweaver/strata/bin").mkdir(exist_ok=True)
    for stale in (host / "opt/astrumweaver/strata/bin/strata",):
        if stale.exists():
            stale.unlink()
    (host / "opt/astrumweaver/strata/bin/strata").symlink_to("/opt/astrumweaver/strata/data/terminal/current/bin/strata")


def add_profile(host: Path) -> None:
    prof = host / "nix/var/nix/profiles"
    prof.mkdir(parents=True, exist_ok=True)
    (prof / "astrumweaver-runtime-qwen38-3-link").symlink_to("/nix/store/" + "b" * 32 + "-profile")
    (prof / "astrumweaver-runtime-qwen38").symlink_to("astrumweaver-runtime-qwen38-3-link")
    (host / "usr/local/bin").mkdir(parents=True, exist_ok=True)
    (host / "usr/local/bin/llama-server").symlink_to("/nix/var/nix/profiles/astrumweaver-runtime-qwen38/bin/llama-server")


def test_absolute_links_into_moved_trees_and_profiles_are_retargeted(host, tmp_path):
    add_strata_links(host)
    add_profile(host)
    plan = migration(host).plan()
    assert len(plan.symlinks) == 3 and len(plan.profiles) == 2 and not plan.blockers
    migration(host, runner=Recorder()).apply(tmp_path / "b")
    link = host / "opt/tsumgi/strata/bin/strata"
    assert os.readlink(link) == "/opt/tsumgi/strata/data/terminal/current/bin/strata"
    assert os.readlink(host / "opt/tsumgi/strata/data/terminal/current") == "/opt/tsumgi/strata/data/terminal/0.1"
    assert os.readlink(host / "usr/local/bin/llama-server") == "/nix/var/nix/profiles/tsumgi-runtime-qwen38/bin/llama-server"
    prof = host / "nix/var/nix/profiles"
    assert os.readlink(prof / "tsumgi-runtime-qwen38") == "tsumgi-runtime-qwen38-3-link"
    assert os.readlink(prof / "tsumgi-runtime-qwen38-3-link").startswith("/nix/store/")
    assert not (prof / "astrumweaver-runtime-qwen38").is_symlink()


def test_rollback_restores_links_and_profiles_exactly(host, tmp_path):
    add_strata_links(host)
    add_profile(host)
    env, _ = _stubs(tmp_path)
    before = _full_snapshot(host)
    backup = tmp_path / "backup"
    migration(host, runner=Recorder()).apply(backup)
    assert _full_snapshot(host) != before
    done = _run_script(backup / "rollback.sh", env)
    assert done.returncode == 0, done.stderr
    assert _full_snapshot(host) == before
    assert _run_script(backup / "rollback.sh", env).returncode == 0
    assert _full_snapshot(host) == before


def test_an_existing_new_profile_name_blocks(host):
    add_profile(host)
    (host / "nix/var/nix/profiles/tsumgi-runtime-qwen38").symlink_to("x")
    assert any("tsumgi-runtime-qwen38" in b for b in migration(host).plan().blockers)


def test_rollback_copies_are_neither_rewritten_nor_checked(host, tmp_path):
    write(host, "etc/astrumweaver/rollback/70.conf.old", "ExecStart=/x/bin/astrumweaver-worker\n")
    write(host, "etc/astrumweaver/rollback/pinned.json", '{"p": "' + STORE + '/bin/x"}')
    plan = migration(host).plan()
    assert not plan.blockers and not any("rollback" in p.relative_to(host).parts for p in plan.rewrites)
    migration(host, runner=Recorder()).apply(tmp_path / "b")
    assert (host / "etc/tsumgi/rollback/70.conf.old").read_text() == "ExecStart=/x/bin/astrumweaver-worker\n"


def test_a_pinned_file_that_nothing_refers_to_only_warns(host, tmp_path):
    write(host, "etc/astrumweaver/tsumgi-rc1-runtime.json", json.dumps({"executable": STORE + "/bin/x"}))
    plan = migration(host).plan()
    assert not plan.blockers and any("nothing refers to it" in w for w in plan.warnings)
    migration(host, runner=Recorder()).apply(tmp_path / "b")
    assert STORE in (host / "etc/tsumgi/tsumgi-rc1-runtime.json").read_text()


def test_a_pinned_file_that_the_config_refers_to_blocks(host):
    write(host, "etc/astrumweaver/p/runtime.json", json.dumps({"executable": STORE + "/bin/x"}))
    assert any("re-pin" in b and "runtime.json" in b for b in migration(host).plan().blockers)


def test_a_reset_base_exec_start_does_not_count_as_a_reference(host, tmp_path):
    write(host, "etc/astrumweaver/tsumgi-rc1-runtime.json", json.dumps({"executable": STORE + "/bin/x"}))
    unit = host / "etc/systemd/system/astrumweaver-worker.service"
    unit.write_text(unit.read_text() + "ExecStart=/x/bin/astrumweaver-worker --runtime-manifest /etc/astrumweaver/tsumgi-rc1-runtime.json\n")
    plan = migration(host).plan()  # the drop-in already resets ExecStart
    assert not plan.blockers and any("nothing refers to it" in w for w in plan.warnings)


def test_an_unreset_base_exec_start_still_counts_as_a_reference(host):
    write(host, "etc/astrumweaver/tsumgi-rc1-runtime.json", json.dumps({"executable": STORE + "/bin/x"}))
    (host / "etc/systemd/system/astrumweaver-worker.service.d/70-strata.conf").write_text(
        "[Service]\nExecStartPre=+/usr/local/libexec/astrumweaver/gpu-preflight /etc/astrumweaver/gpu-uuids\n")
    unit = host / "etc/systemd/system/astrumweaver-worker.service"
    unit.write_text(unit.read_text() + "ExecStart=/x/bin/astrumweaver-worker --runtime-manifest /etc/astrumweaver/tsumgi-rc1-runtime.json\n")
    assert any("tsumgi-rc1-runtime.json" in b for b in migration(host).plan().blockers)


def test_the_backup_keeps_the_owner_so_a_rollback_restores_it(host, tmp_path, monkeypatch):
    """Found on a real host: root:group 0640 files came back root:root and the old Worker could not read them."""
    calls: list[tuple[str, int, int]] = []
    real_chown = os.chown

    def spy(path, uid, gid, *a, **k):
        calls.append((str(path), uid, gid))
        return real_chown(path, uid, gid, *a, **k)

    monkeypatch.setattr(os, "chown", spy)
    worker_toml = host / "etc/astrumweaver/p/worker.toml"
    info = worker_toml.stat()
    migration(host, runner=Recorder()).apply(tmp_path / "b")
    backed_up = [c for c in calls if c[0].endswith("files/etc/astrumweaver/p/worker.toml")]
    assert backed_up == [(str(tmp_path / "b/files/etc/astrumweaver/p/worker.toml"), info.st_uid, info.st_gid)]
    # every backed-up file, units included, had its owner set
    assert len(calls) >= len(list((tmp_path / "b/files").rglob("*.*"))) - 2
    # and the rollback restores with cp -p, which uses that owner
    assert "cp -p" in (tmp_path / "b/rollback.sh").read_text()


def test_allow_pinned_lets_the_operator_move_a_stale_referenced_file(host, tmp_path):
    write(host, "etc/astrumweaver/runtime-deployment.json", json.dumps({"executable": STORE + "/bin/x"}))
    write(host, "etc/astrumweaver/stale-worker.toml", '[runtime]\nmanifest = "/etc/astrumweaver/runtime-deployment.json"\n')
    assert any("runtime-deployment.json" in b for b in migration(host).plan().blockers)
    allowed = migration(host, allow_pinned=["/etc/astrumweaver/runtime-deployment.json"])
    plan = allowed.plan()
    assert not plan.blockers and any("allowed by the operator" in w for w in plan.warnings)
    allowed.run = Recorder()
    allowed.apply(tmp_path / "b")
    assert STORE in (host / "etc/tsumgi/runtime-deployment.json").read_text()


def test_allow_pinned_never_covers_a_unit_file(host):
    unit = host / "etc/systemd/system/astrumweaver-worker.service"
    unit.write_text(unit.read_text() + f"ExecStart={STORE}/bin/x\n")
    path = "/etc/systemd/system/astrumweaver-worker.service"
    assert migration(host, allow_pinned=[path]).plan().blockers


def test_cli_allow_pinned_flag(host, capsys):
    helpers = host.parent / (host.name + "-helpers")
    write(host, "etc/astrumweaver/runtime-deployment.json", json.dumps({"executable": STORE + "/bin/x"}))
    write(host, "etc/astrumweaver/stale.toml", 'm = "/etc/astrumweaver/runtime-deployment.json"\n')
    base = ["plan", "--role", "worker", "--profile", "tsumgi-v030-installer", "--root", str(host), "--helpers-from", str(helpers)]
    assert main(base) == 3
    assert main(base + ["--allow-pinned", "/etc/astrumweaver/runtime-deployment.json"]) == 0
