# TSUMGI #215 B5b — VOICEVOX owned ManagedRuntime, R1 source-only

**Status:** synthetic source contract. No shared RuntimeProvider registration, executable VOICEVOX transport/controller, engine/model installation, actual voice-license authorization, host/GPU operation, production serving, or AC-AUDIO-02 acceptance.

## Relation to existing contracts

- Main at 20c1b064 includes #214 B5a: bare executor.factory is rejected for audio Workers before GPU/Control startup. Worker accepts audio only through reviewed runtime.manifest + serving.manifest, exact READY/ready=True, and an independent fail-closed RuntimeHealthSupervisor.
- #212 provides VoicevoxExecutor and injected engine interface, pinned alias -> speaker UUID -> **global numeric** style ID, exact audio.synthesize, output and artifact limits. Base executor intentionally has **no** serving advertisement.
- R1 adds only provider-local voicevox_managed.py and synthetic tests. It is not in the shared registry: real audio runtime manifests still fail closed pending #180 integration.

## Operator boundary and verification limits

VoicevoxOwnedRuntimeSpec binds the provider-local adapter to one ServingDeploymentDeclaration: engine/adapter/model artifact digests, openai-audio-speech-v1, matching semantic revision and exactly ja/wav, fixed loopback endpoint, process UID and Worker resource identifier, approved alias set, and an operator **declaration** of voice-rights review digest. Declared license IDs or digest-shaped review strings do **not** prove legal authorization.

The runtime hashes two explicit local artifact files with regular-file, no-symlink-component and streamed SHA-256 checks **before** launch. These are point-in-time read-only checks; they cannot prove byte identity at exec or that VOICEVOX loaded precisely those model bytes. A production controller must bind inspected file descriptors/process identity and the complete model/voice inventory to the launched engine.

The injected VoicevoxOwnedProcessControl must inspect **actual** OS ownership, PID, resource owner, deployment revision, launch nonce and listener PID; stop/reap only the exact process it launched. The fake controller demonstrates denial behavior but does not establish OS process/socket authenticity. The runtime rejects an occupied endpoint before launch, rechecks process ownership around probes and fails closed on process, listener, version, UUID or numeric style changes.

The injected VoicevoxManagedEngineApi uses a bounded_json method for /version and /speakers only. The runtime sets max byte limits, checks returned body lengths **before JSON parsing**, rejects duplicate keys and malformed payloads, and bounds the entire probe by a deadline. A production HTTP implementation must enforce byte caps **during reception**, bind all calls to the owned listener, redact or disable logs of native POST /audio_query URLs containing raw source text, never log tokens/paths/engine exceptions, and prevent any nonloopback access. Such a real HTTP transport is **not** implemented in R1.

## State and authority

start() checks artifacts and endpoint, generates a fresh nonce, launches once and reports READY only after observing same-generation process ownership, exact engine version and complete /speakers selection. Every subsequent health probe repeats these checks. Failure latches FAILED, making executor() unavailable and preventing implicit recovery. stop() revokes new admission before process cleanup, cancels and bounds joining active executor tasks, and makes previously cached executors reject new jobs. A generation-bound media wrapper rejects late stage receipts after a stop or health failure, including if an external transport suppressed cancellation (uncertain uploads are not rolled back). stop() and release() are idempotent on the normal path, addressing only the recorded nonce. Timeout/cancellation request owned cleanup; failure of an injected controller to reap is **not** proof of process cleanup.

Only the READY-gated runtime.executor() yields the internal wrapper with serving_features ja/wav. Existing VoicevoxExecutor remains unchanged and unqualified. Worker lifecycle and supervisor retain registration/admission authority. This provider-local source R1 cannot serve in production.

## Tests and remaining gates

Synthetic tests use temporary mock file bytes, fake engine and process observations: valid same-deployment admission, artifact mismatches/symlinks, endpoint collision, wrong UID/PID/model/nonce, version and speaker drift, duplicate global styles, bounded probes, cancellation, timeout, supervisor latch, and owned cleanup. No real TTS/HTTP/GPU evidence is claimed.

Before #215 can be operationally complete: independently review and implement a real bounded/redacted HTTP transport and OS-verifying/reaping controller; pin and verify executable plus full model artifacts and voice rights; prove actual voice/language and Japanese quality/latency; then separately integrate #180 Registry and deterministic Nix/systemd SetupPlan and perform authorized real #181 E2E. This R1 does not close #215/#176/#175/#180 or authorize any release/deployment.
