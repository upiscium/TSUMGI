# TSUMGI #232 — R11 path-neutral execution review and provenance contract

**Status: source-only, REVIEW_ONLY / BLOCKED.** This does not authorize a
managed 12K Worker, Control publication, runtime epoch, service restart,
or Serving Contract revision. The 8K rollback, Deneb, and Qwen12K are untouched.

## Why R11 is needed

R6B demonstrated bounded Native-only 12K and actual 8K restoration.
R8 reconciled six saved Native records but did **not** authenticate the
original HTTP request body bytes, NVML GPU UUID/prefill-stage bindings,
or the host/operator that produced those records. R9/R10 established
synthetic-only Control/Gateway revision and epoch fencing, not real
12K Worker admission. In particular, their hashes are artificial.

Before a new execution identity can even be considered, its input must
not conceal an unreviewed llama.cpp option, Deneb GPU rebind, or the
inclusion of host-private paths or secrets.

## Pure source-only execution review

The R11 tool at tools/bonsai_12k_r11_execution_review.py accepts an R0
**unqualified** Spica 12K source candidate and a caller-provided digest
record for five roles: runtime, adapter, model, optional tokenizer and
optional template. It performs **no filesystem/network/GPU/subprocess I/O**.

The reported model digest must match the pinned Bonsai PQ2_0 GGUF
SHA-256. Other role hashes must be syntactically valid. **No artifact
bytes are hashed or verified by this pure function.** A valid digest
claim is not an independently verified observation.

The output is intentionally named a **review fingerprint, not
execution_config_sha256**, with the status
BLOCKED_UNATTESTED_EXECUTION_AND_MANAGED_WORKER. It must never be copied
to DeploymentIdentity.execution_config_sha256 or used to mint
ServingContract.validation_evidence_sha256.

The canonical *review-only* record contains the distinct opt-in
Bonsai12K profile, Spica Worker ID and Spica GPU UUID, one Native slot,
the 10,240 / 2,048 / 12,288 / 65,536 request limits, digest-shaped
artifact claims, and enumerated normalized llama.cpp provider options.
The GPU UUID is intentionally part of the record: path neutrality
does **not** imply cross-host interchangeability.

An independent OpenCode source review identified a P2: the original R11
validator did not check `source_gpu_uuids.spica_runtime_source`. The
corrected source-only gate now requires that legacy source UUID to be
well formed and **either** the currently declared Spica Worker GPU UUID
(already corrected case) **or** the Deneb Worker UUID (the observed
historical Spica 8K setup-intent drift). Its
`legacy_spica_gpu_uuid_corrected_in_draft` flag must agree exactly with
which case occurred. A third GPU, malformed UUID or inconsistent legacy
flag fails closed. The new 12K draft is still required to reference the
Spica Worker GPU only; neither the old 8K source nor rollback material
is modified. This is consistency of *supplied declarations*, not proof
that source UUIDs or their claimed historical provenance are authentic.

Provider/external chat-template file paths, model file paths, and
loopback endpoint addresses/ports are never encoded in the review
fingerprint. Their expected semantics are validated separately; a
future independent collector must bind the *actual* loaded binary,
model and template bytes to those paths/processes. Path changes with
identical reported digests produce an identical review fingerprint,
which is by design **not** source provenance.

### Real 8K structure observed read-only

Read-only inspection of Spica 8K found **23 provider_config keys and
24 setup_intent.configuration keys**, with 20 common keys.
A notable semantic equivalence is provider split_mode=null versus
setup split_mode="none". R11 normalizes only that representation
difference, while rejecting unrecognized options or unapproved
provider/setup divergences. Synthetic tests include the full-shaped
23/24-key inputs, not only the abbreviated R0 fixture. This is a source
structure observation, **not** a managed 12K Worker acceptance.

The pinned Bonsai provider normalizes some caller inputs, notably
turning a Jinja setting into True. R11 explicitly checks the
**raw provider intent before normalization**; otherwise a caller-supplied
jinja=False could be silently replaced and given a misleading
same-configuration fingerprint. New provider field names also force
R11 to fail closed until explicitly reviewed.

### Source-only tests

The new test module tests path-neutrality; complete 23/24-key shape;
frozen context and GPU UUID; changed artifact SHA; invalid pinned
model hash; source/setup drift; bad/unknown fields; altered context,
flavor, Jinja, fit and offline settings; invalid numeric option; and
invalid/unqualified Serving/Control manifests.

Importantly, all provenance authority flags remain false. No
actual managed 12K serving, Control Catalog, new Runtime epoch, evidence
signature, or deployment-ready JSON is emitted.

## Remaining separate physical and authority gates

An independent collector must check actual full GGUF, runtime executable,
adapter and template bytes plus Nix closure, complete loaded Native argv,
exact Worker/Native process ownership, verified Spica GPU mapping and
a trusted origin/temporal receipt. Self-consistency of a local file set
is not a substitute for independent authenticity. These materials have
**not** been produced by R11.

A separately authorized managed canary must capture **actual serialized
HTTP request bodies**, Native tokenizer counts, Tool Call/Result SSE,
cold Prefill, explicit stream cancellation, window-bound NVML samples
with the concrete Spica GPU UUID and free-memory floor >=2,048 MiB,
and independent post-restore checks on Spica8K, Deneb8K and Control.
Prior R6B evidence must not be falsely relabeled as having contained
the missing raw HTTP bodies or UUID-tagged NVML samples.

Any physical trial requires its own approval for admission drain,
zero Spica-bound Jobs, proven old Native/cgroup/FD/NVML GPU-owner release,
bounded fail-safe and **mandatory byte-identical 8K restoration**.
A later shared-Control Catalog restart and opt-in client advertisement
need another separate authorization and independent reviewed evidence.

Keep Issue #232 OPEN and PR #250 Draft/nonclosing. R9/R10 independent
OpenCode CLI review has not completed after recent timeouts, and R11
does not waive that outstanding review gate.
