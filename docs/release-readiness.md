# Release and production deployment decision record

**Latest authoritative integrated-main checkpoint (2026-10-14):** [TSUMGI source, release and operator handoff](release-gate-2026-10-14.md), bound to merged main `597f1e85ee56fd1b6c997831bac68bbce4b4f289`, completed post-merge GitHub unit/nix CI [#38541792235](https://github.com/upiscium/TSUMGI/actions/runs/38541792235), and scoped PostgreSQL17 best-effort admission tests. The older 2026-10-12 and 2026-10-11 checkpoints below are **historical**, not current GitHub status. #137 is CLOSED via merged #145, #135/#136 have been merged, and #79/#124 remain distinct OPEN gates. No version, tag, publication or production rollout approved.

Status: **PREPARATION ONLY — no version, tag, publication or production rollout approved**.

Tracking: [#79](https://github.com/upiscium/TSUMGI/issues/79) (release),
[#124](https://github.com/upiscium/TSUMGI/issues/124) (operator deployment),
[#129](https://github.com/upiscium/TSUMGI/issues/129) (completed Nix PostgreSQL entrypoint repair).
This is a release-engineering checklist, not a replacement for the
[installation guide](installation.md) or
[GPU ownership ADR](adr/0001-gpu-ownership-and-vram-availability.md).

## Historical qualification checkpoint — 2026-10-12 (superseded by 2026-10-14)

**Integrated source:** `main` at
`6560ec999c9ef58417a97c307aee15eba5d11da2`
([#134](https://github.com/upiscium/TSUMGI/pull/134) SSE disconnect
repair). Post-merge [GitHub CI #38215871622](https://github.com/upiscium/TSUMGI/actions/runs/38215871622)
completed **unit and nix SUCCESS**. This is a verified source boundary,
**not an authorized release candidate or a deployed production version**.

Earlier sections below retain the **historical 2026-10-11 checkpoints**:
they must not be interpreted as today's live issue/PR state. The
technical fixes [#108](https://github.com/upiscium/TSUMGI/issues/108)
(PostgreSQL UTF8-only Control/migration contract),
[#129](https://github.com/upiscium/TSUMGI/issues/129)
(Control/Installer role-scoped psycopg closure), and
[#110](https://github.com/upiscium/TSUMGI/issues/110)
(SSE client disconnect lifecycle) have since all been **merged to main
and CLOSED as completed**. They are no longer open release blockers
on the updated source.

| Qualification dimension | Latest evidence | Remaining authority/limitation |
| --- | --- | --- |
| Reviewed source/CI | #130, #132, #134 code integrated; final #134 main CI unit/nix SUCCESS | A release tag, package/version identifier, artifact checksum and publication authority are not assigned |
| Control/Installer package | Installed Nix `astrumweaver-migrate` has PostgreSQL driver only in relevant roles; Worker package remains psycopg-free | Exact final version/lock must be frozen and independently requalified if changed |
| SQL and persistence | UTF8-only admission; 000–005 migrations; **2026-10-12 PG17.11 disposable Control/Installer build, backup to independent DB, restored Control Ready and durable Job read/mutation PASS** | Does not demonstrate automatic downgrade of a changed production schema or validate site backup/restore RPO/RTO |
| Real native serving | #124 disposable dedicated-UID installed old→new→old systemd rollback with two GPU models PASS; #110 real OpenCode 1.18.35 SIGKILL → durable PostgreSQL CANCELLED → native CUDA inference slot idle **6/6 PASS** | No universal model quality, native inference reliability SLA, production hardware layout, full consumer quota savings or old OpenCode 1.18.30 GPU replay claim |
| Client semantics | Stage B streamed plain/tool OpenCode acceptance and #110 disconnect fix on exact reviewed code | Coding remains experimental, Embedding exact-space scoped, Decision/System-One **OBSERVED_ONLY** and uncalibrated |
| Historical documentation-only smoke | #80 administratively CLOSED on explicit owner instruction | No fabricated post-RS1 full documentation-only Real Smoke PASS; cannot transfer its historical evidence to a new candidate SHA |
| Actual production rollout | [#124](https://github.com/upiscium/TSUMGI/issues/124) remains OPEN; the operator gate runbook is proposed in [PR #135](https://github.com/upiscium/TSUMGI/pull/135) | Permanent site UID/credentials, ingress, GPU/host assumptions, prior binary/DB restore target and explicit GO/NO-GO still require separate operator approval |
| Version and publication | Python `0.1.0.dev0`, Nix `0.1.0-dev`; no `v*` Git tags found in the 2026-10-12 check | **Issue #79 remains OPEN**. Version, release notes, binary provenance, checksums, release support tier and publication are not approved |

The existing dual-track release choice remains explicit:

- **Option A:** Preserve and publish a suitably requalified older
  frozen v0.1 baseline, with no claims about the new #91/110 features.
- **Option B:** Select a new immutable commit from the integrated
  source after documentation is finalized; define the actual version
  and support tier, then perform final-candidate CI, package/PG17
  qualification, security and documented rollback preflight.

**Preparation recommendation:** use **Option B as the next release
planning target**, because the reviewed corrections and merged TSUMGI
capabilities are in the current source. This is **not** owner selection
of a version or authorization to tag/publish. Option A remains
available until the maintainer chooses otherwise.

Release and production activation are **separate decisions**; successful
site-level recovery tests cannot authorize tagging, and source
release sign-off cannot change a site without its own operator approval.

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

## Historical source-line situation (2026-10-11 — superseded)

This table records the pre-#130/#132/#134 state and is intentionally retained
for auditability. **Refer to the current checkpoint above for live readiness.**

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

## Historical candidate qualification checklist (2026-10-11)

The status column below is a retained earlier pre-merge observation, not a
current blocker report. See the authoritative 2026-10-12 checkpoint above.

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
different acceptance tests. #124 has since qualified a *disposable*,
persistent-systemd **old working version → new version → old version**
rollback, but no operator-owned site production recovery was approved.

## Release decision record to fill in

Before tagging or deployment record, in a separately reviewed issue/PR:

- chosen option A/B, exact immutable source/lock/documentation SHAs and
  intended version;
- release notes and support/experimental capability matrix;
- final CI, installed package, PostgreSQL and negative-path evidence;
- explicit disposition of completed technical #108/#110/#129 and still-open
  operator gate #124 and naming-migration gate #120;
- the operator's publication authorization, **separate** from any live
  production deployment authorization.

Never publish private hostnames, device UUIDs, CT IDs, PostgreSQL URIs,
bearer credentials, model path names, raw user prompts or home-lab inventory.
