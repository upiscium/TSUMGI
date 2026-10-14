# TSUMGI integrated-main release and site handoff — 2026-10-14

**Status: RELEASE PREPARATION / OPERATOR PREPARATION. Neither a version freeze, publication approval nor a production GO.**

This is the latest verified checkpoint for [#79](https://github.com/upiscium/TSUMGI/issues/79) (version/publication) and [#124](https://github.com/upiscium/TSUMGI/issues/124) (site rollout and recovery). It supersedes the 2026-10-11/12 *current* wording retained historically in [release-readiness.md](release-readiness.md), but not the scoped experimental evidence or this document's other linked contracts. It is an **evidence and decision manifest**, not an execution script.

## Immutable source / provenance facts

| Item | Verified boundary |
| --- | --- |
| Current integrated `main` | `597f1e85ee56fd1b6c997831bac68bbce4b4f289` |
| `main` Git tree | `4a769b32f8340b20b32e7ee8575d7b7786fa8756` |
| Merged #145 reviewed tree | **Same** tree `4a769b32f8340b20b32e7ee8575d7b7786fa8756`; PR HEAD `2cb07c5151c57ac936a673fb749390cde1bdf8f7` |
| `flake.lock` SHA-256 | `66725f157e1465d8db020dde5b79d3858b5d007949057a2a26a2b5e604895c9e` |
| Post-merge `main` push CI | [#38541792235](https://github.com/upiscium/TSUMGI/actions/runs/38541792235): **unit SUCCESS / nix SUCCESS**, including generated NixOS module evaluation |
| Tested Python/PG boundary | Reviewed PR #145 exact tree on a scratch, Unix-socket-only PostgreSQL **17.11**: **1,167 PASS / 2 SKIP / 0 FAIL**; not a substitute for the operator's site database |
| Packaged Nix roles | `#control`, `#worker`, `#installer` built **PASS** from the same reviewed PR tree; no installation or image publication |
| Declared development versions | Python `0.1.0.dev0`; Nix `0.1.0-dev`. No release version was selected; no release/tag was observed in the 2026-10-14 repository preflight |

**#137 is CLOSED/completed** after [PR #145](https://github.com/upiscium/TSUMGI/pull/145) merged; [#110](https://github.com/upiscium/TSUMGI/issues/110) is also CLOSED/completed. The latter separately demonstrated six real OpenCode 1.18.35 process kills during CUDA model inference on a disposable host, with durable cancellation and Worker capacity release (**6/6**). **This is historical hardware evidence from #134, not a fresh native-GPU replay at the #145 exact tree.**

## Release candidate choice: #79

The maintained [release decision matrix](release-readiness.md) has two options:

- **A — frozen historical v0.1.0 line** (earlier source `b050148ab32ba07cdc82ca776cd7aa3437265139`): requires its *own* immutable candidate requalification, excludes later feature/fix claims, and is no longer `main`.
- **B — integrated current source**: **recommended** for the next release candidate because Embedding + experimental System-One integration and #110/#137 cancellations are now in the main code. Choose an immutable source and version explicitly, requalify installed outputs and documentation, and describe the remaining best-effort limits.

**Neither A nor B has been selected or tagged on behalf of the maintainer.** A future version identifier/RC number, supported platforms, signing/provenance, artifact digests, release notes and actual publication authority require a separate **#79 Human Accept**. Do **not** assume that `0.1.0.dev0`/Nix `0.1.0-dev` are a production release.

### Proposed support/claims matrix for Option B (not yet accepted)

| Surface | Provisional claim / exclusion |
| --- | --- |
| Control persistence | PostgreSQL **17.x**, UTF8 server/client encoding; migrations 000–005 and installed Control/Installer entrypoints. Do not auto-upgrade another major version or touch a live DB. |
| GPU / scheduling | Exact selected-GPU enrollment and fail-closed conservative pre-start free-VRAM check. **No host/Proxmox-wide GPU exclusivity promise or VRAM reservation.** |
| Chat API and OpenCode | Reviewed OpenAI-compatible stream/cancellation implementation, with real prior OpenCode 1.18.35 CUDA SIGKILL acceptance. **No universal model-quality or latency SLA.** |
| Admission cancellation (#137) | Resource-bounded **best effort**. UNKNOWN_COMMIT primary read miss is still UNKNOWN; late COMMIT can leave a runnable orphan until finite Job deadline/Worker lease recovery. No process-crash-safe owner or cross-request exactly-once guarantee. |
| Embedding | Opt-in pinned, immutable embedding-space semantics; no arbitrary space substitution. |
| System-One / Decision | Experimental **OBSERVED_ONLY**, uncalibrated, recommendation-only, no autonomous binding/authority escalation. |
| Native composite dual models (#122) | Source implementation integrated to main via #128; native CUDA co-residency and managed Worker smoke were tested in isolated environments. **Site activation belongs to #124**, not a code-merge side effect. |
| Branding (#120) | **TSUMGI** repository/product branding; `astrumweaver` Python, Nix, CLI, systemd, environment and data paths remain backward-compatible runtime identifiers. Phase B rename is a separately gated project, **not a prerequisite** to publishing the current runtime. |

[#80](https://github.com/upiscium/TSUMGI/issues/80) was **administratively** closed by explicit owner decision. Its earlier documentation-only Real Smoke rerun **did not record an end-to-end technical PASS**. Preserve this as an evidence limitation; do not fabricate a technical observation or silently reinstate an already-waived administrative closure condition. A fresh installed-candidate/specific-site smoke may still be required under the accepted #79/#124 release scope.

## Current deployment inventory: read-only, not site acceptance

The 2026-10-14 **read-only** inspection of **three authorized GPU Worker LXCs** found three active `astrumweaver-worker.service` units under a role-local `astrumweaver` systemd UID; each had root-owned role-config files with observed restrictive access bits, and **one** had an active native llama.cpp process. No local Control service was found on those three Worker nodes. No site-wide Control, database, ingress, backup or complete GPU-host topology was inferred.

This inventory confirms there are **already running services that must not be overwritten casually**. It **does not** show their binaries are the new `main` SHA, that credentials are isolated from all same-UID processes, or that each selected GPU is physically inaccessible to unrelated host workloads. It is not a tested production backup, live rollback, successful #124 GO, new hardware replay or certification of inactive/offline nodes. Hostnames, endpoints, device identifiers, secret values and model paths are intentionally omitted from this document.

## Remaining operator gate: #124

[Operator runbook](operator-rollout-gates.md) is already on `main` via #135. Before any *site* change, the operator must privately record:

1. Exact **site, change owner, maintenance window, approved consumers, acceptable downtime and rollback authority**; select concrete Control/Worker host inventory and reconcile already-running service ownership.
2. Immutable **old/new packages, unit/drop-in files, Nix profile/store identities, config, role-secret ownership and permitted selected GPUs**. Verify dedicated non-shared Worker UID, cross-UID denial, native UDS/bearer negative checks and correct current free-VRAM admission on that host.
3. Verified **PostgreSQL 17 UTF8 database identity, schema/history, encrypted separate backup and restored-Control readback** on an independent recovery target, acceptable RPO/RTO, plus compatibility of any forward-only schema migration with the old executable.
4. A **disposable** prior-installed → candidate → prior-installed rollback rehearsal using the exact candidate and representative native failure/cancel requests. Pre-existing packages, services, GPU allocations, configuration and user data must remain intact outside the approved rehearsal.
5. Written **GO / NO-GO** explicitly permitting only the named persistent site cutover and specifying a rollback trigger. Version/tag/publication is still an independent #79 decision.

Previous isolated PG17 backup/restore and old→new→old dedicated-UID/GPU/systemd rehearsals are recorded in [#124](https://github.com/upiscium/TSUMGI/issues/124) and **PASS in their bounded test scope**. They do not validate an unnamed existing deployment. There is no authorization to reboot hosts, rotate deployed secrets, edit production PostgreSQL, repartition GPUs, change systemd units, replace native runtimes, tag or publish from this document.

## Next human decisions

**#79:** accept **Option B or A**, immutable candidate/RC version, documented experimental feature claims, and eventual publication authorization **as distinct approvals**.

**#124:** name the production site and operator window, review role/private-key and database restore evidence and accept/reject the exact-site rollout separately. If the goal is to finish the source/software phase first, leave #124 OPEN without inventing a production acceptance.

**#122:** dual-runtime source is already integrated; its remaining operator stage is cross-referenced by #124. **#120:** Phase B identifier migration is a separate compatibility project that may remain OPEN beyond an initial TSUMGI release.
