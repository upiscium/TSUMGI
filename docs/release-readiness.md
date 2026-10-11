# Release and production deployment decision record

Status: **PREPARATION ONLY — no version, tag, publication or production rollout approved**.

Tracking: [#79](https://github.com/upiscium/TSUMGI/issues/79) (release),
[#124](https://github.com/upiscium/TSUMGI/issues/124) (operator deployment),
[#129](https://github.com/upiscium/TSUMGI/issues/129) (Nix PostgreSQL entrypoint).
This is a release-engineering checklist, not a replacement for the
[installation guide](installation.md) or
[GPU ownership ADR](adr/0001-gpu-ownership-and-vram-availability.md).

## Distinguish the three acceptance decisions

1. **Source integration:** code and documentation are merged into `main`.
   This happened for the #91 capability-serving Epic via #128.
2. **Package release:** explicitly choose immutable source, documentation,
   package versions, support scope, reproducible artifacts and a release tag.
   No tag or release is created by merely merging code.
3. **Operator deployment:** provision/reconcile a particular running Control,
   Worker, PostgreSQL database, model cache and Gateway with a tested rollback
   procedure. No such production cutover is implied by a source release.

All three decisions require different evidence. In particular, a Nix build
does not prove a persistent service can start and an installed service test
does not authorize publication.

## Exact source-line situation (2026-10-11)

| Line | Source boundary | What is known | What is not established |
| --- | --- | --- | --- |
| Previously frozen v0.1.0 candidate | `b050148ab32ba07cdc82ca776cd7aa3437265139` (after #103) | #79's F1–F5 repairs and scoped GPU/Control acceptance; #80 was administratively closed by the operator | A new #80 documentation-only complete Real Smoke was **not** recorded. The old candidate is no longer `main`; current features and TSUMGI public branding followed it. |
| Post-#91 integrated `main` | `90940d9fc39b003a242fa03d1dc4c1426136eb2a` (#128 merge) | Candidate PR CI unit/nix PASS, 1,021 Python PASS / 49 SKIP, Nix evaluation and worker/control/installer builds PASS, independent conflict-boundary reviews with no findings | Its installed Control/migration Nix CLI lacked psycopg in the effective interpreter (tracked #129). The feature tree is not an already accepted v0.1.0 release candidate or deployed production. |
| #129 repair candidate | [PR #130](https://github.com/upiscium/TSUMGI/pull/130) | Opt-in Control/Installer PostgreSQL packaging correction and regression work in review | No default assumption that an unmerged candidate is part of `main`, a release or an operator-approved upgrade. |

At this checkpoint, the Python project declares `0.1.0.dev0` and the
Nix package declares `0.1.0-dev`. **These are development identifiers,
not a decision to tag or publish `v0.1.0`.** No matching repository
release tag was observed during the 2026-10-11 source check.

## Release-line choice required before freezing a candidate

- **Option A — preserve the older v0.1.0 baseline:** Create an explicit
  release branch/tag candidate from the approved historical SHA, with an
  accurate TSUMGI/AstrumWeaver naming/support statement. Re-evaluate any
  known packaging or installation defects at that *exact* revision. Do not
  claim that the modern #91 functionality is included or that old test
  evidence covers subsequent changes.
- **Option B — nominate the newer integrated main:** Select a new immutable
  commit *after required packaging fixes*, decide the appropriate version
  number (do not assume it is still v0.1.0), and explicitly review database
  migrations, client behavior, release notes and compatibility. Fresh
  validation must bind to that final SHA; do not inherit the historical
  #80 Real Smoke technical PASS that was never recorded.

Neither path is chosen in this document; selecting a version/commit or
creating a tag remains an explicit maintainer action.

## Candidate qualification checklist

| Gate | Required record | Current result at this checkpoint |
| --- | --- | --- |
| Source identity | Frozen source SHA, packaging SHA, documentation SHA, version and intended support tier | **DECISION REQUIRED** |
| Nix/CI | Reproducible builds of actual `#worker`, `#control`, `#installer`; full Python/PostgreSQL regression; `nix flake check`; GitHub CI on final SHA | **PREVIOUS INTEGRATION PASS** at #128 candidate; re-evaluate after changes |
| PostgreSQL/Control | Actual installed `astrumweaver-migrate` has psycopg, migrations 000–005 on fresh compatible DB, Control ready, duplicate migration is safe | **BLOCKED until #129 exact merged revision accepted** |
| DB compatibility | Read-only inventory of server/DB encoding and version, backup/restore proof; clear downgrade policy for migrations 003–005 | **OPERATOR GATE**. SQL_ASCII defect tracked in #108; do not claim supported. |
| Client lifecycle | Chat tools, streaming, cancellation, timeout, embedding space mismatch, Decision invalid semantics and auth checks | **SCOPED PASS** with open real disconnect regression #110; repair or explicitly document impact |
| Security | Runtime model pins, private UDS/bearers, dedicated service UID, permissions, no cross-UID access, reviewed Gateway ingress/auth | **PARTIAL #124** (disposable evidence, not a production identity decision) |
| Service upgrade/rollback | Proven prior-installed-version upgrade and recovery, state/profile/credential snapshots, no orphaned ports/jobs | **NOT RUN for old-version → new-version → old-version**, tracked #124 |
| Claims | Coding experimental, embedding fixture/space-scoped, System-One OBSERVED_ONLY/uncalibrated/recommendation-only; no unsupported remote quota savings | **MUST RETAIN** |
| Publication | Maintainer approves final version/notes, immutable tag, release assets and checksums, maintenance/support scope | **NOT APPROVED / NOT RUN** |

The #80 documentation-only Real Smoke issue is CLOSED by explicit
**operator administrative acceptance**. This must not be transformed into an
assertion that its unchecked technical observations passed on a new revision.

### Database and rollback are separate authorities

Do not run migrations or install a new Control binary against a production
database merely because source CI passed. Confirm database backups, test
restore, application version compatibility, outstanding Worker leases and
operator-maintained schema ownership. The SQL files in `migrations/`
describe *forward* transitions; do not invent or assume a downgrade path.
Restoring an older binary is not equivalent to safely restoring a migrated
PostgreSQL schema. An actual production change needs an explicit runbook and
an accepted recovery point.

For non-production qualification, prefer a disposable PostgreSQL 17 database
with UTF8 encoding; #108 documents a concrete SQL_ASCII migration-tracking
failure. PostgreSQL 16 use in earlier GPU fixtures is bounded evidence,
not a broad support certification.

### Persistent Worker/Control rollback is not source revert

For first installation into a previously empty systemd target, rollback can
mean stopping and removing the *test-owned* units, accounts and config after
verifying no old workload is affected. For an existing production service,
rollback means returning the **pre-existing installed version**, with its
original profile, service unit/drop-ins, files/permissions, credentials,
Control bindings and compatible database state. These are materially
different acceptance tests; #124 has evidence for the former only.

## Release decision record to fill in

Before tagging or deployment record, in a separately reviewed issue/PR:

- chosen option A/B, exact immutable source/lock/documentation SHAs and
  intended version;
- release notes and support/experimental capability matrix;
- final CI, installed package, PostgreSQL and negative-path evidence;
- explicit treatment of open issues #108, #110, #124, #129 and #120;
- the operator's publication authorization, **separate** from any live
  production deployment authorization.

Never publish private hostnames, device UUIDs, CT IDs, PostgreSQL URIs,
bearer credentials, model path names, raw user prompts or home-lab inventory.
