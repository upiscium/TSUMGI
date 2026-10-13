# #137 — Durable admission owner and Worker claim preflight

Status: **Proposal / review artifact only — NOT an accepted ADR, migration, runnable SQL patch, or production authorization**.
Prepared against source main 6560ec9 and the additive Stage 1/2 drafts #138 and #140 (Stage 2 source dfdf4a6). The parent [ADR 0002](../adr/0002-chat-admission-cancellation-ownership.md), PR #139, is still Proposed.

## Purpose and strict non-goals

A cancelled HTTP request may lose the identity of a Job whose PostgreSQL COMMIT succeeds after the request or process disappears. A bounded in-memory task, a new server-side random idempotency key, an HTTP 503, and a readback while the process survives are useful mitigations, but **none persists the request owner and its cancel intent across Gateway/Control process death**.

Target, *if the stronger protocol is separately accepted*: after a bounded owner-lease failure or a durably recorded cancellation, a Gateway-owned Job must not be newly claimed. A late successful Job admission must remain linked to precisely one durable owner record and must be cancelable/reconcilable by that identity. Existing Worker lease/attempt epoch fencing must remain valid.

Non-goals: hard synchronous thread kill; instantaneous detection of a process crash; preventing a legal claim that committed *before* cancellation; preserving live CUDA execution after a host kill; host-wide GPU exclusion; isolation among mutually untrusted clients; exactly-once behavior across a client's unrelated fresh HTTP retry.

In particular, **a new HTTP retry is a new request and may still create another Job**. Internal admission owner identity enables server-side crash reconciliation of the original request, not cross-request deduplication. A trusted, principal-scoped client retry contract would require a separate authorization decision.

## Verified existing code paths (not hypothetical)

The Worker requests work through Control's POST /v1/workers/{worker_id}/jobs/claim endpoint; the Control process calls repository.claim_next_job. The Worker does **not** directly claim rows in PostgreSQL. However, multiple Control instances or external holders of the Control database credentials can run old claim code. Safe activation therefore requires a Control-side version/rollout fence, not merely updated Workers.

At the source baseline, PostgreSQL claim_next_job:
1. Reads a Worker without locking;
2. Selects a queued candidate with priority DESC, sequence ASC and FOR UPDATE SKIP LOCKED (**Job row is locked here**);
3. Locks the Worker row and rechecks its capacity/identity;
4. Transitions the Job to RUNNING and increments Worker active_jobs.

The following paths currently lock **Job first, then Worker where needed**:

| Repository method | Current row-lock scope | Required owner-enabled treatment |
| --- | --- | --- |
| claim_next_job | Job -> Worker | Owner -> Job -> Worker for linked Jobs, recheck after each lock |
| cancel_job | Job -> Worker if RUNNING | Owner -> Job -> Worker; persist intent even if Job not inserted |
| heartbeat_worker with active job | Job -> Worker | Owner -> Job -> Worker before renewing an owner-linked attempt |
| append_job_event | Job | Owner -> Job, or prove owner state is fenced by immutable Job cancellation |
| complete_job / fail_job | Job -> Worker | Owner -> Job -> Worker, maintain epoch/lease rejection |
| expire_deadline_jobs | Job -> Worker | Owner -> Job -> Worker for linked Jobs |
| recover_expired_jobs | Job -> Worker | Owner -> Job -> Worker; owner-ineligible retries must not return QUEUED |
| submit_job | Optional idempotent Job lock, then INSERT | Lock prepared Owner before any owned Job INSERT/link |
| register_worker / set_worker_state | GPU advisory identity -> Worker (no Job lock) | Keep their independent GPU ownership discipline; do not acquire Owner under a held Worker lock |

These are *all* candidate code review targets, not a statement that the proposed lock-order changes are already implemented. The in-memory reference repository must preserve equivalent observable state transitions. Read-only Job lookup and list endpoints are not authority to mark cancellation acknowledged.

Worker runtime checks the durable Job status and fences stale completions. To make an expired owner stop a **running** native task promptly, a reviewed future change must also cover Worker heartbeat/job inspection, expiry maintenance, and bounded native cancellation propagation. Merely preventing a **new** claim after owner expiry does not instantly terminate an already-RUNNING attempt.

## Proposed storage shape (illustrative, not executable migration)

The first feasibility draft placed a nullable job_id on gateway_admissions. Independent Correctness review found that this alone **cannot enforce ownership of Gateway-created Jobs**, since a Job can be inserted directly into jobs without ever being linked. Prefer a **Job-side, insert-time foreign key and explicit origin discriminator** in the next reviewed additive migration:

    gateway_admissions(
      id UUID PRIMARY KEY,                -- generated inside trusted Control/Gateway
      owner_instance_epoch UUID NOT NULL, -- changes on process restart
      state TEXT NOT NULL,                -- ACTIVE | CANCEL_REQUESTED | UNRESOLVED | TERMINAL
      request_deadline_at TIMESTAMPTZ NOT NULL,
      lease_expires_at TIMESTAMPTZ NOT NULL,
      created_at TIMESTAMPTZ NOT NULL,
      updated_at TIMESTAMPTZ NOT NULL,
      cancel_requested_at TIMESTAMPTZ NULL,
      terminal_kind TEXT NULL             -- CANCELLED | SUCCEEDED | FAILED | REJECTED
    )

    jobs.admission_kind TEXT NOT NULL DEFAULT 'direct'
      CHECK (admission_kind IN ('direct', 'gateway'));
    jobs.gateway_admission_id UUID UNIQUE NULL
      REFERENCES gateway_admissions(id) ON DELETE RESTRICT;
    CHECK (
      (admission_kind = 'gateway' AND gateway_admission_id IS NOT NULL)
      OR (admission_kind = 'direct' AND gateway_admission_id IS NULL)
    );

The Gateway-specific Job INSERT must explicitly use admission_kind=gateway and the *pre-committed* owner UUID in the same row, under an Owner row lock. Both fields must be immutable after insertion (trigger or reviewed write permissions); the UNIQUE FK permits **at most one Job per owner**. This eliminates the two-write visibility concern and ensures a *correctly marked* Gateway Job cannot commit without a persisted owner row. Owner-to-Job recovery is a unique indexed lookup on jobs.gateway_admission_id.

**This check is necessary but not sufficient:** an older or buggy Gateway path can omit the new discriminator and be accepted as a legacy direct Job under the proposed default. All gateways/Control instances must therefore be cut over or fenced before activation; an explicit versioned submission API and database-verifiable protocol authority (for example, role-limited insert contract) still require separate design. Never represent the default discriminator as preventing a legacy process from bypassing ownership. The original ADR #139 sketches an owner-side Job ID; the alternative Job-side FK here is an architectural proposal requiring review, **not an amendment already accepted into ADR #139**.

Add an index on nonterminal owner state plus lease_expires_at, CHECKs for valid states, positive finite leases, terminal compatibility, and immutable owner identity. Keep owner rows until every linked Job is durably terminal and the approved audit/retention period passes; **never silently delete a live owner tombstone**. Fix exact DDL, origin authority, permissions, retention and backward compatibility in a separate migration review.

A prepared owner must be durable **before** asynchronous admission begins. A cancelled request can therefore persist CANCEL_REQUESTED even when admission is blocked and no Job yet exists. Use server-generated UUIDs and owner_instance_epoch; never trust a request-supplied Job ID or owner UUID. Repeated prepare and reconciliation must not result in a second Job.

A new API method should encapsulate prepare / admit / cancel-intent / reconcile / renew with explicit versioned contract; do not expose unrestricted owner row mutations to a client bearer. A temporary request-local cancellation Task is a performance optimization only, never the sole durable owner.

## State machine and linearization points

States are monotonic: ACTIVE -> CANCEL_REQUESTED or UNRESOLVED -> TERMINAL after a verified durable outcome. A reader cannot promote a transient error or a missing row to CANCELLED; an already-SUCCEEDED or FAILED Job is terminal, **not successfully cancelled**. A durable CANCELLED Job may be acknowledged idempotently.

1. **PREPARE:** transaction INSERTs owner ACTIVE with a bounded lease and immutable identity; if this fails, do not spawn submission.
2. **ADMIT:** lock Owner FOR UPDATE; if cancelled, unresolved, terminal or expired, reject without a Job; otherwise INSERT one Job with admission_kind=gateway and gateway_admission_id=the locked Owner ID *in one SQL row*. DB COMMIT is the atomic Job/owner-link visibility point. An unknown acknowledgement is reconciled through that prepared Owner identity, never a new admission key.
3. **CANCEL:** lock Owner FOR UPDATE and commit CANCEL_REQUESTED even if no Job is linked. Look up Job by its immutable gateway_admission_id under that lock; if linked, lock Job next and Worker last (if running), durably cancel/fence the exact Job; only after confirmed cancellation mark terminal CANCELLED.
4. **CLAIM:** a read-only ordered candidate enumeration may nominate a Job. For a Gateway-linked candidate, lock its Owner **first** (prefer SKIP LOCKED with reviewed fairness), check ACTIVE, then lock/re-read that exact Job and finally lock/recheck Worker capacity. **After the last potentially blocking Job/Worker lock, check fresh PostgreSQL clock_timestamp again**, compare against Owner lease, request deadline and Job deadline, and only then attempt the RUNNING UPDATE. State/revision, worker capacity, serving match and lease/epoch must still be valid under those locks. The RUNNING transition commits with these locks held. Never rely solely on a clock checked before blocking on a later lock.
5. **CLAIM-FIRST:** a legitimate RUNNING transition may win; subsequent CANCEL must fence the exact lease/epoch, release its Worker capacity and cause eventual native interruption. Do not promise the executor never started.
6. **CANCEL-FIRST:** CANCEL_REQUESTED commits before claim acquires Owner; claim skips it. If no Job exists yet, later admission refuses or creates only a safely terminal artifact.
7. **EXPIRY/CRASH:** once an Owner lease is expired *at the final locked claim eligibility check*, the claim path must reject without needing maintenance. An Owner can expire while waiting for a Job or Worker lock; that path must recheck rather than committing a RUNNING transition based on a stale early clock. A transaction that passes its last eligibility check might still have an ambiguous/delayed COMMIT; **this proposal does not guarantee that COMMIT finishes before expiry**, and that stronger real-time property requires separate protocol proof. Recovery may reconcile a committed Job and durably cancel it; an absent Job is not proof of failed COMMIT while an in-flight transaction might still commit. Until reconciliation establishes an outcome, UNRESOLVED never authorizes a claim.
8. **RENEWAL:** only a matching owner instance epoch with an ACTIVE request may renew; never extend lease beyond approved request deadline. A crashed instance cannot silently regain its old lease. Renewal rate and lease duration require a measured positive bound, not a guessed default.

### Why the current claim query cannot simply add an owner WHERE clause

READ COMMITTED predicates evaluated **before** FOR UPDATE may use a stale candidate snapshot. With the current Job-first SKIP LOCKED query, appending an owner lock afterward violates the Owner -> Job order; cancellation could hold Owner then wait on Job while claim holds Job then waits on Owner. An unprotected JOIN or NOT EXISTS checking owner status cannot establish linearization against a concurrent CANCEL.

A compatible candidate algorithm needs a **non-locking** ordered read, then Owner lock/re-read, then Job lock/re-read, then Worker lock/re-read, followed by a **second fresh DB-time eligibility check after the final lock**. It also needs an efficient bypass for legacy ownerless jobs. Candidate enumeration must be bounded and cursor/pagination-aware so many locked, expired or cancelled owners cannot starve lower-priority eligible jobs. Preserve resource filters, serving revision checks, priority/FIFO policy and retry/lease semantics. Prove the SQL optimizer and row locks respect the order using real PostgreSQL traces; do not infer it merely from textual JOIN ordering.

### Unknown COMMIT and identity limits

A Stage 3A candidate (separate worktree, not changed by this PR) introduces per-request random idempotency keys and bounded readback. That improves the *live-process* ambiguity window, but the key is lost after a crash unless associated with a prior **durable owner**. The proposed pre-committed owner identity is the persistent lookup root. A durable owner does **not** by itself prove the Job never ran after a crash; the lease/claim fence and Worker/maintenance path provide the bounded recovery behavior.

If COMMIT acknowledgement is lost: never create a replacement Job to test reachability. Read the same owner from the primary after recovery, classify bound Job and cancellation state under locks, and mark absent/unconfirmed outcomes UNRESOLVED until a safe proof is obtained. PostgreSQL recovery, connection loss during COMMIT, and cancellation of a transaction waiting for a lock need independent tests.

## Migration, activation and rollback gates

1. **Preflight:** identify all Control processes connected to the *same* database, verify which expose claim endpoints, current PostgreSQL version and migration baseline (six files 000..005), and ensure backups/rollback instructions are operator-reviewed. Do not assume one Control instance just because one is configured locally.
2. **Additive schema only:** add the Owner table, jobs.admission_kind discriminator, nullable unique jobs.gateway_admission_id FK, required origin/FK CHECKs, immutable-binding enforcement and indexes with no automatic feature activation or Worker scheduling change. Qualify DDL lock duration and avoidance of a bulk rewrite of existing Job rows; test forward migration and rollback on disposable PG17. Existing ownerless/direct Jobs remain compatible, but origin-default bypass by an old Gateway remains an activation blocker.
3. **Upgrade Claim and every Job mutator:** deploy owner-aware repository code and configuration with owner admission **OFF**, including claim, cancel, heartbeat, event write, complete/fail, maintenance and retry paths. Every Control instance must be upgraded or fenced before owner-enabled Jobs exist. Worker binaries use the Control HTTP claim endpoint; physical Worker updates are needed only if heartbeat/inspection/native-cancel protocol truly changes.
4. **Compatibility gate:** refuse activation if any Control instance or direct SQL writer can claim owner-linked Jobs using legacy Job-first code. Merely announcing a version in one process is not a database-enforced global fence. If mixed Control versions cannot be excluded, **BLOCK activation** pending a safer DB-level transition protocol.
5. **Opt-in activation:** enable durable owners for only Chat Gateway requests; keep bounded local permits and Stage 2 PostgreSQL timeouts; exercise canaries with crash, cancellation, claim races, competing Workers, restarted Control, and queue fairness. Do not widen client authentication or external network access.
6. **Fail-closed rollback:** stop new owned admission, durably request cancellation for unresolved/active owners, fence or drain owner-linked QUEUED/RUNNING Jobs, verify all Worker capacity released and no claimable owner-linked Job remains. Turn feature OFF only after the safety proof. Keep additive owner schema and tombstones in place during rollback; **never revert claim fencing or DROP owner state while live owned Jobs remain**.
7. **Removal/down migration:** only after separate approval of retention policy, terminal-state readback, physical backup and zero active owner-linked work. Roll-forward recovery is preferable to destructive schema downgrade.

A source-compatible migration does not imply safe deployment compatibility: an old Control process may bypass the new owner fence while connected to the same database. This is a critical operator acceptance item (#124), independent of release (#79).

## Required independently observed acceptance matrix

| Case | Must prove |
| --- | --- |
| Cancel before Job INSERT | CANCEL_REQUESTED commits, Job not claimable, no duplicate insert |
| Cancel-first vs claim | recorded CANCEL_REQUESTED excludes claim; a newly expired Owner fails final post-lock DB-clock check |
| Claim-first vs cancel | legal RUNNING then CANCELLED, lease/epoch fenced and slot reused |
| Cancel concurrently with late COMMIT | one owner/Job identity, never orphan claimable |
| COMMIT reply lost / Gateway killed | owner recovery identifies same Job and does not create second Job |
| Gateway crash before/after PREPARE | no unowned queued Job; stale owner ineligible without maintenance |
| Lease expiry while queue is busy | reject when lease expires during Job/Worker lock wait; fresh final DB-clock check, not stale Python now |
| Owner expiry after RUNNING | prompt bounded Worker stop path or explicitly separate fallback deadline |
| Concurrent claim/cancel/heartbeat/retry | lock ordering has no deadlock, retries/fairness bounded |
| Queue with many ineligible owners | ordinary ownerless work still progresses; no starvation or unbounded scan |
| Terminal outcomes SUCCEEDED, FAILED, CANCELLED | no false cancellation acknowledgement or resurrection |
| PostgreSQL 17 lock contention, clock skew, uncertain COMMIT | no invalid success or visibility gap |
| Migration with old Control/Worker | no activation when legacy claim path can bypass fencing |
| Rollback with active owner-linked job | operation blocked until cancellation/drain verified |
| End-to-end actual OpenCode + GPU Worker | #110 disconnect behavior and slot reuse remain correct |

Run the negative controls first, then the candidate on a disposable PostgreSQL 17 instance. Test real SQL concurrency with bounded barriers and cleanup, not only in-memory stubs or single-connection mocks. Measure deadlock/lock-wait frequencies, throughput and priority ordering against the unchanged scheduler. Independent Correctness and Security reviews must evaluate the **exact** schema, lock order, SQL plan and release protocol.

## Decision gate

This is an evidence-backed feasibility **proposal**, not an acceptance of the strict protocol. Before writing a production migration, a maintainer must explicitly choose a guarantee: (a) bounded best-effort with deadline recovery, or (b) stronger durable-owner/claim fence. For (b), approve precise lease/timeouts, SQL/locking approach, schema retention and mixed-Control rollout. Passing document tests is never a substitute for the real claim/cancel tests.

Until then: #137 remains OPEN, #138 and #140 remain Draft, #139 remains Proposed, no main merge or production DB/Worker/GPU/release change is authorized.

## Initial PostgreSQL lock-order feasibility probe

The deliberately isolated [SQL prototype test](../../tests/test_gateway_owner_claim_sql_probe.py) uses temporary, randomly named schemas and **stand-in** owner/job/worker tables on a disposable PostgreSQL database. It checks:

- CANCEL-FIRST while the owner row is locked: a competing claim skips the locked Owner, and no subsequent claim succeeds;
- CLAIM-FIRST: after a legitimate RUNNING transition, cancellation obtains the Owner fence, cancels the Job and returns Worker active capacity;
- PREPARE commits the Owner **in a separate transaction before ADMIT**, so other connections observe an Owner with no Job; later ADMIT locks this Owner and inserts one linked Job;
- CANCEL before Job insertion and CANCEL holding the Owner row against a later concurrent ADMIT: PostgreSQL lock-wait observation confirms the serialization, and a delayed admission refuses without inventing a Job;
- expired owner at highest priority: it cannot be claimed, while a lower-priority legacy ownerless Job still progresses;
- owner lease expiring **during an independently observed PostgreSQL Worker-row lock wait**: the final fresh database clock must reject the claim;
- database CHECK rejects a Gateway-origin Job without an Owner foreign key;
- database UNIQUE rejects a second Gateway Job attached to the same Owner.

The probe requires both an explicit ASTRUMWEAVER_OWNER_PROBE_DSN and a separately generated, random 256-bit ASTRUMWEAVER_OWNER_PROBE_NONCE stored in a sentinel row by the **disposable database setup**. **On the same physical connection which will issue each DDL or DML statement**, it rejects additional DSN routing parameters such as hostaddr/service, checks that the connected server reports Unix-socket transport rather than TCP, verifies the real server data-directory and socket paths, and checks the independent nonce via read-only SQL. This check repeats for every new connection, including the **DROP SCHEMA ... CASCADE cleanup connection**; the earlier check on the CREATE connection is not reused as authority for DROP. A wrong nonce or alternate hostaddr must be rejected before any write. It then creates and drops uniquely named scratch schemas. The test assumes a trusted local user/host controls the transient Unix-socket server and out-of-band nonce; this is a fail-closed test guard, not a cryptographic remote attestation service. No nonce, socket, DSN, or credentials are committed to the repository; normal CI has no such probe DSN and skips the test.

The revised stand-in experiment contains **12 opt-in PostgreSQL 17.11 cases**, including negative identity guards and actual lock-wait observation. In the isolated test run it completed **10 additional full repetitions × 12 = 120 PASS**. A normal full Python run without the probe DSN skips these opt-in tests rather than connecting to any database. Local probe results are not CI PostgreSQL evidence.

**Passing this stand-in SQL experiment is feasibility evidence only**. It does not exercise the real claim_next_job query, real Control migration, Worker lease fencing, abrupt process crash, real model process interruption, unknown COMMIT recovery, or any production deployment. Those remain explicit future acceptance gates.
