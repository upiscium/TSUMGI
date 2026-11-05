# TSUMGI #235 — Style-Bert-VITS2 B6e provider-local source assembly

**Status: SOURCE-ONLY / REVIEW-ONLY.** B6e is not a RuntimeProvider,
has no shared Registry entry, is not a SetupPlan and is not permission to
enable a Worker. Parent #180; related to #216, #177, #175 and #181.
The B6a binding review and Worker managed-only gate remain intact.

## Declarative configuration contract

StyleBertSourceAssemblyIntent ties one explicit frozen logical profile,
catalog, ServingDeploymentDeclaration, synthesis-only Worker, host snapshot
and StyleBertOwnedRuntimeSpec to the existing B6a review. Frozen fields are
not necessarily a deep immutable snapshot of nested data. Enabled review
revalidates nested model, voice, adapter and runtime constructors to reject
reflective mutation of invalid syntax and normalizes malformed input to a
single fixed, redacted error. All configuration checks are pure and non-I/O.

The review checks exact provider, deployment and semantic revision, model
weight SHA, config.json SHA, style_vectors.npy SHA, interpreter and runner
SHA, runtime/adapter revision, reviewed alias/speaker/style mapping,
model-directory/runner paths, bounded input/WAV/timeout configuration,
Worker resource identity and host RAM. B6a's canonical runtime fingerprint
binds the full private configuration, including native style strings.

A CPU profile requires exactly zero Worker GPUs and no GPU UUID. Declarative
CUDA review requires exactly one selected Worker GPU UUID and requested VRAM
no greater than the declared total/single-device budget. These are declared
values: no real GPU device, CUDA ABI, VRAM availability, loaded-model
identity, driver or process isolation is proven.

Enabled false returns immediately without examining any candidate or
touching filesystem, network, process, GPU, Control or database. Enabled
reviews retain B6a blockers and add model/runner, process/IPC/ABI, rights
and privacy blockers. Always can_apply=False and review_only_blocked;
public reports contain only allowlisted fields and opaque SHA-256 digests.
A digest is not an HMAC, signature, artifact proof, license grant or approval.

## Explicit disposable constructor

compose_disposable_source_runtime re-runs the complete declaration review
against the saved intent and requires identical binding and assembly digests
before constructing StyleBertOwnedManagedRuntime in STOPPED state.
Construction does not call start, launch a child, read a model, open a
socket, perform HTTP or register with Control. The object can only be
started by separately authored disposable synthetic tests.

Revalidation does not provide an atomic, deep-frozen snapshot in the face
of hostile concurrent mutations; do not treat it as a production security
boundary. No shared Worker can construct this assembly from a manifest.

## Synthetic acceptance and negative controls

Tests cover disabled-mode noninspection, public diagnostics redaction,
CPU/single-GPU review, profile/Worker/host mismatch, unexpected capabilities,
provider substitution, wrong GPU UUID/VRAM, malformed private model paths,
weight/config/style hashes, runner hash, revisions, rights and timeouts,
nonfinite values/invalid Unicode and coherent post-review drift.

A real disposable child running only a fake stdlib framed IPC protocol
demonstrates synthetic bootstrap, one exact audio.synthesize Job returning
a PCM16 WAV ArtifactRef, drift in synthetic style-vector bytes after READY,
and cleanup. Wrong child epoch/model probe and cancelled inference must
fail closed without media staging. These tests do not execute native
Style-Bert inference or attest any real model or license.

The executable Registry, runtime.manifest loading, Worker daemon,
Control API, shared SetupPlan, NixOS/generic systemd modules, spool and
deployment are unchanged. There is no substitution to VOICEVOX.

## Mandatory future operator acceptance gates

1. Review the exact native Style-Bert runner implementation, dependency and
   interpreter ABI, artifact path, immutable hashes and package provenance.
2. Independently demonstrate loaded weight/config/style-vector identity,
   actual model inference and bounded readiness/health. A synthetic child
   bootstrap report alone is insufficient.
3. Independently approve model, voice and redistribution license/attribution
   rights. Declared voice_rights_qualified=True does not establish these.
4. Verify source text privacy (not in argv/env/stdout/stderr/public logs),
   process identity, cancellation, child descendants, pidfd/cgroup race,
   Job/attempt/epoch fences, private media, and GPU isolation/resources.
5. Review reversible NixOS and generic systemd SetupPlan using #24/#31;
   run actual #181 E2E only after explicit G-AUDIO-OPS operational approval.

**STOP:** no native engine/model download or dependency install, host/GPU/LXC
mutation, Worker/Control/PostgreSQL configuration changes, provider
Registry activation, Nix/systemd apply, release or deployment. Source-only
implementation is independently granted, Ready/Merge is separately gated.
Keep #235/#180/#216/#177/#175/#181 OPEN pending real acceptance.
