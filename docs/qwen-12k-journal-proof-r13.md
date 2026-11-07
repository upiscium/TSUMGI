# TSUMGI #230 R13 — source-only journal proof, not a production receipt

Status: source-only Draft PR #234; no live service impact. This is read-only recovery of the previously authorized November 6 R8 native Qwen 12K canary journal. The canonical raw projection is retained privately on Adam, not in Git.

## Recovered original Sirius source evidence

journalctl -u tsumgi-qwen12k-canary.service -o json over 2026-11-06 13:16:30–13:21:00 JST returned **93 events**. The canonical minimal projection of timestamp, boot, unit, InvocationID, PID, identifier and message has SHA-256:

    c19e6634a0c85d3584f79d5271165ef9b1186c9e6eaadde646aec13942dea453

Boot ID: a012a42b086646daa4a4728819ee6427. Native InvocationID: fd06e43150b445998ee259334f93845f. Native PID: 11364.

That exact PID and InvocationID logged model loading with n_ctx_slot=12288, prompt eval of 7,841 tokens, and then prompt eval of 10,052 tokens. PID1 logged Start/Stop for the exact temporary unit, **but nonzero exit status 143 / result exit-code**. STOPPED does NOT attest a graceful shutdown or GPU release.

This corroborates the [R8 report](https://github.com/upiscium/TSUMGI/issues/230#issuecomment-6676882528) independently of its Issue comment text. The 98 NVML samples and 4,212/3,207 MiB minimum GPU free values, Tool/Streaming success, and 8K rollback are still based on that earlier report, not proven by this journal. Nor does the journal attest exact model bytes. A local journal digest is not a cryptographic origin signature.

## Pure offline validation

tools/qwen_12k_journal_proof.py accepts a PRIVATE, canonical event projection and an independently acquired SHA. It checks a single boot, a single Native PID/InvocationID, known Unit and message identifier, bounded source, no duplicate/missing markers and proper start–context–model–7841–10052–stop order. It returns source_authenticated=false, independent_nvml_receipt_verified=false, gpu_release_proven=false and gateway_serving_qualified=false.

Command (replace private journal path):

    python3 tools/qwen_12k_journal_proof.py \
      --journal-json /private/canary-journal.json \
      --expected-sha256 c19e6634a0c85d3584f79d5271165ef9b1186c9e6eaadde646aec13942dea453

R12 tools/qwen_12k_revision_bundle.py additionally accepts an optional PAIRED --journal-json and --journal-expected-sha256. It attaches verified-shape journal provenance to the reported Native evidence hash while retaining the REVIEW_REQUIRED_NOT_PRODUCTION_QUALIFIED status. A missing/mismatched pair fails closed. Without a journal, the older report-only R12 behavior still exists.

With real 8K Sirius/Control source manifests and the PROVISIONAL reconstructed R8 record, journal-bound proposed identities are:

- Deployment: sha256:1a42ba7b5da1372bacbd82169f0721ead105a7c8ced55a955dedd364ccc18ffa
- Serving Contract: sha256:7ad46a2ec1c5364d3e0f55979dd8b9fa760127ba6dd1c2ae6230e4245359fa6a
- Logical Profile: sha256:8f71ab54f3e59924e5c7e1b09352243b0b9ebdff14d3adfa0bad0ad314e6f576

**These are private REVIEW_ONLY proposals, not deployable attested digests.** R13's extra evidence changes the Serving Contract and Profile but not the Deployment revision. Do not commit any raw logs, private manifests or provisional receipts.

## Minimum remaining production gates

#240 R5c is paused as directed. For #230 12K, first recheck exact 8K source/model/runtime, zero nonterminal Jobs, and protected 8K rollback. A reversible maintenance cutover must prove the old 8K Native and descendants absent before launching the new 12K Worker; any identity/ownership ambiguity aborts. Then qualify actual 12K Worker/Control/Gateway/Client revisions, peak per-GPU free VRAM >=2 GiB, auth, strict token bounds, Chat, Tool, Stream, cancellation and rollback. Until this acceptance, do not Ready/Merge/Deploy or claim that #240's lifecycle P1 issues are resolved.
