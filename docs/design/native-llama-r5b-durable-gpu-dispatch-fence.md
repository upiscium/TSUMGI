# TSUMGI #240 — R5b durable per-GPU launch exclusion

**Status:** source-only Draft, no activation, no real Broker or PID1
authority. Stacked on the still-unmerged R5a Draft PR #245,
B1b0 PR #244, B1a PR #243, B0 PR #242 and Phase A PR #241.
No parent PR has been Ready/Merged/Deployed. Real GPU release
and 12K qualification remain blocked.

## Problem: R5a had an audit claim, but no enforced gate

R5a recorded a `launch-disabled` claim in the offline audit
journal, but B1a's reservation API did not consult that journal.
The existing UNIQUE GPU-ownership index prevented another run
claiming an already-owned GPU **while that owner row survived**,
but did not independently enforce durable launch exclusion if
a future release path or separate coordinator overlooked R5a.

R5b closes that specific **shared-SQLite, per-GPU** hole.
It is NOT a globally authenticated launch ban and cannot prevent
PID1/foreign callers that ignore the ledger.

## Source-only enforcement

- B1a ledger schema is now **version 2**, adding
  `native_gpu_dispatch_fences(gpu_uuid PRIMARY KEY, run_id REFERENCES
  native_runs)`. Version-1 databases with outstanding runs/GPU
  owners are REFUSED, never silently upgraded. An empty v1
  database may migrate, while an existing version-2 database
  missing the gate table is REFUSED, not silently recreated.
  No production database schema was changed.
- R5a `record_launch_disabled` inserts a sticky GPU fence for
  EVERY GPU in its exact original run, under the **same
  BEGIN IMMEDIATE SQLite transaction** as the first stage audit
  claim. A failure on the second GPU insert or the audit event
  rolls back all partial fences and stage claims.
- B1a `reserve` checks the gate **inside its own BEGIN IMMEDIATE
  transaction before any new GPU owner insert**. That denies
  a new run from another Worker, epoch, ledger instance or
  coordinator on the same SQLite DB when its GPU is fenced.
  Unfenced GPUs remain allocatable.
- Exact run-ID repeats remain only an idempotent diagnostic
  observation (`newly_reserved=False`); B1b's fake Manager
  coordinator refuses an additional native start.
- `list_launch_fences` is read-only. No API clears these
  fences, releases GPUs, starts PID1 services or marks READY.
  After crash or restart, the gate remains closed.
- Fence rows must exactly match B1a GPU ownership; if any
  GPU of one run is fenced, **all** its GPUs must be fenced.
  Every existing R5a seal audit row must also have a matching
  complete gate. Missing, partial, detached or conflicting
  rows fail closed, with no auto-repair.

## Disposable tests

All tests operate on **local synthetic SQLite files only**,
with no actual Manager bus, GPU, PID1 or cgroup system call.

- Same-transaction multi-GPU fence and stage-0 audit commit;
  injected failures on the second GPU fence insert or on
  audit event insert both roll back completely.
- New runs on fenced GPUs are denied across independent
  SQLite handles, while a third unfenced GPU can be reserved.
- Eight concurrent attempts to close a run's gate: only
  one succeeds. Attempts to close it again are rejected.
- A disposable Python subprocess commits the fence and then
  exits through `os._exit(17)`; a reopened ledger still
  refuses to assign that GPU to a different run.
- The B1b FakeManager port sees zero manager calls when a
  request is denied by the persisted GPU fence.
- Deliberately corrupt/missing/partial gate rows, a missing
  gate table, changed owners, live version-1 database and
  falsified stage flags all fail closed.
- Existing R5a tests still ensure that a completed four-stage
  synthetic audit NEVER releases GPU ownership.

## Critical limitations

**Only calls using this same B1a SQLite database are fenced.**
The gate does NOT authenticate the R5a
`claimed_global_dispatch_disabled` boolean or protect against:

- direct PID1 `StartTransientUnit` calls, rogue wrappers,
  independent brokers using another database/host, or
  already-queued asynchronous Manager jobs;
- a privileged Supervisor re-creating a subgroup after
  a temporary `populated=0` (the R4 experiment proved this);
- tampering by a privileged writer who can rewrite the
  SQLite database, a recreated database or recomputed
  audit hashes;
- a malicious client invoking a future insufficiently
  authenticated Broker IPC;
- actual CUDA context teardown, GPU FD leaks or cgroup
  escape, plus systemd-255 patch compatibility on Sirius.

The old R5a parameter name `claimed_global_dispatch_disabled`
is a *synthetic claimed state*, not a Broker-wide lock. R5b
provides only per-GPU **reservation** exclusion. A real
production Broker must atomically fence **all** launch paths
and its privileged Supervisor before considering cleanup.

The R5a journal remains **diagnostic-only**: claimed events and
hashes are not genuine kernel/PID1 attestations. A complete
parent-stopped audit can still be fabricated by a privileged
writer. No release API or real native lifecycle activation
exists in this patch.

## Subsequent acceptance (R5c)

1. Independent security review of B1a v2 schema, atomic fence,
   immutable admin policy, multi-Broker identity and IPC auth.
2. Minimal privileged Supervisor with audited
   `clone3(CLONE_INTO_CGROUP)`, unprivileged Native execution,
   exact executable/Model SHA, cgroup-specific authority and
   no process or writable FD escape.
3. Genuine kernel-backed empty observation, irreversible
   Native subgroup seal/removal and terminal PID1 Unit
   InvocationID proof, reconciled durably across Broker/
   Supervisor crashes before a separately authorized release.
4. Separately approved reversible Sirius 8K Worker stop/
   restore, real Qwen/CUDA stop acceptance, immutable 12K
   evidence and coordinated Control/Gateway serving limits.

#240/#237/#230 OPEN, all PRs Draft and unmerged. No Ready,
Merge, Deploy, Nix switch, Worker restart, GPU/DB mutation,
systemd delegation change or Qwen 12K/16K activation.
