# TSUMGI #230 R12 — Offline 12K revision proposal (non-deployable)

Status: **source-only**, successor to R11 in [Draft PR #234](https://github.com/upiscium/TSUMGI/pull/234). The original Sirius 8K Qwen Worker/Control remain unchanged. This is a proof shape and revision calculator, not an authenticated canary, a safe native-stop implementation, or permission to switch production.

## Purpose

R11 exercised the correct TSUMGI DeploymentIdentity, ServingContract and LogicalServingProfile classes, but used artificial SHA-shaped digests in tests. R12 adds tools/qwen_12k_revision_bundle.py, which accepts independently reviewed-shaped 12K execution and native canary records, computes their canonical SHA-256 digests, and builds an internally aligned real-domain 12K Runtime/Serving/Control proposal **without hardcoded fake digests**.

The tool explicitly reports REVIEW_REQUIRED_NOT_PRODUCTION_QUALIFIED, reviewed_receipts_authenticated=false and kernel_lifecycle_or_gateway_accepted=false, even when all schema checks succeed. The provided records can be fabricated by the caller; hashing them is *not* authentication. Operator review of original evidence and model/runtime bytes remains indispensable.

## Inputs and constraints

Use the same four independent 8K sources as qwen_12k_candidate.py: Sirius Runtime manifest, Sirius Serving manifest, complete Control Qwen+Bonsai catalog and the separately reviewed original Qwen model/reference/revision pin. On top of those, provide **two different** evidence documents.

Execution review JSON (schema qwen12k-reviewed-execution-shape-v1) has exactly the fields schema_version, profile_id, baseline_deployment_revision, model_artifact_sha256, context_size, parallel_slots, gpu_uuids and runtime_options. Context size must be 12288, parallel_slots must be 1, gpu_uuids must identify two distinct GPUs. runtime_options must match the path-free 12K candidate provider properties (context_size, gpu_layers, split_mode, tensor_split, offline, no_webui, and supported optional fit / jinja).

Native canary JSON (schema qwen12k-native-canary-shape-v1) has exactly schema_version, source_run, execution_config_sha256, model_artifact_sha256, native_context_size, max_input_tokens, sample_count, gpu_min_free_mib, chat_ok, tool_ok, stream_ok, rollback8k_ok. The source_run must reference a TSUMGI #230 issue comment; execution SHA must equal the digest of the execution review; native context size 12288; accepted input >=10052 and <=10240; samples >=20; each exact GPU UUID >=2048 MiB; and booleans strictly true.

These are a **conservative structured preflight** for the previous R8 native-only observation, not a claim that the memory figures were remeasured today. Do not set any boolean or measurement to success based only on a template, guess or unconfirmed reviewer claim. Confirm GPU UUID mapping, pinned GGUF, exact runtime executable/arguments and raw source provenance separately.

## CLI: inspection only, then optional private review files

Normal invocation performs no writes:

    PYTHONPATH=src:tools python3 tools/qwen_12k_revision_bundle.py \
      --runtime /private/8k/runtime.json \
      --serving /private/8k/serving.json \
      --control /private/8k/chat-catalog.json \
      --reviewed-pin /private/reviewed-qwen-baseline-pin.json \
      --execution-review /private/reviewed-12k-execution.json \
      --native-canary-record /private/reviewed-12k-canary.json

To produce review-only JSON, add --output-dir /tmp/tsumgi-qwen12k-review-UNIQUE, pointing to a NEW direct child of /tmp. This creates only a 0700 directory with four 0600 files: runtime.REVIEW_ONLY.json, serving.REVIEW_ONLY.json, control.REVIEW_ONLY.json and review-gates.json. No in-place overwrite or production path is accepted. Model reference paths may be sensitive; never commit generated files, attach them to public CI or print their contents. An existing output directory or unsupported location fails closed.

There is no socket, GPU/device operation, systemd control, PostgreSQL, Worker integration or release/rollback command. The proposed Serving/Control JSON is schema-valid, but not trusted production admission because the evidence is supplied by a caller; the REVIEW_ONLY filenames and review-gates.json must remain attached until separate human acceptance.

## R8 evidence and remaining launch gates

The authorized R8 native-only 12K canary reported 10,052 accepted input tokens, 98 sampled GPU free-memory observations, minimum free 4,212/3,207 MiB, Chat/Tool/Streaming pass and restoration of 8K. See [#230 R8](https://github.com/upiscium/TSUMGI/issues/230#issuecomment-6676882528). That issue comment is a **report**, not cryptographically authenticated original logs, a bound execution-config SHA receipt, or a 12K Control/Gateway job.

Before production cutover, require independent confirmation of exact reviewed execution config and original native evidence, coherent 12K Worker+Control revisions, drain of old Jobs, **absence of the previous 8K Native process before loading 12K**, reversibility with protected 8K source/GCroot, and new 12K peak VRAM and Gateway/auth/over-limit/Tool/stream/cancellation acceptance. If stopping Native or reconciling a revision becomes ambiguous, abort and restore/retain 8K. Do not reopen #240 R5c merely because this proof-shape tool was added, and do not claim #240's known shutdown risks are resolved.

No Ready/Merge/deploy, systemd/GPU/Control/DB/Nix profile changes or 12K promotion follow merely from passing offline tests.
