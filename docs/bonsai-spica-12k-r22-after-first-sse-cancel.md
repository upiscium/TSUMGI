# TSUMGI #232 — R22 after-first-SSE-content cancellation boundary

**SOURCE-ONLY proposal. Not physical acceptance, not production promotion.**
R21's bounded physical Spica12K cancellation PASS proves a TCP disconnect
during long prefill while a claimed real lab Job and Native processing were
observed, followed by durable CANCELLED, Native idle, Worker Ready and
exact original8K restoration. It **does not** show a generated SSE text
fragment had reached the client before disconnect.

## What the new test must prove

The R22 prospective after-first-token trial must use the existing isolated
127.0.0.1:19090 InMemory Control/Gateway, the sole experimental
bonsai-spica-r17-lab Worker and separate bonsai2-27b-12k-lab model.
No production Control Catalog or PostgreSQL, Deneb, Sirius, NPM or Cloudflare
publication is allowed.

Require this sequence, in order, in an independently bounded new Spica-only
maintenance window:

1. Fresh private test stage/unit/receipt names, unique lab bearer credentials,
   pinned source hashes, reviewed model/native/config/backup, an original8K
   Ready check, production Control read-only zero nonterminal jobs, and
   independent 780s recovery timer **armed before Spica8K stop**.
2. Dedicated experimental Managed Worker Ready, Native n_ctx=12288,
   one eligible lab Serving profile, fresh RuntimeInstance epoch,
   independently reviewed Control lifetime 20min with >1000s initial
   headroom, observed GPU free >=2048MiB throughout sampled window.
3. Start a bounded non-proxy TCP child. Supply exact serialized lab-model
   streaming Chat request BODY and private Client bearer over stdin; pin the
   body SHA/length and never put bearer or private contents on argv or logs.
   Reject all non-loopback destinations or redirects. Request enough output
   (128–2048 max tokens) to allow the first generated content to arrive
   while the Job is still RUNNING.
4. Require HTTP200 **and the exact first received complete SSE data line**
   whose choice index is zero, finish_reason=null and delta.content is a
   nonempty, non-whitespace string. A role-only, usage-only, reasoning-only,
   Tool Call, [DONE], or incomplete/malformed SSE fragment is NOT evidence.
   Bound total bytes and the time-to-first-content; no retry that creates
   another Job under the same acceptance window.
5. While the sender is still alive and *after that content marker*, require
   **the same** Job RUNNING with exact staged Deployment/Serving Contract/
   Runtime epoch claimed and lab Worker active_jobs=1, and HTTP200 Native
   /slots n_ctx12288/is_processing=true. If the model already finished
   and the Job is SUCCEEDED, mark FAIL, not cancellation PASS.
6. Force actual TCP close by terminating the child; do **not** invoke
   Control's direct cancellation API. The client exit and socket close
   must be observed. Require the SAME Job to become durable CANCELLED,
   its lease owner cleared, active_jobs=0, and Native /slots HTTP200 Idle
   plus Worker /health ready without active_job_id. Repeat these reads
   after a bounded hold to reject late completion/resurrection.
7. Regardless of PASS or FAIL, stop **only** isolated Worker/Native, wait
   until their GPU owners have released the device, restore the original
   Spica8K Worker n_ctx8192 and pinned original bytes/cgroup ownership,
   and only then disarm the independent 8K recovery timer. Stop lab
   Control, release ports, and read-only verify Deneb and production Control.

## Source-only R22 validator

`tools/bonsai_12k_r22_after_sse_evidence.py` contains a pure
state/shape validator for caller-supplied evidence:

- `first_content_frame()` validates a bounded complete raw SSE
  data line, one active choice, explicit null finish_reason and nonempty
  generated content. It returns only its digest, byte length and char count,
  not transcript text.
- `inspect_after_first_content_cancel()` requires a bounded, strict
  single-user-message lab-model streaming request with explicit
  stream_options.include_usage=true; rejects NaN/Infinity, duplicate JSON
  keys, unreviewed fields or malformed/empty messages (the first independent
  source-review P2 correction). It also requires HTTP200, caller-reported first-text-observed
  and actual-client-killed flag, one claimed busy Native/Worker/Job before
  disconnect, and matching CANCELLED Job claim + stably Idle Native/Ready
  Worker afterward. It reuses R21's strict staged Deployment/Contract/epoch
  claim validator. The resulting summary deliberately sets all independent
  TCP-close/time-ordering, source/GPU authentication, full wire capture,
  physical after-first-SSE acceptance and production Serving flags FALSE.

**Important limitation:** caller-provided booleans, job records and SSE
bytes can be fabricated. This module validates *internal consistency*
only. A real R22 runner must collect and bind the true client-first-content
event, process kill time and TCP connection, actual Job lease records,
Native busy and durable cancellation, and UUID-bound GPU samples with
a separate, trusted provenance root. R21's earlier historical receipt
must never be relabelled as after-first-content evidence.

## Explicitly not yet qualified

- Physical R22 actual first-SSE-content-before-disconnect and cancellation.
- Cancellation while processing concurrent unrelated Jobs.
- Signed/authenticated host, executable/mapped CUDA/GGML/model, tokenization,
  real HTTP headers/framing or NVML GPU UUID provenance.
- Genuine production DeploymentIdentity / ServingContract digests and
  independently authenticated RuntimeInstance epoch freshness.
- Production PostgreSQL Control/Gateway/Client model
  `bonsai2-27b-12k` and the separate opt-in cutover.

#232 stays OPEN, PR #250 stays Draft. No Ready/Merge/Deploy/activation
or live Control/Worker changes are authorized by R22 source-only acceptance.
