# TSUMGI #232 — R19 bounded Spica-only isolated Managed 12K Gateway acceptance

**Physical outcome on 2026-11-10: PASS, mandatory original8K restore PASS.**
**This is NOT production Serving, PostgreSQL Control, client promotion,
independent physical artifact attestation, or complete Cancellation E2E.**
Issue #232 remains OPEN, PR #250 Draft/nonclosing.

## Why the preceding R18 failed despite a completed Chat

The R18 lab Gateway returned HTTP200 and a terminal SUCCEEDED Control Job
with the lab Deployment/Serving/Runtime epoch claims. Its operator rejected
the Job because complete_job() correctly cleared assigned_worker_id when
releasing the lease on success. R18 was therefore fail-closed and its 8K
rollback succeeded. The corrected R17 operator verifies terminal-null
lease ownership while requiring all claimed revisions and epoch to match
both the one registered lab Worker and the *staged* Serving declaration.
Independent OpenCode plan-agent static re-review found no P0/P1/P2 after
the additional canonical SHA/UUID and staged-Deployment guards.

## R19 unique physical environment and proof of work

A new private operator /root/tsumgi-r19-operator.py was transferred
byte-for-byte from the independently reviewed R17 operator source and
mechanically rebound to unique R19 root/staging/service names.
Original R17/R18 evidence and config were preserved. The new isolated
Control:
- listened only on 127.0.0.1:19090 with a **20min** systemd RuntimeMaxSec;
- used fresh distinct 256-bit lab Worker and Client bearer secrets;
- used the InMemoryControlRepository, not production PostgreSQL;
- admitted only experimental Worker ID bonsai-spica-r17-lab and advertised
  only the distinct bonsai2-27b-12k-lab model, with a 12,288 native context.
- used port 18085 for Native and 19100 for experimental Worker health,
  separate from production ports 8085 and 9100.

The old Spica8K Worker was stopped only after an independent 780-second
8K recovery watchdog was armed and original GPU ownership released.
The experimental Managed12K Worker registered on the isolated Control
and became Ready with a fresh runtime epoch. **All four Gateway stages
completed with real Worker Jobs:**

| Phase | Observation |
|---|---|
| ordinary Chat | valid response through isolated Gateway/Control/Managed12K Native |
| SSE Streaming | content fragments and terminal DONE received |
| long context | historical Native-tokenizer 10,231-token synthetic recipe routed through Gateway; actual completion received |
| Tool Call/Result | correlated synthetic_echo Tool ID, structured Tool Call SSE and final Tool Result SSE both complete |

The historical 10,231-token recipe count was not independently
remeasured and attested from the live R19 Gateway sender. Five
lab-only terminal Jobs were SUCCEEDED and each retained a claim
matching the one registered lab Worker and staged Serving contract:
- lab Deployment SHA: `sha256:f8808fcb348af977e1cef6c058d0ac382c3d6edf00e66e5cee8112d50a3873c1`;
- lab Serving Contract SHA: `sha256:b533f2d15de1f1614452177d87848ded160965198daeabe78cba390460da6f4b`;
- observed RuntimeInstance epoch: `d52ba5b4-c2b1-4f52-8661-e726a6e3a068`.

This epoch differs from the previously observed R18 lab epoch
`56ef5cd8-965f-44b8-a8b0-16a1d7066cb0`, and the R19 isolated Control
was empty before Worker registration. However, the operator validates
canonical nonzero UUID and exact Job/Worker matching; it **does not**
authenticate cryptographic epoch freshness across every historical runtime
instance. The independent source review tracked that limitation as P2.
A production acceptance must verify freshness through a trusted
process-start/Worker registration provenance path.

The lab SHA values are syntactically valid but based on **unattested
lab-only evidence references**, not real production evidence.

## GPU and rollback receipts

- Native context: 12,288 during isolated Worker trial.
- Sampled minimum free VRAM: **3,359 MiB**, floor 2,048MiB.
- Sample count: **1,073**, maximum reported sample gap **87.82ms**.
- These samples do not establish continuous peak/UUID-authenticated proof.
- Experimental Worker and Native stopped; original Spica Worker restored,
  active and Ready at n_ctx=8192 with NRestarts=0.
- Original runtime/Serving/Worker config bytes remained SHA-identical.
- Independent recovery timer was disarmed **after** restoration, without
  the independent watchdog itself firing.
- Experimental Control unit was stopped; lab ports 19090/18085/19100
  released; production Control/DB/Catalog and Deneb8K remained unchanged.
- **Exact R19 root-owned JSON receipt SHA-256 (hex):**
  `8d9dcc5db57500a4745424b407e7e9c5c5d425a030a7997b6f435763bb2919e5`.
  Scope: `ISOLATED_LAB_MANAGED_JOB_AND_GATEWAY_SMOKE_8K_RESTORED` within the
  lab, not production readiness.

## Remaining acceptance gates

1. **Raw request and physical provenance.** Capture exact actual serialized
   sender HTTP request bytes, complete Native/RPC runtime/loaded-library
   and model bytes, concrete process/cgroup/GPU UUID, interval-tagged NVML
   samples, and independently authenticated source/collector identity.
2. **Cancellation and negative admission E2E.** Prove real cancellation,
   disconnected Streaming cleanup and no leaked Job/GPU owner; reject
   wrong revision/epoch, over-budget requests and attempts to route the
   lab 12K profile to Deneb8K. Synthetic-only tests do not replace these.
3. **Production Serving identity.** Derive independently reviewed, genuine
   (not R17 lab) execution configuration and validation-evidence digests,
   then register a new production 12K Serving revision with a fresh
   RuntimeInstance epoch under a separately approved reversible window.
4. **Shared Control and opt-in promotion.** With explicit authorization,
   publish the **separate** bonsai2-27b-12k Control Catalog and OpenCode
   client model; preserve original Bonsai8K/Deneb and SiriusQwen12K.
   Verify PostgreSQL Control/Gateway/Client real Job lifecycle and
   mandatory exact 8K rollback.

Source review, Green CI and a successful isolated R19 trial are
**necessary but insufficient** to certify production Bonsai12K.
