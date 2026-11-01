# TSUMGI #176 — VOICEVOX pinned TTS adapter: isolated source slice

**Status:** Provider-local adapter code and synthetic tests only, not a real
engine/voice implementation, supported VOICEVOX package, Worker registration,
model qualification or operational acceptance.

## Scope

`runtime/providers/voicevox.py` implements `AudioJobExecutor` and the
generic `JobExecutor` methods with an **injected trusted async** VOICEVOX
engine protocol. It does not import, install, discover, launch, proxy or reach
a real VOICEVOX endpoint and is **not exported in the runtime provider
registry**. The separately reviewed WorkerRuntime (#180 B3b) owns Job
attempts, heartbeats, cancellation, serving epoch and media transport.

Exact `VoicevoxAdapterConfig` holds declared engine version, engine/adapter/
model artifact SHA-256 identity references, frozen serving semantic revision,
and a bounded allowlist of public voice alias -> engine speaker UUID + numeric
style ID, with public voice license and attribution fields. These values are
operator **declarations**. A digest-looking value is not verification of the
binary or a grant to use a VOICEVOX character. No deployment identity or actual
engine health is attested by constructor success.

The single audio operation accepts the already validated Gateway Job shape:
`audio.synthesize`, exactly `input`, `voice` (alias/speaker_id/style_id)
and `response_format=wav`, plus a matching serving semantic revision. Unknown
fields, voice overrides, text size violations, missing identity, incompatible
engine version/voice UUID/style, malformed or structurally oversized audio-query
object and binary audio are rejected before output staging. The numeric style
selector must occur exactly once across the full returned speakers list,
because engine audio_query/synthesis requests carry only style ID, not UUID.
Any exception type from the engine, even a misleading retryable JobExecutionError
containing secrets, is sanitized into a fixed nonretryable public error. The engine mock protocol represents:

1. Check the exact engine version and `/speakers` UUID/style list.
2. Create an `/audio_query` for the **pinned numeric style ID**.
3. Call `/synthesis` with the returned bounded query and same style ID.
4. Require canonical PCM16 WAV, 8–48 kHz, mono/stereo, duration/output limits.
5. Return exactly one `ArtifactRef` from `AudioJobMedia.stage_output`.
   Only public-safe numeric metadata is included in `JobResult`, never raw
   text, WAV bytes, model paths, or tokens.

All provider errors use fixed nonretryable `JobExecutionError` messages and
do not include engine exceptions. A single Job may be in flight; cancellation
requests cancellation of the async task, **not** proof that an engine process
terminated. A failed or uncertain stage cannot be retried automatically.
WorkerRuntime B3b independently remembers media failures, and Control owns
the durable publish/lease/epoch checks.

## Remaining implementation/operations gates

- A separate reviewed engine **ManagedRuntime** must bind a private/loopback
  endpoint, exact engine executable checksum/version, health and speaker UUID/
  style availability **before** allowing a Worker to advertise capabilities.
  An `executor.factory` alone does not prove readiness or safety.
- Native VOICEVOX `POST /audio_query` transports source text in the URL
  query parameters; a stock HTTP process/access log can record that text.
  HTTP client/network implementation, server log redaction, cancellation and
  timeout behavior, CORS/exposure and bounded body transport remain separate.
- Each voice library/character's rules and attribution require explicit
  review, **independent** from the engine software license. A populated
  `license_id` alone does not approve use.
- VOICEVOX WAV output may contain extra RIFF chunks or a different codec.
  The current adapter rejects anything outside TSUMGI's canonical 44-byte
  PCM16 WAV Gateway subset; any reviewed re-encoding must be another phase.
- RuntimeProvider registry, deterministic executable #180 SetupPlan, NixOS/
  generic-systemd packages, installed voice/engine assets and real
  Control→Worker→WAV acceptance remain **unimplemented**.
- No engine or model downloads, host/GPU changes, deployments, migrations,
  production release, or claim of AC-AUDIO-02 PASS.

Synthetic tests inject a fake async API and a bounded fake media context.
They exercise pinned voice/style, fixed fail-closed errors, damaged WAV,
unavailable engine/voice, cancellation, timeout, duplicate Job and single
artifact staging. The tests do **not** open sockets or run real VOICEVOX.

Upstream API:
https://github.com/VOICEVOX/voicevox_engine/blob/master/README.md

VOICEVOX software and character terms:
https://voicevox.hiroshiba.jp/term/

Review and merge gates remain independent; this source slice does not close
#176, #175, #180, or #174.
