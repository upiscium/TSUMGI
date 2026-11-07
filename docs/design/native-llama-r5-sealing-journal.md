# TSUMGI #240 — R5 least-privilege Supervisor and durable seal sequence

Status: **Source-only, offline audit contract, Draft PR.** This R5 work
is stacked on unmerged B1b0 Draft PR #244, which depends on B1a PR #243
and B0 PR #242. Initial design PR #241 also remains Draft. This does not
authorize Ready/Merge, unit changes, a Worker stop, GPU reset, release,
12K/16K promotion or any Sirius operation.

## R4 termination evidence and its insufficiency

GPU-free experiments on **arca-sandbox** (Ubuntu 24.04.5, systemd
255.4-1ubuntu8.18) showed that a trusted delegated per-run Supervisor
can stay in a `monitor` subgroup while Linux
`clone3(CLONE_INTO_CGROUP)` creates Native directly into its sibling
`native` subgroup. A child calling `setsid()` and ignoring SIGTERM
survived its wrapper but was still recursively killed by
`native/cgroup.kill`. The same native directory and events-file inodes
could be read as `populated=0` while `monitor` remained alive.

R4 also **repopulated the same cgroup inode** after a true
`populated=0` sample by launching a second child from the still-live
Supervisor. A point-in-time empty state is not a durable release
receipt. The native launch path must be permanently fenced before
emptiness is observed, then the empty subgroup must be
sealed/removed with no recreation, and the exact generation and
terminal state must be durably recorded.

The older flat transient-service design had an additional problem:
PID1 removed the cgroup on StopUnit, leaving an open events FD
unreadable before the monitor could attest `populated=0`.

## R5 GPU-free least-privilege experiments on systemd 255

All tests below created **temporary, uniquely named** isolated services
on arca-sandbox. No systemd configuration, persistent Unit, Plugin,
Tunnel, existing Worker, GPU, Control database or Sirius service was
changed.

1. `Delegate=yes` plus `DelegateSubgroup=monitor` but ALSO
   `ProtectControlGroups=yes` made cgroup creation fail with
   **EROFS**: cgroup filesystem was read-only within the service.
   It is **not** valid to blindly copy the ordinary Worker's security
   properties into a delegated Supervisor.
2. A narrowly scoped exception
   `ReadWritePaths=/sys/fs/cgroup/system.slice/<exact-run-unit>`
   alongside `ProtectControlGroups=yes` permitted the Supervisor
   to create `native` while keeping the rest of cgroupfs read-only.
   Opening `system.slice/cgroup.procs` or an unrelated
   `dbus.service/cgroup.procs` for write failed with EROFS.
3. The trusted root-UID Supervisor ran with ONLY
   `CAP_SETUID CAP_SETGID` effective/bounding capabilities
   (hex `00000000000000c0`), `NoNewPrivileges=yes`,
   `ProtectSystem=strict`, `ProtectHome=yes` and
   `DevicePolicy=closed`. It could use clone3 to create its Native
   child in the dedicated cgroup and drop that child to UID 65534
   before exec. An unprivileged child could not open its own,
   monitor or parent `cgroup.procs` for write.
4. Without `CAP_KILL`, the Supervisor's direct `kill(pid,SIGTERM)`
   to the cross-UID child failed with EPERM. Rather than broadening
   the capability set, writing `1` only to that run's
   `native/cgroup.kill` succeeded, reaped the UID-65534 child and
   yielded `populated=0` from the same native/events-file inode
   generation, with parent monitor still populated. A separately
   audited **Native-side graceful IPC** path is needed for TERM;
   a broad CAP_KILL grant is not needed solely for forced fallback.
5. An empty native subgroup was `rmdir`-removed while its
   pre-existing directory FD was still held by the monitor.
   A subsequent `clone3(CLONE_INTO_CGROUP)` using that stale FD
   failed with **ENOENT**. This helps demonstrate that the
   particular old cgroup generation cannot be repopulated once
   removed, but a privileged Supervisor could create a *new*
   subgroup of the same name without a durable launch prohibition.

One negative trial sending cross-UID SIGTERM without CAP_KILL left
a **test-only** transient Unit temporarily deactivating. It was
recovered by a SIGKILL **scoped only to that exact temporary Unit**.
No unrelated Unit was signaled.

**Not yet qualified:** These experiments used a root-UID trusted
Supervisor with limited capabilities and mount namespace writes,
not a security-reviewed production executable with vetted IPC.
They did not prove Proxmox LXC permissions on Sirius,
systemd-255.4-1ubuntu8.17 compatibility, real GPU DeviceAllow,
CUDA Model/UDS access, or production crash-safe cleanup. No
Sirius Worker setting, including `Delegate=no` and
`ProtectControlGroups=yes`, was changed.

## Proposed privileged stop/seal order for R5b (NOT implemented)

Every step requires a distinct, immutable run UUID, Worker epoch,
deployment revision, original PID1 InvocationID, exact GPU set and
native cgroup-generation identity.

1. **Disable new launches durably.** The Broker refuses any future
   launch for this run *before* cleanup begins, including after
   Worker, Broker and Supervisor crashes. A B1a GPU reservation
   remains `UNKNOWN_FENCED`. A local boolean is insufficient.
2. **Attest identity.** Obtain original Unit.Id, InvocationID,
   cgroup path, native directory and events-file identities from
   trusted PID1/kernel interfaces. Reject stale, changed or
   unreadable identities. Do not interpret a newly created
   same-named cgroup as the original.
3. **Request graceful shutdown.** Use only reviewed Native IPC
   or a same-UID helper; without CAP_KILL the Supervisor cannot
   send arbitrary cross-UID process signals.
4. **Force only the owned native subgroup if needed.** The monitor
   writes to that run's exact `native/cgroup.kill` after bounded
   failure of graceful shutdown. Never use broad systemctl kill,
   pkill or global GPU resets. Reap the wrapper separately.
5. **Read recursive empty state while monitor lives.** Attest
   `native/cgroup.events populated=0` from its stable original
   file, matching native dir dev/inode, before the parent Unit
   is stopped. Missing file, timeout, changed identity or a
   surviving process means no release.
6. **Seal/remove.** Prevent further launcher calls and delete
   the original empty native subtree. A held old directory FD
   must no longer allow reentry; any unexpected recreation
   or identity mismatch fences the run again. A short-lived
   `populated=0` alone is not proof.
7. **Persist the terminal proof.** Commit the generation-bound
   observations to the SAME local SQLite transaction database
   as the B1a reservation. A crash after any stage must leave
   GPUs fenced and the latest durable audit intact.
8. **Stop/reconcile the parent.** Only after the native subtree
   is sealed should the Broker stop the parent per-run systemd
   Unit, attest the original InvocationID terminal state and
   persist the result. A missing Unit or lost Manager ACK alone
   does not prove correct cleanup.
9. **Release (NOT IMPLEMENTED).** A separately reviewed trusted
   authority must atomically verify all kernel/PID1 evidence,
   the durable launch fence and parent termination before
   releasing a B1a GPU reservation. Neither B0 nor R5a has
   an API to release or mark a Native Runtime READY.

## R5a implemented source-only audit journal

The new `native_seal_journal.py` is an **offline-only**, testable
durable recording layer that operates in **the same B1a SQLite
database**. Transactions use B1a's private 0700/0600 storage,
`BEGIN IMMEDIATE`, WAL and synchronous-FULL. There is NO second
uncoupled evidence database and NO separate GPU release path.

It enforces this strict append-only stage order:

    launch-disabled
      → native-empty-observed
      → native-removed
      → parent-stopped

Each row binds the original B0 request, run UUID, Deployment,
Worker epoch, PID1 InvocationID, native directory and events-file
object keys. An event hash chain identifies accidental edits,
reordered or missing records, and changed run generations. It
is **not a digital signature or trusted attestation**: a party with
write access to SQLite could fabricate observations and rehash the
chain. Actual authority must come from an independently reviewed
privileged adapter with authenticated PID1/kernel evidence.

The journal accepts only an existing B1a `UNKNOWN_FENCED` owner.
It rejects duplicate, skipped, conflicting or forged transitions,
wrong Unit generation, malformed events, missing B1a GPU owners
and a changed/corrupt audit chain. Tests cover concurrent claims,
independent broker-like reopen, and a child process exiting with
`os._exit(17)` immediately after a transaction.

**Critical distinction:** `record_launch_disabled` only records an
asserted boolean; `record_native_empty` verifies synthetic R4
sample structure; `record_subgroup_removed` checks asserted booleans;
and `record_parent_stopped` checks a claimed original InvocationID
and terminal Unit state. They do not perform real operations or
authenticate their source. They return **diagnostic audit stages,
not success receipts**. A four-stage complete claim never changes
the B1a GPU reservation from `UNKNOWN_FENCED`. There is no
system bus, cgroup I/O, GPU, Worker integration, or release method.

## Mandatory next gates

- Independent security review of the root-UID Supervisor and exact
  cgroup mount carveout, delegated controllers, LXC namespace,
  SO_PEERCRED, immutable Nix Model/Executable policy, Native FD
  hygiene and restricted GPU/UDS access.
- Purpose-built audited C/Rust `clone3(CLONE_INTO_CGROUP)` launcher
  with no unsafe fallback, process/cancellation/exec-error proof
  and untrusted Native unable to move outside its subtree.
- A trusted global launch fence, irreversible seal/removal, real
  kernel empty/parent PID1 receipt producer, and crash/lost-ACK
  reconciliation with a separate explicit future release API.
- Disposable GPU-free negative experiments for stale FD, PID
  reuse, SIGTERM escalation, nested fork, concurrent respawn,
  broker restart, permission denial and partial evidence.
- Independent source review plus separately authorized actual
  Sirius 8K stop/restore and immutable real Qwen 12K Serving
  qualification before #230's production transition.

No Ready/Merge/Deploy, production systemd delegation, GPU reset,
Worker restart, Control/DB change, or 12K/16K promotion is
authorized by this source-only R5a slice.

References:
- https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html
- https://systemd.io/CGROUP_DELEGATION/
- https://www.freedesktop.org/software/systemd/man/255/systemd.exec.html
