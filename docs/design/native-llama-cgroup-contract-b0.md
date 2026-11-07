# TSUMGI #240 — Phase B0 isolated native-run contract

**Status:** source-only, not activated, not reviewed, not a cgroup
implementation. This is a small successor to [Phase A design PR
#241](https://github.com/upiscium/TSUMGI/pull/241), independently
reviewed **after** its draft source is published. The Phase A design itself
is not yet merged. Parent #237 and 12K dependent #230 remain open.

## What the code proves (in memory only)

`src/astrumweaver/runtime/native_cgroup_contract.py` is a provider-neutral
**pure Python contract** with no D-Bus, systemd, cgroup filesystem, process,
network, GPU, Worker or database calls. No existing runtime imports or
activates it. It contains:

1. `NativeLaunchRequest`: one canonical UUIDv4 run ID, UUIDv4 Worker epoch,
   bounded Worker ID, immutable deployment revision, absolute canonical model
   reference, explicit GPU UUID tuple and absolute Nix-store binary. It
   **derives**, never accepts, a single-run
   `tsumgi-native-{run-uuid-hex}.service` unit name and corresponding
   `/system.slice` cgroup path. It rejects noncanonical paths, traversal,
   duplicates and malformed identities before any future broker call.
2. `NativeLaunchAllowlist.permits`: exact-value comparison of Worker ID,
   deployment revision, executable/model references and GPU set. Policy
   construction and Worker authentication must ultimately be performed by
   **a separate trusted broker**. A client-provided allowlist is **not**
   a capability, an approval, or a verified executable hash. No arbitrary
   command, environment, unit name or systemd properties are accepted.
3. `NativeRunUnitIdentity`: the run ID, Worker epoch, immutable
   deployment revision, fixed unit name and cgroup path, plus a distinct
   systemd `InvocationID`. Shape-checking alone is **not evidence** the
   unit existed or that PID1 supplied that identity.
4. `NativeStopObservation` and bounded `cgroup.events` parser:
   require a nonambiguous `populated=0` report, a reaped wrapper, an
   inactive unit, and **the same exact unit+InvocationID** before
   release. Malformed, duplicated, oversized, or missing `populated`
   fields fail closed. Tests also reject `populated=1`.
5. `NativeRunTransitionContract`: a deterministic, in-memory transition
   guard from RESERVED through STARTING/LIVE and DRAINING/TERM_ACKED/
   optional KILL_ACKED to EMPTY_VERIFIED/RELEASED. No transition may
   skip TERM acknowledgment, and a unit/epoch/InvocationID mismatch
   irreversibly fences the in-memory instance as UNKNOWN_FENCED.

Tests cover unauthorized substitutions, stale run IDs and epochs,
model or wrapper substitutions, arbitrary paths and traversal, malformed
cgroup state, wrong invocation, release before empty, mismatched worker
binding and unknown-state denial. **They use only synthetic IDs and
data; they do not operate systemd or signal processes.**

## What this cannot prove

- **No broker or activation exists.** No IPC authentication, PID1 D-Bus
  authority, real systemd transient-service creation, automatic initial
  cgroup placement, cgroup fd ownership, GPU allocation or rollback.
- The data passed to `record_starting`, `record_ready` or
  `record_empty` are **untrusted claims** until a future manager-only
  adapter ties them to PID1 and a held kernel cgroup identity. A test can
  fabricate `populated 0`; this is not physical proof of containment.
- `NativeRunTransitionContract` is **not durable nor concurrency-safe**
  and must **never** be used as the sole GPU exclusion or crash recovery
  ledger. A separate trusted, durable per-run reservation and reconciliation
  protocol must prevent run-name and GPU ownership collisions across
  Worker and broker restart, lost start/stop acknowledgements and timeouts.
- The fixed `/system.slice/` placement is a bounded initial assumption:
  a future service design must independently validate its actual slice,
  GPU device permissions and cgroup path from PID1, rather than quietly
  relaxing this check in response to an unexpected path.
- The configured Nix path and model reference must be resolved and
  verified against reviewed hashes, symlinks and a tightly bound allowlist
  **by the broker itself**; syntax and string comparison are not security
  evidence. Local policy values and plain UUID formats are not an
  authentication/entropy proof.
- `cgroup.events populated=0` only means all descendants **currently
  attached** to that cgroup have exited. It cannot prove initial placement
  or stop processes that escaped to a different cgroup. A transient unit
  removed before identity-checked observation **must not** be interpreted
  as verified empty.

## Next review and acceptance gates

Before any Phase B1 systemd adapter: independent threat review of PID1
`StartTransientUnit`, source identity and unit/InvocationID reuse,
pre-exec cgroup placement, process/UDS/device sandboxing, cgroup-only
TERM/KILL, populated-event monitoring, persistent unknown-run recovery,
and stop/cancel concurrency.

In disposable systemd/LXC tests, inject nested fork, fork-during-stop,
PID reuse, `setpgid`/`setsid`, shutdown deadline, broker crash, missing
cgroup files, malicious launch requests, and **lost manager
acknowledgements**. A future broker must avoid arbitrary privileged
command execution, and neither Worker nor client gets unrestricted
systemd/cgroup authority.

No merge, cgroup delegation, systemd unit install, deploy, GPU reset,
Sirius stop/start, Control/DB update, real 12K/16K activation, release
or client limit change is authorized by Phase B0. Current production
Qwen serving remains at 8K.
