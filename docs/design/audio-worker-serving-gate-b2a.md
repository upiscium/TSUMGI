# Worker audio serving gate — #180 Phase B2a (source-only)

## Status and scope

This is a minimal provider-independent startup configuration gate, **necessary
but not sufficient for provider readiness**. A `worker.capabilities` entry alone
must not let the Worker advertise `audio.synthesize` or `audio.transcribe`
without corresponding reviewed serving declarations. Passing this gate proves
only the required declaration shape and operation-schema match; it does not
qualify a provider or attest that its evidence is genuine.

The source-only scope is limited to `src/astrumweaver/worker/daemon.py`, the new
`tests/test_audio_worker_serving_gate.py`, and this document. No shared Nix,
Control, setup, provider or executor sources are changed. PR #204 (Phase B1)
remains a separate OPEN Draft; this phase neither edits nor merges it.

## Fail-closed rule and ordering

After the existing serving-manifest loading and WorkerSpec construction,
`run_worker` checks every advertised audio capability:

| Advertised capability | Required pinned operation schema from `gateway.audio` |
| --- | --- |
| `audio.synthesize` | `SPEECH_OPERATION_SCHEMA` (`openai-audio-speech-v1`) |
| `audio.transcribe` | `TRANSCRIPTION_OPERATION_SCHEMA` (`openai-audio-transcription-metadata-v1`) |

- A serving declaration is mandatory if either capability is present.
- For **each** advertised audio capability there must be **exactly one** contract
  with that capability, and its operation schema must equal the matching
  constant. A schema under a different capability does not count. An extra
  contract for the same capability with a different schema is ambiguous and
  rejected, even if another contract has the expected schema.
- A dual-audio spec requires both contracts. A speech-only contract does not
  qualify transcription, nor vice versa.
- The existing `ServingDeploymentDeclaration` loader validates the contract's
  deployment binding and serialized contract revision. This gate reuses those
  pinned values; it does not generate or repair manifests, evidence, revisions,
  semantic identities or capabilities.
- Rejection occurs before GPU preflight, Control client construction, runtime
  manifest loading/creation/start, executor construction, or Worker registration.
  The existing configuration and Worker-token checks still apply.
- Without either audio capability the new check is a no-op. Unrelated
  capabilities, labels and the existing non-audio registration paths are neither
  rejected nor mutated by this gate. Existing serving-capability subset checks
  and other startup constraints continue to apply independently.

New gate failures use only fixed text, with no model/voice IDs, tokens, paths,
provider names or manifest values:

```text
audio capabilities require a reviewed serving manifest
audio capability requires exactly one matching serving operation contract
```

Malformed serving manifests continue to fail through the existing loader. This
change does not redefine unrelated startup errors.

## Readiness and architecture boundaries

The gate does not identify a provider from its operation schema, whitelist a
provider, infer model/voice quality, verify licenses or artifacts, validate actual
audio semantics, select an STT implementation, or create a model backend or
provider runtime. The declaration's evidence and artifact digests are references,
not evidence verification performed here. Actual adapter qualification remains
with #176/#177/#179 and their separately reviewed evidence and lifecycle work.

The existing optional runtime `ensure_ready` lifecycle, executor capability
validation, executor `serving_features` validation, serving advertisement
construction and Worker registration/readiness logic remain unchanged and
required after the new preflight. Correct declarations do not bypass them or
make an unsupported adapter executable. The new check itself creates no runtime
instance epoch, grants no readiness and performs no provider invocation.

This gate is specifically a `run_worker` startup boundary, not a redesign of
Control registration authorization or an attestation mechanism for arbitrary
external registrants. There is no architecture blocker for this bounded
configuration prerequisite. Treating contract presence as actual provider
readiness would exceed its architecture and must remain blocked pending real
adapter qualification.

## Synthetic test coverage and verification limits

The new tests follow the existing pinned serving-declaration fixture pattern and
exercise the actual daemon entrypoint with temporary synthetic manifests and
scoped monkeypatches. They cover missing manifests on executor and runtime paths,
missing/wrong/ambiguous operation contracts, dual-audio specs with only one
contract, correct pinned single/dual declarations across synthetic provider IDs,
mixed and non-audio capability preservation, and unchanged executor capability,
serving-feature and runtime-readiness rejection gates.

Successful-path tests stop at a fake Control client's registration boundary via
a sentinel exception; no registration is sent or acknowledged. Real provider
creation and host discovery are forbidden by default in the fixture, GPU
preflight and health serving are replaced, executor construction is synthetic,
and the readiness test uses only a failing synthetic lifecycle. No model is
loaded and no provider request is made.

Coordinator verification on the isolated Adam Nix development environment:
focused audio serving gate / existing serving tests **45 PASS**, and expanded
Worker/runtime/serving/Audio Gateway regression **271 PASS / 0 FAIL**.
An independent read-only OpenCode reviewer found no concrete new P1/P2 defects.
These are synthetic source/runtime tests, not a real provider, Control registration,
model, GPU, deployment or production acceptance. No model download, host change,
service operation, release or merge was performed.
