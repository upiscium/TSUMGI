# TSUMGI #240 R4 — Two-tier native cgroup containment feasibility

Status: Source-only design and offline tests in Draft PR #244.
#240, #237 and #230 remain OPEN. No Production GPU work or release
authorization is provided by this document.

## Why the flat transient-service design cannot certify cleanup

On arca-sandbox (Ubuntu 24.04.5, systemd 255.4-1ubuntu8.18), the
ordinary transient service cgroup disappeared on StopUnit, so an
earlier-opened cgroup.events file descriptor returned OSError instead
of an attested populated=0. Testing RemainAfterExit=yes did not help:
the unit remained active (exited) while its ControlGroup was empty.
PID1's stop Job ACK or missing cgroup path is not terminal proof.

## R4 GPU-free, disposable systemd-255 experiments

A temporary, uniquely named, test-only service ran a trusted ROOT
Supervisor with Delegate=yes, DelegateSubgroup=monitor,
NoNewPrivileges=yes, DevicePolicy=closed, KillMode=control-group.
These privileges were granted ONLY to disposable arca-sandbox fixtures,
never to the normal TSUMGI Worker or Sirius.

The Supervisor lived in this test hierarchy:

    /system.slice/tsumgi-b1b0-<run>.service/
      monitor/        # Supervisor remains alive
      native/         # Native-like subprocess and descendants

Four bounded, zero-GPU behaviors were verified:

1. An initial single-threaded Python preexec_fn moved a dummy process
   into native before exec. A live native yielded cgroup.events
   populated=1; after the child exited, the SAME directory and
   events-file inodes yielded populated=0 while monitor was alive.
   This is NOT proof of atomic placement at process creation.
2. Linux clone3(CLONE_INTO_CGROUP) succeeded on the sandbox kernel.
   The dummy child was placed in native AT CREATION, before exec,
   then reaped with populated=0 in the still-existing native cgroup.
3. A wrapper in native forked a grandchild and exited. The
   grandchild used setsid() and ignored SIGTERM, yet inherited
   the native cgroup. The native subtree remained populated=1
   even after wrapper exit. Scoped native/cgroup.kill was able to
   turn it into populated=0 while the monitor parent stayed alive.
4. A combined clone3 test dropped the child to UID 65534 BEFORE
   exec, and its unprivileged grandchild again changed session,
   ignored TERM and outlived the wrapper. A write only to that
   run's native/cgroup.kill yielded populated=0 while monitor
   stayed populated=1. Both native directory and events-file
   inode identities were stable across the before/after samples.

Each temporary service and its scripts were removed by exact unique
name, with no residual fixture process/service. No Nix deployment,
existing unit, Control DB, Plugin/Tunnel, GPU or Sirius service was
changed. The disposable fixture used systemd patch release
255.4-1ubuntu8.18; Sirius is 255.4-1ubuntu8.17.

One failed synthetic probe mistakenly compared the native DIRECTORY
inode to the events FILE inode, which are not expected to be equal.
The harness was corrected to compare each object to its own earlier
generation. The corrected multi-process probes passed.

## Candidate B1b1 design (not implemented)

- Keep the ordinary Worker unprivileged with Delegate=no and
  ProtectControlGroups=yes. It must NOT acquire raw cgroup authority.
- A separately authenticated, tightly restricted privileged broker
  enforces approved executable/model SHA, exact Worker UID, unique
  run/Unit/InvocationID, durable GPU reservation, and allowed IPC
  operations. It must not expose arbitrary systemctl, D-Bus methods,
  signals, cgroup paths, or privileged command execution.
- One trusted per-run Supervisor owns an explicitly bounded
  delegated subtree. It must stay in the monitor subgroup.
  Native must start in its root-owned native sibling through an
  audited minimal CLONE_INTO_CGROUP launcher (or equally reviewed
  truly atomic primitive). The child drops privileges BEFORE exec.
  Failure of placement MUST deny native launch rather than fall
  back silently to launching outside the cgroup.
- Stop first fences any new native launch durably. The Supervisor
  kills only the exact native subgroup, never the broad Worker,
  system.slice or GPU drivers. It must observe native populated=0
  on the SAME held cgroup/events identities while the monitor is
  still alive, and independently reap the original wrapper.
- An important remaining TOCTOU gap: populated=0 at a moment in
  time does NOT prevent later repopulation by a privileged
  supervisor. The monitor must irreversibly seal this run against
  new native launches, remove/seal the empty native subtree,
  and durably bind the terminal proof to the run UUID,
  InvocationID and cgroup generation BEFORE stopping its own
  parent systemd Unit. A failed or unknown seal stays fenced.
- A separate PID1 reconciliation must verify the parent's terminal
  state and original invocation generation before the broker is
  even eligible to consider clearing GPU reservations. A crash,
  loss of Job ACK, alias mismatch, unexpected cgroup disappearance,
  or missing kernel observations remain UNKNOWN_FENCED.
- The current B1a SQLite ledger deliberately has NO release API.
  Adding release requires a new independently reviewed protocol.
  The current B1b0 flat Unit plan also cannot simply be deployed:
  it sets User=astrumweaver, ProtectControlGroups=true, and
  DevicePolicy=closed with no delegated trusted supervisor.
  These security properties must not be silently weakened.

## Added offline-only negative contract

New module native_nested_cgroup.py defines OfflineCgroupObjectKey,
OfflineNestedCgroupSamples and
validate_offline_nested_empty_candidate(). These only validate
claim-shaped data; they NEVER read a real kernel, contact PID1,
mark a Runtime READY, or release a GPU.

They reject foreign monitor/native sibling paths, changed
directory/events inode generation, wrong user, unreaped wrapper,
missing/duplicate cgroup.events values, inconsistent parent
populated state, wrong immutable model policy, changed Worker
epoch/Deployment, and a different Unit InvocationID from the
previously bound start generation. A valid synthetic snapshot
leaves the B1a UNKNOWN_FENCED reservation intact.

## Remaining mandatory acceptance gates

1. Independent privileged-boundary security review, narrowly
   delegated controls, SO_PEERCRED, approved GPU DeviceAllow/
   private UDS, Proxmox LXC and Nix executable/model permissions.
2. Purpose-built reviewed C/Rust clone3(CLONE_INTO_CGROUP) launcher:
   process and FD hygiene, credential drop, cancellation, syscall
   failure, no before-placement native code, and no unsafe fallback.
3. Demonstrate that untrusted Native descendants cannot change
   cgroup membership, including fork/setsid/setpgid, inherited
   handles, model wrappers and CUDA-driver holdouts.
4. Durable, generation-attested native empty + sealing + parent
   service termination + crash reconciliation before any release.
   A surviving process, missing proof or unconfirmed manager stop
   must remain fenced indefinitely until trusted recovery.
5. Independently approved reversible Sirius 8K stop/restore,
   real immutable 12K model validation and coherent Control/Gateway
   Serving promotion. No 12K/16K deployment from this source-only work.

References:
- https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html
- https://systemd.io/CGROUP_DELEGATION/

## Additional negative experiment: transient empty is not sealed

A further disposable GPU-free systemd-255 trial deliberately kept the
same trusted monitor running after an exact cgroup.events
populated=0 observation. A first child was atomically created using
clone3(CLONE_INTO_CGROUP), then terminated and reaped. After reading
native populated=0, the still-active Supervisor created a SECOND
child using the SAME native cgroup FD. The SAME directory inode then
returned populated=1 again. This second child was reaped, populated=0
was observed again, and the empty native subgroup was removed with
rmdir WHILE the monitor stayed populated=1.

This is direct evidence for a TOCTOU condition: a correct empty sample
is only a point-in-time observation. The future privileged Supervisor
MUST first enforce irreversible launch fencing, then verify native
emptiness, and seal/remove that exact native subgroup without
recreating it. The broker must persist/attest this terminal sequence
before final Unit stop and any GPU re-admission. Neither the offline
sample validator nor B1a's current UNKNOWN_FENCED state provides that
release protocol.

All temporary test units and scripts were cleaned up. No GPU,
Sirius Worker, Control DB or existing systemd Unit was changed.
