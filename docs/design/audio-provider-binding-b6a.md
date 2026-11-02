# TSUMGI #180 — B6a audio provider binding: source-only, review-only

**Status:** Source-only, no host/engine/model/package/Registry/SetupPlan mutation. This phase does **not** add VOICEVOX or Style-Bert-VITS2 to the runtime provider registry. The existing runtime.manifest path continues to reject both unregistered audio providers rather than infer qualification from synthetic OwnedManagedRuntime objects.

## Why this phase precedes an executable registry adapter

PRs #220 (VOICEVOX B5b) and #222 (Style-Bert-VITS2 B5c) each implement a provider-local ManagedRuntime source contract. Their fake engine/process and disposable Python child tests established source-level lifecycle behavior, not independently verified VOICEVOX binaries, loaded Style-Bert weights, native voice rights, or installed host dependencies. Registering them as RuntimeProviders without a real process/transport implementation could convert synthetic READY into a public audio-serving claim.

The existing #180 B4 review_audio_setup validates Gateway catalog/profile to immutable ServingDeclaration and Worker, and returns an always-nonapplicable review. B6a adds provider-specific configuration agreement that B4 cannot check. It remains non-actionable and can be used before a separately reviewed real provider implementation.

## Supported source-only binding

The function astrumweaver.setup.review_audio_provider_binding takes enabled, a mandatory explicit catalog profile ID, AudioProfileCatalog, ServingDeploymentDeclaration, one WorkerSpec, an existing SetupHostSnapshot and exactly one provider-local VoicevoxOwnedRuntimeSpec or StyleBertOwnedRuntimeSpec.

- **Disable:** enabled=False returns one constant disabled report without reading optional inputs or doing I/O.
- **Gateway to Serving:** Reuse B4 exact logical Profile revision, deployment, single audio.synthesize / openai-audio-speech-v1 contract and Worker audio-capability preflight. No fallback or automatic model selection.
- **Worker scope:** Require only audio.synthesize capability, not an unsupported mixed non-audio Executor claim. This is narrower than B4 informational Worker view and matches the one-capability B5b/B5c executors.
- **VOICEVOX:** Require actual provider ID voicevox; exact aliases, canonical speaker UUIDs, numeric style IDs, license IDs and public attributions between Gateway and pinned VOICEVOX adapter. Require runtime resource ID to match Worker ID. B5b does not bind an exact GPU UUID; this first integration review allows CPU-only VOICEVOX, rejecting a GPU Worker claim pending a separately qualified GPU ownership design.
- **Style-Bert-VITS2:** Require actual provider ID style-bert-vits2, model-TTS backend, matching Worker and host snapshot, matching alias/speaker ID/style ID. For GPU mode require exactly one Worker-owned GPU UUID with the source spec's VRAM bounds; CPU mode requires a CPU-only Worker. The Gateway has no native Style-Bert style-string field; changes to the native style string are tracked by the provider config digest rather than silently retaining an identical review fingerprint. Specific voice license and attribution still require independent operator confirmation.
- **Bounds:** The Gateway effective input-byte limit must fit the selected provider adapter max_text_bytes. Mismatches reject rather than truncate or route elsewhere. No STT runtime is accepted pending #178 model comparison, Human selection and #179.
- **Immutable review:** The result contains the B4 digest, logical Profile revision, deployment/contract revisions, provider ID, public voice aliases and a SHA-256 fingerprint of the whole declared provider-local runtime spec. This catches changes to runner/binary/model paths, process endpoint, Worker/host facts, native Style-Bert style names, resource assignments and provider limits even when the Gateway logical Profile is unchanged. Private inputs are transiently hashed and never copied into the JSON review. This is **not** a secret-protecting HMAC, proof of actual file bytes, authenticated evidence or authorization.
- **Size/privacy:** Bounded canonical serialization rejects oversized/unknown provider input values. All failures use a fixed redacted error; no raw paths, tokens, transcript, speaker text, GPU UUIDs, worker IDs or private labels appear in public JSON.

AudioProviderBindingReview.can_apply is always false, status is review_only_blocked, and no SetupPlan or RuntimeSetupIntent is issued. All mandatory blockers remain, including real native process/transport qualification, rights/model acceptance, runtime registry integration, real package/ABI and host/GPU preflight, and operator activation approval. The execution_config_sha256 in DeploymentIdentity is still an operator declaration, **not** proof of loaded process state.

## Tests / remaining phase

Synthetic tests cover NixOS and generic-systemd intent as pure metadata (no install), VOICEVOX CPU, Style-Bert CPU/single GPU, exact profile and Worker ownership, speaker/style/rights field mismatches, runtime/Gateway input limits, STT rejection, disabled constant/no-I/O, redacted public outputs, malformed/oversized data, and runtime config/native style fingerprint drift.

Next, under separate source review, implement a **real** VoicevoxProcessControl and bounded/redacted HTTP transport and a native Style-Bert model loader/runner with exact loaded artifact, speaker/style and OS process/resource evidence. Only after evidence and explicit source/operations grants should #180 add executable RuntimeProvider registry construction, reproducible pinned NixOS/generic-systemd package/ABI SetupPlan and rollback, then authorized real #181 E2E on an isolated host.

No model downloads, service starts/stops, GPU/host changes, PostgreSQL, release, deployment, Issue close or PR Ready/Merge are part of B6a.
