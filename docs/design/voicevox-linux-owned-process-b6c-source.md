# TSUMGI #215 B6c — Linux OwnedProcessController (source-only)

**Status:** Provider-local Linux process/port validation only. Not a deployed VOICEVOX engine, verified loaded model, actual voice-license approval, or general-purpose process sandbox. No RuntimeProvider registration, NixOS/generic-systemd SetupPlan, GPU service or actual engine HTTP client is added.

## Source contract

- The provider-local module voicevox_linux_process.py implements VoicevoxOwnedProcessControl: endpoint_owner, inspect, launch and stop_owned.
- The operator supplies a frozen VoicevoxLinuxLaunchSpec with one absolute executable, fixed argv and optional SHA-256-pinned auxiliary file. It is bound to one exact VoicevoxOwnedRuntimeSpec when constructed. No client input or audio Job may choose a runtime, shell command or filename. Path-looking argv tokens (containing a slash or known script extension) must be the exact pinned auxiliary file; recognized inline Python/module switches are rejected. This is intentionally conservative but is **not** a full semantic parser for arbitrary executable flags: opaque arguments that internally load a second code file are still a deployment qualification blocker. A Python binary hash alone must not authorize such unreviewed code.
- Before launching, verify actual executable and model files' bytes against their B5b SHA-256 pins, and the auxiliary file against its pin. Reject symlink components, altered files, occupied endpoint and wrong UID. The controller uses the configured effective UID only (no setuid/sudo). It invokes the executable directly, with minimal fixed environment, disconnected stdin, discarded output, a fresh session/process group and no inherited nonstandard FDs.
- Linux identity observations combine one direct child PID generation from /proc/PID/stat, UID from /proc/PID/status, actual executable inode via /proc/PID/exe, immutable operator-approved argv compared to /proc/PID/cmdline, own process-group leader, and exact 127.0.0.1 listening socket inode cross-checked through /proc/net/tcp and /proc/PID/fd. Unowned ports, duplicate/reuseport or IPv6 ambiguity, and listeners on any other IPv4 interface—including publicly accessible 0.0.0.0 wildcard listeners—are rejected. The TCP scanner inspects every IPv4/IPv6 listener on the target port rather than silently ignoring non-loopback IPv4 bindings. An occupied foreign endpoint is represented by sentinel 0; it is never adopted, signaled or labeled ready.
- File hash preflight plus filesystem observations do not attest that real VOICEVOX has actually **loaded** selected model weights or rights-approved voices. Existing VoicevoxOwnedManagedRuntime still requires exact version/speaker UUID/global numeric style probes and a verified engine HTTP transport before advertising readiness.
- Stop requires the exact launch nonce, checks the returned direct child's PID generation, UID and process group before SIGTERM, bounded wait, SIGKILL if required, and direct-child reaping. **Readiness additionally requires the pinned executable inode and fixed argv.** If the already-owned child re-execs to another executable (for example through a Nix/Python launcher), the runtime must reject READY but still stop that same child generation. Requiring the original executable inode at the *stop* boundary would fail to reap the child and leave an orphan; the split identity policies are deliberate. It also checks that the configured port is no longer listening before reporting success. If ownership cannot be verified it fails closed rather than signaling a potentially foreign process. Startup cancellation returns to the caller without waiting indefinitely for a delayed subprocess spawn. The same live asyncio event loop retains a pending spawn-and-reap task: if process creation eventually completes, it kills and reaps that returned subprocess. During this interval the controller permanently denies new launch, endpoint ownership and readiness; stop_owned times out with a cleanup-uncertain error rather than claiming success, and only returns normally after late reaping has completed. **This is a bounded runtime response, not a guarantee of child cleanup if Worker/event loop itself dies while spawn is still pending.** If a late subprocess creation reports an exception **without returning a child handle**, cleanup remains unverified and stop fails closed rather than claiming there was nothing to reap.
- Filesystem and /proc reads are off the Worker asyncio event loop. Errors are fixed and redact paths, argv, PIDs, tokens, source text and native stderr.

## #215 R4 — bounded stop/reap and reconciliation (source only)

Run `TSUMGI-215-R4-20261106` starts from main
`6d4f2df0d68cdd7238b8a0bbfa233965473fd7be`. R4 addresses two source-observed
failure models: competing nested cleanup timeouts and external cancellation
leaving STOPPED before cleanup was verified. Deliberately delayed post-exit
inspection demonstrates the timeout mechanism; the cause of the historical
**naturally intermittent failure remains unknown**.

`stop_owned` establishes one loop-time cleanup deadline before acquiring its
lock. It covers joining previous inspection work, generation/UID/PGID checks,
TERM, a grace wait of half the remaining budget, conditional KILL, direct-child
reap and fresh all-interface listener absence proof. No new full timeout is
added after TERM. The launch stop budget must not exceed the runtime budget,
including direct construction without source assembly. ManagedRuntime delegates
this bounded controller contract without a competing outer process-stop timeout.
An arbitrary injected controller that does not honor the contract is unqualified.

A direct-child returncode alone is insufficient: `wait()` must complete and the
configured port must be absent. If the child is observed exited after generation
inspection, signaling is skipped but reap and port proof are still required.
A live identity mismatch denies signaling. Changed executable/argv still denies
READY without preventing cleanup of the exact recorded child generation. A
foreign listener or rebind is never adopted or signaled and prevents successful
cleanup while it remains present.

Timeout/cancellation retains the owned handle, nonce and outstanding reap or
inspection tasks. A shield preserves these tracked tasks, but the stop deadline
bounds the caller's await. Cancelling `to_thread` cannot stop its OS thread, so
the controller retains at most one cleanup inspection. Explicit same-nonce
reconciliation must join it before starting another and perform a **fresh**
post-reap scan; an old absence result is not reused as proof. Late task completion
alone never clears ownership or restores admission. Failed/cancelled controllers
permanently deny new launch/readiness even after successful reconciliation.
Verified normal cleanup retains only its nonce receipt for idempotent repeat
stop; a fresh normal launch invalidates that receipt.

Newborn/late-spawn cleanup also bounds kill, reap and listener proof before
clearing a returned handle. The existing delayed-spawn task may still wait for
the subprocess creator to return one: `stop_owned` has a bounded response, not a
guarantee that an unresponsive OS operation or a dead event loop can be cleaned
up. Retained work requires the same live controller/event loop and explicit
bounded reconciliation; a failed late-spawn task does not prove cleanup.
pidfd/cgroup/descendant hardening remains a future operator gate, not an R4 change.

## Synthetic acceptance

Tests launch a disposable **Python TCP listener** under an isolated Linux sandbox; they exercise genuine OS PID, UID, process groups, /proc socket inode correlation, stdout/stderr disconnection, start/stop, forced kill/reap, heartbeat responsiveness and model/source pin validation. No real VOICEVOX engine, model, voice, live GPU, production service or User text is accessed.

A later cross-PR synthetic E2E (B6a binding + B6b actual HTTP transport + B6c exact Linux subprocess) exposed an additional cleanup bug: a Nix Python wrapper re-execed the actual interpreter, invalidating the strict executable identity. The original stop path then refused to terminate its own child. The corrected policy rejects READY for executable drift but reaps the same PID/UID/start-time/process-group child. A regression runs a pinned Python test script that later execs a distinct system executable and verifies both readiness denial and bounded owned-process reaping. This is not approval to execute unpinned wrappers in a real Worker; production must still pin the actual native executable or prove its complete execution chain.

**B6c R6 test-only closure:** Independent review of R5 identified a coverage gap: the changed-ELF regression used a \`sleep\` process without a listening socket. The additional Linux test now launches a pinned synthetic Python child that opens a real 127.0.0.1 listening socket, marks its FD inheritable, then waits for a test-owned gate file before \`execv\` into another ELF. It verifies the exact socket **inode survives exec**, that the foreign ELF still holds that listener in the original child, and that READY/inspection reject the change both normally **and when the fixed-argv validator is temporarily bypassed in the test**. Nonce-bound stop must still reap that same original process generation and release the port. This is a source-only disposable test; it does **not** weaken the existing admission policy or prove a native VOICEVOX engine safe.

Two RED-then-GREEN security regressions matter:
1. An owned listener bound to **0.0.0.0** originally satisfied the PID and socket inode checks. Strict local address matching now rejects it, including when it is the exact child.
2. A pinned Python executable could re-exec into an unreviewed script yet retain PID, start ticks, UID, executable inode and listening socket. Full fixed command-line checks now deny readiness after this change; the same provably owned process generation remains stoppable.

The tests also cover a foreign socket that remains alive after rejection, wrong executable/model/auxiliary SHA pins, symlink auxiliary paths, UID mismatch, changed or exited processes, fake engine integration, and bounded direct-child stop. B6c follow-up tests additionally reproduce and fix an unpinned script-file/inline-module argv path and **unbounded launch cancellation** when subprocess creation stalls; late-spawn cleanup remains tied to the controller's live asyncio loop, with permanent fail-closed status during uncertainty.

## Hard limits and next gates

- **/proc is not a trusted kernel attestation.** A process may rewrite its command-line memory. Launcher/file hash checks have TOCTOU windows; root compromise or same-UID threat can bypass some observations. No pidfd/execveat immutability, cgroups, LSM/seccomp, namespace sandbox or PID reuse-free atomic signaling exists. Review these separately for real native acceptance.
- **Escaped descendants:** SIGTERM/SIGKILL only target the observed process group. A child calling setsid can escape. A remaining listener prevents claiming successful stop; a descendant without a listener is not detected. No general fleet-level process safety or host GPU ownership is claimed.
- **Actual loaded model and voice license:** an on-disk model SHA is not proof a process loaded it. Operator declarations/voice hashes are not a legal approval or real hardware test. Real binary arguments/ABI, model quality, and downstream service ownership remain unqualified.
- **VOICEVOX privacy:** native /audio_query places input text in the URL query. Separate PR #225 uses no client URL logging; real native engine/access/proxy/tracing logs must still be independently verified disabled/redacted before handling user text.
- **Disabled by default:** the common RuntimeProvider registry, Nix modules, Worker manifest, Control service, PostgreSQL, SetupPlan, and deployed hosts are unchanged. Even an observation from this controller is insufficient to enable a production audio Worker.

Sequence: exact PR source/CI review → separate Human Ready/Merge → independently authorized, isolated real engine and process security/voice/model/ABI tests → #180 executable provider and Nix/systemd setup/rollback → #181 real audio acceptance. The #178→#179 STT selection gate remains separate.

Keep #215/#176/#175/#180 OPEN. No engine/model download, host service changes, GPU/DB action, deployment or release.
