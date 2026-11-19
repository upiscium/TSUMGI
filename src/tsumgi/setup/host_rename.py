"""Move one installed host from the pre-0.3 ``astrumweaver`` names to ``tsumgi``.

``plan`` inspects the host and changes nothing. ``apply`` writes a rollback
script and a backup first, then stops the units, renames the service users
and groups, moves the directories, rewrites names inside the configuration
files and units, and starts the new units. ``rollback`` is a plain shell
script that restores the old names without needing this tool or the new build.

Everything that touches the host goes through ``HostMigration`` with an
explicit ``root`` and an injected command runner, so tests run against a
temporary tree and a recording runner. The tool never edits a file that carries
a pinned identity (a Nix store path or the decision operation schema): those
need a re-pin with the new build, and ``plan`` refuses until that is done.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

OLD = "astrumweaver"
NEW = "tsumgi"

# (old path, new path) relative to the host root; moved only when present.
DIRECTORY_MOVES: tuple[tuple[str, str], ...] = (
    ("etc/astrumweaver", "etc/tsumgi"),
    ("var/lib/astrumweaver", "var/lib/tsumgi"),
    ("var/lib/astrumweaver-control", "var/lib/tsumgi-control"),
    ("opt/astrumweaver", "opt/tsumgi"),
    ("var/cache/astrumweaver", "var/cache/tsumgi"),
)
LIBEXEC_OLD = "usr/local/libexec/astrumweaver"
LIBEXEC_NEW = "usr/local/libexec/tsumgi"

UNITS = {
    "worker": ("astrumweaver-worker.service", "astrumweaver-worker-gpu-isolation-preflight.service"),
    "control": ("astrumweaver-control.service",),
}
USERS = (("astrumweaver", "tsumgi"), ("astrumweaver-control", "tsumgi-control"))
GROUPS = (
    ("astrumweaver", "tsumgi"),
    ("astrumweaver-control", "tsumgi-control"),
    ("astrumweaver-config", "tsumgi-config"),
)

MAX_TEXT_BYTES = 2 * 1024 * 1024
_PROTECTED = re.compile(r"/nix/store/[0-9a-z]{32}-[^\s\"',;]*|(?<=://)[^/\s@\"']*@")
_PINNED = re.compile(r"/nix/store/[0-9a-z]{32}-[^\s\"',;]*astrumweaver[^\s\"',;]*|astrumweaver-decisions-v1", re.I)
_EXEC_BIN = re.compile(r"(/nix/var/nix/profiles/)[^/\s]+(/bin/)astrumweaver-")

Runner = Callable[[Sequence[str]], "CommandResult"]


class MigrationError(RuntimeError):
    """The host cannot be migrated as it is; nothing was changed."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int = 0
    stdout: str = ""


def run_command(argv: Sequence[str]) -> CommandResult:
    done = subprocess.run(list(argv), capture_output=True, text=True, check=False)
    return CommandResult(done.returncode, done.stdout)


def rename_text(text: str) -> str:
    """Rename identifiers in a config or unit file.

    Nix store paths (immutable) and the user-info part of a URL (credentials:
    the database role keeps its name) are left alone. Values such as the
    database name in a URL path are renamed on purpose.
    """
    protected: list[str] = []

    def keep(match: re.Match[str]) -> str:
        protected.append(match.group(0))
        return f"\x00{len(protected) - 1}\x00"

    text = _PROTECTED.sub(keep, text)
    text = text.replace("ASTRUMWEAVER_", "TSUMGI_").replace("AstrumWeaver", "TSUMGI").replace(OLD, NEW)
    return re.sub(r"\x00(\d+)\x00", lambda m: protected[int(m.group(1))], text)


@dataclass
class Plan:
    role: str
    root: Path
    profile: str
    live: bool = True
    units: list[str] = field(default_factory=list)
    unit_files: list[Path] = field(default_factory=list)
    users: list[tuple[str, str]] = field(default_factory=list)
    groups: list[tuple[str, str]] = field(default_factory=list)
    moves: list[tuple[Path, Path]] = field(default_factory=list)
    rewrites: list[Path] = field(default_factory=list)
    requirements: list[str] = field(default_factory=list)
    helpers_from: Path | None = None
    repin: list[Path] = field(default_factory=list)
    collisions: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    homes: dict[str, str] = field(default_factory=dict)
    profiles: list[tuple[Path, Path, str]] = field(default_factory=list)  # Nix profile links: old, new, old link target
    symlinks: list[tuple[Path, str, str]] = field(default_factory=list)  # path (old location), old target, new target

    @property
    def blockers(self) -> list[str]:
        out = list(self.requirements)
        out += [f"target already exists: {p}" for p in self.collisions]
        out += [f"needs a re-pin with the new build before migrating: {p}" for p in self.repin]
        return out

    def describe(self) -> str:
        rel = lambda p: "/" + str(p.relative_to(self.root)) if p.is_relative_to(self.root) else str(p)  # noqa: E731
        lines = [f"role: {self.role}", f"new profile: /nix/var/nix/profiles/{self.profile}", "units: " + ", ".join(self.units)]
        lines += [f"user  {a} -> {b}" for a, b in self.users]
        lines += [f"group {a} -> {b}" for a, b in self.groups]
        lines += [f"move  {rel(a)} -> {rel(b)}" for a, b in self.moves]
        lines += [f"profile {rel(a)} -> {rel(b)}" for a, b, _ in self.profiles]
        lines += [f"retarget {rel(p)}: {o} -> {n}" for p, o, n in self.symlinks]
        lines += [f"rewrite {rel(p)}" for p in self.rewrites]
        for warning in self.warnings:
            lines.append(f"warning: {warning}")
        for blocker in self.blockers:
            lines.append(f"BLOCKED: {blocker}")
        return "\n".join(lines)


def _read_text(path: Path) -> str | None:
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_TEXT_BYTES:
            return None
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def _mentions_old_name(path: Path) -> bool:
    """Byte-level check for files the text rules cannot read (binary, huge)."""
    try:
        if not path.is_file() or path.is_symlink():
            return False
        needle = OLD.encode()
        tail = b""
        with path.open("rb") as handle:
            while chunk := handle.read(1 << 20):
                if needle in (tail + chunk).lower():
                    return True
                tail = chunk[-len(needle):]
    except OSError:
        return True  # unreadable: assume it matters
    return False


def _passwd_entries(root: Path) -> dict[str, list[str]]:
    try:
        lines = (root / "etc/passwd").read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    return {line.split(":")[0]: line.split(":") for line in lines if line and not line.startswith("#")}


def _group_names(root: Path) -> set[str]:
    try:
        lines = (root / "etc/group").read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()
    return {line.split(":")[0] for line in lines if line and not line.startswith("#")}


MAX_SYMLINK_SCAN = 300_000


class HostMigration:
    def __init__(self, root: Path, role: str, profile: str, *, helpers_from: Path | None = None,
                 runner: Runner = run_command, clock: Callable[[], float] = time.time,
                 live: bool | None = None) -> None:
        if role not in UNITS:
            raise MigrationError(f"role must be one of {sorted(UNITS)}")
        if not re.fullmatch(r"[A-Za-z0-9._+-]+", profile) or "/" in profile:
            raise MigrationError("profile must be a Nix profile name such as tsumgi-v030-installer")
        self.root = Path(os.path.realpath(root))
        # `live` decides whether the generated rollback script runs system commands;
        # it is true only for the real root (tests may force it).
        self.live = (self.root == Path("/")) if live is None else live
        self.role = role
        self.profile = profile
        self.helpers_from = Path(os.path.abspath(helpers_from)) if helpers_from else None
        self.run = runner
        self.clock = clock

    # ---- planning -------------------------------------------------------------

    def _unit_dir(self) -> Path:
        return self.root / "etc/systemd/system"

    def plan(self) -> Plan:
        plan = Plan(role=self.role, root=self.root, profile=self.profile, live=self.live,
                    helpers_from=self.helpers_from)
        unit_dir = self._unit_dir()
        for unit in UNITS[self.role]:
            if (unit_dir / unit).exists() or (unit_dir / unit).is_symlink():
                plan.units.append(unit)
        if not plan.units:
            raise MigrationError(f"no {self.role} unit found under {unit_dir}: nothing to migrate")
        for other, names in UNITS.items():
            if other != self.role and any((unit_dir / n).exists() for n in names):
                plan.requirements.append(
                    f"a {other} unit is installed on this host too: its service would break "
                    "after the shared users and directories are renamed; migrate both roles together "
                    "(not supported yet) or move one of them off this host")
        for unit in plan.units:
            path = unit_dir / unit
            if path.is_symlink():
                plan.requirements.append(f"{path} is a symlink (alias or masked unit); resolve it first")
                continue
            plan.unit_files.append(path)
            dropins = unit_dir / f"{unit}.d"
            if dropins.is_dir():
                for item in sorted(dropins.iterdir()):
                    if item.is_symlink() or not item.is_file():
                        plan.requirements.append(f"{item} is not a regular drop-in file")
                    else:
                        plan.unit_files.append(item)

        entries, groups = _passwd_entries(self.root), _group_names(self.root)
        plan.users = [(a, b) for a, b in USERS if a in entries]
        plan.groups = [(a, b) for a, b in GROUPS if a in groups]
        for old, new in plan.users:
            plan.homes[old] = entries[old][5] if len(entries[old]) > 5 else ""
        for _, new in plan.users:
            if new in entries:
                plan.collisions.append(self.root / "etc/passwd")
        for _, new in plan.groups:
            if new in groups:
                plan.collisions.append(self.root / "etc/group")
        # A previous run that stopped half-way leaves new names without old ones.
        for old, new in USERS:
            if new in entries and old not in entries:
                plan.requirements.append(f"user {new} exists but {old} does not: a previous run was interrupted; run its rollback.sh first")
        for old, new in GROUPS:
            if new in groups and old not in groups:
                plan.requirements.append(f"group {new} exists but {old} does not: a previous run was interrupted; run its rollback.sh first")

        moves = list(DIRECTORY_MOVES) + [(LIBEXEC_OLD, LIBEXEC_NEW)]
        for old, new in moves:
            old_path, new_path = self.root / old, self.root / new
            if old_path.exists():
                plan.moves.append((old_path, new_path))
                if new_path.exists():
                    plan.collisions.append(new_path)
                if os.path.ismount(old_path):
                    plan.requirements.append(f"{old_path} is a mount point and cannot be renamed")
            elif new_path.exists():
                plan.requirements.append(f"{new_path} exists but {old_path} does not: a previous run was interrupted; run its rollback.sh first")
        if (self.root / LIBEXEC_OLD).exists() and (self.helpers_from is None or not self.helpers_from.is_dir()):
            plan.requirements.append("libexec helpers exist: pass --helpers-from DIR (the new build's libexec) to replace them")
        for unit in plan.units:
            if (unit_dir / unit.replace(OLD, NEW)).exists():
                plan.collisions.append(unit_dir / unit.replace(OLD, NEW))

        binary = self.root / "nix/var/nix/profiles" / self.profile / "bin" / f"tsumgi-{self.role}"
        if not binary.exists():
            message = f"the new build's binary is missing: {binary}"
            (plan.requirements if self.live else plan.warnings).append(message)

        for path in plan.unit_files:
            self._classify(plan, path, rewrite=False)
        for old, _ in plan.moves:
            if old == self.root / LIBEXEC_OLD or old.parent != self.root / "etc":
                continue
            for path in sorted(old.rglob("*")):
                # Earlier rollback copies describe the old build; they stay as they were.
                if "rollback" in path.relative_to(old).parts[:-1]:
                    continue
                if path.is_file() and not path.is_symlink():
                    self._classify(plan, path, rewrite=True)
        self._demote_unreferenced_pins(plan)
        self._plan_profiles(plan)
        self._scan_outside(plan)
        self._scan_symlinks(plan)
        return plan

    def _demote_unreferenced_pins(self, plan: Plan) -> None:
        """A pinned file that no unit or config points at is a leftover: warn, do not block."""
        blob = "\n".join(filter(None, (self._live_text(plan, p) for p in plan.unit_files + plan.rewrites)))
        keep: list[Path] = []
        for path in plan.repin:
            logical = "/" + path.relative_to(self.root).as_posix() if path.is_relative_to(self.root) else str(path)
            if path in plan.unit_files or logical in blob:
                keep.append(path)
            else:
                plan.warnings.append(f"{logical} carries the old name but nothing refers to it; it is moved unchanged")
        plan.repin = keep

    def _live_text(self, plan: Plan, path: Path) -> str | None:
        """File text, minus base-unit ExecStart lines that a drop-in resets (they never run)."""
        text = _read_text(path)
        if text is None or path.suffix != ".service":
            return text
        reset: set[str] = set()
        for other in plan.unit_files:
            if other.parent.name == path.name + ".d":
                other_text = _read_text(other) or ""
                for key in ("ExecStart", "ExecStartPre", "ExecStartPost"):
                    if re.search(rf"^{key}=\s*$", other_text, re.M):
                        reset.add(key)
        if not reset:
            return text
        return "\n".join(
            line for line in text.splitlines()
            if not any(line.startswith(f"{key}=") for key in reset))

    def _plan_profiles(self, plan: Plan) -> None:
        base = self.root / "nix/var/nix/profiles"
        if not base.is_dir():
            return
        for path in sorted(base.iterdir()):
            if not path.name.startswith(OLD + "-") or not path.is_symlink():
                continue
            new = base / (NEW + path.name[len(OLD):])
            if new.exists() or new.is_symlink():
                plan.collisions.append(new)
            plan.profiles.append((path, new, os.readlink(path)))

    def _classify(self, plan: Plan, path: Path, *, rewrite: bool) -> None:
        text = _read_text(path)
        if text is None:
            if _mentions_old_name(path):
                plan.requirements.append(f"{path} mentions the old name but cannot be rewritten (binary, non-UTF-8 or over 2 MiB); handle it by hand")
            return
        if OLD not in text.lower():
            return
        if _PINNED.search(text):
            plan.repin.append(path)
        elif rewrite:
            plan.rewrites.append(path)

    def _scan_outside(self, plan: Plan) -> None:
        migrated = {u for u in plan.units}
        for base, label in ((self.root / "usr/local/sbin", "/usr/local/sbin"), (self._unit_dir(), "/etc/systemd/system")):
            if not base.is_dir():
                continue
            for path in sorted(base.iterdir()):
                if path.name in migrated or path.name.startswith(tuple(u + ".d" for u in migrated)):
                    continue
                text = _read_text(path)
                if text is not None and OLD in text.lower():
                    plan.warnings.append(f"{label}/{path.name} mentions the old names; update it by hand")

    def _scan_symlinks(self, plan: Plan) -> None:
        """Absolute links that name the old paths are retargeted; others are reported."""
        seen = 0
        roots = [old for old, _ in plan.moves] + [self.root / "usr/local/bin", self.root / "usr/local/sbin"]
        report: list[str] = []
        for base in roots:
            if not base.exists():
                continue
            for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
                for name in dirnames + filenames:
                    seen += 1
                    path = Path(dirpath) / name
                    if not path.is_symlink():
                        continue
                    target = os.readlink(path)
                    if OLD not in target.lower():
                        continue
                    new_target = rename_text(target)
                    if target.startswith("/") and new_target != target and not target.startswith("/nix/store/"):
                        plan.symlinks.append((path, target, new_target))
                    else:
                        report.append(f"{path} -> {target}")
                if seen > MAX_SYMLINK_SCAN:
                    plan.warnings.append("symlink scan stopped early (very large tree)")
                    break
        for item in report[:10]:
            plan.warnings.append(f"symlink will dangle after the move and cannot be retargeted: {item}")
        if len(report) > 10:
            plan.warnings.append(f"... and {len(report) - 10} more symlinks that point at old paths")

    # ---- apply ----------------------------------------------------------------

    def apply(self, backup_dir: Path, *, start: bool = True, db_renamed: bool = False) -> Plan:
        plan = self.plan()
        if plan.blockers:
            raise MigrationError("cannot migrate:\n" + "\n".join(f"  - {b}" for b in plan.blockers))
        backup_dir = Path(os.path.abspath(backup_dir))
        if backup_dir.exists() and any(backup_dir.iterdir()):
            raise MigrationError(f"backup directory is not empty: {backup_dir}")
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_dir.chmod(0o700)
        was_enabled = {u: self.run(["systemctl", "is-enabled", u]).returncode == 0 for u in plan.units}
        was_active = {u: self.run(["systemctl", "is-active", u]).returncode == 0 for u in plan.units}
        try:
            self._backup(plan, backup_dir)
            script = self._rollback_script(plan, backup_dir, was_enabled, was_active)
            rollback = backup_dir / "rollback.sh"
            rollback.write_text(script, encoding="utf-8")
            rollback.chmod(0o700)
            (backup_dir / "plan.txt").write_text(plan.describe() + "\n", encoding="utf-8")
        except (OSError, UnicodeError, shutil.Error) as exc:
            raise MigrationError(f"could not prepare the backup (nothing was changed): {exc}") from exc

        hint = f"Nothing is lost: run `sh {backup_dir / 'rollback.sh'}` to restore the old names."
        try:
            self._do(plan, was_enabled, was_active, start=start, db_renamed=db_renamed)
        except MigrationError as exc:
            raise MigrationError(f"{exc}\n{hint}") from exc
        except (OSError, UnicodeError, shutil.Error) as exc:
            raise MigrationError(f"{type(exc).__name__}: {exc}\n{hint}") from exc
        leftovers = self.leftovers(plan)
        plan.warnings += [f"old name still present in {p}" for p in leftovers]
        return plan

    def _backup(self, plan: Plan, backup_dir: Path) -> None:
        files_dir = backup_dir / "files"
        for path in plan.unit_files + plan.rewrites:
            target = files_dir / path.relative_to(self.root)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            # copy2 keeps the mode but not the owner. The rollback restores with
            # `cp -p`, which applies the backup copy's owner: without this a
            # file read through a group (root:service-group 0640) came back
            # root:root and the old service could no longer read it.
            info = path.stat()
            try:
                os.chown(target, info.st_uid, info.st_gid)
            except PermissionError:
                pass  # not root (tests, rehearsal copies)
        libexec_old = self.root / LIBEXEC_OLD
        if libexec_old.is_dir():
            shutil.copytree(libexec_old, backup_dir / "libexec-old", symlinks=True)

    def _do(self, plan: Plan, was_enabled: dict[str, bool], was_active: dict[str, bool], *,
            start: bool, db_renamed: bool) -> None:
        for unit in plan.units:
            self._must(["systemctl", "stop", unit])
        for unit in plan.units:
            if was_enabled[unit]:
                self._must(["systemctl", "disable", unit])
        for old, new in plan.groups:
            self._must(["groupmod", "-n", new, old])
        for old, new in plan.users:
            self._must(["usermod", "-l", new, old])
            if plan.homes.get(old) == f"/var/lib/{old}":
                self._must(["usermod", "-d", f"/var/lib/{new}", new])
        for old, new in plan.moves:
            new.parent.mkdir(parents=True, exist_ok=True)
            os.rename(old, new)
            if old == self.root / LIBEXEC_OLD:
                self._install_helpers(new)
        for old_link, new_link, _ in plan.profiles:
            os.rename(old_link, new_link)
            target = os.readlink(new_link)
            if OLD in target:  # the profile link points at its own generation link by name
                new_link.unlink()
                os.symlink(rename_text(target), new_link)
        for link, old_target, new_target in plan.symlinks:
            current = self._after_moves(link, plan)
            if current.is_symlink() and os.readlink(current) == old_target:
                current.unlink()
                os.symlink(new_target, current)
        for path in plan.rewrites:
            current = self._after_moves(path, plan)
            if current.exists():
                current.write_text(rename_text(current.read_text(encoding="utf-8")), encoding="utf-8")
        self._rewrite_units(plan)
        self._must(["systemctl", "daemon-reload"])
        for unit in plan.units:
            new_unit = unit.replace(OLD, NEW)
            if was_enabled[unit]:
                self._must(["systemctl", "enable", new_unit])
        hold_for_database = plan.role == "control" and not db_renamed
        if hold_for_database:
            plan.warnings.append(
                "Control was NOT started: its database URL now names the database `tsumgi`. "
                "Rename it (ALTER DATABASE astrumweaver RENAME TO tsumgi), then start tsumgi-control.")
        for unit in plan.units:
            if not start or hold_for_database:
                continue
            if not was_active[unit]:
                plan.warnings.append(f"{unit} was not running before; the new unit was left stopped")
                continue
            self._must(["systemctl", "start", unit.replace(OLD, NEW)])

    def _must(self, argv: Sequence[str]) -> None:
        result = self.run(argv)
        if result.returncode != 0:
            raise MigrationError(f"command failed ({result.returncode}): {shlex.join(argv)}")

    def _after_moves(self, path: Path, plan: Plan) -> Path:
        for old, new in plan.moves:
            if path.is_relative_to(old):
                return new / path.relative_to(old)
        return path

    def _install_helpers(self, libexec_new: Path) -> None:
        # The old helpers carry the old names and the pre-0.3 layout; they are
        # replaced, never edited.
        source = self.helpers_from
        if source is None or not source.is_dir():
            raise MigrationError("--helpers-from is required when libexec helpers are installed")
        for existing in list(libexec_new.iterdir()):
            if existing.is_dir() and not existing.is_symlink():
                shutil.rmtree(existing)
            else:
                existing.unlink()
        shutil.copytree(source, libexec_new, dirs_exist_ok=True, symlinks=True)

    def _rewrite_units(self, plan: Plan) -> None:
        unit_dir = self._unit_dir()
        for unit in plan.units:
            new_unit = unit.replace(OLD, NEW)
            os.rename(unit_dir / unit, unit_dir / new_unit)
            dropins = unit_dir / f"{unit}.d"
            if dropins.is_dir():
                os.rename(dropins, unit_dir / f"{new_unit}.d")
        for unit in plan.units:
            new_unit = unit.replace(OLD, NEW)
            files = [unit_dir / new_unit]
            dropins = unit_dir / f"{new_unit}.d"
            if dropins.is_dir():
                files += sorted(p for p in dropins.iterdir() if p.is_file())
            for file in files:
                text = file.read_text(encoding="utf-8")
                text = _EXEC_BIN.sub(lambda m: f"{m.group(1)}{self.profile}{m.group(2)}tsumgi-", text)
                file.write_text(rename_text(text), encoding="utf-8")

    def leftovers(self, plan: Plan) -> list[Path]:
        found: list[Path] = []
        paths = [self._unit_dir() / u.replace(OLD, NEW) for u in plan.units]
        paths += [p for _, new in plan.moves if new.parent.name == "etc" for p in sorted(new.rglob("*"))]
        for path in paths:
            text = _read_text(path)
            if text is not None and OLD in text.lower() and not _PINNED.search(text):
                found.append(path)
        return found

    # ---- rollback --------------------------------------------------------------

    def _rollback_script(self, plan: Plan, backup_dir: Path, was_enabled: dict[str, bool],
                         was_active: dict[str, bool]) -> str:
        q = shlex.quote
        unit_dir = self._unit_dir()
        live = self.live
        lines = [
            "#!/bin/sh",
            "# Restores the pre-migration names. Safe to run more than once.",
            "# Paths are absolute; failures are reported and make the exit code non-zero.",
            "set -u",
            "status=0",
            'fail() { echo "rollback: $*" >&2; status=1; }',
        ]
        if not live:
            lines.append('echo "rehearsal root: only files are restored; no system command is run" >&2')

        def system(command: str) -> None:
            if live:
                lines.append(command)

        for unit in reversed(plan.units):
            new_unit = unit.replace(OLD, NEW)
            system(f"systemctl stop {q(new_unit)} 2>/dev/null")
            system(f"systemctl disable {q(new_unit)} 2>/dev/null")
        for old, new in reversed(plan.moves):
            o, n = q(str(old)), q(str(new))
            lines.append(
                f"if [ -e {n} ] && [ ! -e {o} ]; then mv {n} {o} || fail 'could not move '{n}' back'; "
                f"elif [ ! -e {o} ]; then fail 'neither '{o}' nor '{n}' exists'; fi")
        for old_link, new_link, old_target in reversed(plan.profiles):
            lines.append(f"rm -f {q(str(new_link))} && ln -sfn {q(old_target)} {q(str(old_link))} || fail 'profile {old_link.name}'")
        for link, old_target, _ in plan.symlinks:
            lines.append(
                f"if [ -d {q(str(link.parent))} ]; then ln -sfn {q(old_target)} {q(str(link))} || fail 'symlink {link.name}'; "
                f"else fail 'directory missing: '{q(str(link.parent))}; fi")
        for old, new in reversed(plan.users):
            system(f"if id {q(new)} >/dev/null 2>&1; then usermod -l {q(old)} {q(new)} || fail 'usermod {new}'; fi")
            home = plan.homes.get(old)
            if home and home == f"/var/lib/{old}":  # only when apply changed it
                system(f"usermod -d {q(home)} {q(old)} || fail 'usermod -d {old}'")
        for old, new in reversed(plan.groups):
            system(f"if getent group {q(new)} >/dev/null 2>&1; then groupmod -n {q(old)} {q(new)} || fail 'groupmod {new}'; fi")
        for path in plan.unit_files:
            backup = backup_dir / "files" / path.relative_to(self.root)
            lines.append(
                f"if [ -f {q(str(backup))} ]; then mkdir -p {q(str(path.parent))} && cp -p {q(str(backup))} {q(str(path))} "
                f"|| fail 'could not restore '{q(str(path))}; else fail 'missing backup '{q(str(backup))}; fi")
        for unit in plan.units:
            new_unit = unit.replace(OLD, NEW)
            lines.append(
                f"if [ -f {q(str(unit_dir / unit))} ]; then rm -rf {q(str(unit_dir / new_unit))} {q(str(unit_dir / (new_unit + '.d')))}; fi")
        for path in plan.rewrites:
            backup = backup_dir / "files" / path.relative_to(self.root)
            lines.append(
                f"if [ -d {q(str(path.parent))} ]; then cp -p {q(str(backup))} {q(str(path))} || fail 'could not restore '{q(str(path))}; "
                f"else fail 'directory missing: '{q(str(path.parent))}; fi")
        if any(old == self.root / LIBEXEC_OLD for old, _ in plan.moves):
            old_dir = q(str(self.root / LIBEXEC_OLD))
            saved = q(str(backup_dir / "libexec-old"))
            lines.append(
                f"if [ -d {old_dir} ] && [ -d {saved} ]; then find {old_dir} -mindepth 1 -delete && cp -a {saved}/. {old_dir}/ "
                f"|| fail 'could not restore the old helpers'; fi")
        system("systemctl daemon-reload")
        for unit in plan.units:
            if was_enabled[unit]:
                system(f"systemctl enable {q(unit)} || fail 'enable {unit}'")
            if was_active[unit]:
                system(f"systemctl start {q(unit)} || fail 'start {unit}'")
        lines.append("exit $status")
        return "\n".join(lines) + "\n"


def _rehearsal_runner(argv: Sequence[str]) -> CommandResult:
    print("[skipped, --root is not /] " + shlex.join(argv), file=sys.stderr)
    return CommandResult(0, "")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tsumgi-migrate-host", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "apply"):
        p = sub.add_parser(name)
        p.add_argument("--role", choices=sorted(UNITS), required=True)
        p.add_argument("--profile", required=True, help="new Nix profile name, e.g. tsumgi-v030-installer")
        p.add_argument("--root", default="/", help="host root (for rehearsal on a copy)")
        p.add_argument("--helpers-from", help="directory with the new build's libexec helpers")
        if name == "apply":
            p.add_argument("--backup-dir", required=True)
            p.add_argument("--no-start", action="store_true")
            p.add_argument("--db-renamed", action="store_true",
                           help="Control: the database was already renamed to tsumgi, so start the unit")
    r = sub.add_parser("rollback")
    r.add_argument("--backup-dir", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "rollback":
        script = Path(os.path.abspath(args.backup_dir)) / "rollback.sh"
        if not script.is_file():
            print(f"tsumgi-migrate-host: no rollback.sh in {args.backup_dir}", file=sys.stderr)
            return 2
        return subprocess.run(["sh", str(script)], check=False).returncode
    try:
        migration = HostMigration(
            Path(args.root), args.role, args.profile,
            helpers_from=Path(args.helpers_from) if args.helpers_from else None,
            runner=run_command if Path(os.path.realpath(args.root)) == Path("/") else _rehearsal_runner,
        )
        if args.command == "plan":
            plan = migration.plan()
            print(plan.describe())
            return 3 if plan.blockers else 0
        plan = migration.apply(Path(args.backup_dir), start=not args.no_start, db_renamed=args.db_renamed)
        print(plan.describe())
        print(f"\nmigrated. rollback: sh {Path(os.path.abspath(args.backup_dir)) / 'rollback.sh'}")
        return 0
    except MigrationError as exc:
        print(f"tsumgi-migrate-host: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
