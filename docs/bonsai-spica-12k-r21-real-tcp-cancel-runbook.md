# TSUMGI #232 — R21 Spica real TCP Cancellation lab runbook

**NONPRODUCTION ONLY.** The existing Spica/Deneb 8K and Sirius Qwen12K
remain live. The trial is isolated InMemory Control only, not a real
production Serving revision or shared PostgreSQL Control cutover.

## Evidence and R20 failure

On 2026-11-10, R20's actual Spica12K Managed Worker registered/Ready,
but its Cancellation stage aborted on HTTP503. The exact 503 callback was
not preserved. The original Spica8K Worker was restored and the
watchdog disarmed. Do not claim Cancellation was proven by R20.

R21 makes Native /slots HTTP503 **UNKNOWN**, not processing/idle, and
reads Worker /health instead of the possibly-503 /ready. It also
disables environment HTTP proxies on *all* lab HTTP calls, rejects
HTTP redirects, and requires exact 127.0.0.1 ports/path endpoints. The
child TCP client uses its own direct opener; no test permits private
Bearer or HTTP body forwarding to an ambient proxy. After the CANCELLED
Job and initial Native/Worker idle, it waits 1.5s and rechecks the
same Job and actual Native/Worker idle again. A 503 cannot prove idle.

## Deterministic source and exact identities

Generate from the pinned R17 base and reviewed R20 helper using
tools/bonsai_12k_r21_operator_patch.py. The generator checks exact
input and R20 intermediate hashes. **Final generated R21 Operator SHA256:**
`56286b2c1ccc91a4c2207b76739057d2fba0215e8bf72ba6eb9704d51e0aa988`.
If the source changes, require new review and an explicit pin update.
Never install a generated operator whose hash differs.

Run R21 with unique root-owned files:
- Operator /root/tsumgi-r21-operator.py, mode0600
- New stage /etc/astrumweaver/bonsai-serving-r21/, mode0750, owned by
  root and astrumweaver-config group
- Separate root-only run /root/tsumgi-bonsai12k-serving-r21-run
- Unique lab Worker bonsai-spica-r17-lab, profile bonsai2-27b-12k-lab
- Loopback Control/Gateway 127.0.0.1:19090, Native 18085, health 19100
- Unique 256-bit bearer Client and Worker tokens, private env only
- No production PostgreSQL DSN, Cloudflare Tunnel, NPM or public listener
- Existing pinned Spica GPU UUID GPU-f78bcab4-245f-b3ff-5570-ed629b239331

Copy only reviewed lab Runtime/Serving/Catalog (single model), rebind
the new Worker TOML manifest paths to R21 and stage the exact reviewed
R20 helper. Do not overwrite R20, R19 or original 8K files/receipts.

## Operator sequence and failure bounds

1. Read-only preflight: Spica old8K Ready, native n_ctx8192, no active
   Jobs, GPU free >=3000MiB, exact old8K manifest/model/backup hashes,
   no residual lab service/timer/socket. Shared Control must report
   Spica/Deneb ONLINE, active_jobs=0 and zero nonterminal Jobs.
2. Run the pinned R21 Operator with --prepare (without stopping 8K).
   Require the root-only prepare receipt to hash all staged files,
   helper, Operator, and R6 rollback helpers. Reject unexpected
   owners, modes, symlinks, file revisions, or extra Catalog rows.
3. Start unique tsumgi-bonsai12k-spica-r21-control.service via
   systemd-run as astrumweaver, with astrumweaver-config group,
   EnvironmentFile from R21 private stage, Restart=no,
   RuntimeMaxSec=20min, KillMode=control-group,
   NoNewPrivileges=yes, PrivateTmp=yes, ProtectHome=yes,
   ProtectSystem=strict, and network families restricted to local
   Unix/INET. Execute pinned TSUMGI installer Python and staged
   bonsai_12k_r17_lab_control.py. Verify exact PID/argv/env/cgroup
   and 127.0.0.1:19090 socket ownership and empty lab repository.
4. Run pinned R21 Operator --preflight. It must confirm finite exact
   20min Control lifetime with >=1000s remaining and fail closed
   before any old8K stop if all required predicates are not met.
5. With distinct unit tsumgi-bonsai12k-spica-r21-operator.service,
   RuntimeMaxSec=850s, run Operator --execute only under
   TSUMGI_R21_OPERATOR_APPROVED equal to
   SPICA_R21_ISOLATED_REAL_TCP_CANCEL_8K_RESTORE.
   The independent 780s recovery timer MUST arm before the original
   Spica8K Worker stop. Require full old Native/cgroup/GPU release
   and minimum >=2048MiB sampled free throughout the trial.
6. Client child sends a pinned ~10K token-equivalent long streaming
   request through direct TCP. Require HTTP200 sender-body SHA256;
   exact raw body stored in private root file (not HTTP headers/wire
   capture). Require a RUNNING and claimed lab Job, active_jobs=1,
   Native /slots actual HTTP200 and is_processing=true before client
   subprocess kill and TCP close.
7. Require the same Job durably CANCELLED with retained claimed
   Deployment/Contract/Runtime epoch, lease owner null, active_jobs=0,
   actual Native /slots HTTP200 idle, Worker /health ready and no
   active_job_id. Recheck all after 1.5s. Never accept 503 as idle.
8. Regardless of test outcome, stop only the transient lab Worker
   and Native; check GPU release, restore exact original8K
   n_ctx8192/Worker owner and hashes, then disarm watchdog.
   Stop isolated Control, release lab sockets, and confirm shared
   Control/Deneb unaffected with read-only checks.

Only a final lab receipt, source hashes, zero-exit Operator and proven
8K rollback can qualify an **isolated Cancellation result**. Failure
is FAIL + mandatory 8K rollback. Do not mark production12K ready:
after-first-SSE-token cancellation, authenticated full model/runtime,
NVML-GPU-origin, HTTP wire evidence, and production PostgreSQL
Serving/Gateway/Client remain distinct unqualified gates.

Keep Issue #232 OPEN, PR #250 Draft/unmerged.
