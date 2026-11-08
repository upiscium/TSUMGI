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


## R5 — read-only rollback asset verifier (2026-11-08 JST)

The source-only tool tools/bonsai_12k_rollback_preflight.py validates one
host's **protected original 8K snapshot** against its present live file
bytes, owner/group/mode and the pinned Nix closure. It runs with ordinary
Python standard library; it contains no subprocess, systemd operations,
network access, write calls or GPU-related code. A root-owned host may run
a disposable copy of the tool; this is **read-only site validation**, not
activation, installation or permission to stop a Worker.

The two operations are independent, and must be run separately on the
respective site *after* confirming the tool's source revision:

- On Spica: python3 bonsai_12k_rollback_preflight.py --site spica
- On Control: python3 bonsai_12k_rollback_preflight.py --site control

Spica verifies 9 source/snapshot pairs, the original runtime still has
8K and its *legacy* Deneb setup GPU UUID, the actual Worker is
bonsai-spica and correctly targets the Spica GPU, and two named Nix
GCroots still point to their accepted original profiles. Control
verifies 4 source/snapshot pairs, the current Qwen12K/Bonsai8K
Catalog without any newly activated 12K Bonsai profile, and the
accepted Control Nix GCroot.

Snapshots are private directories (0700) holding mode-0400 originals
and mode-0600 receipts. The tool validates receipt identity,
individual file SHA256/size/mode, original owner/group, source and
snapshot symlink restrictions, strict pinned Nix store references,
GCroot symlink targets and *current* active profile targets. A
synthetic --root option exists for isolated unit tests and visibly
sets synthetic_root=true in its output.

At the R5 bounded read-only host test, the exact source tool returned:

- Spica: HOST_LOCAL_BACKUP_BYTES_MATCH_NO_LIVE_ADMISSION, 9 files,
  2 GCroots, synthetic_root=false, canary_activation_authorized=false.
- Control: HOST_LOCAL_BACKUP_BYTES_MATCH_NO_LIVE_ADMISSION, 4 files,
  1 GCroot, synthetic_root=false, canary_activation_authorized=false.

The source script was temporarily copied to /tmp on each host, run
and removed. Neither Worker/Control service nor original source/backup
was edited. The tool's output intentionally records all of:
live_rollback_verified=false, cross_host_atomic=false,
offhost_backup=false, model_full_hash_verified_this_run=false,
canary_activation_authorized=false.

A verifier PASS is **necessary but not sufficient** for a real Spica
Canary. In particular, it does not verify the physical 7GB model's
current full SHA in the same invocation; this must be freshly checked
with the preexisting pinned model receipt before any cutover. It does
not verify actual Native/process/group/GPU teardown, admission freeze,
nonterminal jobs at the moment of a stop, disk failure recovery,
Control restarts or a time-bounded live 8K rollback. The rollback
snapshots are **host-local** and not cryptographically authenticated
independently of their host; do not claim disaster-recovery coverage.

The 90-second Spica systemd TimeoutStopSec, the historical #237
shutdown timeout and the small projected 12K VRAM margin remain.
The final production-risk stop decision must still be an explicit
operator-authorized, synchronous, no-overlap decision with an actual
old-Native and GPU FDs/NVML clearance gate. Do not infer authority
to perform any 12K Native launch, Ready/Merge or Control catalog
publication from any of these status objects.


## R6 — Native 12K residual acceptance gates after first live canary (2026-11-08 JST)

### What the first *approved* Spica-only canary actually proved

The separate, explicitly approved live Spica Native canary completed on
2026-11-08 at approximately 15:31–15:33 JST; evidence lives in a protected
**Spica-host-local**, non-GitHub directory
/var/lib/astrumweaver/acceptance/bonsai12k-spica-native-canary-20261108-r1.
Old managed 8K Native was drained/stopped and verified GPU-empty before
a new transient Bonsai Native was launched at n_ctx=12288. Exact synthetic
tokenizer requests matched 7,842, 10,011 and 10,231 tokens. All three
nonstreaming Native completions passed, including a structured Tool Call.
270 periodically sampled VRAM readings yielded **3,356 MiB minimum free**
against the 2,048 MiB policy floor. The transient Native was stopped,
GPU ownership verified absent and the original managed 8K Spica Worker
was **actually restored Ready**, with n_ctx=8192. Deneb8K and the
shared Control Qwen12K/Bonsai8K Catalog were unchanged.

**Do not overinterpret these results.** The 10,231-token completion
finished in 4.78 seconds *after* a highly overlapping 10,011-token
prompt, making cache-warm reuse plausible. Therefore the 4.78-second
result is **NOT cold-prefill latency evidence**, and the first canary
did not exercise an actual Tool Result round trip, SSE streaming
completion, interrupted stream or Control serving admission. The
minimum free VRAM is a **sampled** metric; it is not an upper bound
on unseen transient consumption between sample timestamps.

### New R6 source-only probe contract

tools/bonsai_12k_r6_native_probes.py creates JSON-only receipts and
bounded request bodies without network access, disk writes, Native
launch, Worker stop, model loading or GPU access. Tests cover exact
cold prompt hash, per-stage schemas, Tool Result call-ID correlation,
full/partial SSE structural validation and dishonest success claims.
The CLI **defaults to an unqualified plan or hash receipt**, and
requires an explicit --emit-request for full synthetic input. The
tool never grants operator authority or makes a production-ready
Serving identity. A future canary must be approved separately.

**Required execution order in a new, separately approved Spica window:**

1. Retain and requalify the existing 8K original Worker/GGUF/model
   SHA, backup files/Nix roots and Control/Deneb read-only state.
   Drain ONLY Spica, reject new admission and demand zero Spica-bound
   nonterminal Jobs. Stop the old Native; verify process descendants,
   GPU FDs and NVML compute owners are absent before starting 12K.
2. **Cold-first 10,231 input tokens:** launch a fresh 12K single-slot
   Bonsai Native. The pinned installed llama-server help advertises
   --no-cache-prompt; use this or a separately proven cache-clearing
   mechanism. Run the exact previously pinned 10,231-token prompt
   **before any related/overlapping prompt**. Recount its tokens
   on the fresh instance; record prompt processing timing and any
   supported actual cache-hit fields. If a reliable independent
   cold/uncached measurement cannot be established, record UNKNOWN,
   not PASS. Sample VRAM during the real long Prefill and Generation
   with a 2,048 MiB minimum observed free floor. No extrapolation
   from previous warm-cache latency.
3. **Tool Call + Tool Result:** use a short synthetic streaming
   function-call request. Complete and correlate the actual streamed
   call ID, function name and JSON arguments. Construct a second
   request containing that observed assistant Tool Call and a matching
   Tool Result whose synthetic echo text derives from the *observed
   arguments*, then verify a complete final assistant response. Do
   not invent an ID or assume receiving a Tool Call is the same as
   handling a Tool Result. A complete first stream needs
   finish_reason=tool_calls; the final assistant text needs
   finish_reason=stop. Incompatible or truncated finishes fail closed.
4. **Streaming completion:** collect ordered Native SSE frames and
   a single final [DONE] marker; check that function-call fragments
   and final content parse correctly. The R6 pure parser validates
   only **structure**; a passing parser is not a live stream receipt
   or proof of timely consumption by an OpenCode client.
5. **Cancellation:** start a separate bounded Native streaming
   request, explicitly disconnect the requesting client while it is
   active, and independently verify slot is no longer processing
   and any owned work is quiescent within a defined deadline.
   Absence of [DONE] merely identifies a partial stream; it does
   **not** prove the Native honored cancellation. A client may disconnect
   before the first SSE event or after only a role event. The parser
   accepts those as partial captures with zero/one frames, and never
   labels Native cancellation proven. Treat timeout, failed cleanup
   or ambiguous GPU owners as FAIL CLOSED.
6. **Always restore the original 8K Worker** after the test, even
   when all stages pass. Verify 8K n_ctx/Ready, matching original
   Runtime/Serving hashes, Deneb8K unchanged, Control Qwen12K and
   Bonsai8K unchanged, DB zero nonterminal Jobs and no surviving
   12K Native/GPU holder. Retain local evidence and bounded rollback
   receipts for independent review.

### R6 offline parser and Tool Result provenance limitations

After independent source review, the R6 Tool Result constructor no longer
accepts a separate asserted Tool Call ID or argument string. It accepts
a **complete Tool Call SSE byte capture**, verifies the expected
structured Tool Call and finish_reason=tool_calls with [DONE], then
derives the ID and exact JSON arguments to construct the Tool Result
follow-up. The synthetic echo response uses the observed parsed text
rather than a hard-coded value. The final streamed assistant text must
finish with finish_reason=stop and [DONE]; a stream that emits more
choice-bearing content *after* a terminal finish reason is rejected,
although a trailing usage-only frame is permitted.

The parser also accepts a role-only or empty partial capture as a
possible **pre-first-token disconnect**, while *never* returning an
assertion that Native cancellation was proven. The operator must
separately show the client disconnected and the Native slot stopped
processing. Crucially, caller-supplied SSE bytes are only **validated
for structure, not authenticated as genuine device output**. A
separately approved site test must capture and retain the actual
first response; fabricated fixtures can never be substituted for
that evidence, and the source-only parser cannot authorize live use.

The 10,240-token contract is a **Gateway logical Serving limit**,
not an automatic promise that the raw Native refuses 10,241-token
requests. A strict contract-overlimit test is deferred to a
separately approved *opt-in Gateway profile* phase. No future
Control/Deneb changes, Ready/Merge, 16K promotion or source-only
Draft PR publication should be inferred from R6 recipes.

The original R2 snapshot receipts retain the truthful
live_rollback_verified=false historical creation state. The
**separate** R6 evidence of one actual successful 8K service restore
is recorded on Issue #232, and must not be backfilled by mutating
the original protected immutable receipt.


## R6 live follow-up — approved cold-prefill and streaming acceptance (2026-11-08 JST)

The operator expressly approved a **Spica-only 12K Native Cold Prefill,
Tool Result/Streaming/Cancellation Canary with mandatory 8K rollback**;
the authorization excluded Deneb/Control mutation and PR Ready/Merge.
The first bounded R6 run was **incomplete**, and the corrected bounded
R6 retry completed all four requested Native-only stages. Neither run
registered a 12K Serving/Gateway profile.

**First run — safely failed closed and restored original 8K.** At
approximately 16:56–16:58 JST, the original Worker was drained, old
Native and GPU ownership checked absent, and a fresh Native launched at
n_ctx=12288 with --no-cache-prompt. The pinned 10,231-token cold input
ran first; its real Native prompt_ms was **45,632.446 milliseconds**,
end-to-end wall time **46.361 seconds**, and the sampler minimum
available VRAM was **3,360 MiB**. The genuine streamed Tool Call returned
an opaque **32-character alphanumeric ID without a call_ prefix**.
The source-only R6 validator incorrectly required that prefix, so its
Tool stream parser rejected the legitimate response with
tool-call-id-invalid. The canary stopped immediately and restored the
original 8K Worker, with Ready confirmed. Real SSE remains in
the private Spica local directory
/var/lib/astrumweaver/acceptance/bonsai12k-spica-native-r6-20261108
(SSE SHA256 497f8b5ad65aff848601e354a504ae0585d95f82b31ee7f8031b6f2deb293f28).
**No Tool Result or Cancellation was accepted in this first run.**

**Corrected R6 ID compatibility.** Tool Call IDs from the observed
pinned Bonsai Native are not required to begin with call_. The R6
validator now accepts bounded nonempty identifiers containing ASCII
letters, digits, underscore or hyphen, with maximum length 128; it
retains exact ID and argument correlation, structured function/tool
validation, and rejection of control/special characters. Offline
regression tests cover the actual observed 32-character ID and ensure
it survives the Tool Call -> Tool Result boundary. The exact corrected
R6 source copied from Adam and executed on Spica had SHA256
31e1883843cffc624d9ddfe386db32fa582b24e4cd290ae87c1e35ec158f37cf.
A separate read-only independent diff review found no P0/P1 defects
for the observed Bonsai ID, but noted that this alphabet restriction
may reject unusual opaque IDs from *other* providers. The R6 scope is
pinned Bonsai only; do not claim universal provider compatibility.
After correction the preserved genuine first-run Native SSE passed the
strict source parser and produced a correlated synthetic follow-up.

**R6 retry — 17:06–17:08 JST, four Native stages PASS.** Spica was
again drained with zero active and nonterminal jobs, old Worker stopped
cleanly, and old Native GPU holders/FDs verified absent before the
fresh single-slot 12K Native started. Its actual process argv was
checked for --ctx-size 12288 and --no-cache-prompt. The 10,231-token
cold request was the first generation (no prior overlapping prompt);
Native returned prompt_ms **45,768.320 ms**, wall time **46.500 s**,
usage.prompt_tokens **10,231**, and a completed response.

The next *real* Native SSE stream contained a structured synthetic
function Tool Call with finish_reason=tool_calls and [DONE],
**9 frames / 2,895 bytes**. A second streaming request was built
from the **actual received Call ID and exact arguments**, with a
matching synthetic Tool Result. It returned final assistant text
with finish_reason=stop and [DONE], **33 frames / 8,512 bytes**.
Both raw SSE captures and their SHA256s remain on Spica in the
private evidence directory; no user content or production secrets
were sent to the Native.

For cancellation, a separate synthetic stream was started, the
single Native slot was explicitly observed as processing=true,
and the client socket was actually shut down and closed. The slot
subsequently reported processing=false **8.545 seconds after the
disconnect**. This proves slot quiescence after explicit client
disconnect in that bounded trial; it does not independently prove
internal request-level cancellation, packet delivery, or exactly
which Native cancellation mechanism ran.

The NVML sampler collected **171 periodic measurements**; minimum
observed GPU-local free memory was **3,360 MiB**, above the required
2,048 MiB sampled floor. Samples cannot rule out unsampled transient
peaks. The transient 12K Native was stopped, old/new GPU owners
verified absent, and the exact original managed Spica 8K Worker was
restarted. At **17:08:13 JST** its Ready/slot n_ctx=8192 passed.
Subsequent read-only checks found original Runtime/Serving hashes,
Deneb 8K Ready, shared Control Qwen12K/Bonsai8K Catalog unchanged,
Spica and Deneb online with zero active jobs, and zero nonterminal
jobs. The controller exited with code 0. The distinct retry evidence
directory, preserved on Spica only, is
/var/lib/astrumweaver/acceptance/bonsai12k-spica-native-r6b-20261108.
Its canary-result and restore-status receipts attest this one
Native-only reversible trial; they do NOT authorize activation.

**Unqualified future scope:** This result does not test the actual
OpenCode client round trip through Control, fail-closed Gateway input
limit enforcement at 10,240 tokens, complete Serving profile
identity/revisions/evidence provenance, stable 12K managed Worker
registration, non-peak sample worst-case memory, or repeated load
and concurrency. Continue to keep Bonsai's original 8K profile
and Deneb8K/Control Qwen12K live. Next, independently review the
updated PR source and live evidence contract before separately
authorizing any Spica-only Serving-profile admission or shared
Control Catalog promotion. No PR Ready/Merge/Release was approved.


## R7 — Post-R6 Native stream compatibility hardening (source-only)

The exact-published-head independent PR review after R6B's
successful Native 12K Cold/Tool/Stream/Cancel Canary identified
three P2 parser/compatibility limitations. None prevented the
pinned Bonsai Native from working, but they should be resolved
before managed Worker/Serving and opt-in Gateway admission.

The pure tools/bonsai_12k_r6_native_probes.py source parser now:

- Treats the Tool Call ID as an **opaque, bounded, printable string**
  rather than requiring an ASCII-only alphabet or call_ prefix.
  It preserves the same exact ID in assistant Tool Call and Tool
  Result JSON. IDs are bounded to 128 codepoints / 256 UTF-8
  bytes, with control/format characters rejected. This is not
  a guarantee that every provider's arbitrary ID is supported.
- Rejects duplicate keys in both SSE frame JSON and reconstructed
  Tool Call arguments, including duplicates with Unicode key
  escapes. Python's default json.loads had silently selected
  the last occurrence; ambiguous Tool arguments must fail closed.
- Allows **index-only Tool Call SSE continuation fragments**, where
  the function property is omitted from a particular chunk,
  and type can be omitted as well. If supplied, type must be
  function, and function must be an object. The *completed*
  Tool Call must still have the sole expected synthetic_echo
  function name, a bounded JSON object with exactly one text
  field, a stable valid Call ID, single zero-index choice/tool,
  terminal finish_reason=tool_calls, and final [DONE].
- Preserves final-text finish_reason=stop plus [DONE], while
  empty/partial cancel traces never certify actual cancellation.

The corrected parser was independently replayed **read-only**
against both previously captured real Spica Tool Call SSE streams
from R6 Run A and Run B, plus the final 33-frame Tool Result
stream from Run B. All three replay checks passed. No Worker
stop, Native inference, GPU reset or Control modification
occurred during R7 compatibility validation.

### Managed Worker/Serving phase remains separately authorized

R7 is **not** a new live canary or profile registration. Before
any managed 12K admission, a separate reviewed plan must bind:

1. Pinned Bonsai GGUF, real 12K Native Canary evidence, exact
   Spica GPU UUID, Runtime setup intent and reviewed execution
   configuration SHA. Do not carry the original 8K legacy Deneb
   setup-intent GPU UUID into a proposed Spica 12K Runtime.
2. Real non-PENDING Deployment, validation-evidence and Serving
   Contract revisions and a fresh RuntimeInstance epoch, with
   an auditable source-to-live evidence chain. Local file hashes
   or synthetic test fixtures alone are not independently
   authenticated production provenance.
3. Spica **only** managed 12K Worker admission and correct
   context/tool/stream/cancel behavior, leaving the existing
   Bonsai8K model binding, Deneb8K and Qwen12K untouched.
   New opt-in bonsai2-27b-12k must not silently downgrade or
   fall back to the previous Bonsai8K profile.
4. A **separately approved** complete Control Catalog backup
   and coordinated promotion that accounts for shared-Control
   restart risk, with strict logical limits: 10,240 input /
   2,048 output / 12,288 total. Verify rejection of over-limit
   and unknown model requests at the actual Gateway.
5. An **explicitly authorized** bounded Spica maintenance
   window: drain and stop 8K only after zero nonterminal Jobs,
   prove old Native descendants/cgroup/GPU owners are gone,
   qualify new 12K Worker, and rollback to byte-identical
   original 8K Worker and Catalog on failure. Keep the
   original protected backup and Nix roots until accepted.

Source-only R7 parser and test changes may be published to
the existing Draft PR. They do not authorize production
profile promotion, Control or Deneb changes, Ready/Merge,
Release or a new GPU-native canary.
