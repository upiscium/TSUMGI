# TSUMGI #240 — Phase B1a persistent native-run reservations

**Status:** source-only / offline / Draft. This change builds on the
unmerged [Phase B0 contract PR #242](https://github.com/upiscium/TSUMGI/pull/242)
and [Phase A design PR #241](https://github.com/upiscium/TSUMGI/pull/241).
It is *not* merged, does not start a service and must never be deployed
as a complete native-run supervisor. Parent #237 and 12K dependency #230
remain blocked.

## Narrow purpose

A native run must reserve all of its GPU UUIDs **durably before** it asks
PID1 to create any transient unit. If the future Broker crashes, or its
`StartTransientUnit` response is lost, those GPU reservations must not
vanish, be automatically reused or be treated as a successful stop.

`src/astrumweaver/runtime/native_cgroup_ledger.py` introduces an
**isolated, standard-library SQLite** ledger with these properties:

- An **already existing**, broker-owned private 0700 local state directory
  and a non-symlink regular 0600 SQLite file are required. No automatic
  directory creation, permission weakening or Nix/systemd deployment.
  A local, SQLite WAL-compatible filesystem and private ownership are
  operator prerequisites. The SQLite database is local to a future broker;
  it is **not** the production TSUMGI Control PostgreSQL database.
- `NativeRunReservationLedger.reserve()` checks the B0
  `NativeLaunchRequest` against a caller-supplied **exact allowlist**,
  stores the canonical request/Worker epoch/unit identity, and reserves
  **all** listed GPUs in one `BEGIN IMMEDIATE` transaction.
  The unique GPU index excludes overlapping runs across independent
  ledger handles. A request for two GPUs cannot partially claim one if
  the other has already been reserved. The serialized request is bounded
  to 8 KiB and no run may reserve more than 16 GPU identifiers.
- Same run ID + exact request + same claimed caller UID is an idempotent
  **observation** (`newly_reserved=False`), *not* permission to reissue
  `StartTransientUnit`. A different model, executable, revision, epoch,
  GPU list, or caller UID for the same run ID fails closed.
- **Before** any future PID1 launch invocation, the broker must call
  `mark_start_intent()`. It commits `UNKNOWN_FENCED` on the exact run
  **before** handing control to an external manager. Retried/mismatched
  start intents are rejected. A crash immediately after commit or a lost
  manager reply leaves the run/GPU reservation fenced.
- `fence_on_broker_restart()` conservatively marks still-RESERVED
  entries `UNKNOWN_FENCED`; it never releases GPUs. The future broker
  must invoke this before accepting new work during startup.
- A full consistency scan on open and every operation verifies that
  **all** serialized run declarations match their unit/epoch identity
  and exactly match the GPU ownership index. Missing/rewritten owner
  rows fail closed; the ledger never silently rebuilds a missing index.
  Schema versions not explicitly supported are refused.
- There is deliberately **no API to release, mark a cgroup empty,
  send a signal, reconcile against PID1, or promote a run to READY**.
  Those operations are downstream security-critical Phase B1b work,
  contingent on a trusted manager adapter, an immutable run/unit
  identity, a reconciler and a specific kernel-backed empty proof.

The SQLite schema and transactions use `foreign_keys=ON`, WAL mode and
`synchronous=FULL`. The code does not claim immunity to disk failure,
a compromised same-UID user, a network filesystem, or malicious changes
to files outside its protected state directory. A future privileged
broker must own its private filesystem and monitor actual error codes.

## Tests and what they cannot attest

`tests/test_native_cgroup_ledger.py` uses only disposable SQLite files
and synthetic B0 request declarations. Tests cover:

- unique GPU ownership with eight competing reservation attempts;
- all-or-nothing two-GPU reservation and collision across reopened
  database handles;
- exact-idempotent replay and refusal of mismatched run/epoch/model/UID;
- permission and symlink denial for state files, unfamiliar schema;
- corrupt/missing GPU ownership index or changed unit/epoch fails closed;
- overlong request and excessive GPU identifiers fail before persistence;
- unknown-fenced dispatch state preserved across broker re-instantiation;
- an **actual disposable Python child process** committing the launch
  intent and then exiting immediately with `os._exit(17)`, followed by
  a fresh process's inability to reserve the same GPU.

No test accesses a live systemd manager, writes to `/sys/fs/cgroup`,
runs llama.cpp, opens a GPU device or changes any Worker/Control
configuration. A SQLite `UNKNOWN_FENCED` row is a durable *barrier*;
it is **not** proof that any systemd unit actually started.

## Authority and outstanding Phase B1b requirements

All calls in B1a are made by tests; the `trusted_peer_uid` argument is
**a claim until a real Broker authenticator exists**. It cannot be read
from an untrusted JSON request and assumed correct. The exact allowlist
is similarly just an in-memory value until a privileged, independently
reviewed broker loads it from administrator-controlled configuration.

A real broker will need a *separate* IPC layer that authenticates
`SO_PEERCRED`/Worker identity and an approved GPU+revision policy,
an admin-owned durable ledger path, a strictly limited PID1 API,
safe pre-exec systemd service placement, an atomic stop/kill protocol,
an exact unit InvocationID and immutable cgroup identity, and
identity-checked recursive `cgroup.events: populated=0` confirmation.
A manager acknowledgement loss must trigger durable reconciliation
rather than a second launch. A failed/unavailable service or an
unreaped descendant must not clear the GPU reservation.

A real-time request must never be allowed to control arbitrary
`ExecStart`, environment variables, systemd properties, unit names,
privilege flags, host cgroups, or GPU resets. The broker must
independently verify executable/model integrity; exact string checks
are only a prerequisite. Phase B1b requires an independent threat
review and disposable systemd/cgroup test before any activation.

**Explicitly not authorized by B1a:** Unit installation or start,
root escalation, `Delegate=yes`, `ProtectControlGroups` relaxation,
Sirius Worker stop/start, GPU/kernel reset, Control/DB write,
12K/16K serving promotion, release, Ready/Merge of other PRs,
or deployment of an incompletely supervised native process.
