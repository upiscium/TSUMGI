# TSUMGI #232 — R21 real TCP disconnect Cancellation acceptance

**2026-11-10 bounded physical Spica-only lab PASS + original8K restore PASS.**
**NOT production12K Serving, shared PostgreSQL Control, client publication,
independent runtime/host attestation, or after-first-SSE-token acceptance.**
Issue #232 remains OPEN and PR #250 Draft/nonclosing.

## Historical R20 and R21 preparation

The first physical R20 trial had actual 12K Managed Worker registration/Ready
but HTTP503 while observing the Cancellation stage; its root-only receipt
records FAIL + exact8K rollback PASS, not a Cancellation acceptance.
The callback that produced 503 was not independently identified.

An independent OpenCode source review of R20/R21 identified two P1:
ambient environment proxy could redirect private localhost Bearer/body to
a foreign host, and there was no R21-specific operator runbook. It also
identified two P2: the delayed final readback did not recheck actual
Native slot idle / Worker health, and final R21 Operator SHA was not pinned.

All four source findings were addressed before physical R21:
- explicit urllib ProxyHandler({}) and redirect rejection in both
  root operator and independent TCP client child; exact loopback
  host/port/path allowlist, no fallback to environment proxy;
- R21 standalone runbook with bounded 20min Control, unique R21 units,
  780s independent 8K rollback watchdog and exact source pin checks;
- second stable Job CANCELLED + Native Idle + Worker /health readback;
  HTTP503 Native /slots remains UNKNOWN, never processed/idle proof;
- exact final R21 generated Operator SHA pin and synthetic regressions.

Independent OpenCode *static source review* after these corrections
reported **no P0/P1/P2 remaining** in its specified source/test/doc
scope. It did not authenticate the real host or approve production.

R21 Operator was generated from the exact reviewed R17 and R20 helper
sources, and checked by SHA256 before use:
- final generated root Operator SHA256:
  `56286b2c1ccc91a4c2207b76739057d2fba0215e8bf72ba6eb9704d51e0aa988`;
- pinned R20 Cancellation helper SHA256:
  `dfe4fab2b251106c7002b77486e685edf5fa7b6dfc369ecdbdb30c4dae0b426d`.

R21 used new private root/staging/service names and two fresh distinct
256-bit lab bearer credentials; R20/R19 and earlier receipts were retained.
Its lab Control used localhost 127.0.0.1:19090, no shared PostgreSQL,
the sole lab Worker bonsai-spica-r17-lab, the sole lab model
bonsai2-27b-12k-lab, Native port 18085 and health port 19100.
The Control was bounded by RuntimeMaxSec=20min; the Operator preflight
confirmed at least 1,160s remaining without stopping Spica8K.

## Actual R21 physical sequence

- Shared production Control read-only preflight: Spica/Deneb ONLINE,
  active_jobs=0, zero nonterminal Jobs. Original Spica8K Native context
  8192; all old runtime/Serving/Worker backup hashes pinned.
- Independent 780s restoration timer armed before original Spica8K stop.
  Original Native/cgroup/GPU owners were released (observed free 11164MiB).
- Experimental Spica Managed Worker registered on isolated Control,
  Ready, Native context12288, with observed lab runtime epoch
  `252a0a3a-3219-46f1-9e5a-7a2e176ea5fa`.
- A child sent its exact 47,522-byte serialized streaming HTTP body to
  the isolated loopback Gateway through a direct proxy-disabled opener.
  Operator observed a single RUNNING lab Job claimed with matching
  Deployment/Contract/epoch and Native is_processing=true before
  killing that client subprocess (actual TCP close; no cancellation
  endpoint shortcut).
- After disconnect the **same** Job became durably CANCELLED,
  assigned_worker_id null and Worker active_jobs=0. The Native slot
  returned is_processing=false on actual HTTP200, and Worker /health
  reported ready=true and no active_job_id. After a 1.5s hold, the
  terminal Job + Native idle + Worker Ready conditions were all
  revalidated; no 503 was treated as success.
- Lab-only cancellation status:
  `ISOLATED_LAB_REAL_TCP_DISCONNECT_CANCELLED`.
- Dedicated temporary Worker/Native stopped; GPU owner cleared.
  Original Spica8K Worker restored, Ready with native n_ctx8192,
  cgroup ownership rechecked, original file hashes unchanged.
  Independent rollback timer disarmed only after verified restore
  and did not itself fire. Isolated Control stopped; ports freed.

## Receipt facts, scope, and boundaries

**Final root-only physical result:**
`ISOLATED_LAB_REAL_TCP_DISCONNECT_CANCELLED_8K_RESTORED`,
Operator exit0. Exact root-only R21 JSON receipt SHA256:
`536603d3c363ae023c8b4ad33a7aee1f3c6a57fc51e6f6f67b431ce1bc65e25e`.

The actual serialized sender body was stored privately as
`/root/tsumgi-bonsai12k-serving-r21-run/actual-sender-http-body.bin`,
length **47,522** bytes and SHA256:
`84842dae483e64657199243c60ed821e91a951eb05b99c629d5ac909c50947e6`.
Only its digest and bounded metadata are reported; the body/private bearer
were not uploaded to GitHub. This is **sender-side HTTP body bytes**, not
full HTTP headers, on-the-wire framing or independently attested transport.

NVML reports **960 sampled observations**, minimum free **3,359MiB**
(above the 2,048MiB floor), maximum sample gap **120.44ms**, no
safety abort. No continuous/free-VRAM, full GPU/driver identity or
independently authenticated source origin is established by this trace.

Original8K file digests remained:
- Runtime: `f0c20685800548151197c13b9e9418f29f62e4739fffb72c66545bb3379a4623`;
- Serving: `6cad463e2e07c41850db29aeb6afc53e24b54932190472ecf41040049f84d01b`;
- Worker: `16f12f70e52e5de84280ee44123b8a07e76282df8ada516f26f68b16199c6f6d`.

Posttest production Control read-only confirmed Spica/Deneb ONLINE,
active_jobs=0, no nonterminal Jobs; Control Ready HTTP200, Catalog
contains only existing qwen3.8-27b and bonsai2-27b. Deneb native
context8192 and Sirius Qwen native context12288 remained unchanged.

## Remaining distinct production gates

The R21 trial specifically proves **claimed in-flight prefill TCP
disconnect -> lab Job CANCELLED -> Native/Worker quiescence and
original8K restoration**. It does NOT prove cancellation after model
SSE content has already started, concurrent-job isolation, production
PostgreSQL Control, complete trusted process/cgroup/GPU/loaded-library
provenance, full HTTP wire bytes, independently authenticated runtime
epoch uniqueness, or production Serving validation-evidence SHA.

Before production opt-in bonsai2-27b-12k publish:
1. Independently authenticated process/model/GPU/source evidence and
   real path-neutral Deployment/Serving identity must be reviewed.
2. Distinct after-first-SSE-token cancellation, old8K/wrong epoch
   rejection, over-budget and post-disconnect Job cleanup should
   receive explicit negative live acceptance.
3. PostgreSQL Control/Gateway/Client actual12K serving and rollback
   needs a separately approved bounded production maintenance window.
4. Publish opt-in 12K under its own model ID while preserving old
   bonsai2-27b (8K) and Deneb8K; never silently fallback 12K to 8K.

No production Catalog/Client/Serving promotion, Ready/Merge/Deploy, or
model replacement is implied by R21's isolated PASS.
