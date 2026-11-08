# TSUMGI #232 — R10 synthetic opt-in Gateway/Control proof

Status: **source-only synthetic, not production qualified**. This adds no
real 12K serving identity, admission to live Control, GPU occupancy or
configuration changes. The R9 independent OpenCode review remains unresolved
after its CLI attempts timed out, and R10 does not waive that gate.

## Goal and boundary

Keep the old Bonsai 8K logical profile explicit and unchanged. Separately
advertise a proposed opt-in Bonsai 12K model in an **in-memory**
ChatProfileCatalog, then exercise the actual FastAPI Gateway and
InMemoryControlRepository with synthetic Worker advertisements.

- The existing Qwen12K and Bonsai8K rows are byte-equivalent in semantic
  object form to the source catalog. The new profile is named
  `bonsai2-27b-12k`, not a replacement for `bonsai2-27b`.
- The 12K budget is 10,240 input / 2,048 output / 12,288 total / 65,536
  request bytes; old Bonsai8K remains 6,144 / 2,048 / 8,192 / 65,536.
- Current live Control was previously observed using client_auth=none.
  The isolated app uses that setting solely to test the actual site auth
  semantics, **not** to authorize unauthenticated public proxy exposure.
  No external HTTP request is sent: httpx ASGITransport runs in process.

## What the new tests observe

The four synthetic tests in tests/test_bonsai_12k_r10_gateway_opt_in.py:

1. GET /v1/models exposes both unmodified existing profiles and the
   distinct new opt-in profile, with the expected effective limits.
2. With only old Spica and Deneb 8K advertisements ONLINE, an HTTP
   POST explicitly selecting `bonsai2-27b-12k` returns
   503 no_compatible_deployment, with **zero durable Jobs**.
   A nonexistent model is 404 model_not_found rather than falling back.
3. Output-token budget 2,049 is rejected 400 output_limit_exceeded
   before any Job, in both stream and non-stream request forms.
   An exactly 65,536-byte valid JSON body passes the HTTP byte limit
   and proceeds to Control, where it fails 503 because no 12K Worker
   is registered. At 65,537 bytes it fails 413 request_too_large.
   This is an HTTP byte-bound test, **not** a 65,536-token prefill.
4. With a fake matching 12K Worker and Deneb8K in the same in-memory
   repository, an explicit 12K request is claimed only under the
   new exact Deployment/Serving revision and RuntimeInstance epoch.
   A separate explicit 8K request is claimed by Deneb8K alone.
   The apparent successful HTTP response is built by an in-memory
   fake function returning a constant `FAKE:` string; **no real LLM
   generated a token**.

All SHAs in this test come from R9's synthetic, deliberately artificial
review fixtures. The R0/R8 PENDING_* manifests remain unqualified and
are not used as deployable Worker or Control manifests.

## What this does NOT demonstrate

- Managed 12K Worker/Nix/service lifecycle, GPU ownership release,
  native model readiness or live Control/PostgreSQL registration.
- Input token accounting from actual OpenCode requests or actual Native
  tokenizer (the Gateway cannot infer every native prompt token solely
  from an HTTP byte count).
- Actual streaming SSE, structured Tool Call/Result/cancellation from
  Gateway through a Native Worker.
- Native prompt quality, model response content, 12K cold-prefill latency,
  continuous GPU headroom or 8K rollback for a new managed deployment.
- Cryptographic provenance of the R6B physical records, actual HTTP body
  hashes, NVML GPU UUID/prefill-stage binding or host-independent backup.
- Production Control Catalog startup reload or permission to update any
  client OpenCode model declaration.

## Next evidence / decision sequence

1. Obtain an independent OpenCode source review of the exact published
   R9/R10 revisions; do not treat a timed-out CLI as review PASS.
2. Independently collect actual Spica execution provenance and immutable
   artifact/config bytes in a path-neutral, secret-free record; resolve the
   legacy Deneb UUID embedded in the Spica 8K setup intent for **the new
   candidate only**, preserving byte-identical 8K rollback files.
3. Design a verifiable signed (or equivalent trusted provenance) Native
   and managed evidence receipt, including actual serialized HTTP
   request bytes and GPU UUID-bound peak sample windows. Keep the source-only
   REVIEW_ONLY/PENDING outputs nonloadable before independent acceptance.
4. Require a **new explicit operator authorization** before any Spica-only
   managed 12K service stop/cutover, and an additional authorization before
   shared Control restart or new opt-in catalog/client publication.
5. During an approved bounded canary, enforce admission drain, exact
   old Native/cgroup/FDS/NVML owner release, >=2,048MiB observed free on
   the specific Spica GPU, fresh per-start Runtime epoch, and mandatory
   original 8K restore. Preserve Deneb8K and Qwen12K.

Issue #232 remains OPEN and PR #250 remains Draft/nonclosing. Source tests
do not authorize Ready/Merge/Release/Deploy, shared Control changes or any
changes to Deneb and the Qwen 12K serving contract.
