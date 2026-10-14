# TSUMGI v0.1.0-rc.1 qualification checklist

**PREPARATION ONLY — NO TAG, NO PRODUCTION DEPLOYMENT, NO PUBLICATION.**

Tracking: [#79 release decision](https://github.com/upiscium/TSUMGI/issues/79), [#124 operator GO](https://github.com/upiscium/TSUMGI/issues/124), [draft release notes](releases/v0.1.0-rc.1.md).

The owner accepted Option B preparation, not publication. This branch starts from merged main `4c7bd0b6fa28942b00804319921fbb1af07ff8d3`. Neither this initial base nor the proposed RC PR commits are final tag targets without a separate acceptance. The final RC requires an **exact commit**, tree and `flake.lock` digest.

| Surface | Prepared proposal |
| --- | --- |
| Public identity | TSUMGI `v0.1.0-rc.1`, tag **not created** |
| Python metadata | `astrumweaver==0.1.0rc1` |
| Nix product packages | `0.1.0-rc.1` |
| Decision CUDA provider | Retain separately pinned `0.6.0-dev-<revision>` |
| Technical naming | Retain `astrumweaver` CLI, import, Nix, systemd and data compatibility; #120 Phase B remains separate |

## Required evidence before publication

- [ ] Freeze final source, tree, lockfile, documentation and build output IDs to one **exact commit**. Record source/archive and artifact SHA-256 digests only after freezing.
- [ ] Independent correctness and security review of version synchronisation and legacy-identifier compatibility.
- [ ] Exact-HEAD GitHub unit and nix jobs SUCCESS, including actual generated NixOS modules.
- [ ] Python full regression on disposable PostgreSQL **17 UTF8**, including #137 UNKNOWN_COMMIT, HTTP disconnect and deadline tests.
- [ ] Build and verify `#control`, `#worker`, `#installer` and a versioned Python wheel without publishing; check their actual metadata and dependency closure.
- [ ] Approve release notes including Chat bounded best-effort, historical GPU SIGKILL evidence, exact Embedding identity and Decision/System-One **OBSERVED_ONLY**.
- [ ] Select the exact tag/commit, check artifact checksums and explicitly **Accept publication under #79**. Publish only as a GitHub **prerelease** after that distinct approval.
- [ ] Obtain a separate #124 site-specific GO/NO-GO, recoverable PG17 backup/readback and old-version rollback rehearsal **before modifying a running installation**.

The earlier #80 administrative closure is **not** a newly observed full documentation-only Real Smoke technical PASS. #124 production acceptance is independent of a source prerelease. No cross-request exactly-once, zero-orphan Job, hard GPU memory reservation, host-wide GPU exclusivity, production SLA or calibrated System-One authority is implied.

No version/tag/release asset is published by merging a preparation PR. The repository's CI alone does not prove wheel attestation or signed assets.
