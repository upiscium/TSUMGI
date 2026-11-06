# Owned llama.cpp subprocess group shutdown (TSUMGI #237)

This is **source-only**, pending independent review and a separately
approved service-impacting canary. It neither deploys a new Runtime nor
modifies running Worker/Control configurations.

## Problem and evidence

The approved temporary Sirius Qwen3.8 12K native canary (Issue #230,
2026-11-06) required stopping the original **8K** Worker. The Worker main
process exited but a native llama-server descendant remained in
`astrumweaver-worker.service`'s systemd cgroup until systemd's configured
90-second stop timeout. The failed stop is **observed**; its exact
signal-level cause is not. The original 8K Worker was subsequently
restored to Ready and the native server to `/slots n_ctx=8192`.

The Qwen/Bonsai host-CUDA bridge wrappers intentionally launch the
native server as a child, so monitoring only the wrapper's exit code
does not prove the native child is gone. Previously
`LlamaCppSubprocessController.stop()` signaled only the immediate
wrapper with `terminate()`, then `kill()` after 10 seconds. A wrapper
could exit early, leaving a descendant still holding GPU residency.

## Source-only correction and safety boundaries

The managed Runtime now also rejects `start()` and returns
`RuntimeHealthState.FAILED` with `ready=False` whenever the owned
controller reports an incomplete shutdown. In particular, an old native
server that still answers an HTTP health check cannot be re-admitted
as a healthy Deployment merely because its wrapper PID is still live.

A repeated cancellation can also race a failing OS spawn. Even if
`create_subprocess_exec()` eventually raises `OSError` **without
returning a Process handle**, its internal subprocess launcher could
have briefly executed and forked native descendants before the error.
The controller therefore deliberately remains **fail closed**: a later
`stop()` cannot certify cleanup merely from that exception and denies
the next GPU launch. A disposable regression verifies this conservative
boundary. In particular, after **one cancellation** the `start()`
exception handler must never clear `_stop_incomplete` merely because
`_process=None`; this was a second fail-open path still present after
the earlier two-cancellation test. A new one-cancel/OSError negative
regression first reproduced that false unfence, then passed after it
was removed. Recovery from this unverified state requires a higher-trust
service/cgroup containment proof or an operator-controlled restart,
not a synthetic assumption of "no children".

Kernel identity semantics: Linux `pidfd_send_signal(2)` documents that
`/proc/pid` directory FDs are stable references to individual processes,
but this does *not* attest to all descendants of a failed unreturned
spawn: https://man7.org/linux/man-pages/man2/pidfd_send_signal.2.html

A further cancellation race was reproduced with an actual disposable
subprocess: cancelling `start()` after the OS child had been spawned
but before `create_subprocess_exec()` returned caused the native-like
child to be left live with `_process=None`. The launch is now shielded
inside a separate owned task. That task assigns the process to the
controller before completing; cancellation marks shutdown incomplete
and sends group-scoped SIGKILL to a late process. The caller also
attempts bounded cleanup, while repeated cancellation retains the
pending launch/owner and cannot silently authorize a new GPU process.
The original failing reproduction and a repeated-cancellation scenario
are tested with real no-GPU synthetic processes. The successful
cancelled-launch cleanup also closes its captured `/proc` leader FD;
this was previously leaked even after the native child was reaped,
and is now asserted in the disposable regression.

An additional ownership gap is explicitly fenced: even if the wrapper
has exited, `start()` rejects a new native launch while the previous
process owner remains bound, until `stop()` successfully verifies group
cleanup and clears ownership. Launch and shutdown are serialized by a
per-controller async lock, so a concurrent stop cannot race an
in-progress spawn. An attempted stop marks shutdown incomplete until
group disappearance is verified; even a still-running wrapper cannot
be silently re-admitted if that stop failed or was cancelled. A later
successful explicit stop clears that fence. Cancellation sends group-scoped SIGKILL
as a best-effort last resort and propagates cancellation **without**
claiming a successful release.

`LlamaCppSubprocessController.start()` creates a private POSIX session
(`start_new_session=True`) and binds the original Linux group leader's
`/proc` identity and cgroup while it is available. Stopping **does not
use numeric `killpg`**. The new `OwnedPosixGroup` helper enumerates
members of only that original session, verifies its group number,
session number, start-time bound and inherited cgroup, opens each
candidate's own stable `/proc/<pid>` directory FD, and uses
`signal.pidfd_send_signal(fd, SIGTERM|SIGKILL)` for the exact process.
Those FDs protect against the member PID being recycled between its
verification and signal delivery. The process group must disappear and
the wrapper must be reaped before cleanup is reported successful.

This is important because a Linux `pidfd_send_signal` call with the
**process-group flag** on the *original leader's* PIDFD may return
`ESRCH` after the wrapper is reaped, even while a native child remains
in the old group. A disposable Adam test reproduced that failure; a
second test proved that signalling the surviving child through its own
`/proc` pidfd works after leader reaping. A group numeric PID/PGID can
be recycled; a mismatched *live* group leader generation or cgroup
causes an explicit fail-closed error.

The controller escalates from per-member SIGTERM to per-member SIGKILL
after an eight-second limit, then allows a further four seconds to
confirm absence. Unverifiable identity, inaccessible `/proc` data,
escaped descendants or persistent processes **must not** be reported
as successful cleanup. The original ownership remains fenced.

This is deliberately specific to managed llama.cpp; it does not run
`pkill`, terminate unrelated GPUs or processes, stop an unrelated
Worker, send signals to systemd's entire service cgroup, or change
model/serving configuration. It also catches the failure mode where
the wrapper exits first but a remaining descendant stays in its
owned group.

Because process-group shutdown is POSIX-specific, this correction is
for the supported Linux Worker platform. A worker lifecycle timeout or
unusual native CUDA driver hang can still produce a failure; the source
does **not** claim that the native binary or GPU is always recoverable.
The service's `TimeoutStopSec=90s` and Worker's existing runtime
`shutdown_timeout_seconds=60s` exceed the nominal 8+4 second
group stop budget; this does not prove latency under all host loads.

## Disposable tests, acceptance, next gate

Source-only tests launch their own temporary executable without a model,
CUDA or a listening network service and verify:

- clean single-process shutdown and repeated stop;
- forked child ignoring SIGTERM, requiring bounded group SIGKILL;
- wrapper exit while a native-like descendant is still alive;
- missing/invalid group identity denied without unscoped signals;
- forced cleanup failure leaves the owner bound and raises an error;
- wrapper exited but owner not released prevents another GPU launch;
- concurrent spawn/stop and duplicate start attempts are serialized;
- stop cancellation attempts group-scoped SIGKILL without clearing owner;
- start cancellation after actual OS spawn retains/cleans late ownership;
- repeated cancellation does not discard a pending launch task;
- original group leader reaped, native-like child targeted by stable
  per-member pidfd and no numeric killpg;
- synthetic reused group leader and foreign cgroup refused before signals.

Run the full existing llama.cpp provider regression tests and Nix
evaluation before marking any PR ready. Independent review and one
**separately authorized** physical Sirius Worker stop/start confirmation
are still required before deployment. The production 8K Worker and
Control/Gateway profile must remain unchanged until that authorization.
This fix does not independently qualify Qwen 12K Serving Contract
revision promotion or Model Client limit changes tracked under #230.

**Residual constraints:** A process that explicitly escapes its inherited
process group using `setsid()` is not contained by this particular
mechanism; the currently reviewed Qwen/Bonsai wrapper does not do so.
POSIX group/session IDs are numeric and can be reused after the old
group disappears. Individual targets use stable pidfd references and the
capture-time leader start bound; nevertheless, arbitrary newly reused
groups whose old leader has already been reaped cannot be proven to be
the original group from these fields alone. Do not treat this as formal
containment: future higher-assurance admission may need an independently
owned cgroup or a persistently held supervisor, and a real physical
stop/start remains a mandatory separate acceptance gate.
The targeted source tests prove the behaviour of synthetic groups only,
not kernel/GPU shutdown under actual model load. A higher-level service
cgroup remains the final emergency cleanup owner.
