# TSUMGI v0.1.0 — stable release qualification (DRAFT)

**FEATURE FROZEN 2026-10-17; GA TAG/PUBLICATION NOT AUTHORIZED.**

This is a **source/artifact publication gate**. It does **not** replace named-site installation and rollback acceptance under [#124](https://github.com/upiscium/TSUMGI/issues/124). Feature additions, model swaps, SSE tuning, native-credential hardening, and technical identifier renaming are not part of v0.1.0.

## Frozen baseline and release identity

| Item | Current evidence / action |
| --- | --- |
| Feature Freeze authority | Explicit maintainer instruction of 2026-10-17: freeze features and prepare the v0.1.0 Release PR |
| Base source and already published RC | `40e0151e44e7e19e7cc4c36ae873e2b138b4d7d4` / [RC1 release](https://github.com/upiscium/TSUMGI/releases/tag/v0.1.0-rc.1) |
| Source change policy | Product-version alignment, current public docs and release qualification regression checks **only** |
| Python metadata | `astrumweaver==0.1.0` |
| Nix product packages | `0.1.0`; experimental upstream Decision CUDA provider version remains independently pinned |
| Public tag | `v0.1.0` — **NOT CREATED** |
| Final commit, tree and `flake.lock` | **PENDING** exact post-merge binding |
| Source archive, wheel, manifests, signatures | **PENDING** approved immutable artifacts and SHA-256 readback |
| Support surface | x86_64 Linux, PostgreSQL 17 UTF8, pre-provisioned nodes, trusted clients, no production SLA |
| Runtime compatibility | Keep `astrumweaver` imports, commands, Nix option/service identifiers and on-disk state; [#120](https://github.com/upiscium/TSUMGI/issues/120) remains separate |

## Existing evidence already accepted for RC1

- [x] [RC1 exact-main unit + Nix CI SUCCESS](https://github.com/upiscium/TSUMGI/actions/runs/38657662180); full isolated PostgreSQL17.11 suite 1,169 PASS / 2 SKIP, installed Nix Control/Worker/Installer builds.
- [x] [#124](https://github.com/upiscium/TSUMGI/issues/124#issuecomment-6227235648) real operator-site Control/GPU/OpenCode acceptance: 56/56 CLI, 12/12 file edits, actual SIGKILL / cancellation / recovery, bounded soak.
- [x] Risks stated without exaggeration: `UNKNOWN_COMMIT` best-effort cancellation; measured VRAM != reservation; System-One `OBSERVED_ONLY`; RAM plateau [#150](https://github.com/upiscium/TSUMGI/issues/150) not proven.
- [x] [#80](https://github.com/upiscium/TSUMGI/issues/80) Real Smoke is administratively closed **by maintainer waiver**, explicitly **not** a new technical PASS.

## Mandatory GA-only verification before release approval

- [ ] Review that the Release PR changes only package versions, tests, and release-facing docs; preserve SQL migrations 000–005, protocol, Worker/Control runtime implementation, model pins and `flake.lock`.
- [ ] Independently review version sync, existing `astrumweaver` compatibility, documented trust boundary and current-vs-historical RC statements.
- [ ] Exact-HEAD **CI unit + Nix + generated NixOS module** SUCCESS on the final Release PR commit, followed by **post-merge main** readback/CI or a documented equivalent exact-tree qualification.
- [ ] Full Python regression on disposable **PostgreSQL 17 UTF8**, including no duplicate Job admission, cancellation/deadline/`UNKNOWN_COMMIT`, Worker readiness and recovery; record PASS/SKIP/FAIL count and environment.
- [ ] Build actual Nix **`#control`, `#worker`, `#installer`** outputs; inspect product versions, role-scoped dependencies and six SQL migrations. Build **Python wheel** and verify `Version: 0.1.0`, packaged modules and migration data.
- [ ] Freeze exact candidate commit/tree, `flake.lock` SHA-256, docs digest and final package outputs; validate no unintended dependency/runtime drift from RC1.
- [ ] Prepare and read back version-pinned GitHub source archive, wheel, `RELEASE-MANIFEST.txt`, `SHA256SUMS` for **this exact GA commit**; verify checksums of downloaded artifacts. Do not claim signatures or cross-host deterministic builds without evidence.
- [ ] Review [GA notes](releases/v0.1.0.md): supported platform and PostgreSQL UTF8, private/trusted deployment boundary, known limitations, deferrals [#120](https://github.com/upiscium/TSUMGI/issues/120) / [#149](https://github.com/upiscium/TSUMGI/issues/149) / [#150](https://github.com/upiscium/TSUMGI/issues/150) / [#151](https://github.com/upiscium/TSUMGI/issues/151), #80 waiver and #124 site rollout separation.
- [ ] Obtain an **explicit human approval for stable publication** identifying exact commit, artifacts/checksums, release notes, known limitations and destination. Only then create `v0.1.0` and publish a **non-prerelease GitHub Release**. Git tag signing/PyPI require separate authorization and evidence.

## Operator site gate (distinct, not an automatic publication blocker)

Any **existing running** Control, Worker or PostgreSQL state remains untouched by this PR. A site-specific rollout requires [#124](https://github.com/upiscium/TSUMGI/issues/124), recoverable database backup plus restore readback, old and new immutable Nix profiles, trusted UID/secret boundary and written GO/NO-GO. Closing a release gate does not approve site migration, and successful site tests do not approve a public tag.

Do **not** close #124/#149/#150/#151 as an effect of preparing v0.1.0. SSE latency optimization remains HOLD.
