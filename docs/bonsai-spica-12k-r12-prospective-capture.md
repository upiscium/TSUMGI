# TSUMGI #232 — R12 prospective Spica 12K capture consistency (source only)

**State: BLOCKED / no actual Native capture or managed Worker acceptance.**
This is a proposed *future* evidence format, not approval to run a test or
trust R6B's earlier host-local traces. Keep #232 OPEN and PR #250 Draft.

## Exact problem being addressed

R6B's Native-only 12K canary returned valid long-prefill responses,
structured Tool Call/Result streams and an observed 8K restoration.
R8 reconciled earlier saved reports, but the R6B inputs did not preserve:
- the actual raw HTTP request bytes from the wire/client send path;
- NVML sample GPU UUIDs bound to each stage of the Prefill run;
- an independently authenticated capture origin or live managed Worker.

A SHA in a self-reported JSON stage summary does *not* prove the HTTP
bytes were sent, and a timestamp/free-memory CSV without a GPU UUID
cannot independently establish the GPU the samples came from.
R11's path-neutral fingerprint is deliberately only a claim digest,
not proof of the binary or model loaded by Native.

## New pure R12 source/test

The pure function in tools/bonsai_12k_r12_capture_binding.py accepts
only caller-supplied in-memory data. It does **not** perform HTTP,
filesystem, GPU, NVML, PostgreSQL, Worker, Control, Nix or process I/O.

It requires a still-blocked R11 execution review record with the
correct synthetic fingerprint, distinct opt-in Bonsai12K profile,
Spica worker ID and GPU UUID, context 12,288, one slot and the exact
10,240/2,048/12,288/65,536 serving limits.

The prospective input contains these explicit, ordered phases:
1. cold_long — a fresh uncached 10,231-token **reported** Native input;
2. tool_stream — a complete structured synthetic_echo Tool Call SSE;
3. tool_result_stream — correlated raw request body constructed from
   the Tool Call's observed ID and arguments, plus final SSE;
4. cancel_stream — an incomplete SSE trace with a reported explicit
   client disconnect and subsequent slot idle.

For each phase, supplied raw request bytes must **exactly match** the
pinned R6 synthetic recipe. That check prevents a changed JSON body,
lost Tool Call ID or changed request byte budget from being silently
treated as the accepted fixture. Each reported stage record must contain
a digest of those *supplied* request bytes, context size, nonoverlapping
start/end millisecond window and appropriate HTTP or cancellation fields.
For the cold stage, the positive, finite reported Native prompt-processing
milliseconds must fit inside its reported stage interval (with 1s tolerance).
For the cancel stage, an explicit disconnect timestamp and subsequent
Slot Idle timestamp must both fall inside its reported stage window and
their difference must equal the reported quiescence delay. This is still
self-reported timing, not an authenticated cancellation observation.

Each supplied NVML row must have: stage, timestamp in milliseconds,
the **same nominated Spica GPU UUID**, and free VRAM (MiB). The
validator requires strictly increasing timestamps, samples within
the matching stage window, at least 16 samples per stage, at least
64 total, observed free >=2,048MiB throughout the supplied rows and
bounded 1,000ms coverage gaps, including each stage window's edges.

These rules are *necessary* for reviewing a future capture, not
sufficient for claiming actual peak VRAM safety. Sampling cannot
mathematically exclude a transient sub-second dip, and a fabricated
GPU UUID/time series can still pass a purely structural validator.

The output exposes a bounded review summary and request SHA-256
digests, **not raw request text or any deployable configuration**.
All authority bits remain false:
- actual request bodies independently captured;
- Native process/host authentication and GPU UUID authentication;
- independently verified provenance and managed Worker 12K;
- fresh live 8K rollback recheck;
- new RuntimeInstance epoch, Serving revision, Control Catalog changes;
- canary activation, Ready or Merge.

It cannot reclassify the previous R6B record as complete. In particular,
the new format's stage/UUID fields are **proposed for a separately
authorized new run**, not reconstructed observations of the old one.

## Synthetic negative/positive gates

tests/test_bonsai_12k_r12_capture_binding.py exercises the four
synthetic phases, exact raw request bytes, R11 review fingerprint binding,
correlated Tool Result SSE, no-cache input/token count, missing/invalid
stage windows, overlapping or reordered phases, false cancellation
claims, invalid/missing SSE, Deneb or malformed GPU UUID, VRAM below
2,048MiB, samples outside stages, insufficient coverage, duplicate
or negative timestamps, and rejection of unrecognized evidence keys.

All fixtures are fully synthetic, created inside the test process.
No test uses the actual Spica or Deneb GPU and no actual managed
Serving/Control 12K profile is registered.

## Remaining actual acceptance gates

1. Independently verify full GGUF, executable/adapter/template bytes,
   Nix closure and exact executable argv against the proposed R11 claim.
2. Define a trustworthy capture origin, immutable source/host/process
   identifiers and an authenticated signing or equivalent provenance
   procedure; otherwise a coherent fake evidence bundle can still pass.
3. Obtain explicit approval for a bounded **Spica-only** physical
   managed 12K canary. Stop admission, drain relevant Jobs, prove all
   old Worker/Native descendants and device holders absent, then run
   the pinned model with GPU-bound sampling and a fail-safe timer.
4. Capture raw HTTP bytes at the actual sender, a stage-tagged NVML
   trace from Spica's exact GPU, Tokenizer/Chat/Tool/Stream/cancellation
   output, and an independently checked original 8K restoration.
   Deneb8K and Qwen12K must remain unchanged.
5. Only after that evidence and independent review, consider a distinct
   approval for opt-in Control/Gateway profile and client publication.

**No production activation, Ready/Merge, model download, host/kernel,
GPU allocation, DB or shared Control mutation is implied by R12.**
