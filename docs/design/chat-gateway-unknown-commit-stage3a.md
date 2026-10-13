# Chat admission unknown COMMIT — bounded best-effort Stage 3A (#137)

Status: **Accepted bounded best-effort design, limited to reachable PostgreSQL
and a live Gateway.** Stacked on Draft #140 (`dfdf4a6`). Not a durable Owner.

Before scheduling a chat submission thread, Gateway generates a random internal
idempotency key for that *one* request. Client input never supplies this key or
a Job ID. The existing unique `jobs.idempotency_key` index is the fence against
duplicate inserts for that identity. No client-visible cross-request retry key
is introduced; a new HTTP request is a new identity, so this stage cannot
promise deduplication across client retries.

On ambiguous storage failure, wait for the shielded submit thread to finish,
then read the primary by exact internal key (not by a guessed Job ID). A
successful empty read *after* the submit thread ends only establishes that no
row was visible at read time; because PostgreSQL may COMMIT *after* a lost client
acknowledgement, absence is also UNKNOWN, not a safe non-admission verdict.
An observed row is cancelled by its exact server Job ID. A CANCELLED record is the only positive cancel
acknowledgement: SUCCEEDED/FAILED or a failed/ambiguous cancel is unresolved,
not success. The connected caller gets a generic 503 on lost acknowledgement
even if cleanup succeeds, not a response from an unowned Job. Cancelled HTTP
tasks transfer their admission permit to the cleanup task until reconciliation
finishes. The post-body ASGI receive watcher detects an HTTP disconnect while
the submit thread is in progress even when the ASGI server does not cancel the
request task. It stops and releases receive ownership before streaming begins;
it never reads an unparsed request body. A disconnect transfers the same permit
to late cleanup. Only the existing `max_pending_admissions` slots may own a
submit/cleanup at once. Each fresh read/cancel is bound by PostgreSQL 17
admission deadlines; PG16 fails closed for these bounded operations.

A synthetic negative control demonstrates the remaining safety boundary:
the client submit thread can return an ambiguous error, primary readback can
find no Job, and a delayed server-side COMMIT can later create a claimable
QUEUED Job. We conservatively record `UNKNOWN` for the read miss, but this
in-memory counter does **not** stop a Worker or persist across process death.
This residual risk is explicitly accepted for best-effort #137; a persistent
Owner/Claim fence is **not** part of this task.

Readback and cancellation each use at most three bounded attempts; an empty
primary read is retried but the final absence remains UNKNOWN. PostgreSQL
uses a fresh per-operation deadline (independent of the expired chat deadline)
and a primary read. An exhausted operation records explicit UNKNOWN disposition
on the live service before releasing its permit. No infinite background retry
or silent claim of cancelled state. Non-chat Control submissions and existing
public Control routes remain unchanged. These are best-effort bounds: libpq
address rounding and the initial server-settings probe/network blackholes are
not hard real-time bounded by this stage.

Process crash or forced shutdown during a thread can still leave a claimable
orphan QUEUED Job until its Job deadline. Unavailable PostgreSQL, a server
COMMIT after the last read miss, uncertain cancellation after the last attempt,
and a Worker completing before cancel acknowledgement likewise remain
best-effort limits. No durable Owner table, Worker claim rewrite, or migration
is introduced. The UNKNOWN counter and safe logs describe uncertainty on the
surviving Gateway, not a cross-process recovery mechanism; logs never include
the key, credentials, or request payload.
