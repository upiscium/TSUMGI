# Moving a host from the old names to TSUMGI

For v0.3.0. Hosts installed before it use the `astrumweaver` names for units,
service users, paths and environment variables. `tsumgi-migrate-host` moves one
host at a time. See [design/rename-phase-b.md](design/rename-phase-b.md) for the
plan and [naming.md](naming.md) for the names.

## Before you start

1. **Install the new build** as a Nix profile, for example
   `tsumgi-v030-installer` (Worker) or `tsumgi-v030-control` (Control). The tool
   points the unit at `/nix/var/nix/profiles/<profile>/bin/tsumgi-*`. Keep the
   old profile: the rollback needs it.
2. **Drain.** No non-terminal Jobs for the host's capabilities (for the decision
   Worker, none at all). A Control and Worker on different names cannot work together mid-flight: migrate
   in a window where clients are stopped.
3. **Re-pin what carries a pinned identity.** `plan` refuses (exit 3) while a file
   under `/etc/astrumweaver` mentions a Nix store path with the old name, or
   `astrumweaver-decisions-v1`. Regenerate those files with the new build
   (the decision runtime deployment, serving declaration and the Control
   decision catalog) and replace them first. The tool never edits them.
4. **Helpers.** If `/usr/local/libexec/astrumweaver` exists, pass
   `--helpers-from DIR` with the new build's `libexec` directory. The old helpers
   are replaced, not edited.
5. Scripts outside the tool (for example `/usr/local/sbin/tsumgi-gpu-mode`) are
   only reported. Update them by hand.

## Steps

```sh
tsumgi-migrate-host plan  --role worker --profile tsumgi-v030-installer --helpers-from DIR
tsumgi-migrate-host apply --role worker --profile tsumgi-v030-installer --helpers-from DIR \
    --backup-dir /root/tsumgi-migration-$(date +%Y%m%d)
```

`apply` writes `rollback.sh`, copies the files it will rewrite and the old
helpers into the backup directory, then: stops and disables the old unit,
renames the groups and users (same uid and gid), moves the directories
(`/etc`, `/var/lib`, `/opt`, `/var/cache`, `libexec`), renames the names inside
the configuration files and units (secret values are not touched, store paths
are never rewritten), renames the unit and its drop-ins, runs `daemon-reload`
and starts the new unit.

For a rehearsal on a copy of a host tree, pass `--root DIR`: no system command is
run, they are printed instead.

## If something goes wrong

```sh
sh /root/tsumgi-migration-YYYYMMDD/rollback.sh      # or: tsumgi-migrate-host rollback --backup-dir ...
```

It stops the new unit, moves the directories and the user and group names back,
restores the unit and configuration files and the old helpers, and starts the
old unit. It can be run more than once. It needs the old profile, not this tool.

## Not done by the tool

- **PostgreSQL.** Rename the database last, with Control stopped and nothing else
  connected: `ALTER DATABASE astrumweaver RENAME TO tsumgi;`. The URL inside
  `control.env` already follows the rename.
- **Strata and other state under `/opt`.** The directory is renamed, not copied
  (same file system). Files inside it that store absolute paths are not
  rewritten; check after the move.
- `/opt/astrumweaver-gpu-*` evidence logs from old acceptance runs stay as they are.
