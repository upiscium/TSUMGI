# TSUMGI #247 — Local VOICEVOX preview (rapid E2E vertical slice)

This is a separate, **explicitly opted-in localhost development/operator preview**, not production TSUMGI voice serving. It verifies **HTTP text → user-managed VOICEVOX → bounded PCM16 WAV → HTTP audio/wav** without changing Control, PostgreSQL, Worker, Job lease, Registry, or the existing production speech route.

Disabled by default. TSUMGI does not install, launch, manage, restart, kill, upgrade or download VOICEVOX. The operator installs and starts an engine **in the same network namespace** as this separate preview process.

## Local preview recipe

First review the engine's license/voice terms and access logging. The native VOICEVOX /audio_query path includes **raw input text in its URL query**, which the engine/host/proxy may log. The explicit acknowledgement flags below acknowledge this risk; they do not certify engine authenticity, license, voice rights or safe logging. Use non-sensitive demonstration text only.

1. Install TSUMGI's Python package in an isolated environment, and separately run your own VOICEVOX at the normal 127.0.0.1:50021 address. No VOICEVOX installation is performed by TSUMGI.
2. List observed voices using: python -m astrumweaver.audio.voicevox_local_preview --list-voices. Copy a speaker_uuid and style_id from the output. A discovery observation is not a serving grant.
3. Set TSUMGI_VOICEVOX_PREVIEW_TOKEN to a long private random token using a secure local environment or secret injection (do not put credentials in source or shell history).
4. Run the dedicated preview with the observed values:

    python -m astrumweaver.audio.voicevox_local_preview --speaker-uuid SPEAKER_UUID --style-id STYLE_ID --accept-unverified-engine --accept-native-text-url-logging

    The server only binds 127.0.0.1:9811. Do not publish through NPM, a reverse proxy, a tunnel or a production service. For an alternative already-running engine port, specify --endpoint http://127.0.0.1:PORT (same namespace).
5. Send a non-sensitive Japanese test sentence:

    curl --fail-with-body --silent --show-error -H "Authorization: Bearer $TSUMGI_VOICEVOX_PREVIEW_TOKEN" -H 'Content-Type: application/json' --data '{"input":"こんにちは。音声の接続テストです。"}' http://127.0.0.1:9811/preview/audio/speech --output ./voicevox-preview.wav

The preview discovers the engine on every request using bounded /version and /speakers checks, ensures the operator-selected speaker UUID/style ID remains present, then sends bounded /audio_query and /synthesis POST requests, validates PCM16 WAV, and returns audio/wav. GET /preview/audio/voices, with the same bearer token, shows the observed voice IDs with ready=false and control_authoritative=false. Request bodies, native errors and secrets are not included in public error text.

## Guarantees / outstanding safety gates

- Bearer authentication, explicit operator risk acceptance, fixed IPv4 loopback authority, bounded ingress/response sizes, single simultaneous request, total deadline, no redirects, retries, environment proxy or client-supplied native URLs, no Uvicorn access logs, Cache-Control: no-store.
- This is a **standalone developer/operator HTTP-to-WAV proof**. It does **not** implement production Control/PostgreSQL durability, Job/Lease/Epoch fencing, authenticated Worker provenance, retry reconciliation, artifact persistence or OpenAI-compatible production /v1/audio/speech binding. It does not register/activate any RuntimeProvider.
- Engine observations are not cryptographic engine identity, trusted model provenance or voice licensing/credit verification. Explicit operator acknowledgement is not objective evidence.
- The WAV validator intentionally accepts a conservative RIFF PCM16 subset; extra native RIFF chunks or unsupported sample rates may be rejected.
- Synthetic loopback engine tests prove the wire path in GitHub CI only; they do not prove the user's real engine works.
- R4-C2-B/C, R4-C3/C4 and R4-D remain separate implementation, integration, rights and privacy review gates.

**G-AUDIO-OPS=STOP for production.** Keep Issues #247/#215/#180 OPEN. No production Deploy/Activation or real Control/DB/Worker changes are authorized by this source/test/docs-only preview.
