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
_STORE_TOKEN = re.compile(r"/nix/store/[0-9a-z]{32}-[^\s\"',;]*")
_PINNED = re.compile(r"/nix/store/[0-9a-z]{32}-[^\s\"',;]*astrumweaver[^\s\"',;]*|astrumweaver-decisions-v1", re.I)
_EXEC_BIN = re.compile(r"(/nix/var/nix/profiles/)[^/\s]+(/bin/)astrumweaver-")
_TEXT_SUFFIXES = {".toml", ".json", ".env", ".conf", ".service", ".sh", ".txt", ""}

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
    """Rename identifiers in a config or unit file, leaving Nix store paths alone."""
    protected: list[str] = []

    def keep(match: re.Match[str]) -> str:
        protected.append(match.group(0))
        return f"\x00{len(protected) - 1}\x00"

    text = _STORE_TOKEN.sub(keep, text)
    text = text.replace("ASTRUMWEAVER_", "TSUMGI_").replace("AstrumWeaver", "TSUMGI").replace(OLD, NEW)
    return re.sub(r"\x00(\d+)\x00", lambda m: protected[int(m.group(1))], text)


@dataclass
class Plan:
    role: str
    root: Path
    profile: str
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
        lines += [f"rewrite {rel(p)}" for p in self.rewrites]
        for warning in self.warnings:
            lines.append(f"warning: {warning}")
        for blocker in self.blockers:
            lines.append(f"BLOCKED: {blocker}")
        return "\n".join(lines)


def _is_text_candidate(path: Path) -> bool:
    try:
        return path.is_file() and not path.is_symlink() and path.stat().st_size <= MAX_TEXT_BYTES
    except OSError:
        return False


def _read_text(path: Path) -> str | None:
    if not _is_text_candidate(path):
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def _names(root: Path, relative: str) -> set[str]:
    path = root / relative
    try:
        return {line.split(":", 1)[0] for line in path.read_text(encoding="utf-8").splitlines() if line and not line.startswith("#")}
    except OSError:
        return set()


class HostMigration:
    def __init__(self, root: Path, role: str, profile: str, *, helpers_from: Path | None = None,
                 runner: Runner = run_command, clock: Callable[[], float] = time.time) -> None:
        if role not in UNITS:
            raise MigrationError(f"role must be one of {sorted(UNITS)}")
        if not re.fullmatch(r"[A-Za-z0-9._+-]+", profile) or "/" in profile:
            raise MigrationError("profile must be a Nix profile name such as tsumgi-v030-installer")
        self.root = Path(root)
        self.role = role
        self.profile = profile
        self.helpers_from = helpers_from
        self.run = runner
        self.clock = clock

    # ---- planning -------------------------------------------------------------

    def _unit_dir(self) -> Path:
        return self.root / "etc/systemd/system"

    def plan(self) -> Plan:
        plan = Plan(role=self.role, root=self.root, profile=self.profile, helpers_from=self.helpers_from)
        unit_dir = self._unit_dir()
        for unit in UNITS[self.role]:
            if (unit_dir / unit).exists():
                plan.units.append(unit)
        if not plan.units:
            raise MigrationError(f"no {self.role} unit found under {unit_dir}: nothing to migrate")
        for unit in plan.units:
            plan.unit_files.append(unit_dir / unit)
            dropins = unit_dir / f"{unit}.d"
            if dropins.is_dir():
                plan.unit_files += sorted(p for p in dropins.iterdir() if p.is_file())

        passwd, group = _names(self.root, "etc/passwd"), _names(self.root, "etc/group")
        plan.users = [(a, b) for a, b in USERS if a in passwd]
        plan.groups = [(a, b) for a, b in GROUPS if a in group]
        for _, new in plan.users:
            if new in passwd:
                plan.collisions.append(self.root / "etc/passwd")
        for _, new in plan.groups:
            if new in group:
                plan.collisions.append(self.root / "etc/group")

        for old, new in DIRECTORY_MOVES:
            if (self.root / old).exists():
                plan.moves.append((self.root / old, self.root / new))
                if (self.root / new).exists():
                    plan.collisions.append(self.root / new)
        if (self.root / LIBEXEC_OLD).exists():
            plan.moves.append((self.root / LIBEXEC_OLD, self.root / LIBEXEC_NEW))
            if (self.root / LIBEXEC_NEW).exists():
                plan.collisions.append(self.root / LIBEXEC_NEW)
            if self.helpers_from is None or not Path(self.helpers_from).is_dir():
                plan.requirements.append("libexec helpers exist: pass --helpers-from DIR (the new build's libexec) to replace them")
        for unit in plan.units:
            if (unit_dir / unit.replace(OLD, NEW)).exists():
                plan.collisions.append(unit_dir / unit.replace(OLD, NEW))

        for path in plan.unit_files:
            self._classify(plan, path, rewrite=False)
        for old, _ in plan.moves:
            if old.name == "astrumweaver" and old.parent.name == "libexec":
                continue
            if old.is_dir() and old.parent.name in {"etc"}:
                for path in sorted(old.rglob("*")):
                    self._classify(plan, path, rewrite=True)
        self._scan_outside(plan)
        return plan

    def _classify(self, plan: Plan, path: Path, *, rewrite: bool) -> None:
        if path.suffix not in _TEXT_SUFFIXES:
            return
        text = _read_text(path)
        if text is None or OLD not in text.lower():
            return
        if _PINNED.search(text):
            plan.repin.append(path)
        elif rewrite:
            plan.rewrites.append(path)

    def _scan_outside(self, plan: Plan) -> None:
        sbin = self.root / "usr/local/sbin"
        if sbin.is_dir():
            for path in sorted(sbin.iterdir()):
                text = _read_text(path)
                if text is not None and OLD in text.lower():
                    plan.warnings.append(f"/usr/local/sbin/{path.name} mentions the old names; update it by hand")

    # ---- apply ----------------------------------------------------------------

    def apply(self, backup_dir: Path, *, start: bool = True) -> Plan:
        plan = self.plan()
        if plan.blockers:
            raise MigrationError("cannot migrate:\n" + "\n".join(f"  - {b}" for b in plan.blockers))
        backup_dir = Path(backup_dir)
        if backup_dir.exists() and any(backup_dir.iterdir()):
            raise MigrationError(f"backup directory is not empty: {backup_dir}")
        backup_dir.mkdir(parents=True, exist_ok=True)
        files_dir = backup_dir / "files"
        for path in plan.unit_files + plan.rewrites:
            target = files_dir / path.relative_to(self.root)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
        libexec_old = self.root / LIBEXEC_OLD
        if libexec_old.is_dir():
            shutil.copytree(libexec_old, backup_dir / "libexec-old", symlinks=True)
        (backup_dir / "plan.txt").write_text(plan.describe() + "\n", encoding="utf-8")
        was_enabled = {u: self.run(["systemctl", "is-enabled", u]).returncode == 0 for u in plan.units}
        script = self._rollback_script(plan, backup_dir, was_enabled)
        rollback = backup_dir / "rollback.sh"
        rollback.write_text(script, encoding="utf-8")
        rollback.chmod(0o700)

        for unit in plan.units:
            self._must(["systemctl", "stop", unit])
        for unit in plan.units:
            if was_enabled[unit]:
                self._must(["systemctl", "disable", unit])
        for old, new in plan.groups:
            self._must(["groupmod", "-n", new, old])
        for old, new in plan.users:
            self._must(["usermod", "-l", new, old])
            home = f"/var/lib/{new}"
            if (self.root / f"var/lib/{old}").exists():
                self._must(["usermod", "-d", home, new])
        for old, new in plan.moves:
            old_is_libexec = old == self.root / LIBEXEC_OLD
            new.parent.mkdir(parents=True, exist_ok=True)
            os.rename(old, new)
            if old_is_libexec:
                self._install_helpers(new)
        for path in plan.rewrites:
            current = self._after_moves(path, plan)
            if current.exists():
                current.write_text(rename_text(current.read_text(encoding="utf-8")), encoding="utf-8")
        self._rewrite_units(plan)
        self._must(["systemctl", "daemon-reload"])
        if start:
            for unit in plan.units:
                new_unit = unit.replace(OLD, NEW)
                if was_enabled[unit]:
                    self._must(["systemctl", "enable", new_unit])
                self._must(["systemctl", "start", new_unit])
        leftovers = self.leftovers(plan)
        if leftovers:
            plan.warnings += [f"old name still present in {p}" for p in leftovers]
        return plan

    def _must(self, argv: Sequence[str]) -> None:
        result = self.run(argv)
        if result.returncode != 0:
            raise MigrationError(
                f"command failed ({result.returncode}): {shlex.join(argv)}\n"
                "Run the rollback.sh in the backup directory to restore the old names."
            )

    def _after_moves(self, path: Path, plan: Plan) -> Path:
        for old, new in plan.moves:
            if path.is_relative_to(old):
                return new / path.relative_to(old)
        return path

    def _install_helpers(self, libexec_new: Path) -> None:
        # The old helpers carry the old names and the pre-0.3 layout; they are
        # replaced, never edited.
        source = Path(self.helpers_from or "")
        if not source.is_dir():
            raise MigrationError("--helpers-from is required when libexec helpers are installed")
        for existing in libexec_new.iterdir():
            if existing.is_file():
                existing.unlink()
        for item in source.iterdir():
            if item.is_file():
                target = libexec_new / item.name
                shutil.copy2(item, target)

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

    def _rollback_script(self, plan: Plan, backup_dir: Path, was_enabled: dict[str, bool]) -> str:
        q = shlex.quote
        unit_dir = self._unit_dir()
        lines = [
            "#!/bin/sh",
            "# Restores the pre-migration names. Safe to run more than once.",
            "set -u",
        ]
        for unit in reversed(plan.units):
            new_unit = unit.replace(OLD, NEW)
            lines += [f"systemctl stop {q(new_unit)} 2>/dev/null", f"systemctl disable {q(new_unit)} 2>/dev/null"]
        for old, new in reversed(plan.moves):
            lines.append(f"[ -e {q(str(new))} ] && [ ! -e {q(str(old))} ] && mv {q(str(new))} {q(str(old))}")
        for old, new in reversed(plan.users):
            lines.append(f"id {q(new)} >/dev/null 2>&1 && usermod -l {q(old)} {q(new)}")
            lines.append(f"[ -d {q(str(self.root / ('var/lib/' + old)))} ] && usermod -d {q('/var/lib/' + old)} {q(old)}")
        for old, new in reversed(plan.groups):
            lines.append(f"getent group {q(new)} >/dev/null 2>&1 && groupmod -n {q(old)} {q(new)}")
        for unit in plan.units:
            new_unit = unit.replace(OLD, NEW)
            lines += [
                f"rm -rf {q(str(unit_dir / new_unit))} {q(str(unit_dir / (new_unit + '.d')))}",
            ]
        for path in plan.unit_files:
            backup = backup_dir / "files" / path.relative_to(self.root)
            lines += [f"mkdir -p {q(str(path.parent))}", f"cp -p {q(str(backup))} {q(str(path))}"]
        for path in plan.rewrites:
            backup = backup_dir / "files" / path.relative_to(self.root)
            lines += [f"[ -d {q(str(path.parent))} ] && cp -p {q(str(backup))} {q(str(path))}"]
        if any(old == self.root / LIBEXEC_OLD for old, _ in plan.moves):
            old_dir = str(self.root / LIBEXEC_OLD)
            lines.append(f"[ -d {q(old_dir)} ] && rm -rf {q(old_dir)}/* && cp -a {q(str(backup_dir / 'libexec-old'))}/. {q(old_dir)}/")
        lines.append("systemctl daemon-reload")
        for unit in plan.units:
            if was_enabled[unit]:
                lines.append(f"systemctl enable {q(unit)}")
            lines.append(f"systemctl start {q(unit)}")
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
    r = sub.add_parser("rollback")
    r.add_argument("--backup-dir", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "rollback":
        script = Path(args.backup_dir) / "rollback.sh"
        if not script.is_file():
            print(f"tsumgi-migrate-host: no rollback.sh in {args.backup_dir}", file=sys.stderr)
            return 2
        return subprocess.run(["sh", str(script)], check=False).returncode
    try:
        migration = HostMigration(
            Path(args.root), args.role, args.profile,
            helpers_from=Path(args.helpers_from) if args.helpers_from else None,
            runner=run_command if Path(args.root) == Path("/") else _rehearsal_runner,
        )
        if args.command == "plan":
            plan = migration.plan()
            print(plan.describe())
            return 3 if plan.blockers else 0
        plan = migration.apply(Path(args.backup_dir), start=not args.no_start)
        print(plan.describe())
        print(f"\nmigrated. rollback: sh {Path(args.backup_dir) / 'rollback.sh'}")
        return 0
    except MigrationError as exc:
        print(f"tsumgi-migrate-host: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
