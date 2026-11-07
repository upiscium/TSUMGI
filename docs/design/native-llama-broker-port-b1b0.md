# TSUMGI #240 — Phase B1b0 isolated Manager dispatch contract

**Status:** source-only **offline port/plan**, no concrete systemd/D-Bus
transport, no real credential validation, no Worker integration, no GPU or
cgroup action. Follows unmerged [B1a Draft PR #243](https://github.com/upiscium/TSUMGI/pull/243)
which follows [B0 Draft PR #242](https://github.com/upiscium/TSUMGI/pull/242).
The initial containment design is [Phase A Draft PR #241](https://github.com/upiscium/TSUMGI/pull/241).
None is Ready/Merge/Deploy-approved.

## Narrow purpose

The new `src/astrumweaver/runtime/native_systemd_port.py` defines a
**protocol and offline planner**, plus a coordinator that is tested
**only with an injected FakeManager**.

The required future PID1 interface is
`org.freedesktop.systemd1.Manager.StartTransientUnit(name, mode, properties, aux)`.
The mode is fixed to `fail` and the fourth argument is an empty array,
so a caller cannot request `replace`, `isolate`, an arbitrary unit name
or auxiliary units. The return is a **queued start-Job object path**,
*not* evidence that the native process started, reached READY, was
placed in the intended cgroup, or that its descendants were stopped.

The `StartTransientUnitPlan` can only be built by deriving a fixed
`tsumgi-native-{run-id}.service` identity from a valid B0 request and
an exact `ReviewedNativeCommand`. The latter contains the approved
absolute Nix executable, complete exact argv including a unique
model path, Worker UID and immutable B0 allowlist. A future trusted
broker MUST load this policy from administrator-controlled
configuration, authenticate its IPC caller and compare the actual
peer UID to that policy; **caller-supplied UID strings or JSON are not
authorization**. The source-only coordinator's `trusted_peer_uid`
argument is merely an asserted input, not SO_PEERCRED.

The proposed fixed-property subset includes a named `system.slice`
transient `.service`, `Type=exec`, `User=Group=astrumweaver`,
`NoNewPrivileges=true`, `ProtectControlGroups=true`, restricted
filesystem and kernel interfaces, `Restart=no`, `KillMode=control-group`,
and `DevicePolicy=closed`. The separate offline
`SystemdStartTransientCallShape` declares the exact Manager method,
D-Bus signature `ssa(sv)a(sa(sv))`, string/boolean property variants,
`ExecStart` as `a(sasb)`, and empty auxiliary array. **This is not an
actual marshalled D-Bus frame or validated systemd version-specific
service invocation**. No `DeviceAllow` is present; GPU runtime access
is therefore intentionally not enabled in this design stage. UDS,
GPU device nodes, network loopback, model packaging and permissions
require a separately approved policy and disposable host tests.

## Durable order and negative scenarios

The `NativeBrokerDispatchCoordinator.dispatch_once` sequence is:

1. Check **exact request and admin-reviewed command** before making
   any reservation. The fake tests reject mismatched Worker peer UID,
   unreviewed model or executable, altered GPU set and malformed argv.
2. Use B1a `BEGIN IMMEDIATE` atomic reservation. Reject **all**
   idempotent replays as launch attempts; they must be reconciled, not
   dispatched again.
3. Commit B1a `UNKNOWN_FENCED` via `mark_start_intent` **before**
   the first Manager-port call. If the commit fails, do not invoke
   Manager. The prelaunch reservation remains durable.
4. Issue precisely one `start_transient_unit` through the injected
   manager interface, with a positive, finite Manager **reply** timeout
   bounded at 30 seconds (default 30). A missing/late response,
   timeout, fake transport error or cancellation never releases/retries
   the GPU owner. The future PID1 operation may still have been queued
   when the caller times out; this timeout is **not** proof of failure
   or a replacement launch authorization. An invalid Job object path
   is rejected while the ledger remains FENCED.
5. Even if a valid Job path is returned, emit only a
   `NativeStartPendingReceipt` with `UNKNOWN_FENCED`, **never** a
   READY/RELEASED state or real containment proof.

Tests exercise an in-memory Manager, 8 competing coordinators for one
GPU, cancellation during Manager call, lost ACK, malformed Job reply,
untrusted properties, bad peer identity, corrupt ledger and blocked
start-intent persistence. **No fake manager test can establish atomic
native pre-exec placement or safe cgroup shutdown.**

## Not implemented / required before Phase B1b1

- A concrete system-bus transport with process confinement, polkit/
  system-bus permissions, `SO_PEERCRED`, absolute configured policy
  validation, correct systemd D-Bus variants, and *verified* UID.
- Manager job completion monitoring and PID1 attestation of
  `Transient`, `InvocationID`, `ControlGroup`, load/active state,
  restricted GPU devices, and unit identity. A start Job ACK is
  not `LlamaCppManagedRuntime.health.ready`.
- Dedicated PID1-owned run cgroup and proof that the native binary
  executes **only after** atomic membership, including grandchildren
  and bridge wrappers. Do not use a running Worker child moved later
  into `cgroup.procs`.
- Reviewed per-run scoped TERM/kill escalation and recursive
  `cgroup.events populated=0` **read from a trusted manager/cgroup
  handle**; durable B1a reconciliation of ambiguous manager/stop ACKs,
  unknown reservations and broker restart before GPU re-admission.
- Independent security review, disposable systemd/LXC tests and
  explicit operator acceptance. A transient `.service` can outlive
  its separate Worker; an orphaned native unit must remain fenced
  across crash/reboot until a trusted reconciler proves cleanup.

**No deployment, no Nix switch, no systemd service install/restart,
no `Delegate` changes, no GPU operation, no Control/DB mutation,
no Qwen 12K/16K activation, no Merge or release.** Production Qwen
stays 8K until the full #230/#237/#240 acceptance gates pass.

## Upstream systemd references

- [PID1 Manager D-Bus interface](https://www.freedesktop.org/software/systemd/man/latest/org.freedesktop.systemd1.html)
- [Control group interfaces](https://systemd.io/CONTROL_GROUP_INTERFACE/)

## Offline typed-call adapter (additional source slice)

`ShapedSystemdManagerPort` binds a **single reviewed request+command
policy+claimed peer UID** and accepts only the exact canonical
`StartTransientUnitPlan`. It forwards the fixed Manager method shape
through an **injected** `NativeOfflineDbusTransport` Protocol. Tests use
only `FakeDbusTransport`. A caller cannot alter the unit name, switch
mode to `replace`, or add `Delegate`/environment properties through
this adapter without getting an error **before** any fake transport call.

This provides source-level method/variant shaping and the B1a durable
fence-to-fake-transport integration, but **there is no concrete
transport connecting to PID1**. It cannot authenticate `SO_PEERCRED`,
attest the real transient unit or grant a GPU permission. No positive
hardware/systemd acceptance is claimed.

## R1 hardening — Manager response bound and target version mismatch

An isolated negative test first failed on the original B1b0 source:
`NativeBrokerDispatchCoordinator` had **no Manager acknowledgement
deadline** and could wait indefinitely for a hung fake Manager call.
The source-only coordinator now bounds this *caller reply wait* to a
finite, strictly positive value **no greater than 30 seconds**,
defaulting to 30 seconds. A second negative regression exposed a
subtle AsyncIO problem: directly using
`wait_for(manager.start_transient_unit())` still hangs after the
deadline when the Manager coroutine catches and suppresses
cancellation. The corrected code creates a separately tracked Manager
task and awaits `wait_for(shield(task))`, so the calling coroutine
times out promptly without asserting that the underlying request
was cancelled or that the service never started. A late task
completion/error is harvested only to avoid leaking task exceptions;
it cannot mark READY, free the GPU, or authorize retry. The durable
`UNKNOWN_FENCED` owner remains intact until a future trusted broker
reconciles the exact run/unit with PID1.

This is an **acknowledgement wait bound only**. It is not a systemd
service start/stop deadline, is not a trusted manager completion
receipt, and is not proof of kernel cgroup containment. A future
broker must also limit/account for unresolved in-flight operations,
especially across Broker crashes, before any production activation.

Read-only environment compatibility observation: Adam currently runs
systemd **261**, while Sirius has systemd **255**, and the latter
does not provide `systemd-analyze transient-settings`. Success of
Adam-side property introspection is therefore **not evidence** that
Sirius will accept this draft's D-Bus property/variant combination.
Require isolated systemd-255-target compatibility tests under a
separately approved Phase B1b1 before any real system bus connection.
