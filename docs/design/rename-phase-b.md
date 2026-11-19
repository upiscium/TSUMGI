# TSUMGI rename, Phase B: runtime and wire rename (design)

Status: **draft for approval**. Parent: #120. Target release: **v0.3.0** (breaking).

## Decisions already made (owner, 2026-11-19)

1. The runtime is renamed **in full**: Python package, commands, Nix, systemd
   units, service users and groups, paths, environment variables and the
   database-facing names. `docs/naming.md` ("stable until proven") is replaced
   by this plan once it is done.
2. The **wire and API names change too**, as a breaking change. There are no
   long-lived compatibility aliases for the new release.

## Principles

- One flag day per host, one host at a time, always with a rollback.
- No bulk search-and-replace: each category below is its own reviewed change.
- The code reads only the new names. Old names exist only inside the **host
  migration tool** (it rewrites on-host state) and the documented upgrade notes.
- Nothing in production changes until an isolated rehearsal has passed and the
  owner approves the cutover plan.

## Name map

| Area | Old | New |
| --- | --- | --- |
| Python distribution, import package | `astrumweaver`, `src/astrumweaver` | `tsumgi`, `src/tsumgi` |
| Console commands (16) | `astrumweaver-<x>` | `tsumgi-<x>` |
| Module strings in generated config | `astrumweaver.executors.structured_echo:create_executor` | `tsumgi.executors.structured_echo:create_executor` |
| Nix flake outputs and derivations | `astrumweaver*` | `tsumgi*` |
| NixOS options | `services.astrumweaver.{control,worker}` | `services.tsumgi.{control,worker}` |
| systemd units | `astrumweaver-control.service`, `astrumweaver-worker.service`, `astrumweaver-worker-gpu-isolation-preflight.service` (+ `.d/` drop-ins) | `tsumgi-control.service`, `tsumgi-worker.service`, `tsumgi-worker-gpu-isolation-preflight.service` |
| Users and groups | `astrumweaver`, `astrumweaver-control`, group `astrumweaver-config` | `tsumgi`, `tsumgi-control`, `tsumgi-config` (same uid/gid, renamed in place) |
| systemd directories | `RuntimeDirectory=astrumweaver-{control,worker}`, `StateDirectory=astrumweaver` | `tsumgi-{control,worker}`, `tsumgi` |
| Config and state paths | `/etc/astrumweaver`, `/var/lib/astrumweaver`, `/var/lib/astrumweaver-control`, `/opt/astrumweaver`, `/usr/local/libexec/astrumweaver`, `/run/astrumweaver*`, `/var/cache/astrumweaver` | the same paths with `tsumgi` |
| Nix profiles | `/nix/var/nix/profiles/astrumweaver-*` | `/nix/var/nix/profiles/tsumgi-*` |
| Environment variables (39) | `ASTRUMWEAVER_*` | `TSUMGI_*` |
| Client API | `x_astrumweaver` response object | `x_tsumgi` |
| Embedding request fields | `x_astrumweaver_input_type`, `x_astrumweaver_embedding_space_id` | `x_tsumgi_input_type`, `x_tsumgi_embedding_space_id` |
| Object type strings | `astrumweaver.decision`, `astrumweaver.decision_profile`, `astrumweaver.embedding_space` | `tsumgi.*` |
| Operation schema | `astrumweaver-decisions-v1` | `tsumgi-decisions-v1` |
| Evidence field and schema ids | `astrumweaver_revision`, `astrumweaver-d1-gpu-smoke-v1` | `tsumgi_revision`, `tsumgi-d1-gpu-smoke-v1` |
| PostgreSQL `application_name` | `astrumweaver-control`, `astrumweaver-migrate` | `tsumgi-control`, `tsumgi-migrate` |
| PostgreSQL database | `astrumweaver` (production) | `tsumgi` (renamed in place) |
| Process names | `astrumweaver-llama-server` | `tsumgi-llama-server` |

Left alone: published release notes under `docs/releases/` (history), and the
`github:upiscium/AstrumWeaver` URL (GitHub redirects it).

## Things that can break silently (check first, in stage 1)

- **Stored values compared against code.** `operation_schema` reaches the
  gateway from serving profile data (catalog JSON, serving tables). The code
  compares it with a constant; existing rows and catalogs must be rewritten or
  the gateway rejects them. Also check migration 003/004 content.
- **Digests.** Embedding space ids and the decision profile digest are sha256
  of canonical JSON. If the hashed payload contains a renamed string, the id
  changes and clients holding the old id break. Verify, then decide per id
  whether to keep the hashed bytes stable or accept the new value.
- **Deployment revisions and pins.** `executorFactory` strings and Nix store
  path names feed deployment revisions. `validation/runtime_deployment.py`
  matches store paths by name (`-astrumweaver-gpu-preflight/`). Every Worker's
  revision will change at cutover; Control's serving contracts must follow.
- **Shared `StateDirectory`.** Control and Worker both declare
  `StateDirectory=astrumweaver`, and `control.nix` asserts that literal value.
  Renaming without moving data makes systemd create an empty directory and
  orphan the old one.
- **Host-state comparisons.** `setup/migration.py` compares User, Group and unit
  text against what is on the host. It is the place to extend, not duplicate.
- **Migration checksums.** Confirm whether `schema_migrations` stores file
  checksums; header-comment edits in SQL files would then break upgrades.
- **Secrets and env files** outside the repository (`/run/secrets/*.env`,
  `Environment=` lines, operators' own env files) carry the old variable names.

## Stages

Each stage is one or more PRs, merged to `main` in order. Stages 1-3 change no
host. The version stays 0.2.1 until the release in stage 6.

1. **Stored-data audit** (read-only): answer every item above with evidence,
   against a copy of the production database and the live catalogs. Output: an
   addendum to this document.
2. **Code rename**: Python package and import strings, console commands, Nix
   package, outputs and module options, CI. Tests green; `nix flake check`;
   fresh-install and generated-NixOS checks.
3. **Wire rename**: client API fields, object types, schema ids, evidence
   fields, plus the data rewrite for stored values. Docs and clients updated.
   Clients known to read these fields are listed and fixed in the same
   release (dotnix is checked explicitly).
4. **Host migration tool**: one idempotent command (`tsumgi-migrate-host`,
   built on `setup/migration.py`) that, on one host: stops the unit, renames
   the user, group, units, drop-ins, directories and profiles, rewrites env
   and config names and module strings, moves state in place (same file
   system), starts the new unit and runs the health checks. It writes a
   rollback script first. Unit-tested against synthetic hosts, including
   half-done states.
5. **Rehearsal**: run the whole path on an isolated host. There is no
   dedicated test host, so lend one 64K replica with
   `tsumgi-gpu-mode development`, use a copy of the database, and rehearse
   Control and one Worker, then the mixed-version window and the rollback.
6. **Release v0.3.0**: release PR and recipe as for v0.2.x (reproducible
   archive, notes with the upgrade guide and the breaking-change list).
7. **Production cutover** (separate owner approval, with a time window):
   - Order: database untouched first, then Control (new database name last,
     after Control is stopped), then the 64K replicas one at a time, then
     vega, altair, polaris (these three stop their service for a few minutes).
   - Each host has a rollback and a post-check (health, a chat request, the
     console). Auto-rollback timers are cancelled once the check passes.
   - Clients that use the renamed API fields are switched in the same window.
8. **Close-out**: update `docs/naming.md`, close #120 and its follow-ups, and
   record the new layout in the operations notes.

## Out of scope

Renaming the repository again, changing the model or runtime behavior, and any
protocol change other than the names above.

## Stage 1 audit results (2026-11-19, read-only)

Method: `git grep` on `main` (f758468); read-only queries on the production
database (`default_transaction_read_only`); read of the live Control, spica
and altair config; offline digest checks. Nothing was changed on any host.

### Verdicts on the open questions

| Question | Result | Consequence |
| --- | --- | --- |
| Do table, column or role names contain the old name? | **No.** Migrations only mention it in header comments. | The database keeps its structure. Only the database name (`astrumweaver`) and `application_name` change. |
| Does `schema_migrations` check file contents? | **No.** It stores the file name and the time only. | Editing migration header comments is safe. |
| Rows that contain the old name | `jobs` 1831, `job_events` 25, `workers` 7 of 1960, 95039 and 7. Almost all are **user content** (file paths a model was asked about, model metadata with `/opt/astrumweaver/...` paths) and old `model_alias` values. | Immutable history: **do not rewrite it**. The rename must not need it. |
| Stored `operation_schema` | `astrumweaver-decisions-v1` in 152 terminal jobs and in altair's advertised contract. The claim query compares a job's serving `operation_schema` with the Worker's advertised contract (`control/postgres.py:1401`). | The rename must happen at a moment with **no non-terminal decision Jobs**, with Control and altair moved together. Old terminal rows stay as they are. |
| Do renamed strings feed digests? | **Yes for decision, no for the rest.** A serving contract's `revision` hashes its `operation_schema`, so `tsumgi-decisions-v1` gives altair's decision contract a new revision (and a new profile revision). Chat (`openai-chat-completions-v1`) and embeddings (`openai-embeddings-v1`) contain no old name. Embedding space ids and the decision semantics id hash only artifact digests and numbers. | The decision catalog and altair's serving declaration are **regenerated and re-pinned** at cutover. Chat and embedding pins stay valid. |
| Do deployment revisions depend on on-host paths? | **No for Strata.** The deployment identity hashes artifact digests, provider id, quantization and `execution_config_sha256`; the `/opt/astrumweaver/...` paths in `runtime.json` are not part of it. **For altair** the runtime artifact is a Nix store path named `astrumweaver-d1-decision-cuda-sm61-runtime`, so renaming the derivation changes the store path and must be checked against `runtime_artifact_sha256`. | Strata hosts keep their revisions through the path rename. altair's runtime artifact hash is verified in the rehearsal (stage 5). |
| Do outside repositories or clients read the old API names? | **No.** A code search across the owner's repositories finds the old names only in this repository (gateway, validation CLIs and docs). dotnix does not use them. The live `/v1/models` already reports `owned_by: tsumgi` (set by the catalog); the code default is still `astrumweaver`. | Clients need no change at cutover. Only the repository's own validation CLIs and docs move. |

### Additions to the name map

- `owned_by` default in the chat gateway (`"astrumweaver"` -> `"tsumgi"`).
- Worker heartbeat metadata `"runtime": "astrumweaver-worker"` (7 `workers` rows).
- Task and thread names inside the Worker (`astrumweaver-worker-*`) and the
  temporary prefixes `astrumweaver-*-accept-`; renamed with the package.
- Sentinel strings `ASTRUMWEAVER_TOOL_OK` and `ASTRUMWEAVER_CHAT_OK` in the OpenCode
  acceptance CLI.

### Live host inventory (what the host migration tool must handle)

- Users and groups: `astrumweaver` (uid 999, home `/var/lib/astrumweaver`),
  `astrumweaver-control` (uid 996), group `astrumweaver-config`.
- Units: one `astrumweaver-worker.service` per Worker (+ drop-ins `20-`, `70-`,
  `80-`), `astrumweaver-control.service` (+ `70-`, `71-`).
- Paths per Worker: `/etc/astrumweaver` (including per-profile directories),
  `/var/lib/astrumweaver`, `/opt/astrumweaver` (Strata, 24 GB; **same file
  system, so a rename, not a copy**), `/usr/local/libexec/astrumweaver`,
  `/run/astrumweaver-worker`. Control: also `/var/lib/astrumweaver-control`
  (holds the console credentials file) and `/var/lib/astrumweaver-legacy-review`.
- Leftovers on polaris: about 40 `/opt/astrumweaver-gpu-*` evidence logs and
  directories from old acceptance runs. They are history, not runtime; the
  tool leaves them alone and the cutover notes say so.
- Environment variables in use on hosts: Control `ASTRUMWEAVER_DATABASE_URL`
  and `ASTRUMWEAVER_WORKER_TOKEN`; Workers `ASTRUMWEAVER_WORKER_TOKEN`. The names
  are stored in `/etc/astrumweaver/control.env` and the Worker env file.
- Databases on the DB host: `astrumweaver` (production, 4 tables) plus two
  rehearsal databases from October (`tsumgi_rc1_*`, already renamed).
- The Control admin console keeps working across the rename: its credentials
  file lives under `/var/lib/astrumweaver-control`, which moves with the
  directory.

### Plan changes from the audit

1. Stage 3 gets an explicit **decision-catalog and altair re-pin** step, and a
   rule: cut over decision traffic only with zero non-terminal decision Jobs.
2. Stage 4's tool also renames the env var *names* inside env files and
   systemd drop-ins, and keeps secret values untouched.
3. Stage 5's rehearsal must verify altair's `runtime_artifact_sha256` against
   the renamed Nix derivation, and measure the Strata directory rename.
4. The database rename (`ALTER DATABASE ... RENAME`) needs every connection
   closed: Control stopped, and no other client. It is the last step on the
   Control host.

## Stage 5 results (2026-11-19, live rehearsal on one lent host)

Host: canopus (a 64K Strata replica), lent with `tsumgi-gpu-mode development`; the
other three replicas kept serving. Control and every other host were not touched;
on them only the read-only `plan` was run.

### What worked

- `plan`, `apply` and `rollback` ran for real (`usermod`, `groupmod`, `systemctl`,
  directory renames, profile and symlink retargeting). uid 999 and the group ids
  stayed; the 24 GB Strata directory was renamed, not copied.
- A **mixed-version window works**: the migrated Worker (new build, `tsumgi` names,
  config under `/etc/tsumgi`) joined the old Control, loaded its model in about
  70 s, and took its share of streaming requests (2 of 8, evenly with the three
  old-name replicas).
- The rollback took 5.6 s and restored everything. A second round trip compared
  mode, owner, type and link target of 55 entries before and after: identical.
- The lent host returned to production in about 120 s.

### What the rehearsal found (all fixed in the tool, each with a test)

1. Strata's absolute symlinks (`bin/strata`, `data/terminal/current`) would have
   dangled after the move: now retargeted and restored by the rollback.
2. polaris's live config names a Nix profile `astrumweaver-runtime-qwen38`: such
   profiles and their generation links are now renamed too.
3. Earlier rollback copies under `/etc/astrumweaver/rollback` were being rewritten:
   now moved untouched. A leftover pinned file that nothing refers to warns instead
   of blocking; a base unit `ExecStart` that a drop-in resets is not a reference;
   `--allow-pinned` lets the operator name a stale file that something still
   mentions.
4. **The rollback restored files as `root:root`** (the backup lost the owner and
   `cp -p` applied it), so the old Worker, which reads its config through the
   `astrumweaver-config` group, crash-looped with `PermissionError` until the
   owners were corrected by hand. The backup now keeps the owner; verified with
   the second live round trip. The unit tests run as an ordinary user and could not
   have found this: a rollback needs a live check on every host class.

### Read-only `plan` on the other hosts

| Host | Result |
| --- | --- |
| polaris, vega | clean (warnings about old rollback scripts in `/usr/local/sbin` only) |
| spica | blocked by a stale `runtime-deployment.json` that a stale config names: needs `--allow-pinned` after checking it is unused (same for sirius, deneb as they turn out) |
| altair | blocked, as designed: `runtime-deployment.json` and `serving-deployment.json` must be re-pinned (decision) |
| tsumgi-control | blocked, as designed: the decision catalog must be re-pinned |

### Still open before the cutover (stage 7)

- **Re-pin altair and the Control decision catalog** with the new build: not
  rehearsed. It changes altair's decision contract revision.
- **Control and the database** were not rehearsed live (Control is the single
  entry point). The ordering is in `docs/host-migration.md`; a transient new-build
  Control against a copy of the database is the next check.
- `/usr/local/sbin` scripts (`tsumgi-gpu-mode`, the `tsumgi-rollback-*` family,
  `astrumweaver-install-llama-cpp-pascal`) still name the old units and binaries
  and must be updated by hand per host; `tsumgi-gpu-mode` stops working on a
  migrated host until it is.
- Strata's own files under `/opt` were not checked for stored absolute paths
  beyond what a live start proved (the engine loaded and served).
