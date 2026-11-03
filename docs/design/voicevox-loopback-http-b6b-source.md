# TSUMGI #215 / #180 — B6b bounded, redacted VOICEVOX HTTP (source-only)

**Status:** Provider-local native HTTP transport source and disposable loopback wire tests, not real VOICEVOX deployment, process ownership, real model/voice readiness, license acceptance or worker registration. No actual engine or live user traffic was contacted.

## Native endpoint contract

The module voicevox_http.py adds VoicevoxLoopbackHttpApi implementing the previously injected VoicevoxEngineApi and VoicevoxManagedEngineApi method shapes: version(), speakers(), bounded_json(path, max_bytes), audio_query(text, speaker), synthesis(query, speaker) and close().

VOICEVOX upstream documents GET /version, GET /speakers, native POST /audio_query?text=...&speaker=<numeric-style-id> and POST /synthesis?speaker=<numeric-style-id> with JSON query body, returning a WAV. The selector is a **global numeric style ID**, not a UUID: the B5b owned runtime must independently verify the speaker UUID and uniqueness of styles before admitting the Worker. Reference: https://github.com/VOICEVOX/voicevox_engine/blob/master/README.md

This source implementation intentionally does **not** use a general-purpose HTTP client. A small bounded HTTP/1.1 client uses only asyncio.open_connection to the literal operator-declared 127.0.0.1:<high-port> address, ignores all proxy environment variables, and has no request/URL logger. It does not support arbitrary domains, redirects, cookies, auth, relative targets, proxies, optional native/mutable endpoints or any retry. A new TCP connection is used for each operation; async timeouts and caller cancellation close its writer. Client-side exceptions are always a fixed value-redacted VoicevoxHttpError (or propagate asyncio.CancelledError).

Response parsing accepts only status 200, the expected exact JSON or audio/wav media type, uncompressed bodies, one unambiguous Content-Length or strict chunked transfer, or EOF-delimited bodies. Header size and line length are finite. It validates Content-Length **before** allocation and caps actual response bytes during read, including chunked and missing-Length cases. Version probes are capped at 256 bytes, speaker probes at 256 KiB, engine audio-query JSON at the pinned adapter limit, and WAV at the pinned adapter max output. Duplicate JSON fields, non-JSON constants, invalid UTF-8, bad status, wrong MIME, response redirects, malformed chunk size, excessive lengths, unknown endpoints and version/voice mismatches fail closed. The existing B5b adapter is still the authority on canonical PCM16 WAV, duration, voice UUID and globally unique style ID; it also stages a bounded ArtifactRef through the Worker media context.

## Non-negotiable privacy and readiness gates

**VOICEVOX native /audio_query places raw input text in URL query parameters.** This source code does not log that URL, but an upstream web server/proxy/access-log component could. Operators must independently prove the exact launched engine has HTTP/access logs **disabled or redacted** for text/query parameters (including exception diagnostics, tracing and packet capture policy) before permitting any real user text. There is no generic way for a bare HTTP client to prove that the server is not logging requests.

VoicevoxHttpConfig and successful HTTP probes are not evidence of **ownership** of the local port or of a reviewed executable/model. A malicious/foreign listener on 127.0.0.1 could still answer. The existing VoicevoxOwnedManagedRuntime needs a separately reviewed OS-validating VoicevoxOwnedProcessControl with exact launch nonce, executable/model artifact and listener PID ownership, plus voice/rights qualification, before binding this transport to an actual Worker. Merely constructing this HTTP transport does **not** register anything; it is intentionally not exported from shared provider registry/Nix setup. License IDs and SHA-256-shaped declarations remain declarations, not verified rights or process identities.

The special VOICEVOX /cancellable_synthesis endpoint exists upstream but is experimental and opt-in; this implementation explicitly uses /synthesis without claiming computation is stopped when HTTP cancellation closes a socket. The B5b Worker/Control attempt fencing still owns no-retry and no-terminal-success behavior for abandoned synthesis.

## Synthetic evidence, not operational qualification

tests/test_voicevox_http.py starts only an ephemeral **fake** asyncio TCP server on 127.0.0.1. It checks actual outbound HTTP target shape and JSON/WAV response handling, native endpoint restrictions, decoded Japanese text vs posted raw body, no proxy use, streaming/chunked/EOF bounds, wrong version/voice/PCM16, malformed or oversized responses, redirect/error rejection, timeout/cancellation and absence of logs emitted by this transport. One additional composition test wires the real synthetic TCP transport into VoicevoxOwnedManagedRuntime with a deliberately fake process observer and validates READY/Job→WAV without a real engine or ownership proof. Tests do not run the real VOICEVOX engine, GPU, actual speaker assets, a true process owner or upstream access logging.

## Remaining gates

1. Independently review the source and tests plus exact GitHub PR-head CI; no Ready/Merge without explicit Human approval.
2. Implement/review a real owned engine process controller and exact launched executable/model/socket verification; demonstrate native engine log redaction and model/license/voice rights on an isolated test environment.
3. Only after B6a Provider Binding and real engine process attestation are approved may #180 add executable RuntimeProvider Registry + pinned NixOS/generic-systemd closure, deterministic SetupPlan/rollback, and real #181 Japanese TTS E2E. #178→#179 STT selection remains separate.

No engine/model downloads, host/GPU changes, database, service changes, release, deployment or production acceptance authorized by this source-only transport.
