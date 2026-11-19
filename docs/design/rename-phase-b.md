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
