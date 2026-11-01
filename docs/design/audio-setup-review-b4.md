# TSUMGI #180 Phase B4 — Audio Setup Review (source-only)

**Status:** informational source API; no runnable audio SetupPlan or backend
installer exists. This is the *review boundary*, not deployment acceptance,
setup authorization, or a runtime/provider implementation.

## Why a separate record?

The existing `SetupPlan` represents actionable runtime changes and may include
package installation, model downloads/conversions and `RUNTIME_START`. The
current LLM-specific planner also renders Worker execution capabilities for
text-generation workloads. Reusing it for VOICEVOX, Style-Bert-VITS2 or an
**unselected** STT backend would make inappropriate assumptions about what is
installed, license-qualified and ready. An unverified provider **must not**
produce an apparently executable `SetupPlan`.

`astrumweaver.setup.review_audio_setup` instead accepts only previously
constructed in-memory, validated objects: `AudioProfileCatalog`, a
`ServingDeploymentDeclaration`, `WorkerSpec`, and a `SetupHostSnapshot`, plus an
**explicit** public profile ID and `enabled=True`. It checks:

1. Exactly one selected, existing logical audio profile; no fallback to another
   profile/engine or implicit provider selection
2. Exact declared deployment and single serving contract, including its
   operation schema, semantic voice/profile revision, artifact digests and
   effective limits already checked by the existing catalog/serving validators
3. Exact single audio capability claimed by the Worker; unrelated non-audio
   capability strings are left unchanged (but their coexistence is **not**
   certified or configured by this review)
4. Declared CPU/GPU counts, target deployment path and public voice aliases
   as **declarations**, not actual observed machine resources or license grants

A missing, ambiguous or mismatched input raises one fixed, value-redacted
`AudioSetupReviewError`. A disabled request (`enabled=False`) returns a stable
`disabled` review without touching provider, catalog or host inputs.

## Safe output and remaining blockers

Enabled output is always `review_only_blocked` / `can_apply=False`. It has a
canonical SHA-256 digest of a bounded, allowlisted JSON representation. It
contains only the public profile/provider/backend/capability, pinned logical-profile, deployment,
contract and model digests, NixOS/generic-systemd path type, declared CPU/GPU/RAM
counts, public voice alias IDs and **fixed** remaining review checks. Hostnames,
GPU UUIDs, raw source config, local file paths, tokens and private recordings
are excluded. The digest is an immutable *record fingerprint*, not a verified
artifact checksum, approval token or signed attestation.

The remaining checks deliberately never auto-pass:

- Independently qualify exact runtime/adapter versions and package/ABI on the
  intended host; verify model artifact bytes, trust sources and runtime health
- Check each voice/model license, attribution and usage terms (catalog metadata
  alone cannot authorize usage)
- Confirm GPU UUID isolation/VRAM, CPU/RAM and the actual NixOS/generic-systemd
  service user, secret-file paths and private audio spool directory permissions
- Obtain separate maintainer authorization to download/convert anything, apply
  a `SetupPlan`, operate a test host, perform model E2E or deploy to production
- For model-STT, retain a fixed `stt_model_selection_pending` gate until #178
  produces evidence and the maintainer explicitly selects a model/runtime and
  thresholds; **#179 remains unimplemented until that selection**

```python
from astrumweaver.setup import review_audio_setup

review = review_audio_setup(
    enabled=True,
    profile_id="explicit-reviewed-audio-profile",
    catalog=operator_catalog,
    declaration=operator_serving_declaration,
    worker=operator_worker_spec,
    snapshot=operator_host_snapshot,
)
assert review.can_apply is False
print(review.to_json())  # redacted, deterministic, review-only
```

An actual supported audio installation **still requires** provider-specific
#176/#177 (and #179 after #178), a reviewed mapping from pinned Provider
runtime/model/voice identities to existing SetupActionDrivers, an approved
isolated-host smoke and rollback plan. No application mechanism consumes or
accepts this `AudioSetupReview`, and this phase must not extend the existing
SetupActionDriver to treat a review record as executable.

## Verification boundary

Tests use synthetic profile/provider/license identities, fake declared host
snapshots, and pure object construction. They cover both deployment paths,
explicit selection, contract/deployment/Worker mismatch rejection, one-vs-dual
audio capabilities, STT selection blockers, disabled no-inspection behavior,
allowlisted JSON, deterministic digest and no secret/path leak. No file,
network, host GPU or model operation is performed; exact pinned digests are
**declared** evidence, not the result of reading model bytes.

This deliberately does not satisfy AC-AUDIO-07 operational acceptance, actual
VOICEVOX/Style-Bert model inference, Japanese STT accuracy or #174 post-crash
artifact GC. #180/#175 remain OPEN and production/host G-AUDIO-OPS remains STOP.
