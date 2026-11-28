# VOICEVOX managed runtime latched FAILED while the engine was healthy (G1, #317)

**This is evidence and a root-cause analysis for the maintainer's decision. It is
NOT a qualification and NOT a release gate.** Nothing here approves, activates or
releases any provider, voice or deployment. It was measured on one developer
workstation, not on the production Ubuntu LXC.

No audio, text, token or private path is recorded here.

## Summary

* **Root cause: a `/proc` race in the probe's own identity check, not slowness.**
  `_process_socket_inodes` in `src/tsumgi/runtime/providers/voicevox_linux_process.py`
  lists `/proc/<pid>/fd` and then reads every link. The VOICEVOX engine opens and
  closes one socket per HTTP request. A descriptor that closes between the listing
  and its `readlink` raises `FileNotFoundError`, which the function turned into a
  hard failure. `health()` in `voicevox_managed.py` runs this scan four times per
  probe (`inspect()` and `endpoint_owner()` before and after the two HTTP calls),
  and any exception latches the runtime FAILED for good.
* The probe is **not** slow. It takes about 24 ms idle and stays at or below 0.6 s
  even with 60 busy threads, heavy disk writes and 20 GiB of memory pressure. The
  failing probes took 10 to 80 ms; they failed, they did not time out.
* The Worker log shows only `health_not_ready` because `worker/supervision.py`
  `check` discards the provider's own reason (`voicevox_probe_failed` here). That is
  why #317 looked like a timeout.
* **Fixed** in a separate commit: a descriptor that has vanished is skipped; every
  other error, and a missing fd directory (dead process), still fails. A new
  deterministic test fails on the old code.
* A **second, rare, unexplained mode** was seen: one probe request to `/speakers`
  never completed while other requests were answered in milliseconds, so the 1.0 s
  (or 5 s) budget expired. It happened about once per 3,000 probes. It is not fixed
  here; the options below address it.

## Environment

| Item | Value |
| --- | --- |
| Machine | one RTX 4080 workstation, 12 CPU threads, 62 GiB RAM, no swap, Linux 6.18; the GPU is irrelevant (the VOICEVOX engine is CPU only) |
| Loader | a nix-ld shim provides the host loader (not the production LXC image) |
| Host load | average 1.5 to 3 before the runs; the host was shared with unrelated jobs |
| Engine | VOICEVOX 0.26.2 Linux CPU x64, the pinned artifact, started by the repository's own process control |
| Repository | `origin/main` 82f7935 (fixed code: plus the fix commit) |
| Runtime under test | `VoicevoxProvider.create_manifest_runtime` (real `LinuxVoicevoxOwnedProcessControl`, real `VoicevoxLoopbackHttpApi`), `RuntimeLifecycleManager`, and the real `RuntimeHealthSupervisor` at its production policy (1 s interval, 5 s outer budget, `probe_timeout_seconds` 1.0) |
| Load | 0 to 4 concurrent synthesis loops through `executor().execute_audio` (media stage stubbed, real engine); fresh private HOME per start (tmpfs) |

## Method

A harness (kept private, not in the repository) drove the runtime above and wrapped,
without changing behaviour, `health()`, `bounded_json` and `asyncio.to_thread`, to
record for every probe: total time, each `/proc` scan (thread-pool queue delay and
run time), each HTTP call, and the provider's failure code. A 10 ms ticker measured
event-loop lateness. Two modes were used:

* **supervised**: the real supervisor; on a latch the runtime is restarted and the
  run goes on (counts latches);
* **measurement**: probe budget 5 s and no supervisor, to observe real durations
  (the provider state still latches on the first failure).

Contention came from `stress-ng` (CPU workers, disk writers capped at 200 MiB each,
anonymous-memory workers) and from a 0.6 s busy block of the event loop every 7 s.
Total soak time was well over 30 minutes per code version.

## Measured probe duration (ready probes, milliseconds)

Old code. Each measurement run ended at its first race hit, so `n` is how long the
race let it run; "race at" is seconds after the first ready probe.

| Phase | n | p50 | p95 | p99 | max | First failure |
| --- | --- | --- | --- | --- | --- | --- |
| idle | 182 | 24.0 | 25.3 | 26.9 | 50.2 | none |
| load, 1 loop | 54 | 35.2 | 49.3 | 54.7 | 88.6 | race at 53 s |
| load, 2 loops | 109 | 38.4 | 55.4 | 71.5 | 90.3 | race at 108 s |
| load, 4 loops | 16 | 33.3 | 45.6 | 48.1 | 48.1 | race at 15 s |
| load 4 + 0.6 s loop stalls | 79 | 37.3 | 48.0 | 52.0 | 60.0 | race at 84 s |
| load 4 + 12 CPU workers | 164 | 58.1 | 85.2 | 102.0 | 164.3 | race at 163 s |
| load 4 + 36 CPU workers | 220 | 89.0 | 143.4 | 181.0 | 255.1 | none in 220 s |
| load 4 + disk writers | 180 | 56.5 | 89.5 | 143.9 | 166.2 | none in 180 s |
| load 4 + 12 GiB memory pressure | 31 | 55.9 | 87.7 | 149.7 | 149.7 | race at 30 s |
| supervised, load 4 | 321 | | | | | **3 latches at 47, 49 and 50 s**, then none for 250 s |

Fixed code (same harness):

| Phase | n | p50 | p95 | p99 | max | Latches |
| --- | --- | --- | --- | --- | --- | --- |
| load, 1 loop | 118 | 53.9 | 73.4 | 103.3 | 129.3 | 0 |
| load, 2 loops | 122 | 54.2 | 85.5 | 143.2 | 152.3 | 0 |
| load, 4 loops | 151 | 57.4 | 84.0 | 92.4 | 109.4 | 0 |
| supervised, load 4, 10 min | 572 | 47.3 | 78.3 | 92.9 | 137.6 | 0 |
| supervised, load 4 + 12 CPU + disk + 8 GiB memory, 4 min | 223 | 66.1 | 112.2 | 131.5 | 136.9 | 0 |
| supervised, load 4 + 60 CPU + disk + 20 GiB memory, 4 min | 196 | 161.1 | 284.1 | 384.6 | 596.4 | 0 |
| 24 cold starts with immediate load 4 | 668 | none above 0.3 s | | | | 0 |

Where the time goes (idle, about 24 ms): the four `/proc/net/tcp` reads cost about
5 ms each with 400 sockets on the host (up to about 70 ms under 60 busy threads), the two
HTTP calls about 1.5 and 3 ms, thread-pool queue delay at most 14 ms. The worst
probe (596 ms) was a single `/speakers` call that took 420 ms inside an engine
that was sharing 12 cores with 60 spinning workers and four syntheses. Event-loop
lateness stayed below 90 ms except where it was injected.

Only the `/proc` scan, the HTTP calls and the thread hand-offs were ever on the
critical path; the engine being busy with synthesis did not queue the probe
behind it (requests are accepted concurrently; `/speakers` stayed at a few ms).

## Root cause

Call chain: `RuntimeHealthSupervisor.check` -> `VoicevoxOwnedManagedRuntime.health`
-> `LinuxVoicevoxOwnedProcessControl.inspect` / `endpoint_owner` ->
`_read_owner` -> `_process_socket_inodes`:

```
for entry in Path(f"/proc/{pid}/fd").iterdir():      # a snapshot of the names
    link = os.readlink(entry)                        # entry may be gone now
```

Every HTTP request opens a new engine-side socket (the repository's client sends
`Connection: close`, so one connection per request). The listing is a snapshot, the
reads happen later, and a closed descriptor gives `FileNotFoundError`. That was
caught by `except (OSError, ValueError): _fail()`, so the whole identity scan
reported "owned process unavailable". `health()` caught it as a generic exception
(`voicevox_probe_failed`), the provider latched FAILED, and `check` logged
`health_not_ready`.

Evidence:

* All captured latches under the real supervisor but one (the 1.001 s timeout below) had `voicevox_probe_failed` and
  a probe time of 10 to 82 ms.
* A direct test (four threads opening and closing connections to the engine, two
  threads scanning): of 5,529 scan loops, the fd scan raised in 2,420; the
  listener scan (`/proc/net/tcp`) never failed. After the fix: 0 of 4,705.
* The hazard follows request rate, not host load. It never occurred idle (190 probes)
  and it did occur with no contention at all. This also explains #317's pattern:
  it appeared in hot loops and in a Control restart (bursty retries) and not in a
  12 s Control stall.
* Fixed code: 10 minutes supervised at load 4 with 0 latches, where the old code
  latched three times in 50 seconds.

Why the e2e run missed it: the provider's reason is dropped by the supervisor, and
the run was explained as "probably host contention".

## Fix (implemented, separate commit)

`_process_socket_inodes` now skips a descriptor whose `readlink` raises
`FileNotFoundError`. Strictness is unchanged:

* the listener's socket inode must still be among the process's descriptors (a
  closed or foreign listener is still "not owned" and still latches promptly);
* a missing `/proc/<pid>/fd` (the process is gone), `EACCES`, a malformed link
  and every other error still fail;
* no retry, no longer budget, and no behaviour change anywhere else.

Tests added to `tests/test_voicevox_linux_process.py`: a vanishing descriptor is
tolerated (fails on the old code), a vanishing listener descriptor is still not
owned (fails on the old code), a dead process and `PermissionError` still fail.

**Digest consequence.** `voicevox_linux_process.py` is part of the adapter bundle
hashed by `voicevox_manifest._installed_adapter_digest`. This change therefore
changes the adapter artifact digest and the deployment revision. Serving manifests
and validation evidence generated for the previous bundle must be regenerated, and
under `docs/design/voicevox-explicit-worker-manifest.md` a changed adapter bundle
needs a new reviewed serving revision.

## Remaining risk and options (not implemented)

The second mode, one hung probe request, and the unbounded cost of a 1.0 s budget
under severe contention are independent of the fix. Observed: once as a 5.0 s
timeout in a 150 s run, once as a 1.001 s timeout under CPU, disk and memory
contention (the latter only in a run that did not save per-step data). Both
latched a healthy engine, and the next probe would have succeeded.

| Option | Effect | Cost and risk |
| --- | --- | --- |
| A. Log the provider's reason in `check` (`health.detail`) | makes the next G1 diagnosable; no behaviour change | one log field, must stay redacted (the detail is a fixed code) |
| B. N consecutive **timeouts** before latching (for example 3), identity and foreign failures still latch at once | removes the single-hung-request latch; a dead or foreign process is still caught by the identity checks | worst-case detection of a truly hung engine grows to about N probes; needs a counter in the provider, so the adapter digest changes again |
| C. Separate liveness from readiness: process identity every probe, HTTP `/version` and `/speakers` only every few seconds, never on the failure latch | cuts probe cost to about 6 ms and removes both modes | larger design change; `/speakers` catalog drift is detected later |
| D. Larger or adaptive timeout (2 to 3 s) | covers contention spikes | slower detection of real hangs; does not cover a request that never answers |
| E. Read `/proc/net/tcp` once per probe instead of four times | about 70 % of the idle cost | the before/after identity bracket around the HTTP calls would be weaker |
| F. Process-liveness only for FAILED | no healthy-engine latches | gives up detection of an engine that is alive but not serving or changed |

Recommendation: keep the implemented fix (the clear bug), add **A** now (diagnosis),
and decide **B** after one production-LXC trace; do not adopt D or F. B keeps every
strict identity property because only timeouts are tolerated.

## What was not established

* The production LXC (own network namespace, smaller `/proc/net/tcp`, real
  systemd units with `Restart=`) was not measured; the race exists there as well
  because it is in the repository code, but its rate depends on the request rate.
* The cause of the single hung `/speakers` request. It happened once in about
  3,000 probes and did not reproduce in 24 cold starts. Proposed instrumentation
  for the LXC: log, per probe, the duration of each step and the provider code,
  and on a timeout record the engine's socket state (`ss -tni` for the port).
* Two of about 30 engine starts failed with `voicevox_foreign_endpoint` before the
  first ready probe (startup, not the supervised probe); that is the launcher
  re-exec behaviour already tracked as D1 in the real-engine note and is unrelated
  to this change.
* The full Worker plus Control plus PostgreSQL stack was not rerun; the runtime,
  engine, process control, HTTP transport and supervisor were the real ones.
