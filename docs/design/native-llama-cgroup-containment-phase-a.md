# TSUMGI #240 — Native llama.cpp per-run containment, Phase A decision record

Status: **design-only / Draft / not approved for deployment** (2026-11-07).
Parent #237, production 12K qualification dependency #230. No worker unit,
systemd, GPU, model, Nix switch, Control, DB, or client policy modification is
authorized by this document.

## Why PR #238 alone is insufficient

The actual November 6 Sirius canary/8K rollback exposed a surviving native
llama-server under a Worker service that reached its 90-second systemd stop
timeout. The exact initial signal-level failure is not proven. The two
independent read-only reviews of PR #238 established unresolved P1 cases:

1. Numeric process-group/session IDs can be reused after their leader has
   exited. Per-member pidfds pin *selected* targets, not the original group's
   lineage; a recycled session in the same cgroup can select the wrong target.
2. /proc enumeration is not atomic with fork/exit. The last observed member can
   fork another member after enumeration, then exit; a transient empty scan can
   falsely certify cleanup.

PR #238 includes bounded source mitigations for observed setpgid group drift,
startup cancellation and Ready races, but does **not** establish full process
containment or qualify an actual GPU stop. This design does not call that PR
Ready or supersede the independent review.

## Sirius read-only environment (verified November 7)

- GPU Worker: `astrumweaver-worker.service`, `Type=simple`, `User/Group=astrumweaver`.
- `KillMode=control-group`, `TimeoutStopSec=90s`, `Delegate=no`,
  `ProtectControlGroups=yes`, `NoNewPrivileges=yes`.
- Worker cgroup: `/system.slice/astrumweaver-worker.service`, owned
  `root:root` mode 0755; the unprivileged Worker cannot write its
  `cgroup.procs`. Unified cgroup v2 is mounted; the worker cgroup has
  `cgroup.kill` and `cgroup.events`; available controllers include memory
  and pids. The Proxmox LXC's systemd/cgroup and device namespace further
  constrain changes and are **not** assumed writable by TSUMGI.
- The source-generated Nix Worker service also sets
  `ProtectControlGroups=true` (`nix/modules/worker.nix`); the
  stand-alone unit template sets `ProtectControlGroups=yes`
  (`systemd/astrumweaver-worker.service.in`). Changing this baseline is
  **not** part of Phase A.

## Decision: prefer a narrow systemd-managed transient .service per native run

**Option A (preferred for a separate implementation feasibility exercise):**
A separately installed, privileged but tightly constrained local *broker*
asks PID 1 to create one named transient `.service` per reviewed native run
using systemd's `StartTransientUnit` manager interface. The service manager
creates its cgroup and starts the executable under that unit; native
descendants inherit membership before any model process runs. Do not use a
transient `.scope` that attaches already-running Worker children after fork:
that creates the exact fork-before-placement gap this project must eliminate.

The broker is **not** a general sudo/systemctl proxy. Its only operations are
Start/Inspect/Stop for a reviewed native-runtime identity and an exact
per-run unit ID. It may select **only** a pre-authorized absolute Nix store
wrapper executable, immutable model reference and validated bounded arguments
from an administrator-owned allowlist tied to the deployment revision.
Requests carry a cryptographically unpredictable run ID and the owning
Worker identity/epoch. The broker authenticates local IPC callers (peer UID
and a separate scoped authority, if applicable), rejects symlinks/path
substitution, alternate unit names, arbitrary ExecStart, environment, cgroup
paths or property maps, and never grants the Worker unrestricted system bus
rights. Credentials remain out of command arguments, unit description,
journals, and untrusted environment.

**Important:** the broker must keep the native service in its *own* cgroup
but retain the Worker/supervisor-to-service ownership mapping across Worker
death/restart. A detached systemd service is NOT automatically terminated by
`KillMode=control-group` of the *parent Worker unit*. The broker must provide
crash recovery and bounded reconciliation of orphaned run units before another
GPU owner becomes eligible. Neither a Process PID nor the unit name alone is
a release proof. The exact systemd service name, created unit object path,
ControlGroup cgroup path, verified deployment revision and launch UUID should
all be checked together. Unit names are single-use for an unexpired run;
do not recycle a name before confirmed empty/cleanup.

The transient unit must be constrained as tightly as the current Worker:
`User=astrumweaver`, limited supplementary groups/devices, no extra Linux
capabilities, `NoNewPrivileges`, `ProtectSystem`, `ProtectHome`,
`ProtectControlGroups`, network namespace/loopback and private native
socket access as required. **Do not blindly copy** unit properties: GPU
device allowlists, native host-CUDA access, private Unix socket paths and
filesystem visibility must be proved in an isolated disposable environment
before any source merge. `Type=exec` and fixed credentials are candidates;
exact properties depend on Nix/systemd packaging tests. The final IPC
mechanism between Worker and native server (private UDS, managed readiness,
model manifest and tool stream) is a design gate, not assumed solved.

### Alternatives rejected/deferred

- **B: `Delegate=yes` on the normal Worker.** Not a sufficient isolated
  fix: the current root-owned cgroup, `ProtectControlGroups=yes`, LXC
  delegation rules, and initial subprocess cgroup placement all need explicit
  changes. A simple `mkdir` followed by writing a child PID to
  `cgroup.procs` is **racy** because native code may fork before relocation.
  `clone3(CLONE_INTO_CGROUP)` could provide atomic placement in a suitably
  delegated hierarchy, but requires a purpose-built launcher and kernel/
  LXC permission tests. It is a separate potentially higher-risk design.
- **C: PID/PGID polling, `killpg`, or per-member pidfd.** Cannot prove
  lineage across ID reuse or atomic empty state. PR #238 remains a bounded
  interim source guard, not the final acceptance mechanism.
- **D: global `systemctl kill` of Worker, `pkill llama-server`, or GPU reset.**
  Explicitly out of scope; may affect unrelated models/Jobs/GPU workloads.

## Proposed per-run state machine (not implemented)

`UNREQUESTED → RESERVED → STARTING → LIVE → DRAINING →
TERM_SENT → KILL_SENT → EMPTY_VERIFIED → RELEASED`.

Any unverified transition goes to `UNKNOWN_FENCED`: no second native
GPU launch, no READY, and no Serving advertisement for the corresponding
GPU/Worker ownership epoch. A terminal service job alone is **not** proof
the cgroup is empty. A crash in the broker/Worker must preserve the
run's record and let a trusted on-start reconciler inspect/stop the
specific prior unit. Reconciliation must never infer clean release
from a missing DB record or lost completion acknowledgement.

On STOP: request bounded TERM of **only the exact run unit**. After its
deadline, escalate **only that run cgroup** via a trusted manager stop/kill
operation or a broker-owned `cgroup.kill` fd when available and verified.
The run cgroup's `cgroup.events: populated=0` is the kernel's recursive
empty indication, including forked descendants. Verify the original
wrapper's exit/collect status and the same run's unit/cgroup identity
before `RELEASED`. Never delete/reassign run ownership until both the
service and cgroup have been reconciled. An absent/disappeared cgroup,
permission failure, stale unit, incomplete launch or timeout goes to
`UNKNOWN_FENCED`, with operator-safe diagnostics.

**Identity caveat:** reading `populated=0` after a unit has already
disappeared is not itself proof that a *specific prior* run cleaned up.
The monitor must hold the exact unit/run identity and enforce service
name non-reuse through the terminal verification. Kernel cgroup.kill
handles descendants, but containment depends on *initial placement
and no escape to a different cgroup*. Any escape must fail closed and
not be silently considered success.

## Proof obligations before implementation / operator approval

1. **Atomic placement**: trace a deterministic fork-on-entry binary and prove
   neither wrapper nor nested children can execute outside the reserved
   per-run cgroup, including concurrent cancellation and rapid failure.
2. **Authority**: malicious Worker IPC attempts for another run name, arbitrary
   ExecStart, symlink, injected environment, different UID/GPU or Control
   revision are denied; no extra Worker cgroup/systemd privilege.
3. **Containment**: a disposable native-like synthetic tree attempts
   `fork()`, double fork, `setpgid()`, `setsid()`, leader exit, descendant
   churn during stop, PID reuse, cancellation and interruption. Only the
   exact run unit receives signals. Prove cgroup state remains populated
   until all in-cgroup descendants exit.
4. **Cleanup deadlines**: bounded per-run TERM, cgroup-only KILL, populated=0,
   service/job terminality, monitor crash/restart, missing `cgroup.kill`,
   permission denial, timeout and operation-retry idempotency.
5. **Model/Worker integration**: existing Worker READY and Serving epoch
   reflect the supervised process; Qwen/Bonsai host-CUDA wrapper and private
   UDS still work within the unit; ordinary 8K serving stays unaffected.
6. **LXC/Nix support**: isolated systemd unit tests and Proxmox LXC
   read-only feasibility; no new permanent permissions by default,
   clear package/bootstrap/rollback story, tests in both standalone and
   NixOS modes. Verify `cgroup.events` and `cgroup.kill` behavior
   in the actual target namespace only after approval.
7. **Crash recovery**: an orphaned per-run transient unit cannot outlive
   Worker restart and allow simultaneous GPU ownership, even when the
   Worker never received its launch acknowledgement.

## Rollout gates and explicit prohibitions

- **Phase A — this document:** design-only review and scope approval.
- **Phase B — separate authorized source-only PR:** broker contract,
  disposable systemd/cgroup tests, review and Green CI. No activation.
- **Phase C — separately approved reversible Sirius maintenance:** exact
  source/commit identity, previously verified 8K backup, protected rollback,
  zero ongoing critical Jobs, GPU/VRAM/cgroup observations, Worker/Control
  READY and no service stop timeout. Roll back to 8K if any ownership
  uncertainty; never treat 12K native canary as production Gateway proof.

Do not change `Delegate`, `ProtectControlGroups`, systemd units, host kernel,
GPU driver, credentials, Control DB, live Worker, OpenCode context limits,
or published 12K Serving Contracts in Phase A. The existing #230 Draft PR
#234 demonstrates *synthetic* revision-chain/admission only, not model
attestation.

## References

- Kernel cgroup v2 (`cgroup.events`, `cgroup.kill`):
  https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html
- systemd control-group interface and `StartTransientUnit`:
  https://systemd.io/CONTROL_GROUP_INTERFACE/
- systemd-run transient service versus scope:
  https://www.freedesktop.org/software/systemd/man/devel/systemd-run.html
- systemd.exec `ProtectControlGroups`:
  https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html
- TSUMGI issue #240: https://github.com/upiscium/TSUMGI/issues/240
- TSUMGI Draft PR #238: https://github.com/upiscium/TSUMGI/pull/238
