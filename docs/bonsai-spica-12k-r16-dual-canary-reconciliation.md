# TSUMGI #232 — R16 actual Native/Managed 12K receipt reconciliation

**Status: historical test receipts structurally reconciled; production Serving
is not qualified.** This is source/test/docs work on Draft PR #250. It does
not authorize or perform a 12K runtime activation, Control Catalog edit,
Worker registration, release, or Ready/Merge.

## Real site observations (November 9, 2026; read-only recheck)

Two existing root-owned Spica receipt files from separate, bounded physical
Canaries were examined using a byte-for-byte identical copy of the new R16
pure Python validator. The utility did not open a socket, access CUDA,
execute Native, or modify Spica's Worker or configuration. The caller
explicitly read these files in a separate, read-only script:

| Source | Exact file SHA-256 (hex) | Recorded tests | Reported minimum free VRAM |
|---|---|---|---|
| Native R2 | `2cb28007dfb762eeaf1b09f4111c9c0cca3ef43f10350b1fe011d2376bf49cdb` | small_chat, stream, long_safe, long, tool_and_result | 3,359 MiB / 1,604 samples |
| Managed R15 | `a54017f3674590ff03eb39eb63f11d1afff16dfd65dac462b5f3ccd98005490b` | small_chat, stream, long_cold, tool_result | 3,359 MiB / 1,069 samples |

R2's maximum reported sample gap was 105.1ms; R15's was 96.44ms.
The original 8K Worker was restored after each trial. At a separate
read-only site check, Spica and Deneb continued serving 8K, and Control
advertised the existing Qwen12K and Bonsai8K profiles. Neither receipt
records a production 12K Serving advertisement or actual Gateway-to-Managed
12K Job lease/claim.

## What R16 validates

`tools/bonsai_12k_r16_dual_canary_receipts.py` is a **pure, no-I/O**
consistency verifier of caller-supplied bytes and caller-supplied expected
SHA-256 digests. No host paths or private transcript bodies are embedded
in source; no values are signed or promoted.

It explicitly enforces:
- Bounded JSON receipt bodies and different correctly shaped role digests;
  duplicate JSON keys, NaN/Infinity and unexpected schema fields rejected.
- Exact historical Native R2 and Managed R15 schemas and ordered test names,
  the 12,288 context claim and Native test unit name.
- Old GPU release and 8K rollback/completion flags, while **rejecting**
  any claim that Deneb/Control were changed, Serving was issued, HTTP bodies
  were independently attested, or the 12K profile permanently promoted.
- Reported GPU sample count >=64, maximum reported gap <=250ms,
  reported minimum free >=2,048MiB, safety-abort flags false and explicit
  acknowledgement that sampling is **not continuous**.
- A bounded output containing receipt role digests and summarized sample
  metadata only, never full request contents or deployable Serving files.

The validator returns
`NATIVE_AND_MANAGED_SMOKE_INTERNAL_CONSISTENCY_ONLY`. Host
authentication, real HTTP-body capture, 12K Serving/Control Job claim,
Gateway Tool/Streaming/Cancellation E2E, and production authorization
remain **false**.

File SHA-256 equality checks content against a caller-provided expected
digest; they do not establish whether an independent party actually
observed the process, GPU or network action. An operator who can forge
both file and expected digest could pass this parser.

## Concrete implementation gap after physical R15 PASS

GPU sizing and basic Managed Worker/Native process execution are no longer
the first priorities. Production cutover still requires:

1. **Fresh process-bound artifact provenance.** In a separately authorized
   Spica-only maintenance window, independently inspect the *new* Managed
   12K process's Wrapper, Native binary, actually mapped Nix/GGML/CUDA
   libraries, complete dependency closure, pinned GGUF, template/tokenizer
   bytes. Bind to PID/start time/cgroup/argv/Spica GPU UUID/context options.
   R13's 14 SONAMEs were sampled from an older **8K** process, not future
   12K executable attestation.
2. **Sender-side capture and GPU evidence.** Preserve exact serialized
   outgoing HTTP request bytes in a private bounded store, bind SHA to
   Native SSE/token counts, Tool Call ID/Tool Result, client disconnect
   and UUID/window-tagged NVML samples. R6B/R2/R15 summaries cannot
   retroactively recreate missing original byte streams.
3. **Independent authority.** Obtain an explicitly trusted collection
   root; a signature from the same unrestricted host is not independent
   evidence against privileged tampering.
4. **Managed Serving/claim proof.** Only after trusted evidence, derive
   a **real**, path-neutral DeploymentIdentity execution-config SHA,
   ServingContract validation-evidence SHA and fresh RuntimeInstance
   epoch. Test exact 12K Job lease/claim and old 8K/Deneb exclusion.
   Prefer an isolated Control endpoint and **unique experimental Worker ID**
   before live shared-Control re-registration of bonsai-spica.
5. **Separate opt-in publication.** With explicit authorization, stage
   bonsai2-27b-12k in Control and OpenCode, preserving Qwen12K and Bonsai8K.
   Verify actual Gateway limits and Tool/Stream/Cancellation. Maintain
   byte-exact 8K rollback and never infer readiness from fake SHAs.

Issue #232 remains OPEN, PR #250 remains Draft/unmerged, and no production
Serving 12K identity is issued by R16.
