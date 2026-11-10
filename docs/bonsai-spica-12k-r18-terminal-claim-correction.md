# TSUMGI #232 — R18 terminal Job Claim reconciliation

**Scope:** source/test/docs-only correction for the isolated Control/Gateway
12K canary. The physical R18 attempt on 2026-11-10 was **FAIL** under its
strict operator assertion, followed by **PASS original 8K restoration**.
Do not relabel it as a full 12K Gateway acceptance.

## What the actual Spica R18 trial established

The R18 test used a unique, root-owned Spica work directory and staging
directory, fresh lab Worker/Client tokens, isolated loopback InMemoryControl
on port 19090 with an exact 20-minute RuntimeMaxSec, an R18 lab Worker ID
and the independent original-8K restoration watchdog.

The actual 12K Managed Worker became Ready with a fresh RuntimeInstance
epoch and a distinct lab-only Deployment revision, Native n_ctx=12288,
and a measured 3,396MiB free near readiness.

Its **first real Gateway plain Chat succeeded** (HTTP 200, nonempty
response). The actual InMemory Control Job had terminal status succeeded,
Serving bound, and claimed deployment revision, Serving contract revision
and runtime instance epoch matching the only admitted lab Worker. Yet the
R17-derived operator failed with the reported reason
`Blocked:lab-job-claim-or-epoch-not-verified`. Consequently,
Streaming, long-prefill and Tool Result through the lab Gateway were
**not executed** in this attempt.

The sampled minimum free VRAM was 3,383MiB, max sample gap 147.3ms.
The old8K Worker was restored, n_ctx=8192 and original Worker/Runtime/
Serving file hashes were byte-identical; the recovery timer was disarmed.
R18 host-local receipt SHA256:
`4596d686c4ebafabf1bd2864b34f24694987f0b6457f257f6cf76d2fc747c2b0`.
These are host-local, not independent cryptographic attestations.

## Exact mismatch identified

TSUMGI's InMemoryControlRepository.complete_job() changes a RUNNING Job
to SUCCEEDED and **clears assigned_worker_id and the transient lease**.
It preserves claimed_deployment_revision, claimed_serving_contract_revision
and claimed_runtime_instance_epoch. The old R17 operator incorrectly
expected assigned_worker_id to remain equal to the lab Worker *after*
completion. The actual R18 Control status returned assigned_worker_id=null
and matching claimed revisions/epoch: correct **terminal** semantics,
not a failed Job Claim.

The corrected R17 lab operator now requires, for every completed Job:
- exactly the sole expected lab Worker, online, with Serving advertised;
- terminal status succeeded, Serving bound, and assigned_worker_id=null;
- claimed Deployment revision, Serving contract revision and Runtime epoch
  equal to the registered lab Worker and the staged lab Serving revision.

It rejects stale/foreign revision and epoch, remaining lease ownership
on a completed Job, failed or nonterminal Jobs, or an offline/non-serving
Worker. New isolated R18 regression tests cover all cases.
This proves a bounded in-memory lab Claim relationship through revision/
epoch checks; direct independent capture of physical Claim timestamps and
operator identity is an **additional** provenance gate.

## Next

Use a fresh, private and independently reviewed follow-on Spica-only
lab Control window with 20-minute bounded lifetime and new lab tokens,
distinct Worker/Control units and prepared state, the 780-second
independent recovery watchdog, active-job admission checks and exact
restoration of original Spica8K. Repeat actual Gateway plain Chat, SSE
stream, >10K native-token-equivalent input and Tool Call/Tool Result as
separate Job phases. Do not count unexecuted phases as PASS.

This change **does not** issue a production Serving identity, alter shared
Control/PostgreSQL/Catalog/Client, or approve the public opt-in Bonsai12K
model. Issue #232 remains OPEN, Draft PR #250 remains unmerged.

## Subsequent bounded trial

The separate R19 isolated-Control physical trial subsequently PASSED
all four Gateway stages, five terminal Job Claim checks and original8K
restoration. See `docs/bonsai-spica-12k-r19-isolated-managed-gateway-acceptance.md`.
R18's own FAIL/rollback receipt is preserved and not rewritten.
