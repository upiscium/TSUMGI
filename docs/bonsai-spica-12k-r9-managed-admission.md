# TSUMGI #232 — R9 synthetic managed admission boundary

**Scope:** source-only test/design. No live service change, Worker stop, GPU access,
Control catalog change, release or Ready/Merge. R9 uses an in-memory Control and
fake revision digests; it does not attest the R6B physical measurements.

## Immutable separation

- Original Bonsai profile **bonsai2-27b** remains 8K (6,144 input / 2,048
  output / 8,192 total / 65,536 request bytes).
- Production Qwen profile **qwen3.8-27b** remains 12K and unchanged.
- New Bonsai **bonsai2-27b-12k** is a separate opt-in profile with 10,240
  input / 2,048 output / 12,288 total / 65,536 request bytes. No silent
  alias migration or silent fallback to an 8K Worker.
- Deneb retains only its original 8K Deployment/Serving identities.
  Spica's original 8K Runtime setup intent contains Deneb's GPU UUID.
  The 12K candidate corrects the proposed Spica GPU, preserving all original
  8K rollback source bytes unchanged.

## What the R9 synthetic tests prove

The new synthetic-only test module, tests/test_bonsai_12k_r9_managed_admission.py,
builds an in-memory schema-valid DeploymentIdentity, ServingContract and
LogicalServingProfile from deliberately fake digests (sha256:aaaa... and
sha256:bbbb...). No test object is written as a deployable manifest.

- The candidate catalog preserves both pre-existing Qwen12K and Bonsai8K
  rows exactly, and appends only the explicit Bonsai12K profile.
- Both the Deployment and Contract revisions differ from existing 8K.
  Reattaching any old 8K Deployment, Contract or Profile fails parsing.
  R0 Serving/Control PENDING placeholders are still invalid.
- All four 12K limit boundaries admit the exact maximum and reject
  maximum + 1 in the actual TSUMGI domain resolution function. This is
  NOT proof of HTTP Gateway behavior or Native tokenizer semantics.
- With old Spica8K and Deneb8K advertisements alone, new 12K-bound
  admission raises NoCompatibleDeployment and creates zero durable Jobs.
- When an entirely synthetic 12K Worker is advertised with an exact
  new revision pair and new RuntimeInstance epoch, it alone can claim
  the corresponding synthetic Job. Stale epochs are rejected.
  Conversely, the synthetic 12K Worker cannot steal an old 8K Job.
- With the **same synthetic bonsai-spica ID**, active 8K Jobs prevent a
  changed Serving re-registration. Only after the old in-memory Job is
  cancelled does Control accept the new 12K revision and epoch, reject
  the stale epoch, and route a new 12K-bound Job to the updated Worker.
  Deneb8K cannot claim that new Job. This remains solely a Control test.

**Physical limitation:** Control permits same-ID re-registration of a
different Serving revision when active Jobs are zero. This does not prove
that the previous Native child, GPU device handles or cgroup are gone,
nor does a point-in-time zero-job count implement an admission fence.
R9 exercises both a different fake Worker ID and an in-memory same-ID
transition; neither executes or authorizes physical Spica reuse.
Actual Spica reuse must be separately gated before re-registration.

## Unresolved real managed-12K acceptance gates

1. **Independent live-source provenance:** preserve and recheck protected
   original Spica Worker/Runtime/Serving/systemd byte hashes, current
   Control Catalog and all three Nix GCroots. Independently verify
   complete GGUF SHA/size, actual loaded runtime binary, adapter and
   template bytes. File references and self-reported hashes alone are
   not signatures or independent attestation.

2. **Canonical execution configuration identity:** review a stable,
   secret-free, path-neutral digest of actual runtime/model/binary/adapter/
   template versions, GPU topology, one-slot settings, and the corrected
   Spica GPU UUID, with context_size=12,288 in runtime provider_config
   and setup intent. Do not hash private host-local paths or credentials.
   Validate that every proposed digest represents actual approved bytes.

3. **Separate physical maintenance approval:** stop admissions and drain
   only bonsai-spica; verify no active/nonterminal bound Jobs before
   any Worker stop. Verify the original Native process and all owned
   descendants, cgroup, FDs and NVML GPU compute owners are absent
   before another Native launch. Keep Deneb8K, Control and Qwen12K live.

4. **New independently traceable managed native evidence:** record the
   actual raw HTTP request bytes, hashes and responses; re-tokenize
   on the real 12K Runtime, qualify cold 10,231-token Prefill,
   structured Tool Call and Tool Result, streaming, cancellation and
   Native slot quiescence. Timestamp and associate each sampled
   GPU-local NVML reading with Spica GPU UUID and Prefill windows.
   Abort below 2,048 MiB free, on CUDA/OOM, or uncertain GPU owner.
   The earlier R6B evidence lacks captured actual request bodies and
   GPU UUID/Prefill stage binding; no retroactive reconstruction
   can authenticate evidence that was never collected.

5. **Reviewed Serving and runtime identity:** independently inspect
   authentic evidence and execution receipts before finalizing
   DeploymentIdentity.revision, ServingContract.revision,
   LogicalServingProfile.revision and fresh per-start
   RuntimeInstance.epoch. A schema-valid fake SHA from R9 is NOT
   production evidence. R0/R8 PENDING placeholders remain invalid.

6. **Mandatory actual 8K restoration:** stop temporary 12K Native,
   restore exact original Spica 8K Worker files/process and verify
   /ready, /slots n_ctx=8192, original Serving digest, old GPU ownership,
   Deneb8K readiness, Control catalog digest and zero nonterminal Jobs.
   Only disarm the fail-safe after positive postcondition readback.
   Host-local backups are not cross-host atomic or off-host recovery.

7. **Later, separately approved opt-in publication:** only after managed
   Spica12K acceptance, authorize any shared Control restart and the
   new opt-in profile. Verify actual Gateway pre-job input/output/
   total/request-byte enforcement and real Tool/Stream/Cancel E2E.
   A 12K request must fail explicitly if no matching Worker exists,
   never transparently fall back to Deneb8K or old Spica8K.
   OpenCode client advertising follows verified Control admission.

## Decision

R9 is a **synthetic Control isolation and epoch proof**, not managed-12K
qualification, live GPU or Gateway testing, executable provenance or
production approval. Keep #232 OPEN and PR #250 Draft/nonclosing,
preserve Deneb8K/Qwen12K/Control, and require new operator approval
for any physical Spica cutover, catalog promotion or Ready/Merge.
