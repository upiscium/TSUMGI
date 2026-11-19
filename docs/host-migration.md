# Moving a host from the old names to TSUMGI

For v0.3.0. Hosts installed before it use the `astrumweaver` names for units,
service users, paths and environment variables. `tsumgi-migrate-host` moves one
host at a time. See [design/rename-phase-b.md](design/rename-phase-b.md) for the
plan and [naming.md](naming.md) for the names.

## Before you start

1. **Install the new build** as a Nix profile, for example
   `tsumgi-v030-installer` (Worker) or `tsumgi-v030-control` (Control). The tool
   points the unit at `/nix/var/nix/profiles/<profile>/bin/tsumgi-*` and refuses
   to run if that binary is missing. Keep the old profile: the rollback needs it.
2. **Drain.** No non-terminal Jobs for the host's capabilities (for the decision
   Worker, none at all). A Control and a Worker on different names cannot work
   together: migrate in a window where clients are stopped.
3. **Re-pin what carries a pinned identity.** `plan` refuses (exit 3) while a file
   under `/etc/astrumweaver` mentions a Nix store path with the old name, or
   `astrumweaver-decisions-v1`. Regenerate those files with the new build (the
   decision runtime deployment, the serving declaration and the Control decision
   catalog) and replace them first. The tool never edits them.
4. **Helpers.** If `/usr/local/libexec/astrumweaver` exists, pass
   `--helpers-from DIR` with the new build's `libexec` directory. The old helpers
   (including dotfiles and subdirectories) are replaced, not edited.
5. **Nix profiles** named `astrumweaver-*` (and their generation links) are renamed to `tsumgi-*`, because rewritten configs refer to them by name.
6. Read the warnings. Scripts outside the tool (for example
   `/usr/local/sbin/tsumgi-gpu-mode`), other units that mention the old unit
   names, and symlinks that point at old paths are only reported.

Earlier rollback copies under `/etc/astrumweaver/rollback` are moved but never rewritten or checked. A file that carries a pinned identity but that no unit or config refers to is a leftover: it only warns and is moved unchanged.

`plan` also refuses when: both roles are installed on the host; a target name
already exists; a previous run was interrupted (a new name exists without the
old one: run its `rollback.sh` first); a unit or drop-in is a symlink; a path to
move is a mount point; or a file mentions the old name but cannot be rewritten
(binary, not UTF-8, over 2 MiB).

## Steps

```sh
tsumgi-migrate-host plan  --role worker --profile tsumgi-v030-installer --helpers-from DIR
tsumgi-migrate-host apply --role worker --profile tsumgi-v030-installer --helpers-from DIR \
    --backup-dir /root/tsumgi-migration-$(date +%Y%m%d)
```

`apply` records the state, writes `rollback.sh` (mode 0700, absolute paths) and a
backup (also 0700) first, then: stops and disables the old unit, renames the
groups and users (same uid and gid), moves the directories (`/etc`, `/var/lib`,
`/opt`, `/var/cache`, `libexec`), renames names inside the configuration files
and units, renames the unit and its drop-ins, runs `daemon-reload` and enables
and starts the new unit.

- **A unit that was stopped stays stopped** (a lent GPU, for example), in `apply`
  and in the rollback. A unit that was disabled stays disabled.
- **What is rewritten:** variable names, paths, unit and user names, module
  strings. Secret *values* are not read for their meaning, but a value that
  contains the old name as text is renamed like any other text, except the
  user-info part of a URL (`scheme://user:password@host`), which is kept. Nix store
  paths are never rewritten.
- **Control** is not started by `apply`: its database URL then names the database
  `tsumgi`, which has to exist first. Rename it, then start the unit, or pass
  `--db-renamed` when it is already done:

  ```sh
  # Control stopped by apply, nothing else connected to the database
  sudo -u postgres psql -c 'ALTER DATABASE astrumweaver RENAME TO tsumgi;'
  systemctl start tsumgi-control
  ```

For a rehearsal on a copy of a host tree, pass `--root DIR`: no system command is
run (they are printed), and the generated `rollback.sh` only restores files.

## If something goes wrong

Any failure after the backup stops the run and prints the command to use:

```sh
sh /root/tsumgi-migration-YYYYMMDD/rollback.sh      # or: tsumgi-migrate-host rollback --backup-dir ...
```

It stops the new unit, moves the directories and the user and group names back,
restores the unit and configuration files and the old helpers, and starts the old
unit if it was running. It reports every step it could not do and exits non-zero
then. It can be run more than once, and it needs the old profile, not this tool.
A rollback after the database was renamed needs the reverse `ALTER DATABASE`
first.

## Not done by the tool

- **Strata and other state under `/opt`.** The directory is renamed, not copied
  (same file system). Files inside it that store absolute paths are not
  rewritten; check after the move.
- Anything under `/var/lib` or `/opt` is moved but never read or rewritten. Absolute symlinks there (and in `/usr/local/bin`, `/usr/local/sbin`) that name the old paths are retargeted; others are only reported.
- `/opt/astrumweaver-gpu-*` evidence logs from old acceptance runs stay as they are.
