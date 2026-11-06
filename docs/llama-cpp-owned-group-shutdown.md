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

`LlamaCppSubprocessController.start()` now uses
`asyncio.create_subprocess_exec(..., start_new_session=True)`, giving
each newly launched wrapper an **owned POSIX session/process group**
independent of the Worker. Stopping sends SIGTERM **only to that newly
owned group**, observes **both** group disappearance and wrapper exit,
and escalates to group-scoped SIGKILL if still present after eight
seconds. A second bounded four-second wait must confirm group
termination. Failure does **not** clear the internal process owner or
report successful cleanup.

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
- forced cleanup failure leaves the owner bound and raises an error.

Run the full existing llama.cpp provider regression tests and Nix
evaluation before marking any PR ready. Independent review and one
**separately authorized** physical Sirius Worker stop/start confirmation
are still required before deployment. The production 8K Worker and
Control/Gateway profile must remain unchanged until that authorization.
This fix does not independently qualify Qwen 12K Serving Contract
revision promotion or Model Client limit changes tracked under #230.
