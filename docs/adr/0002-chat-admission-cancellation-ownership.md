# ADR 0002 — Bounded ownership of asynchronous chat admission

Status: **Proposed — not an accepted implementation or production contract**

Date: 2026-10-12

Tracking: [Issue #137](https://github.com/upiscium/TSUMGI/issues/137),
[Draft PR #138](https://github.com/upiscium/TSUMGI/pull/138);
distinct completed [Issue #110](https://github.com/upiscium/TSUMGI/issues/110).

## Problem boundary

A chat request can be cancelled while its synchronous
`repository.submit_job()` is still running inside `asyncio.to_thread()`.
Cancelling the HTTP coroutine does **not** cancel a running Python thread or
roll back an already issued PostgreSQL transaction. That thread may later
commit a durable QUEUED Job after its original consumer is gone.

The reviewed candidate in Draft #138 retains a shielded submission Task and
starts a cleanup Task that awaits the eventual Job ID. This fixes the
**finite-delay** case. Independent Correctness and Security reviewers found
two different blockers: no bound on retained tasks/DB work under stalled
PostgreSQL, and no verified cleanup owner across gateway shutdown; repeated
disconnects can exhaust resources. Transient failures of `cancel_job()`
are currently swallowed without a confirmed durable resolution.

Do not conflate this **pre-admission race** with #110's already accepted
**RUNNING-Job** streaming disconnect behavior. #110's SSE response ownership,
real OpenCode/CUDA cancellation and worker-fencing implementation remain
unchanged.

## Safety goals and non-goals

The desired invariant for a gateway-owned request is: after the request loses
its consumer, any Job it admits late is either **rejected before commit** or
**durably CANCELLED** as soon as the exact server-assigned Job identity is
available. A disconnected client cannot specify a Job ID to cancel.

A caller must never be told that a submission is cancelled merely because
the coroutine exited while a database thread still owns admission. Cancellation
and overload must retain the existing authorization, deadline, durable
Job terminal-state and Worker lease/epoch fencing semantics.

Resource consumption by lost-client admissions must be **bounded** under a
slow or unreachable database. Service shutdown must either drain ownership or
record an explicit unresolved state; absence of a Task is not proof of durable
cancellation.

**Current client-auth boundary is one shared-trust Control domain**, not
multi-tenant per-Job access control. The existing bearer authorizes a client
to use Control endpoints, including
`POST /v1/jobs/{job_id}/cancel`, for any known Job ID; `client_auth=none`
is suitable only for an operator-isolated local deployment. The new
Gateway-owned cancellation identity avoids introducing *another* arbitrary
client-selected target, but **does not** provide a new cross-client ACL for
the existing endpoint. Deployments with mutually untrusted clients require
a separate durable principal/Job-owner authorization design and site
ingress approval; do not claim such tenancy is supported under #137.
Preserve #124's separate network/credential go/no-go gate.

This ADR does **not** promise synchronous Python thread interruption, a
universal TCP/network timeout, zero window during abrupt process death,
host-wide GPU isolation, native process snapshot/restore, or a production
database migration.

## Proposed staged implementation

### A. Bound admission work before creating a thread

Introduce a configurable **per-Gateway admission budget**, initially a small
reviewed finite number (candidate default **8**, not a throughput promise).
The permit must be acquired **before** spawning `asyncio.to_thread()`;
reject additional arrivals with a deterministic public-safe 429/503
response instead of enqueueing waiting tasks or threads without a limit.

An accepted permit remains owned through: admission success/failure; HTTP
cancellation followed by late submission; and the last known cancellation
acknowledgement attempt. A cancelled HTTP caller does **not** immediately free
the permit while its underlying database operation is still in flight.

Counters and permits are service-local. They do not replace Control's global
scheduling capacity, PostgreSQL transaction bounds or a durable cancellation
record. Cancellation storms must not build an unbounded queue even when every
client is independently authenticated.

### B. Enforce bounds **inside** the synchronous PostgreSQL operation

Carry the request's absolute deadline into the admission operation. An
`asyncio.wait_for(shield(task))` is **not sufficient**: its timeout only
ends the coroutine's wait, not the database thread.

For PostgreSQL submission specifically, qualify bounded connection setup,
server-side statement timeout and lock timeout, and a deadline check after
the last potentially blocking operation and **before transaction commit**.
The server/driver must roll back a timed-out or interrupted admission rather
than treating its original request-start timestamp as proof that a later
commit is within deadline. Use a **fresh PostgreSQL `clock_timestamp()`**
check after potentially blocking SQL and immediately before the commit
boundary; `now()`/`transaction_timestamp()` and the request's original
Python `now` are not sufficient because they may predate a long lock wait.

Checking before `COMMIT` cannot prove the **commit itself** finished before
the deadline. A lost connection or commit acknowledgement is an
**UNKNOWN_COMMIT** outcome, not evidence that no Job was created. Reconcile
using the same server-generated admission ID or idempotency identity after
connectivity returns, then cancel any committed Job whose consumer departed;
do not replay as a second Job. State and test a positive clock-skew policy
between application-generated absolute deadlines and the server clock.
Do not modify unrelated Control transactions or weaken their
`READ COMMITTED` lock/fencing order.

The exact timeout values must be derived from a positive bounded policy and
verified against the deployed PostgreSQL version; they must not be inferred
from a gateway HTTP timeout alone. A failed or unreachable database still
needs a finite operator timeout/recovery state, since network blackholes and
process kills are not under PostgreSQL's complete control.

### C. Own cancellation until durable result, not merely task completion

Represent each attempt with a **server-created** admission identity, deadline
and an explicit lifecycle:

`NEW → ADMITTING → (REJECTED | COMMITTED) → (OWNED | CANCEL_REQUESTED)
→ (CANCELLED | TERMINAL | UNRESOLVED)`.

A delayed successful submission after the consumer's cancellation must
receive cancellation of **that exact Job ID**. Distinguish a failed submit
(no Job), an already-terminal Job, an acknowledged cancellation, and a
transient or permanent cancellation failure. Use a bounded retry/backoff
policy for transient repository failures; a swallowed exception cannot be
reported as a verified cancellation. Preserve a visible fail-closed
disposition until the bounded cancellation outcome is known.

A finite in-memory Task set is **not durable** across abrupt process death.
For a strict ownership guarantee beyond orderly shutdown, use a persistent,
reviewed cancellation-intent / owner-lease protocol tied transactionally to
durable admission. This may require a separate PostgreSQL migration and
changes to Worker claim eligibility; it is **not** part of Draft #138 and
must not be implied by the initial Task-only patch. Without that extension,
call the result **bounded best-effort with deadline recovery**, not a
process-crash-proof cancellation guarantee.

#### Persistent owner/claim fencing, if the stronger option is chosen

A proposed durable `gateway_admissions` record must be keyed by a
**server-generated UUID** (never an untrusted request field), carry a finite
owner lease and deadline, and persist one of `ACTIVE`,
`CANCEL_REQUESTED` or terminal states, with its optional exact Job ID.
Its schema, retention, backward-compatibility behavior and any Worker-claim
changes require a **separate reviewed migration decision**; none are
accepted by this ADR alone.

Define one linearization/lock order for **every** participant:
`admission-owner row → owned Job row → Worker capacity row` when both or
all are involved. Admission and cancellation create/lock the *same* owner
record transactionally (including cancel intent arriving before submission);
the Job must not become visible as claimable until its durable owner state
and Job relationship are committed. Worker claim must acquire/revalidate
the owner fence **before** locking or claiming the Job, rather than relying
on a stale `READ COMMITTED` snapshot. Re-check `ACTIVE`, owner-lease
expiry, Job deadline and `QUEUED` status while holding the locks.
Retrofitting this into the existing priority/lease claim query requires a
separate fairness, lock-order and deadlock analysis.

Two legal serial outcomes must be explicit:

- **Cancel-first:** `CANCEL_REQUESTED` commits before claim obtains the
  owner fence. The Job is not claimable; a later admission for that owner
  refuses/rolls back or produces a durably cancelled Job.
- **Claim-first:** Worker locks and commits the legitimate RUNNING transition
  first. Cancellation subsequently obtains the fence, cancels that *exact*
  Job, clears its lease/fences any stale completion, and the running native
  operation stops through the existing Worker cancellation path. This
  does **not** promise that the model never began work before cancellation
  won the race.

The existing non-gateway Job paths and their Job/Worker lock order must
not silently invert this sequence. A query filtered by an owner field
without an accompanying lock/recheck is **not** sufficient evidence of a
safe fence. A hard Gateway process crash may still leave an `ACTIVE`
owner until its lease expires; claim eligibility must fail closed at the
lease expiry even if the owner never writes a cancel intent. This is a
bounded lease-based guarantee, **not instantaneous cancellation at
process-death time**.

Treat `SUCCEEDED` and `FAILED` returned by a cancellation retry as
**terminal but not successfully cancelled**; only a durable `CANCELLED`
record proves cancellation. A retry that receives an already-`CANCELLED`
Job may succeed idempotently. An unknown result remains
`UNRESOLVED` until owner/Job state is read back or the explicit
deadline/lease recovery contract completes.

### D. Explicit Gateway shutdown and recovery boundary

Make the chat Gateway service owner reachable from the FastAPI/Control
lifespan. On graceful shutdown: reject new admissions; signal cancellation
for outstanding requests; wait only a **bounded grace** for retained
admission/cancellation owners; surface unresolved exact-count evidence
without logging secrets. Do not drop the only cancellation owner silently
or claim success when a synchronous thread can still commit.

On startup, reconcile any durable owner/cancellation records if the durable
protocol is selected. Otherwise document the explicit limited fallback:
Control's existing Job deadline and lease recovery will eventually fence
expired work, but **cannot alone prove prompt cancellation before a stale
consumer's deadline**.

Never use unconditional application-wide `asyncio.all_tasks()` cancellation
or a global kill of the shared thread pool to repair this one Gateway path.

## Why not just shield more Tasks?

Keeping a strong reference to `asyncio.shield(asyncio.to_thread(...))` avoids
losing a Job ID after a *finite* PostgreSQL wait, but does not bound pending
connections/threads, stop a stalled transaction, retry failed acknowledgement,
or keep a cancellation owner alive after process termination. It is a
valuable incremental fix only under an explicit finite-work assumption.

Similarly, letting the HTTP caller wait for the whole database transaction
after disconnect could avoid detachment, but can hang ASGI shutdown forever
and cannot prevent a process kill. The service needs admission concurrency,
bounded DB operations and a recovery policy regardless of the caller policy.

## Required acceptance matrix

| Scenario | Mandatory observable evidence |
| --- | --- |
| Cancellation before submission begins | No Job created; no leaked permit |
| Cancel while delayed admission thread is blocked, then DB unblocks | At most the exact owned Job admitted; it is CANCELLED or the transaction rolls back |
| DB lock wait longer than request deadline | DB operation is actually bounded/rolled back, not only the HTTP task |
| Repeated cancellations and blocked DB connections | Concurrent work stays under reviewed admission budget; excess requests promptly fail closed |
| Submission fails after disconnect | No phantom Job, strong cleanup owner released, no unhandled Task exception |
| Cancellation acknowledgement temporarily fails | Retry/disposition observable; no claim of success without durable confirmation |
| Cancel-first versus Worker claim, with row locks | Cancel intent serialized before claim; Job never accepted for execution |
| Shared client bearer versus mutually untrusted clients | Document present domain-wide cancel authority; forbid unsupported multi-tenant exposure without separate authorization |
| Claim-first versus cancel, with row locks | Exact Job becomes RUNNING, then CANCELLED; lease/epoch fenced and Worker eventually stops |
| Retry after already-SUCCEEDED or already-FAILED | Classified TERMINAL, not misreported as CANCELLED; no overwrite |
| Retry after already-CANCELLED | Idempotent acknowledgement without second Job or new capacity usage |
| Lost connection around COMMIT/uncertain acknowledgement | Reconcile by server-generated identity; no duplicate replay or false not-admitted claim |
| Stale gateway owner lease after abrupt crash | Claim forbidden after owner lease expires, independent of late maintenance |
| Cancel during service shutdown | Bounded drain/visible unresolved count, no silent disappearance of owners |
| Actual hard process termination | Explicitly distinguish deadline/lease recovery from a proven durable cancellation-intent protocol |
| Real PostgreSQL 17 lock/transaction contention | Rollback/idempotency, no late viable Job, correct cleanup and original DB unaffected |
| Successful request, successful stream, worker terminal Job | No inappropriate cancellation of a different or already-successful Job |
| Real loopback disconnect and OpenCode→native GPU path | #110's accepted RUNNING-state tests remain green and Worker capacity is reusable |
| Independent correctness/security review | Confirm finite resource ownership, shutdown and the precise trust scope; exact HEAD CI Green |

Test code must avoid a permanent blocked thread in CI: bounded barriers,
disposable PostgreSQL data, finite deadlines and unconditional cleanup.
Include negative tests that first demonstrate the old candidate's
unbounded/abandoned behavior and positive tests on the corrected code.
Private DSNs, model paths, credentials, process identifiers and host
topology are not publication evidence.

## Integration and operator gates

1. This **proposal** does not accept either a schema migration or any fixed
   timeout value. A reviewed implementation must choose the bounded
   best-effort scope or the stronger durable cancellation-intent option and
   state its guarantee exactly.
2. Keep [Draft #138](https://github.com/upiscium/TSUMGI/pull/138)
   unmerged while the independent P2/High concerns remain unresolved.
   Historical competing Draft #133 is not a safe replacement for #134.
3. No change here authorizes production PostgreSQL schema migration,
   systemd Control/Worker restart, GPU reassignment, or release tagging.
   Site rollout and recovery acceptance remain [#124](https://github.com/upiscium/TSUMGI/issues/124);
   version/publication remain [#79](https://github.com/upiscium/TSUMGI/issues/79).
4. A review-ready code PR must record exact commit/reproducer evidence,
   focused/full tests, actual PostgreSQL qualification, source reviewers and
   whether its guarantee survives a Gateway process crash.
