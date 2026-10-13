# #137 — Real Control scheduler and legacy-version activation preflight

Status: **Review-only preflight; no owner-aware scheduler, database migration, rollout or release has been implemented.**

This document extends the proposed [durable Owner/Claim preflight](137-persistent-owner-claim-preflight.md), stacked on [ADR #139](https://github.com/upiscium/TSUMGI/pull/139). Stages #138 (admission concurrency), #140 (bounded PostgreSQL submission) and a separate in-progress UNKNOWN_COMMIT worktree are independent. This branch does not modify their code.

## Observation: the real, current scheduler

Unlike the simplified Owner/Job/Worker stand-in in #141, the new [compatibility test](../../tests/test_gateway_scheduler_compatibility_preflight.py) calls the **unmodified** `PostgresControlRepository.register_worker / submit_job / claim_next_job / cancel_job` methods against actual TSUMGI migrations on a *disposable PostgreSQL 17.11 database*. The test is opt-in and checks the identity of **every PostgreSQL connection** before running migration or repository SQL. It does not exercise actual GPU execution.

| Controlled scenario | Verified current behavior | What it establishes |
| --- | --- | --- |
| Highest-priority Job cannot meet worker resource labels | Real Control selects lower-priority compatible Job | Existing resource filtering and order do not blindly block on this head |
| Two equal-priority compatible Jobs | Earlier sequence claimed first; after cancellation the second is claimable | Baseline FIFO tie-break and release path |
| High-priority compatible Job row locked by independent transaction | Real `FOR UPDATE SKIP LOCKED` selects the lower-priority ready Job; after lock release, high-priority Job remains eligible | Baseline head-of-line lock skipping |
| Gateway-origin Job linked to CANCEL_REQUESTED owner, using **legacy Control source** | The legacy Controller **still transitions the Job to RUNNING** | **Expected unsafe negative control**; existing Claim SQL never consults Owner |
| Old Gateway omits the origin marker, using the proposed `admission_kind='direct'` default | Legacy Job submission and Claim succeed without Owner link | Unique FK / CHECK does **not** prevent an old Gateway from invoking a direct submission path |

Passing the two final negative controls **means the unsafe behavior was reproduced**. It is not approval to activate ownership. The tests temporarily add illustrative Owner columns to the disposable database and remove them before teardown. They do not modify the repository's migrations or any running Control.

### PostgreSQL role/RLS feasibility — strictly provisional

A separate test creates a random, non-login, **non-owner / non-BYPASSRLS** role, grants it limited disposable DB rights, and enables a per-row policy that permits only `admission_kind='direct'` Jobs for that role. Running the **unmodified** Control repository with `SET ROLE` verifies that its legacy Claim still processes direct Jobs but no longer sees an owner-linked Job, including one with CANCEL_REQUESTED.

In the **same test**, the privileged/table-owner connection can still claim that owner-linked Job. This is an essential negative control: RLS is only meaningful if old Control credentials are actually restricted and no owner/superuser/BYPASSRLS connection can execute a legacy Claim. A session-level `SET ROLE` on a test superuser proves PostgreSQL policy feasibility, **not** a deployable credential/role migration or resistance to a privileged server operator.

Even if RLS hides correctly marked owner-linked Jobs, an **old Gateway can still insert them as default direct Jobs**. Do not use RLS as a substitute for disabling/fencing legacy Gateway submission routes or proving the new origin contract on every Control process. A database owner role, shared superuser credential, a restored old Control container, or a direct SQL writer can bypass the simple role model.

## Claim fairness after adding Owner fencing

Current `claim_next_job` uses a single SQL candidate query with priority DESC, sequence ASC, compatibility filters, and `FOR UPDATE SKIP LOCKED`, then locks the Worker and updates both rows. The proposed Owner-first sequence cannot preserve this exact Job-first lock acquisition: a CANCEL transaction taking Owner -> Job and CLAIM taking Job -> Owner can deadlock.

The design candidate (NOT accepted SQL):

1. Read an ordered bounded candidate window **without locking Jobs**. An advisory Owner state/lease filter may reduce needless reads, but cannot replace locked revalidation.
2. For an owner-linked candidate, lock Owner first, then lock/recheck Job, then Worker. Legacy ownerless Jobs skip the Owner step.
3. After the last potentially blocking lock, re-check fresh DB time against Owner lease, request and Job deadlines. Re-check Owner state, Job status/priority/resource/serving eligibility, Worker state/capacity and attempt/epoch fencing.
4. If a candidate became invalid, **continue searching eligible candidates rather than returning immediately**. Avoid retaining unnecessary row locks for skipped candidates. Use a bounded transaction/cursor strategy with a documented retry budget and explicit continuation position.
5. Commit one legitimate RUNNING transition and the Worker capacity increment atomically. A legal claim-first win can later be cancelled, but a cancellation/expiry must never allow a new claim to pass its final locked eligibility check.
6. Measure liveness when many top-ranked Owners are cancelled, expired, or continuously become ineligible while waiting for the Worker. Static tuple ordering alone is not a starvation guarantee.

**Unresolved trade-off:** simple fixed candidate LIMIT without any cross-attempt resume can starve a lower-priority eligible Job when the high-priority prefix stays permanently ineligible. A cursor or fair rescan scheme is needed; the particular window size, retry frequency, and scheduling guarantee must be justified by load testing rather than chosen from intuition. Releasing skipped row locks may require per-candidate transactions or verified savepoint semantics. No algorithm in this document establishes COMMIT-before-owner-expiry.

### Required real-scheduler tests before code acceptance

- Current filter parity: capabilities, labels, GPU topology, serving revision, priority/FIFO and max concurrency; unrelated ownerless Jobs continue to work.
- CANCEL_FIRST: after Owner lock and durable CANCEL_REQUESTED, no claim can become RUNNING, even if candidate enumeration used an earlier snapshot.
- CLAIM_FIRST: RUNNING wins legally, later CANCEL fences lease/epoch and frees Worker capacity; the Worker eventually stops native inference within a measured bound.
- Lease passes expiry during **observed** Worker/Job row lock wait; final PostgreSQL-clock check rejects it. Inspect exact blocker PID/lock target, not merely a sleeping test thread.
- A high-ranked Owner becomes ineligible **after** locks are acquired; lower-priority ready work must progress after bounded additional claim cycles.
- A long prefix of continually ineligible Owners must not induce unbounded candidate scans, deadlocks or permanent starvation. Verify under concurrent claim/cancel/heartbeat/recovery and mixed legacy jobs.
- Real isolated PostgreSQL 17 migration and rollback, including a simulated still-running legacy Control, with negative controls proving that unsafe activation is rejected.

The standalone #141 prototype proves none of this against actual Owner-aware `PostgresControlRepository`; this PR only establishes the genuine legacy baseline and a fail-open counterexample.

## Rollout choices and fail-closed requirements

### Preferred initial deployment: no mixed Control writers

The simpler initial plan is to **disable owner admission and block activation** until every Control process or direct SQL writer that may reach the shared database is upgraded or fenced. This requires an operator-controlled inventory, stopped/disabled legacy services, credentials revoked or segregated, and a rollback plan. Merely observing zero old sessions in `pg_stat_activity` is insufficient: old processes could reconnect later. A single upgraded process advertising a version cannot enforce other processes' behavior.

Activate only after an independent disposable-DB run proves all remaining connection paths have the reviewed Owner-aware Claim and Job-mutator paths, or a database-enforced fence excludes every older writer. The existing shared Control bearer is one trusted domain, not client-principal ownership.

### Optional mixed-version transition: explicit database roles

PostgreSQL RLS/privilege separation **might** let an old Control safely continue serving ownerless/direct Jobs while denying it reads/updates of owner-linked Jobs. This adds its own release/security surface:

- Distinct non-owner, non-superuser/non-BYPASSRLS legacy and new Control roles; table grants and `FORCE ROW LEVEL SECURITY` policy must be reviewed.
- PostgreSQL role credential provisioning/revocation; no new/old service uses the table-owner credential.
- The old Gateway's ability to create default direct Jobs must be removed or refused when durable admission is enabled. An RLS read filter alone cannot enforce source identity.
- RLS policies affect ALL actual SELECT/UPDATE/INSERT paths, including heartbeat and recovery. New Claim order still needs Owner-row locking; a role filter by itself cannot provide it.
- Negative tests for old Control reconnect, session credential reuse, superuser bypass, process rollback, and queue/lease recovery.
- Separate schema and operational review before enabling such a transition.

**Recommendation for the first rollout:** use the no-mixed-Control approach. Role/RLS transition should be considered only if preserving old Control operation is a hard requirement, because it materially expands migration, credentials, SQL policy and rollback complexity.

### Activation and rollback acceptance gates

Before allowing a single owner-linked Job to be QUEUED, verify all admission/claim/heartbeat/terminal/maintenance paths, disable legacy Gateway entrypoints, verify database identity and every Control version, make an offline backup, and run a full abortable rollback drill. Refuse activation when any old database writer still has unrestricted access or any owner-linked job can be admitted as direct. Do not infer safe deployment solely from passing an in-memory test.

On rollback, first disable new Owner admissions. Preserve durable Owner state and claim fences for existing linked QUEUED/RUNNING Jobs until cancellation and native Worker slot recovery are verified. **Do not downgrade or drop Owner fields while linked Jobs remain active.** If verification fails, leave the feature disabled and preserve evidence for operator reconciliation.

## Test environment and evidence limit

The new test module is optional (no DSN -> SKIP). **Before invoking libpq**, it rejects any explicit alternate DSN/connection options and known `PGHOSTADDR`, `PGSERVICE`, credential or other libpq environment overrides. This matters because checking server identity only *after* a TCP connection can be too late to prevent credential exchange. A negative test replaces the socket constructor with a forbidden-network stub and confirms that `PGHOSTADDR` is rejected without invoking it.

On **each actual connection** opened by the real migration and repository code, it then verifies PostgreSQL 17+, the private Unix transport, exact disposable data directory/database/socket path and independently seeded random 256-bit sentinel nonce **before any application SQL or DDL**. This is a trusted-local disposable test harness, not remote server authentication against a malicious same-UID operator.

All role creation, RLS policy, schema/column mutations, Worker/Job records and cleanup are on an isolated disposable PostgreSQL database. Setup schema/role DDL runs transactionally; negative fault-injection tests deliberately fail after successful CREATE/ALTER and verify that neither a temporary table/column nor a random cluster role survives. The provisional RLS legacy role receives only `SELECT` and `UPDATE` on the two tested tables, **not INSERT or DELETE**, checked under its effective role; even that is not a production least-privilege policy. The test-local `SET ROLE` and RLS are never published as an actual TSUMGI migration or applied to an existing site.

**Only an explicit human architecture choice can authorize a product Owner-aware migration or Claim rewrite.** Keep #137 OPEN and Draft #138/#140/#141 unmerged; ADR #139 remains Proposed. Do not modify production PostgreSQL, Control/Worker, GPU placement, credentials, releases or tags on the basis of this preflight.
