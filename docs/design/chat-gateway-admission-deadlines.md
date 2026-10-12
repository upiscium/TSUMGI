# PostgreSQL chat admission deadline — experimental Stage 2 (#137)

Status: **Draft implementation only; not a production or architecture acceptance.**

This incremental change builds on the Stage 1 per-Gateway admission budget in
Draft PR #138, and implements the blocking database admission portion of
[proposed ADR 0002](https://github.com/upiscium/TSUMGI/pull/139). No new
durable cancellation intent or Worker claim fencing is introduced.

## Execution boundary

ChatGatewayService computes an absolute UTC deadline when a request is compiled
from the profile's positive request timeout. It passes this value to the
repository's optional admission_deadline_at keyword, separately from the
existing Job deadline and the original now timestamp.

- Ordinary Control submissions without this keyword retain their previously
  reviewed transaction semantics. Other gateways are not implicitly changed.
- In-memory submission checks a fresh clock under its existing lock; stale
  request-start timestamps do not allow delayed admission.
- PostgreSQL **17 or newer** is required for this bounded opt-in path because
  transaction_timeout is needed for a transaction-wide server-side bound.
  Older servers fail closed for bounded chat admission. This is not permission
  to upgrade a production server.
- A fresh PostgreSQL clock_timestamp query must agree with the application
  UTC clock to within a conservative 2 s bracket tolerance. A larger skew
  refuses admission before mutating any Job.

Limits are clamped to the remaining absolute deadline:

| Operation | Proposed cap | Mechanism |
| --- | --- | --- |
| Connection | 5 s, rounded up to whole seconds | libpq connect_timeout |
| Lock wait | 2,000 ms | PostgreSQL lock_timeout |
| Statement | 5,000 ms | PostgreSQL statement_timeout |
| Transaction | 10,000 ms | PostgreSQL 17 transaction_timeout |

Existing operator libpq options, database/role timeout settings and session
timeouts are not relaxed: the implementation reads the effective server
pg_settings and selects the smaller positive timeout before running the
encoding probe and again inside the submission transaction. The initial
read-only settings query itself is not covered by a newly established
server-side timeout; a connected network blackhole may still stall it.
Transaction-local settings constrain SQL, locks and the transaction itself.
The code also checks
both a fresh application clock and a PostgreSQL clock_timestamp after all
admission/idempotency SQL and **inside the transaction immediately before
COMMIT**. An elapsed deadline rolls back instead of admitting a late Job.
The original now parameter, PostgreSQL now() and transaction_timestamp()
cannot be substituted for this fresh check.

## Boundaries not yet proved

- A check before COMMIT cannot prove COMMIT completed before the deadline.
  Failure to receive the COMMIT acknowledgement is UNKNOWN_COMMIT, **not
  evidence of no Job**. The Gateway returns a generic 503 for unavailable or
  unconfirmed admission, but no durable identity-based reconciliation exists.
- A connect_timeout is per address and may round up to whole seconds.
  Network blackholes after connection, OS-level TCP behavior, a hard process
  kill or a server crash are not universal hard real-time guarantees.
- The earlier cleanup helper still suppresses failed cancellation
  acknowledgements. A finished Task does not prove durable CANCELLED.
  Bounded Gateway shutdown, startup reconciliation and Worker-claim fencing
  remain unresolved.
- Existing client bearer access remains one shared-trust Control domain, not
  per-principal multi-tenant Job authorization.

## Validation gate

Disposable PostgreSQL 17 UTF8 tests cover successful bounded submission,
already-expired rejection, actual SQL lock contention past deadline, a pause
between INSERT and COMMIT, server-side statement timeout, Gateway cancellation
before late admission, Gateway cancellation followed by database expiry, and
ordinary unbounded direct Job submissions. Real PostgreSQL tests skip when
no dedicated test DSN is provided; standard CI alone does not prove this gate.

Before production acceptance: exact revision tests, GitHub CI, independent
Correctness/Security review and site-specific validation. These checks do not
authorize production PostgreSQL migrations, Control/Worker restarts, GPU
changes or release tagging. Issue #137 stays open until the UNKNOWN_COMMIT,
cancellation outcome and shutdown / Worker claim decisions are resolved.
