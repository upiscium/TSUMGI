# TSUMGI #214 — B5a managed-only audio registration gate

**Status:** source-only daemon policy and synthetic tests. No real TTS/STT
runtime, model, installed process, package, deployed service, or operational
acceptance is supplied by this change.

## Problem and scope

The Worker already validates that each advertised audio capability has exactly
one reviewed operation schema, and that an executor has an async
`execute_audio()` method and declares sufficient serving features. These are
**structural** checks. A generic `executor.factory` creates no
`RuntimeLifecycleManager` or `RuntimeHealthSupervisor`, and
`WorkerRuntime.runtime_available` historically allows a missing supervisor
for generic/non-audio workloads. A custom audio factory could claim `ja/wav`
without having a running engine. The #176/#177 source-only adapters deliberately
avoid their own `serving_features` declaration, but that does not protect the
Worker against arbitrary future factories.

**One narrow rule:** if the configured Worker capabilities contain
`audio.synthesize` or `audio.transcribe`, reject the bare `executor.factory`
path. A reviewed `runtime.manifest` and existing `serving.manifest` are
required. This takes effect after the existing serving schema check and before
GPU preflight, creating a Control HTTP client, resolving a runtime/provider,
starting any service or registering with Control. Failure uses a fixed,
credential-redacted message. A mixed audio + non-audio Worker is covered by
this same rule. Non-audio `executor.factory` behavior remains unchanged.

This does **not** make arbitrary `runtime.manifest` configurations valid or
introduce a generalized module/ownership/security framework. An audio provider
not yet present in the reviewed runtime registry still fails closed.

## Managed readiness consistency

In the existing generic lifecycle, `ensure_ready()` can return an inconsistent
provider health snapshot with `ready=True` but a non-READY state. The daemon
now additionally requires, **for audio only**, an exact
`RuntimeHealth(state=RuntimeHealthState.READY, ready=True)` from the managed
lifecycle before obtaining the executor or generating the audio serving
epoch/registration. STOPPED, STARTING, DEGRADED, FAILED, `ready=1`, missing or
foreign health objects are rejected. The existing
`RuntimeHealthSupervisor` continues its own independent health probe before
Worker registration, latches health loss and suspends later admission.

The new check only verifies **internal state consistency**; it cannot by
itself establish process ownership, artifact digest validation, installed
model or voice rights. A malicious or noncompliant provider may still report
false READY. Issue #215 must implement real VOICEVOX ownership/voice/version
verification, and #216 must own a cancellable/reapable Style-Bert inference
process. No automatic audio capability registration is enabled by B5a.

## Synthetic source-only verification

Tests cover TTS, STT, dual-operation and mixed-capability Worker settings:
- Exact valid serving manifests **do not** waive the managed-only requirement.
  Early failures observe **zero** calls to GPU preflight, ControlClient,
  executor factory/import, runtime provider setup, Worker registration or
  claim. Error messages contain no configured endpoint or credentials.
- Missing/wrong/ambiguous audio serving operation schemas still fail before
  side effects using the pre-existing policy; no broadening of acceptance.
- Non-audio `executor.factory` remains functional and prior unit behavior is
  preserved. Existing managed Worker + real generic lifecycle/supervisor with
  fake owned health can reach the test registration boundary.
- Contradictory or fabricated managed health fails before audio Executor
  retrieval and registration. The independently checked capability/feature
  gates still apply to the managed path.

No model downloads, process launch, real HTTP, GPU, PostgreSQL, file setup,
NixOS/generic-systemd deployment, migration, or release. Source-only gate
checks are not real TTS/STT E2E evidence. PR #210, #212 and #213 are separate
workstreams; no provider registry changes or new SetupPlan actions are in
scope here. #175/#176/#177/#180 remain OPEN. B5a Ready/Merge requires its own
Human authorization, independent of prior merge approval for #210/#212/#213.
