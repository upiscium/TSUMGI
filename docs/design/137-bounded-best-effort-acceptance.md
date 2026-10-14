# #137 bounded best-effort chat admission: release acceptance boundary

Status: **maintainer scope decision, not release or production approval**. Based
on `main` at `0869f93` (2026-10-13); the #137 follow-up slices described below
are staged separately and require integration and requalification on an exact
candidate SHA. This is REQUIREMENT 1, **not** a durable admission-owner or
Worker claim-fence design.

## Operational contract and accepted risk

- Keep the merged [#134 running-job SSE disconnect cancellation](chat-gateway-stage-b.md)
  behavior: disconnect cancels the owned durable Job and fences subsequent
  Worker events. That accepted fix does not settle cancellation *during*
  admission.
- Integrate and qualify #137's cancellation-after-admission handling, #138's
  finite per-Gateway pending-admission budget, and #140's opt-in PostgreSQL
  bounded chat admission deadline. Waiting requests must not consume unlimited
  Gateway tasks; elapsed admission deadlines must reject/roll back before
  confirmed admission. The Stage 3A internal idempotency/readback path can
  reconcile a known Job identity while the Gateway remains alive; **it does
  not guarantee idempotency or reconciliation across separate HTTP retries**.
  A 503/timeout is not proof that no Job was committed. The Gateway now bounds
  concurrent pre-admission HTTP body reads/JSON parsing (per-instance ingress
  slots) and stops buffering once the largest configured chat request byte
  limit is exceeded; upload time is bounded by the largest configured profile
  timeout. After admission, cancellation has an independent per-instance
  concurrency budget and a finite caller wait, while the synchronous DB
  thread retains its permit until actual completion. If the Control database
  stays unreachable, cancellation can still remain unresolved; subsequent
  requests may fall back to the finite Job deadline rather than immediate
  interruption. Do not advertise exactly-once execution, zero orphan Jobs,
  universal network-deadline guarantees, or a hard end-to-end cancellation bound.
- #140's PostgreSQL 17+ `transaction_timeout` requirement applies to the
  **currently staged opt-in bounded chat path**, not a new universal product
  requirement for every Control submission. The existing
  [installation](../installation.md) and
  [operator gates](../operator-rollout-gates.md) already specify PG17.x for
  supported deployment. No implicit database upgrade is authorized.
- There is **no Owner DB migration, durable pre-admission cancellation intent,
  claim fence, or crash-surviving Gateway ownership record** in this choice.
  On Gateway crash or an unacknowledged commit (UNKNOWN_COMMIT), a committed
  Job can remain QUEUED or briefly execute without its client. The operator
  accepts this residual until the existing Job deadline / Worker lease recovery
  path resolves it; neither mechanism is an immediate kill guarantee. Scope
  this risk to trusted clients and bounded request deadlines, monitor queued /
  running Jobs after Gateway failures, and investigate/cancel by durable Job ID
  where safely identifiable rather than blindly retrying a non-idempotent HTTP
  request. Do not present durable owner/claim-fence proposals as prerequisites
  to this explicitly chosen bounded best-effort scope.

## Evidence required before release nomination

1. Freeze the integrated SHA and configuration; review the final #137/#138/#140
   and Stage 3A diffs for deadline, budget and cancellation interactions. Run
   exact-SHA GitHub CI, installed Nix Control/Installer/Worker checks and the
   relevant PostgreSQL 17 UTF8 suite on a disposable database. A skipped
   database test is **not** a PostgreSQL PASS.
2. Record real ASGI disconnect/cancel races (including late admission) and
   real PostgreSQL lock/transaction timeout, before-COMMIT pause and
   UNKNOWN_COMMIT/readback tests, not only mocked/unit paths. Verify normal
   direct Control submissions are unaffected, existing stricter operator
   timeouts are not weakened, and no new owner schema migration is applied.
   Identify remaining network-blackhole, process-kill and cross-request retry
   limitations in the release notes. A pre-COMMIT deadline check does not prove
   that COMMIT was acknowledged before the deadline.
3. Obtain correctness/security review of the **bounded best-effort** contract,
   state the orphan/503 policy and supported client claims explicitly, and
   bind all observations to the immutable final candidate. Source CI, prior
   disposable GPU and PG17 proofs in
   [release readiness](../release-readiness.md) cannot be transferred to an
   altered candidate. #80's administrative closure is **not** a manual
   documentation-only Real Smoke technical PASS; perform and record any new
   candidate-specific smoke required for the selected support scope.

## Remaining owner decisions: #124 and #79

- **#124 site activation (separate GO/NO-GO):** the authorized operator must
  name the site/change owner, window, affected clients and rollback authority;
  inventory immutable candidate and existing executable/unit/config/credential
  identities, PG17 UTF8 schema/roles and active Jobs/leases, dedicated UIDs,
  ingress/auth and selected GPU isolation. Prove private credentials and
  cross-UID denial, independent encrypted backup/restore with a usable
  Control read, acceptable RPO/RTO, and old-installed → candidate → old-installed
  rehearsal including native requests, failure/cancellation and recovery.
  Specify a compatible schema restore target and reconciliation plan if a
  forward migration prevents binary rollback. Record a site-specific rollback
  trigger and explicit production GO; the disposable #124 proof does not grant
  permission to change production. See [operator rollout gates](../operator-rollout-gates.md).
- **#79 publication (independent decision):** maintainer explicitly selects
  older frozen Option A or requalified integrated Option B, immutable source /
  lock / docs SHA, version and support tier; approves release notes (including
  best-effort/orphan risk and experimental feature claims), final-candidate
  CI/PG17/installed package and security evidence, provenance/checksums and
  publication authority. No tag, publication or merge follows from this note;
  operator site GO cannot substitute for release approval, or vice versa.
