# Operator-owned Control / Worker rollout and recovery gate

**Current integrated-main handoff (2026-10-14):** [release/source/site decision evidence](release-gate-2026-10-14.md) at merged main `597f1e85ee56fd1b6c997831bac68bbce4b4f289`. #135's runbook has been merged; #137's bounded best-effort source fix was subsequently integrated via #145 with main CI unit/nix SUCCESS. Read-only inspection found three active Worker services in authorized isolated GPU test LXCs. This is **not** proof that the named production site, Control/database/ingress, permanent Worker-UID compartment, old-version rollback or maintenance window have been accepted. Do not replace already-running services or treat test evidence as site GO.

Status: **OPERATOR DECISION REQUIRED — NOT A PRODUCTION RUNBOOK AUTHORIZATION**.

Tracking: [#124](https://github.com/upiscium/TSUMGI/issues/124)
(operator production acceptance),
[#79](https://github.com/upiscium/TSUMGI/issues/79) (version/publication)
and [release readiness](release-readiness.md). This checklist preserves
the separation between a reviewed repository commit, a published package
release and an approved change to a particular running site. It must be
completed against one **immutable candidate**, not whatever `main` points
to at execution time.

## Evidence already available (bounded, not transferable)

- **2026-10-11:** A disposable GPU LXC exercised a real *already-running*
  old candidate → new candidate → return to the **previously working old**
  Control/Worker systemd executables. Dedicated non-shared Control and
  Worker accounts, private role credentials, real GPU Embedding and
  experimental System-One requests, unauthorized 401, pre-change unit and
  config hashes, backup to a second PostgreSQL database, restoration of
  the original binaries and byte-identical unit/config state all passed.
  The experiment did not downgrade a modified schema, bind a published
  version or change a production machine.
- **2026-10-12:** The post-#134 main source
  `6560ec999c9ef58417a97c307aee15eba5d11da2` built actual Nix
  Control/Installer/CLI-check outputs. Fresh isolated **PostgreSQL 17.11
  UTF8** accepted migrations 000–005, Control Ready and authenticated
  durable Job operations. A custom-format backup was restored to a
  **separate database**; Control then read and updated the restored Job
  and the original database remained unchanged. The test database and
  services were removed.
- **2026-10-12:** #110 / #134 reached main with post-merge CI Green.
  Real OpenCode subprocess termination was previously verified on a
  disposable CUDA Worker; release qualification still depends on the
  exact deployment model and operating context.

These are **disposable technical observations**, not a production acceptance
signature. No global GPU exclusivity, schema downgrade, production disaster
recovery time, universal model quality or service availability is implied.

## Gate A — Authority, identity and immutable baseline

Record privately, before any change:

- Named operator/change owner, scope of approved host(s), maintenance window,
  rollback authority, affected consumers and safe downtime threshold.
- SHA of source, flake lock, intended packages and documentation; previously
  installed Control/Worker binaries and **their immutable store outputs**.
- All pre-existing systemd units, drop-ins, environment files, profile
  symlinks, Config and Serving/Runtime manifests, model pin/identity,
  active Worker enrollments, jobs and leases. Save read-only checksums and
  ownership/permissions outside the Worker-writable state.
- Confirm distinct dedicated Control and Worker service identities. Worker
  UID must not be shared with arbitrary inference consumers, downloaders,
  shell users or untrusted processes. Prove different unprivileged UIDs
  cannot read the role-private API keys, model-owned native bearers or UDS
  directory. Same-UID and root access remain within the trusted boundary.
- Confirm exact selected GPU UUIDs, visible/allowed device nodes and the
  Worker isolation mode. Per [GPU ownership ADR](adr/0001-gpu-ownership-and-vram-availability.md),
  physical hypervisor/host exclusion of unrelated processes is
  **infrastructure/operator responsibility**; TSUMGI enforces enrolled
  logical ownership, selected-device identity and VRAM availability checks.
  A snapshot of free VRAM is **not** a reservation.

**NO-GO** if the rollback binary/config, secret identity, selected GPU
boundary, operator authority or recoverable state cannot be identified.

## Gate B — Data compatibility and recoverable backup

- Inventory actual production PostgreSQL version, **server/client UTF8**
  encoding, role privileges, database extensions and schema migration
  history. PostgreSQL 17 is the supported/recommended major; a PG16
  disposable test is not equivalent to certifying a production DB.
- Before starting new migrations, capture an encrypted/authorized backup
  and prove **restore into an independent compatible database**. Verify
  that the restored Control can become Ready and read representative
  durable records. Record the recovery point and acceptable data-loss
  window without printing the connection URL or credentials.
- Inspect migrations 000–005 and any later forward migration. An old
  Control binary working against an old schema does **not** establish
  compatibility with a newer schema. Do **not** assume `pg_restore`
  into a populated production database is safe, or that reverting Nix
  executables reverses SQL migrations.
- If the new schema is not backward compatible with the old binary, the
  rollback plan must identify an **operator-reviewed restore target**,
  application drain/stop sequence and data-reconciliation policy before
  any schema mutation. A restoration target must not overwrite the only
  good copy or silently discard newer accepted Jobs.

**NO-GO** if backup restoration, data compatibility or recovery authority
is unverified for the specific target site.

## Gate C — Rehearsal, upgrade and negative paths

On an *independently authorized disposable* instance matching the intended
candidate, confirm the sequence:

1. Start the **old installed** version as an operating baseline, not a
   freshly empty unit pretending to be an upgrade. Check READY, enrolment,
   native GPU model availability, authenticated requests and denial (401)
   for unauthorized clients.
2. Capture old unit/config/profile hashes, release/lock SHAs and a
   recoverable PostgreSQL snapshot. Confirm token/storage permissions under
   the effective systemd UIDs, including a negative cross-UID probe.
3. Stop/drain relevant services deliberately, apply *only* candidate-owned
   package/profile/unit changes, and restart at the new immutable source.
   Verify READY, exact executables, durable Worker identity, lease safety,
   logical GPU selection, native inference, cancellation/restart/health
   failure and unauthorized client rejection.
4. Exercise rollback **to the old *already working* executables**, removing
   only new-change-owned drop-ins. Check old service readiness and native
   requests; compare pre-change unit, environment, profile and serving
   identity digests. If schema was modified, exercise the independently
   accepted compatible data-recovery path rather than just process restart.
5. Remove *only* test-owned accounts, units, data and native model processes.
   Revalidate previously existing profiles and work are untouched.

A fail-closed VRAM admission is acceptable; it is not acceptable to report
READY while a native child is unavailable or to reassign a GPU belonging
to an unrelated Worker's authority domain.

## Gate D — Site-specific go / no-go and publication separation

**Production rollout remains HOLD** until an authorized operator records:

| Required field | Record |
| --- | --- |
| Site/change owner and scheduled window | Decision pending |
| Selected immutable code/lock/package/manifest digests | Decision pending |
| Original installed version and rollback target | Decision pending |
| PG17 UTF8 inventory, snapshot/restore and RPO/RTO | Decision pending |
| Dedicated UID, native secret and model path negative probes | Decision pending |
| GPU selected UUID/device exposure and infrastructure assumptions | Decision pending |
| Client ingress/authentication and unaffected consumers | Decision pending |
| Dry-run upgraded service, recovery evidence and GO/NO-GO | Decision pending |
| Authorized production activation and explicit rollback trigger | Decision pending |

Do not publish database DSNs, tokens, model file paths, exact host identity,
GPU UUIDs or unredacted inference payloads in GitHub issues or release notes.
A source release may be approved **without** approving site deployment, and
site readiness does not automatically approve a version tag or public release.
System-One stays **OBSERVED_ONLY**, uncalibrated and recommendation-only;
Coding is experimental; Embedding acceptance is exact-space/fixture-scoped.
