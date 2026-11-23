# TSUMGI rename: production cutover plan (stage 7)

Status: **plan approved in outline; the window is not set. Nothing here has been done.** It needs the owner's
approval and a time window before every phase. It rests on what stages 1-5
measured: the audit and rehearsal results in [rename-phase-b.md](rename-phase-b.md),
the tool in [../host-migration.md](../host-migration.md), and the live rehearsal on
one lent host (canopus), a rehearsal Control against a database copy, and
read-only `plan` runs on the other hosts.

## What is being moved

Seven Worker hosts, Control and PostgreSQL, from the `astrumweaver` names (v0.2.1
builds) to `tsumgi` (v0.3.0). The code reads only the new names, so a host runs
either the old stack or the new one, never a mixture of one host's parts.

| Host | Role | Stop in the cutover | Extra work |
| --- | --- | --- | --- |
| spica, canopus, sirius, deneb | 64K Strata replicas | none for the service as a whole (3 of 4 keep serving); about 2 min per host | `--allow-pinned` for a stale file if `plan` blocks (spica: `runtime-deployment.json`) |
| vega | 12K Strata | a few minutes (single replica) | stale `tsumgi-rc1-runtime.json` only warns |
| polaris | 192K llama.cpp | a few minutes (single replica) | the Nix profile `astrumweaver-runtime-qwen38` is renamed by the tool |
| altair | embedding + decision | a few minutes, **together with Control** | re-pin (below), new runtime package |
| tsumgi-control | Control + Gateway + console | a window of roughly 10-15 minutes | migrations 006/007, database rename, decision catalog re-pin |
| tsumgi-db | PostgreSQL | none | `ALTER DATABASE` with Control stopped |

## Before the window (no service is touched)

1. Merge `rename/0.3.0` to `main` and publish the release (stage 6). Build and
   install the profiles on every host from that **release tag**, not from a branch:
   `tsumgi-v030-installer` on Workers, `tsumgi-v030-control` on Control. Keep the
   old profiles; the rollbacks need them.
2. Build the new decision runtime package on altair (store only): `nix build
   github:upiscium/TSUMGI/<tag>#decision-llama-cuda-sm61`. The heavy CUDA llama.cpp
   build keeps its old store name on purpose, so only three small derivations are
   built.
3. Generate the re-pinned files with `tsumgi-repin-decision` into a separate
   directory and keep them ready (altair's serving declaration and runtime
   manifest, Control's decision catalog). Load them offline with the new build
   (the tool already does) and start a rehearsal Control against a database copy
   with them, as in the stage 5 rehearsal.
4. Run `tsumgi-migrate-host plan` on every host with the final build. Expected: only
   warnings, except altair and Control, which block until the re-pinned files are in
   place. Decide `--allow-pinned` per file after checking nothing uses it.
5. Update the scripts in `/usr/local/sbin` by hand, per host: `tsumgi-gpu-mode`
   (unit `tsumgi-worker.service`, `tsumgi-worker-mode`, `/etc/tsumgi/gpu-uuids`, and
   the mode name `tsumgi`, not `astrumweaver`). The old `tsumgi-rollback-*` and
   `astrumweaver-install-llama-cpp-pascal` scripts describe the old build and are
   retired in favour of each migration's own `rollback.sh`.
6. Take a database backup (`pg_dump -Fc` of `astrumweaver`) and keep it until the
   release is accepted.
7. Fix the window, tell the clients (OpenCode users see errors while Control is
   down), and agree who may call a rollback.

## The cutover, in order

Each step has its own check. Do not start a step before the previous check passes.
Cancel any auto-rollback timer once a step's check passes.

### Phase 1: the 64K replicas, one at a time (old Control still running)

Per host (spica, canopus, sirius, deneb):

1. Lend nothing; the other three keep serving. Check no active job on the host.
2. `tsumgi-migrate-host plan ...`, then `apply ... --backup-dir /root/tsumgi-migration-<date>`.
   `apply` stops the old unit, renames users/groups/directories/profiles, rewrites
   names, and starts the new unit if it was running.
3. Wait for the Worker to be ready (about 70 s for Strata). Check: the Worker is
   `online` in Control with `runtime: tsumgi-worker`, a short streaming request
   through the public endpoint is spread over the four replicas, journal has no errors.
4. If anything is wrong: `sh rollback.sh`, check the old unit is `online`, stop.

A new-build Worker next to the old Control was rehearsed and works.

### Phase 2: vega, then polaris (a few minutes each)

Same steps. Check with a chat request on the `short` (12K) and `fast` (192K)
profiles. They are single replicas, so choose a quiet moment.

### Phase 3: Control, altair and the database (one window, clients stopped)

1. Stop clients or accept errors. Confirm there are **no non-terminal Jobs**
   (decision traffic especially): `select status, count(*) from jobs where status not in
   ('succeeded','failed','cancelled','expired') group by 1`.
2. Stop the old Control (`systemctl stop astrumweaver-control`). Workers already on
   the new build log "control unavailable" and retry; that is expected.
3. **Migrate the schema:** run `tsumgi-migrate` from the new build against the
   production database (migrations 006 and 007 add tables; nothing is dropped or
   changed). The old Control only checks that *its own* migrations are applied and
   ignores extra ones (v0.2.1 `check_storage`), so it still starts against the
   migrated database; a Control rollback therefore needs no data restore.
4. Rename the database: `ALTER DATABASE astrumweaver RENAME TO tsumgi;` (nothing
   else connected). Update the database URL in `control.env` happens in the next step.
5. Put the re-pinned decision catalog in place, then `tsumgi-migrate-host apply
   --role control --db-renamed --backup-dir ...` on the Control host. It renames the
   user, directories (including the console credentials file) and units and starts
   the new Control.
6. Check Control: `/v1/models` lists the three profiles with the unchanged chat
   revisions; the console at `/admin` asks for the login; the Workers re-appear
   `online`.
7. Migrate altair with the re-pinned serving declaration and runtime manifest:
   `tsumgi-migrate-host apply --role worker ...`. Check: embedding and decision
   answer; the embedding vector head and decision scores equal the pre-cutover
   values (the stage 3 notes compare them byte for byte).
8. Final checks: all seven Workers `online` on the new build, three chat
   profiles answer, a streaming request is spread over the replicas, the public
   `/admin` is 401 without login, no errors in any journal for ten minutes.

### Rollback

- **Per host (phases 1-2, altair after step 7):** `sh rollback.sh` in that host's
  backup directory (tested live: 5.6 s, a second round trip identical).
- **Control and database (phase 3):** stop the new Control, `ALTER DATABASE tsumgi
  RENAME TO astrumweaver`, run the Control host's `rollback.sh`, start the old Control.
  Migrations 006/007 stay in the database as unused tables; the old Control accepts
  them. The `pg_dump` taken before the window is the restore point for anything
  unexpected, not a step of the rollback.
- A Worker moved before Control is rolled back keeps working with the old Control.

## Decisions

Decided by the owner on 2026-11-23:

- **Release shape:** `v0.3.0-rc.1` first, used for the cutover; GA is promoted after a
  quiet period, by changing version metadata only (as for v0.2.0).
- **Migrations 006/007** (audio epoch fences and preparations, additive) are applied
  in the cutover window, at phase 3 step 3.

Still needed before the window:

1. Approval of this plan, and the window (date, time, who is on hand).
2. The checks in "Before the window" completed, in particular the offline load of the
   re-pinned files and the rehearsal Control against a database copy.

## Not covered

Re-pinning anything other than the decision files, a change of models or runtime
behavior, the audio features themselves (default off), and enabling
`[audio_epoch_preparation]` anywhere (it has no release path yet).
