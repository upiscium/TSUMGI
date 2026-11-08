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
- **R2 pre-canary protected host-local snapshots (2026-11-08 JST):**
  after rechecking both original 8K workers and the Qwen12K/Bonsai8K
  Catalog, protected snapshots were created **without stopping/restarting
  any service or changing any active configuration**.
  - Spica: private mode-0700
    /var/lib/astrumweaver/acceptance/bonsai12k-8k-fallback-20261108-r0;
    9 mode-0400 originals: active Bonsai Worker TOML, Runtime/Serving JSON,
    model checksum receipt, GPU UUID list, Worker environment, systemd
    service Unit and two drop-ins. SHA256/bytes match current live sources
    (including the **legacy Deneb setup-intent UUID** in the actual old
    8K Runtime, which must remain exact for a byte-preserving rollback).
    Full 7,206,168,928-byte Bonsai GGUF SHA256 verified against the
    original pinned receipt **PASS**; model weights were NOT copied.
    Two Nix GCroots now pin the real current Bonsai Runtime profile and
    Worker installer profile. A disposable host-local **byte-copy restore
    rehearsal** verified 9/9 and its temporary directory was removed.
  - Control: same named private acceptance snapshot in its *own* LXC;
    4 originals (Control Unit/TOML/environment/full current Catalog),
    SHA256/bytes match live sources. One Nix GCroot pins the current
    Control executable profile. Disposable restore rehearsal verified
    4/4, temporary directory removed. The snapshot Catalog retains
    **Qwen12K and Bonsai8K**, not the older pre-Qwen12K contents.
  - Both machines have owner-private receipt JSON files that explicitly
    record **host-local only**, live_rollback_verified=false,
    cross_host_atomic=false, and **no off-host backup**. This does not
    prove a live Worker or shared Control recovery, or resilience against
    loss of the same host/disk. Future maintenance MUST compare live
    source hashes to these receipts, reconfirm the pinned Nix GCroots
    still resolve, preserve the GGUF and repeat zero-Job/drain checks.
    If either site's original baseline changes, regenerate and
    independently review the protected originals before any cutover.
- Pinned Bonsai PQ2_0 model SHA256
  3907dc1658db1f78a9826bf8d5bcb8dc65db0d466388937af57f2294fae62ec1.
  The full on-host hash was checked before the Spica 8K backup; the
  7GB model file itself is retained in place, not included in the
  small-file snapshot or pinned by the Nix GCroots.
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

## R4 synthetic tokenizer-only preflight (2026-11-08 JST)

With **Spica and Deneb both still serving 8K**, their respective
pinned Bonsai native localhost /v1/chat/completions/input_tokens
endpoints were queried using the exact same synthetic generated JSON
requests. Both 8K replicas independently matched **3/3 input counts
and request SHA256s**; only token counting was performed. No 12K Native
process was started, no completion was requested, no long prefill was
executed and GPU load was not qualified. The model alias
'astrumweaver' matches the existing Bonsai Native on both hosts.
The three observations were first measured on Spica and repeated
against the independent Deneb worker. Source-only tests reproduce
the requests and their hashes but **do not contact the Native API**:
the measured token counts are site observations, not independently
reproduced by CI or authenticated signed attestations.

| Fixture | 8K native tokenizer input count | Request bytes | SHA256 of JSON request |
| --- | ---: | ---: | --- |
| tool | 7,842 | 35,483 | c1fc1d2a9ba8ad77a275905a4bb8161a355ca66fd0e50781dfa7b0f750db176e |
| long_safe | 10,011 | 46,441 | adb452d00e8a612c21a37a2dc45a10345d1ec8218b542c1b1293fc9d1db5e18c |
| long | 10,231 | 47,461 | af877ad92bed850b1a11ed57bb4d7908eaaec3d84f5d7f9f0955c4be0bd42eab |

All requests are within 65,536 bytes. The bounded deterministic
recipes live in tools/bonsai_12k_spica_fixtures.py. Importing it
does not send HTTP requests or write files; the CLI prints *only a
short receipt* unless --emit-request is explicitly supplied. The
attached tests prove exact request bytes and expected hashes. No
production secrets or user data are included.

A future authorized 12K Native-only canary should **recount tokens
against that exact 12K instance**, then test the tool fixture first,
progress to long_safe only if VRAM remains >=2,048 MiB under sampled
prefill, and attempt long only if headroom is still sufficient.
The long fixture is **nine input tokens** below the 10,240-input
contract, so *any* 12K tokenizer/template difference must trigger
an explicit rejection/recalibration. Counting under the 8K instance
does not prove admission or success under 12K.

The tool fixture is a fabricated function-call request with a similar
7.8K count, **not the actual historical OpenCode main-request
prompt** (previously projected near 7,843). Real client/tool
behavior and actual Tool Result round trips still require
independent qualification. Equal token counts are not equal prompt
content. For the future canary, require the same pinned GGUF/runtime,
one slot, live native n_ctx=12288, continuous NVML sampling during
a real 10K prefill, no OOM, Tool/Stream/Cancel and a fresh 8K
rollback plan before any Control promotion.

## Bounded future rollout — requires separate operator live approval

1. Independently re-check live Spica/Deneb/Control source hashes, exact
   Bonsai GGUF bytes, GPU IDs, correct active Worker file, runtime executable
   and systemd startup/stop properties. Do not use the legacy 4K file.
2. Verify the R2 Spica **8K** and Control protected backup receipts
   against the fresh live files, full model SHA, pinned Nix GCroots, and
   their 9/9 and 4/4 isolated-restore evidence. Re-snapshot if live bytes
   drift. **This is not a live rollback or off-host disaster backup.**
   Preserve Deneb unchanged. Obtain a fresh zero-active-Job readback and
   a separately effective admission drain, not just a point-in-time query.
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
