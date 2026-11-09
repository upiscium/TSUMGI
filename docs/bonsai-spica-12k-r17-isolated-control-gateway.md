# TSUMGI #232 — R17 isolated Control + lab-only 12K Serving gate

**NONPRODUCTION ONLY.** This is an incremental acceptance environment for
actual Spica Managed 12K Worker/Native, following the bounded R15 PASS
and original 8K restoration. Nothing here is a production release.

## Separation / admission

The R17 experiment uses:
- Control/Gateway: `http://127.0.0.1:19090`, an InMemoryControlRepository,
  distinct random client/Worker tokens, bearer auth on both endpoints.
- Experimental Worker ID: `bonsai-spica-r17-lab`, not production Spica's
  Worker ID. It must contact only localhost R17 Control.
- Test Native `http://127.0.0.1:18085`; health port 19100.
  The production Native and health ports remain 8085/9100.
- Experimental profile ID `bonsai2-27b-12k-lab`, distinct from
  the existing Bonsai8K ID and the prospective opt-in production 12K ID.
  The catalog contains exactly this one lab entry.
- Spica GTX1080Ti GPU UUID, one Worker and one Native slot.

The R17 Control is **not connected** to production PostgreSQL, shared
Control or Cloudflare. Do not publicly proxy its listener or reuse the
test environment as a persistent production model service.

## Identity is a LAB-ONLY CLAIM

The pure source-only R17 config builder accepts the existing 8K
Bonsai Runtime/Serving declarations and derives a distinct 12K
Deployment/Serving/Profile triple. Context limits: 10,240 input,
2,048 output, 12,288 total; request byte bound 65,536.

The new execution-config SHA is only a deterministic path-neutral
**lab configuration claim**, not independently attested model/native
bytes. Its evidence SHA binds the historic R2/R15 receipt digests
under an explicitly **UNATTESTED** lab-only scope. The generated
declaration is shape-valid but must **never** be installed in the
production Control or treated as a genuinely qualified Serving
revision. Even a successful lab-only Job cannot waive this distinction.

## Source-only R17 acceptance

`tools/bonsai_12k_r17_lab_control.py` uses real TSUMGI FastAPI
Control+Gateway on an InMemory repository, enforcing bearer auth.
An auxiliary authenticated `GET /__lab/status` returns only
worker/job/revision/epoch metadata, no prompts, results or tokens.

The isolated source tests cover:
- unmodified original8K sources; new context, GPU and profile binding;
- rejection of unreviewed source config and weak/matching credentials;
- rejection of more than one lab profile;
- GET models only advertises the lab profile;
- unauthorized access returns 401;
- requests for old Bonsai8K return 404 instead of silent fallback;
- lab 12K request with no eligible Worker returns 503 and creates no Job;
- output cap 2,049 returns 400 before creating a Job.

These are in-memory FastAPI tests, not GPU or physical Worker acceptance.

## Remaining actual physical E2E gate

Before a physical R17 test, require independent review, read-only old8K
postconditions, zero Spica/nonterminal Jobs, strict localhost isolation
and an independent fail-safe timer. Any Spica stop/cutover must gate
on zero old Native/cgroup/GPU owners, >=2,048MiB sampled GPU headroom
and **mandatory exact original8K restore**, preserving Deneb.

**P1 Control runtime lifetime fence (R17 review correction):** An earlier
physical R17 attempt failed because the original local Control's
`register_worker()` override did not accept Control API's `recovery` keyword;
the current source and a real FastAPI ASGI registration regression test
address that mismatch. A later Control-only R17b attempt was automatically
stopped after its configured 15-minute `RuntimeMaxSec`, with no successful
Worker E2E before its expiration. The operator now checks the actual
isolated Control unit's finite RuntimeMaxUSec and monotonic activation
instant, requires a finite runtime cap of **exactly 1,200 seconds (20min)**, **1,000 seconds
remaining before stopping original Spica8K**, and a **stage-specific time
budget** before each Gateway stage (300s for small Chat/Stream/long, **520s
for Tool Call and its sequential Tool Result**; both Tool HTTP requests
may independently use up to 210s). The operator also arms a main-thread
**SIGALRM total-stage deadline** before the first HTTP request: 260s for
each single-request stage and 480s for the two-request Tool stage. This is
separate from socket read timeouts, so a peer cannot extend the stage
indefinitely by trickling SSE fragments. The independent 780s systemd
8K recovery timer remains a second failure boundary. Infinity, expired, missing,
unknown and unbounded unit settings fail closed. A fresh separately staged
isolated Control with an actual finite 20-minute RuntimeMaxSec is required;
old 15-minute R17b service units cannot pass this admission. The independent
780-second recovery watchdog protects the 8K return and does not extend
Control's own lifetime. These synthetic TTL tests are necessary but are
not a physical recovery or real Control lifecycle acceptance.

A new physical R17 trial should prove the experimental Worker's
fresh RuntimeInstance epoch, exact 12K Serving revision, actual
Control Job lease/Claim, Gateway Chat/Tool/Streaming and cancellation,
and rejection of wrong-revision/old8K routing. A pass must separately
identify any cancellation or streaming path not truly exercised.

Production cutover still requires trusted complete source/runtime
provenance, exact HTTP send bytes and GPU UUID-bound evidence, a real
reviewed Serving revision, PostgreSQL Control/Gateway E2E, and separate
approval for shared Control/opt-in OpenCode client publication.

Issue #232 OPEN; PR #250 Draft/unmerged. Production Spica/Deneb Bonsai8K
and Sirius Qwen12K remain unchanged by the R17 source-only build.
