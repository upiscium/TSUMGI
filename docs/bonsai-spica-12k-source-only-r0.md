# TSUMGI #232 — Spica Bonsai 12K opt-in preview and 8K rollback (R0)

**Status:** source-only, unqualified, no production activation and no Ready/Merge.

## Goal

Qualify one Spica Bonsai2 27B / GTX1080Ti replica at 12,288 total context,
10,240 input tokens, 2,048 output tokens and 65,536 request bytes. Preserve
Deneb on the existing 8K Serving revision and do not silently raise the
existing bonsai2-27b alias. The new opt-in logical ID is bonsai2-27b-12k.

## Verified read-only baseline (November 8, 2026 JST)

- Spica: one GTX1080Ti, 11,264 MiB total, 3,610 MiB free at idle. Worker
  bonsai-spica active and Ready; native port 8085 one slot n_ctx=8192.
- Deneb: same GPU class, 3,614 MiB free at idle. Worker bonsai-deneb active
  and Ready; one native slot n_ctx=8192.
- The active systemd units use the Bonsai-specific worker TOML and Bonsai
  Runtime and Serving JSONs in /etc/astrumweaver. The older generic
  runtime-deployment.json is *not* the active Bonsai configuration.
- Both hosts' Bonsai Runtime files are byte-identical, SHA256
  f0c20685800548151197c13b9e9418f29f62e4739fffb72c66545bb3379a4623.
  Both Serving files are identical, SHA256
  6cad463e2e07c41850db29aeb6afc53e24b54932190472ecf41040049f84d01b.
- **Spica manifest defect:** its active Worker TOML declares the correct Spica
  GPU UUID GPU-f78bcab4-245f-b3ff-5570-ed629b239331, but the *Runtime setup
  intent* still embeds the Deneb UUID,
  GPU-8bcf8ba8-4610-d9bb-3862-a03b5ea7d931. This is probably a copied
  deployment manifest. Spica is currently Ready; a runtime fault on future
  restart has not been demonstrated. The proposed Spica-only 12K Runtime
  fixes the setup-intent GPU UUID without editing the running 8K deployment.
- Current full Control Catalog SHA256
  c28cad94b73d7948de5ec9dbbff4b4211b9df849c579d2307d9835bb9d5719d6
  contains Qwen12K and Bonsai8K. Both old Bonsai Workers advertise the same
  accepted 8K deployment and Serving Contract.
- **R1 pre-canary recovery gap (November 8, ~11:58 JST):** Read-only searches
  of Spica's acceptance directory and Nix GC roots did **not** find a
  *dedicated named* Bonsai/Spica 8K rollback snapshot or pinned GC root.
  This does not prove that no alternate old Nix closure exists, but means
  a verified rollback set is **NOT yet qualified**. Before any approved
  Spica stop, preserve and byte-rehearse the real 8K Worker/Runtime/Serving
  configuration and pin its exact Nix runtime closure. Control currently
  has an older Qwen cutover catalog backup, but no Bonsai 12K promotion
  backup yet; capture the current full Qwen12K/Bonsai8K Catalog at the
  actual maintenance window.
- Pinned Bonsai PQ2_0 model SHA256
  3907dc1658db1f78a9826bf8d5bcb8dc65db0d466388937af57f2294fae62ec1.
  This source-only run did not repeat a full 7GB model hash on Spica.
- Conservative FP16 KV estimate: an extra 1,024 MiB is needed for 8K to 12K,
  leaving approximately 2,586 MiB idle free on Spica, only 538 MiB above
  the mandatory 2,048 MiB **at peak**. The estimate excludes prefill,
  fragmentation and other users. 16K is out of scope as its estimated
  remaining idle free is already below 2GiB.

## Offline review-only candidate

The pure Python tool tools/bonsai_12k_spica_candidate.py reads seven
preexisting, privately retained source files: Spica and Deneb active Bonsai
Runtime+Serving, their actual Worker TOMLs, and the full current Control
Catalog. It does not contact Control, GPUs, PostgreSQL, systemd or Nix.

The CLI uses --spica-runtime, --spica-serving, --spica-worker,
--deneb-runtime, --deneb-serving, --deneb-worker and --control-catalog.
Optional --spica-free-mib 3610 enables a labelled idle capacity estimate.
By default it emits only a bounded status object, not deployment files.

The optional --output-dir must identify a *new direct /tmp child* named
tsumgi-bonsai12k-*. Its files are private (directory 0700, files 0600):

- spica-runtime.REVIEW_WRAPPER.json: a NONDEPLOYABLE wrapper with a nested,
  purely reviewed Runtime shape. The nested provider and setup context are
  12,288, with GPU UUID corrected to Spica. **The outer JSON is deliberately
  not a RuntimeDeploymentSpec** and fails when installed directly in a Worker
  Runtime manifest path; the nested shape remains usable for offline review.
- spica-serving.INVALID.json: one new 10,240/2,048/12,288 Bonsai Chat
  contract with deliberately **invalid PENDING execution/evidence/revisions**.
- control.INVALID.json: original Qwen12K and Bonsai8K rows **identical** to
  their 8K/12K baseline; one newly appended opt-in bonsai2-27b-12k row
  containing those same deliberately INVALID revisions.
- review-gates.json: observed legacy Spica/Deneb UUID mismatch, source
  digests, capacity estimate, and separately required physical gates.

The tool explicitly checks that unqualified Serving and Control proposals
**cannot be loaded** by the real domain types. Tests resolve synthetic
SHA256-only fixtures in memory strictly to prove eventual structure; the
resulting fake digests are never accepted as production evidence. Source
JSON hashes prove local consistency, not independent origin authenticity.

## Bounded future rollout — requires separate operator live approval

1. Independently re-check live Spica/Deneb/Control source hashes, exact
   Bonsai GGUF bytes, GPU IDs, correct active Worker file, runtime executable
   and systemd startup/stop properties. Do not use the legacy 4K file.
2. Preserve full Spica **8K** Worker TOML, Runtime/Serving, service/drop-ins,
   model SHA receipt and Nix closure as immutable private backup. Preserve
   the complete Control Catalog and Deneb unchanged. Rehearse byte restore
   into an isolated location; take a fresh zero-active-Job readback and a
   separately effective admission drain, not just a point-in-time query.
3. With approved maintenance, drain and stop only the Spica Worker. Validate
   the old native/wrapper descendants, known systemd cgroup, GPU-device FDs,
   local fuser and NVML compute rows show no ambiguous owner. NVML host PID
   and LXC PID may differ. If any are nonterminal, **fail closed**; no new
   GPU load, global kill or GPU reset.
4. Run a single Spica native 12K canary with the pinned Bonsai model.
   Verify real 7,843-token projected OpenCode tool-bearing input and
   near-10,240 input prefill; plain Chat, Tool Call/Result, Streaming,
   rejection, cancellation and no OOM or CUDA faults. Continuously record
   VRAM during high prefill/generation: **>=2,048 MiB free at peak**. Abort
   below the floor; safely remove new Native before restoring 8K.
5. Only after real evidence and independent review, derive a path-neutral
   execution SHA, validation evidence SHA, Deployment revision, Serving
   Contract revision, Logical Serving Profile revision and new
   RuntimeInstance epoch. No PENDING or synthetic digest may be published.
   Never rebind already admitted 8K jobs to a 12K revision.
6. Qualify the Spica Worker with the new Serving identity while Deneb
   remains 8K. Append ONLY the new opt-in model bonsai2-27b-12k to the
   shared Control Catalog; a Control restart is necessary since the catalog
   loads on startup, potentially briefly interrupting Qwen and Deneb.
   Verify real 12K opt-in Gateway, existing Bonsai8K and Qwen12K, explicit
   model bindings, limits and absence of silent cross-profile fallback.
7. Change OpenCode only after the new opt-in model is actually Ready.
   If any gate fails, stop Spica 12K without GPU overlap, restore original
   Spica 8K and Control Catalog, verify Deneb8K/Qwen12K stay intact.
   Keep prior Nix roots and protected source backups until validated.

**No live service restart, publication, GPU allocation, 16K, Deneb upgrade,
#240 Broker activation, Ready, Merge or Release is approved by this R0.**
