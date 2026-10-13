# #137 — Durable chat admission ownership and Worker claim fence (design v0.1)

Status: **PROPOSED / DESIGN-ONLY / NOT ACCEPTED** (2026-10-13 JST)
Tracking: [Issue #137](https://github.com/upiscium/TSUMGI/issues/137);
[ADR 0002 proposal, PR #139](https://github.com/upiscium/TSUMGI/pull/139).
Source inspection: `main` `6560ec9` (#134 accepted SSE cancellation) and stacked
Draft #140 `dfdf4a6` (PostgreSQL deadline qualification). This document is
based on `main`, contains **no migration or deployed runtime code**, and does
not authorize production changes. The isolated unmerged Stage 3A attempt
provides only live-process, bounded best-effort readback: a missing row can be
followed by a late COMMIT, which is **not proof of no Job**.

## 1. Decision proposed and the actual guarantee

Prefer a **durable admission row plus claim-side fencing** over certifying a
shielded `asyncio.to_thread` task, an in-memory UNKNOWN counter, or a single
post-error lookup as crash-safe. Stage 1's permit and Stage 2's synchronous
database deadline remain separate necessary resource controls.

For a newly owned chat admission with generated identity `A`, require:

- **I1 / Unique identity:** at most one committed Job `J` is linked to `A`.
  A retry within the *same* server request reuses `A`. A fresh HTTP request
  gets a new `A`; cross-request deduplication is explicitly **not** promised.
- **I2 / Pre-activation:** a committed `PENDING` owner or an unconfirmed
  admission cannot be claimed by any Worker. A lost COMMIT acknowledgement
  therefore cannot create an immediately executable chat Job.
- **I3 / Cancel-first:** once `CANCEL_REQUESTED` is durably recorded for
  `A`, no subsequent *new* claim or activation may succeed for its Job.
  An already RUNNING claim is instead cancelled/fenced using the established
  Job lease and native cancellation path.
- **I4 / Active lease:** a claim is permitted only under a locked `ACTIVE`
  owner with an unexpired database-clock owner lease and unexpired Job
  deadline, in addition to all existing Worker/serving predicates. The owner
  must be revalidated on lease renewal, event append, completion, failure and
  recovery paths; no old completion may be accepted after cancellation or
  owner expiry.
- **I5 / Durability:** a Gateway crash cannot erase durable cancellation
  intent. A restarting Control/reconciler must be able to identify and
  complete unresolved owned Jobs without relying on a Python Task.
- **I6 / No privilege expansion:** admission IDs, owner epochs and Job IDs are
  created/selected by trusted server code, not supplied as arbitrary client
  cancellation targets. This does **not** add principal-scoped authorization
  to the existing shared-trust bearer Control API.

**Important limitation:** a hard Gateway crash or a physical TCP disconnection
may be unobservable until detection or the owner lease expires. If the owner
was already `ACTIVE`, a legitimate claim may occur within that lease window.
Do not claim instantaneous 'zero execution after disconnect'. The stronger
guarantee is **no claim for PENDING/CANCEL_REQUESTED/expired owners, and bounded
recovery after lease expiry**, subject to functioning PostgreSQL and Control
processing. A Worker already executing a native operation still needs a
bounded cancellation/lease-check reaction; GPU process termination is not
atomic with database state.

## 2. Proposed schema (illustrative, NOT a migration file)

Add an additive owner table and a nullable, unique owned-job reference:

| Field | Contract |
| --- | --- |
| `gateway_admissions.admission_id UUID PRIMARY KEY` | Cryptographically random, server-generated before an admission thread starts |
| `state` | `PENDING`, `ACTIVE`, `CANCEL_REQUESTED`, `CLOSED` |
| `owner_epoch UUID` | Private Gateway instance/fencing epoch; epoch mismatch denies activation/renewal |
| `request_deadline_at TIMESTAMPTZ` | Immutable finite request deadline, checked using PostgreSQL `clock_timestamp()` |
| `lease_expires_at TIMESTAMPTZ` | Finite Gateway-owner liveness bound; never beyond request deadline |
| `close_outcome` | Nullable terminal classification: `CANCELLED`, `SUCCEEDED`, `FAILED`, `NO_JOB`, `UNKNOWN`; success is **not** cancellation |
| `created_at`, `updated_at`, `closed_at` | Durable operational evidence; no bearer tokens/prompt payloads |
| `jobs.gateway_admission_id UUID NULL UNIQUE REFERENCES gateway_admissions` | Existing non-Gateway Jobs remain NULL; non-null binding immutable once set |

A forward migration would require reviewed foreign keys, checks/indexes,
collision handling and an immutable owned-Job binding. The owner-to-Job
relationship is derived from `jobs.gateway_admission_id` under a unique
constraint: **do not also create a circular owner.job_id foreign key**.
Use the existing `jobs.idempotency_key` UNIQUE facility with a namespaced key
derived from `A`, validated against the exact request content and serving
binding. No caller-supplied idempotency identity may impersonate `A`.
A `CANCEL_REQUESTED` record with no Job is a **durable tombstone**, not proof
the original submission thread has stopped.

Schema validation must reject inconsistent states (e.g. `ACTIVE` with no Job,
lease after request deadline, mismatched owner epoch), including on repeated
calls. `CLOSED` must not be silently promoted back to `ACTIVE`. Avoid
using application UTC timestamps as the sole lease authority: require DB
`clock_timestamp()` and explicit 2-second app/DB skew policy from Stage 2
where app-derived deadlines are involved.

## 3. Exact state machine and operations

```text
                   cancel or owner expiry
                   ┌──────────────────────────────┐
                   ▼                              │
NEW --atomic admit--> PENDING --activate--> ACTIVE│
 │                    │                   │       │
 └--cancel(upsert)-----┴-------------------┴----> CANCEL_REQUESTED
                                                  │
                      no Job / CANCELLED / TERMINAL readback
                                                  ▼
                                                CLOSED
```

**Admission / COMMIT ambiguity:**

1. Gateway generates `A`, private owner epoch `E`, a bounded deadline and
   takes a finite admission permit **before** creating the synchronous thread.
   Persisting `A` is not necessarily possible before network failure; its
   possible late Job must nevertheless be `PENDING`.
2. The PostgreSQL transaction inserts/locks owner `A` **first** (idempotent
   `INSERT ... ON CONFLICT` plus `SELECT ... FOR UPDATE`). If
   `CANCEL_REQUESTED` or `CLOSED`, it refuses submission. If a valid
   `PENDING` owner already exists, it reuses `A` without resetting any
   cancellation state or lease.
3. Only under this owner lock, insert/select the same unique owned Job
   `J`, verifying the complete submission identity. Commit **both** owner and
   Job while owner remains `PENDING`. Apply Stage 2 PG17 deadline checks,
   preserving stricter operator settings.
4. A lost COMMIT acknowledgement stays `UNKNOWN`. A primary read under `A`
   may find a Job, but **a miss cannot prove it will never commit later**.
   Do not retry with a newly generated identity; do not mark `NO_JOB` without
   a separate durable fence proving the admission attempt cannot still
   commit. Even if the Gateway disappears, `PENDING` prevents Worker claim.

**Activation:** Gateway must explicitly confirm the request remains owned
(including ASGI pre-response disconnect observation where available), then
lock owner `A` followed by Job `J`. It may set `ACTIVE` only if
`PENDING`, same private epoch, linked Job still `QUEUED`, and both server
deadline and lease are fresh. Response lifecycle ownership and service
lifespan must then renew the finite lease. Activation is *not* part of the
initial admission COMMIT. A disconnect missed by ASGI can still leave an
`ACTIVE` lease until expiry; do not misstate that limitation.

**Cancellation before admission (including lost HTTP task):** persist an
idempotent `CANCEL_REQUESTED` owner tombstone keyed by `A`, even if no Job
is yet visible. An in-flight submission transaction either sees this state
and aborts, or commits `PENDING` first and the following cancellation
serializes on its owner lock. A cancelled owner must never be resurrected.
If a linked Job exists, lock it after the owner, transition
`QUEUED`/`RUNNING` to `CANCELLED`, revoke the running attempt and decrement
Worker capacity exactly once through existing fencing. `SUCCEEDED`/`FAILED`
are terminal **not cancelled**; `CANCELLED` is acknowledged idempotently.
Failed cancellation acknowledgements remain persisted for bounded retry,
readback and operator-visible UNKNOWN.

**Graceful close/heartbeat:** service lifespan closes admissions first,
sends cancellation intent, drains tasks for a finite grace, then reports
outstanding owner IDs/counts safely. A successful normal stream closes the
owner with `SUCCEEDED` only if the linked Job really succeeded; no blanket
`_cancel` may retroactively relabel a successful terminal Job. Renewals require
matching private epoch and real response/request lifecycle ownership. Do not
log private owner tokens, payloads or credentials.

## 4. Worker claim transaction and lock-order proof obligation

Current `PostgresControlRepository.claim_next_job` locks the selected
`jobs` row using `FOR UPDATE SKIP LOCKED`, then locks `workers`. Existing
`cancel_job`, `heartbeat_worker`, `complete_job`, `fail_job`,
`append_job_event`, `expire_deadline_jobs` and
`recover_expired_jobs` also mutate Job rows. Joining an owner to that
existing `FOR UPDATE` query is **not enough**: locking Job then owner in
one path while cancellation locks owner then Job creates a deadlock cycle.

Required global lock order **where an owner exists**:

```text
gateway_admissions[A]  →  jobs[J]  →  workers[W]
```

For legacy unowned Jobs, the existing `Job → Worker` order remains. The
candidate query can read ordered Job IDs and immutable owner IDs **without a
row lock**, as an advisory prefilter. It must skip ineligible `PENDING`,
`CANCEL_REQUESTED` and expired owners in that prefilter (to prevent
starvation of ordinary Jobs), but must **revalidate under locks**:

1. Read a bounded, priority/sequence-ordered candidate batch without
   `FOR UPDATE`. Never trust this snapshot as authorization.
2. If an owner ID exists, lock the owner `FOR UPDATE SKIP LOCKED` **first**;
   if unavailable, skip this candidate without blocking all dispatch.
3. Lock the exact Job row `FOR UPDATE SKIP LOCKED`; recheck its immutable
   owner binding, `QUEUED`, deadline, available time, attempts, serving,
   requirements and owner `ACTIVE`/fresh lease under the locked rows.
4. Lock the Worker row **last**, recheck online heartbeat, runtime epoch,
   capacity and all GPU/serving capability predicates. Only then assign a
   lease, increment attempts and Worker active count in the same COMMIT.

Candidate scanning requires a tested bounded batch/cursor policy that
preserves priority without starving ready Jobs behind blocked owners. Snapshot
prefilters must not be mistaken for a fence. Never `SKIP LOCKED` a whole
`jobs` scan and then acquire owner locks in the opposite order. Define
atomic CAS/row locking for cancellation versus claim:

- **Cancel-first:** locks owner then Job, commits `CANCEL_REQUESTED`. Claim
  later sees non-`ACTIVE` under owner lock; no new RUNNING transition.
- **Claim-first:** holds owner then Job then Worker, commits RUNNING. Cancel
  then locks owner then Job/Worker, marks exact Job CANCELLED and fences its
  lease/epoch. It is possible native execution began before cancellation.
- **Crash before activation:** Job may exist but remains PENDING; Worker
  must never claim it; reconciler later cancels.
- **Crash after activation:** owner lease permits a bounded claim window
  until expiry. Worker renewal/completion must fail closed after expiry.

The **same owner-first helper** (read immutable owner ref without locking,
then lock owner → Job → Worker, recheck) must govern owned transitions in:
`heartbeat_worker`, `complete_job`, `fail_job`,
`append_job_event`, `cancel_job`, `expire_deadline_jobs`,
`recover_expired_jobs`, and any retry/requeue or expiration path. An
outstanding claim that predates cancellation must never regain claimability
via `recover_expired_jobs` or retryable `fail_job`. Verify all other Worker
registration/ownership operations for reverse lock acquisition.

## 5. Reconciler, bounded resources and startup behavior

A Control-side reconciler, **not only the Gateway process**, scans due owners
by indexed state/deadline/lease expiration using bounded batches and
`SKIP LOCKED`. It must cancel expired `PENDING` and `ACTIVE` owners, and
resume `CANCEL_REQUESTED` cleanup after a crash. In each transaction lock
owner then exact Job then Worker; preserve `CANCEL_REQUESTED` on DB/ack
failure, retry with bounded backoff and observable unresolved counters.
Use durable timestamps and a finite maintenance retention policy; do not
delete owner tombstones while a stale request could be confused for an
activated owner. Even after retention, a late fresh registration for the same
ID must start PENDING and require the original live epoch to activate,
which a dead Gateway cannot do.

Worker heartbeat/lease renewal and result/event APIs must reject stale
owner epoch, cancelled/expired owner or stale Job attempt, and native work
must converge to stopped after rejection. Expiry must be enforced at the
transaction boundary, **not only by scheduled sweeping**. An unavailable
PostgreSQL or network blackhole may delay notification/cleanup; document a
finite retry/disposition policy without claiming to interrupt hung OS threads.

Keep two separately scoped limits: the per-Gateway in-flight admission
budget from Stage 1 and durable pending/active owner counts in Control
(admission/lease/reconciler load). Neither is a global GPU physical isolation
feature. Avoid unbounded `asyncio.shield()` cleanup chains and unbounded
PostgreSQL cancellation/retry loops.

## 6. Deployment compatibility and rollback are hard gates

**Do not enable owned submissions during mixed-version Control deployments.**
An old Control binary's claim SQL ignores `gateway_admission_id`, so it can
claim a PENDING or cancelled owned Job despite the new schema. Application-only
fencing therefore requires all claiming Control instances to be upgraded
and verified **before** enabling the Gateway owner-writing feature.

Proposed staged rollout:

1. Additive, reviewed `006`-series migration only after schema acceptance;
   no rewriting `001`–`005`. Take and verify a PG17 UTF8 backup/restore.
2. Deploy a revision of Control with owner-aware claim/result/recovery logic
   to **all** Control instances, but keep owner-write feature OFF by default.
   Verify the complete owner API and compatibility metadata before any Gateway
   advertises or activates durable admissions.
3. Deploy Gateway owner intent, activation, renewal and shutdown routing.
   Run negative isolation/compatibility tests; only then enable owned chat
   admission through an operator-owned rollout decision.
4. To downgrade, first **quiesce new admission and all owned claims**, drain or
   durably cancel and verify every owned Job, then stop new Controls.
   Downgrading the executable without this proof is unsafe even though the
   schema is additive. Do **not** claim PostgreSQL schema rollback by merely
   reverting binaries. A future trigger-based DB gate would require a
   separate lock-order/deadlock proof and is not assumed here.

These are #124 **operator/site** gates. #79 version/tag/publication remains
independent. No effect on physical Proxmox GPU exclusivity is implied; TSUMGI
still handles only its logical assignment/fencing and conservative VRAM
fail-closed path.

## 7. Required qualification before an implementation PR is mergeable

| Gate | Evidence required |
| --- | --- |
| Deterministic admission/cancel order | Cancel-before-insert, insert-before-cancel, cancelled task before response, idempotent duplicate same identity |
| UNKNOWN_COMMIT | Real PG17 commit-ack loss, lookup miss followed by late COMMIT, recovery without executable PENDING job |
| Owner state | PENDING never claimable; activation needs live epoch; expired/closed/cancelled cannot reactivate |
| Claim race | Two independent PG17 sessions, cancel-first and claim-first with row locks; no deadlock, no duplicate/phantom capacity |
| Multiple Workers | Priority/cursor fairness, SKIP LOCKED contention and retries; valid lower-priority Jobs not starved |
| Existing APIs | Heartbeat, append, success, failure, retry, expiration, ownerless Job and stale runtime epoch all obey lock/fence |
| Client lifecycle | ASGI pre-admission disconnect while handler stays alive, abort before first SSE token, abrupt Gateway SIGKILL |
| Unavailable DB | Connection blackhole/timeout, bounded permit and reconciliation disposition, fail closed without CPU/thread amplification |
| Recovery | Control restart, expired owner lease, idempotent sweeper, pending tombstone, no phantom slot occupancy |
| Migration | 005→006 path, old live data unchanged, constraints, new and old executable compatibility and required quiesce-to-downgrade |
| Source reviews | Independent Correctness/Security exact-HEAD reviews and full Python/Nix regression |
| Real system | Existing #134 OpenCode 1.18.35 → real PostgreSQL Control → CUDA llama.cpp Worker SIGKILL test remains PASS |

This document is **a design proposal**, not a completed proof for any of the
above new gates. Changes to schema, code, production Control or Worker, GPU
allocation, systemd services and releases are outside this design PR.

## 8. Specific unresolved decisions

- Exact lease lifetime/heartbeat cadence and process-death detection SLA:
  must be load tested, not invented.
- Minimum PostgreSQL version and HA/failover/read-after-write semantics:
  Stage 2 currently requires 17+ for bounded chat admission; primary read
  and safe promotion must be qualified under topology actually deployed.
- Complete Job-owner FK/check/immutability rules, index sizes, owner/tombstone
  retention and migration/rollback SQL need schema review.
- Candidate claim fairness under high backlog, retryable result edge cases
  and relationship with existing Worker/Job lock order require real PG tests.
- `client_auth=none` remains for operator-isolated installations only.
  Mutually untrusted clients require a separate per-principal authorization
  contract; a random admission ID is not an ACL.

**Decision requested later:** accept this stronger persistent-owner and
claim-fence scope as the intended #137 architecture, or explicitly choose
bounded best-effort instead and state which safety guarantees are waived.
Neither choice is silently accepted by writing this proposal.
