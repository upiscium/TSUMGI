# TSUMGI #232 — Spica Bonsai12K production opt-in cutover (2026-11-11)

**Operational disposition: live production cutover PASS.** This record
describes a completed on-host service/configuration change, not a GA release
or independent cryptographic provenance attestation. Source PR #250 still
requires separate review/merge disposition.

## Scope and final running state

Spica's existing systemd Worker is now serving Bonsai2 27B PQ2_0 with
Native **n_ctx=12288**, using a separate production Deployment and
Serving Contract revision. Its original Worker ID **bonsai-spica** and
GPU UUID remain unchanged. The original8K Worker TOML/Runtime/Serving
configuration files were preserved byte-for-byte and backed up.

The existing shared PostgreSQL Control was restarted once after an
atomic Catalog file update. Its model list now has exactly:

- qwen3.8-27b — original Sirius 12K profile, unchanged
- bonsai2-27b — original 8K profile, still served by Deneb8K
- **bonsai2-27b-12k** — NEW opt-in 12K profile, served by Spica12K

The first two Catalog profile entries are structurally unchanged.
Sirius and Deneb Workers were not restarted, migrated or reconfigured.

The new production Deployment revision is
`sha256:e2aa515ca8c46ab158e13fb6d1d078eb448e88afaa096b698ba16f2d263b88c3`;
new Serving Contract revision
`sha256:85c1a43ca84dd5fab2a7a7bbb814ec34b55d8f9fbbc9ae351c425abaa005e634`.
These new digests reference a normalized, path-neutral 12K execution
declaration and the already accepted **operator-reported physical**
R2/R15/R19/R21 receipts. They do **not** claim fully independent host,
GPU, complete runtime-library or network-wire attestation.
The distinct R17/R19 lab-only identity was NOT published into production.

## Exact staged and activated file hashes

| Spica file under /etc/astrumweaver/bonsai-production12k-20261111/ | SHA-256 |
|---|---|
| runtime12k.json | da1f603e0e4a7764c77fbd23cc5484e94adbf3f1a50d589595e1f397cfb59507 |
| serving12k.json | 0d6f55a2ca36638f69da72ba6f21b7559151c25d94daf7a62eeb344fa3c7fb16 |
| worker12k.toml | 75582a0045f8fd4f04fbb380293e954fdc8996878e1bc33fb45c4b93dbd59424 |
| catalog12k-entry.json | b607e809ab4f2aa3a372a663b6c8f37f12ddddeacd0b5d1e6c03de6123157bb2 |
| operational-evidence.json | a9384ca535988cc3aa631c3c4fa1423416b1fe561140d9694fb79edced29d865 |

Spica's systemd override is
`/etc/systemd/system/astrumweaver-worker.service.d/60-bonsai-production12k.conf`,
SHA-256
`1deb997d3406263b862cc56a6e04e294f6f6e0eec865913cc436a7a9a1d6523c`.

Shared Control Catalog path:
`/etc/astrumweaver/tsumgi-qwen38-bonsai-chat-catalog.json`,
new SHA-256
`0454f8f37219dfc051130a000abc309e116c2ee5db4899b6b8ac6096699289cb`.

The original catalog SHA-256 was
`c28cad94b73d7948de5ec9dbbff4b4211b9df849c579d2307d9835bb9d5719d6`.

## Guards, actual checks and acceptance

Before Worker stop, Control PostgreSQL read-only showed all active
Workers ONLINE, active_jobs=0 and zero nonterminal Jobs. Spica's old
native process and GPU owners released the GPU, leaving approximately
11,164 MiB free. Distinct local Spica and Control systemd auto-rollback
timers were armed before modifying service or Catalog configuration.

After Spica Native loaded **n_ctx=12288**, Control registered
bonsai-spica ONLINE under the new Deployment revision and a new
RuntimeInstance epoch. Control's Catalog was then atomically updated
and Control restarted, with HTTP200 Ready and exactly three model IDs.

**Real shared PostgreSQL Control/Gateway acceptance:**

| Request | Observed result |
|---|---|
| Small non-streaming 12K Chat | HTTP200, nonempty response |
| Streaming 12K Chat | HTTP200, 12 SSE frames, terminal DONE, generated text |
| 12K synthetic_echo Tool Call | 9 SSE frames, correlated valid tool name/JSON args |
| Tool Result continuation | 29 SSE frames, terminal STOP, nonempty content |
| Long historical ~10,231 native-token synthetic Chat | HTTP200, 47,464 serialized body bytes, ~47.3s |
| Output limit 2049 tokens | HTTP400 before model execution |
| Existing bonsai2-27b 8K Chat | HTTP200, remains bounded to 8192 context |
| Control Job state after trials | all targets ONLINE, zero nonterminal Jobs |

The historical long request's 10,231-token count was measured in an
earlier Native tokenizer trial; the production HTTP test did **not**
independently remeasure tokenization. Production-Spica Native idle
readback returned n_ctx=12288, Worker Ready true, sampled free VRAM
**3360 MiB** after acceptance, Worker NRestarts=0.

Both public model-list routes returned HTTP200 with all three models:
- https://tsumgi.arc.upiscium.dev/v1/models
- https://astrum-control.arc.upiscium.dev/v1/models

After all acceptance gates passed, both independent systemd rollback
timers were **disarmed**. The production 12K service and 3-model
Catalog remain active. The following root-only rollback assets remain
available for manual operations:

- Spica backup and script:
  `/root/tsumgi-prod12k-20261111-spica/rollback_spica.py`.
- Control backup and script:
  `/root/tsumgi-prod12k-20261111-control/rollback_control.py`.

If rollback is required, coordinate a maintenance window and drain
running Jobs first. Restore the Control Catalog and original Spica8K
service, then verify both old model entries and Native n_ctx=8192.
Manual root fallback commands (on their respective hosts) are:

`python3 /root/tsumgi-prod12k-20261111-control/rollback_control.py`

`python3 /root/tsumgi-prod12k-20261111-spica/rollback_spica.py`

The Spica script checks the exact new override contents before
removing it, then reloads systemd and restores original8K. The
Control script refuses unknown third-party Catalog mutations and
restores only the pinned original two-profile Catalog. Neither script
should be used during unrelated live Job processing.

## Explicitly deferred hardening (not production cutover blockers)

- Full independent, trusted-process/cgroup/GPU/library/complete GGUF
  provenance and HTTP wire-level attestation.
- After-first-SSE-generated-content real TCP disconnect/cancellation
  (earlier R21 isolated prefill-disconnect cancellation PASS is distinct).
- Extended multi-hour stability, concurrency/load and performance
  characterization beyond the bounded acceptance in this record.

The deferred hardening work is grouped into Issue #259; it is not a
blocker to the already completed bounded production cutover.

This file documents existing operational configuration; committing it
to a Draft PR does **not** perform, undo, merge or re-deploy the service.
