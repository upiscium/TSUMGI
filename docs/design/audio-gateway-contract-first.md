# Audio Gateway: artifact-independent contract-first slice

Tracking: #175; TSUMGI-AUDIO-FC-001/r0, DES-AUDIO-002, AC-AUDIO-01 and
AC-AUDIO-05. Workstream: `TSUMGI-W1-20261028-ISSUE175-R2`.
Execution packet SHA256:
`bb7dfae7c9011e6768a18356c659fc94e5da37247194f2af3e163d2c97bfdc36`.
Base: `98d4f0c631e2acc18985f4b206bc1b1d1c80d0f9`.

## Scope and non-claims

`astrumweaver.gateway.audio` is a pure configuration/catalog/validation module.
It imports no audio artifact, Control, Worker, transport or runtime modules.
It adds **no HTTP endpoints**, multipart parser, binary transfer, model loading,
runtime discovery, Job submission, admission, lease, claim or persistence code.
Passing these tests is not completion of #175, AC-AUDIO-01/05, an audio API,
license verification, or TTS/STT end-to-end acceptance. Existing non-audio
Gateway code and the audio artifact implementation remain unchanged.

The feature-contract/design identifiers above are traceability labels supplied
by the execution packet, not a claim that external specifications were fetched
or independently verified. The versioned schemas below define this slice only.

## Reviewed immutable configuration

`AudioGatewayProfile` wraps an existing `ResolvedServingProfile`. Public `model`
is **exactly** `LogicalServingProfile.profile_id`; it is not a runtime model name
or an arbitrary provider alias. The existing serving layer pins deployment,
runtime/adapter/model/execution-configuration digests, contract, evidence and
effective limits. Profile construction revalidates that binding. A digest is a
declared provenance reference, not proof of artifact availability or readiness.

Backend kinds are explicitly `voicevox`, `model-tts`, and `model-stt`. The
`voicevox` kind requires logical provider ID `voicevox`; model-TTS and model-STT
require a different explicit provider ID. This is a configuration convention,
not implemented adapter support. A catalog rejects a model-TTS entry sharing
VOICEVOX's model artifact digest, even if its provider has been relabeled.
Deployment/provider/model selection is never inferred or substituted.

Each TTS profile contains a nonempty tuple of reviewed `VoiceAlias` records:
`alias`, backend `speaker_id`, backend `style_id`, `locale=ja`, `license_id`, and
public-safe `attribution`. IDs are exact bounded identifiers; numeric backend
IDs use decimal strings. Attribution is nonblank, valid UTF-8, at most 2048
bytes, and contains no ASCII control characters. Operator approval and actual
license permissions must be established outside this module. There are no real
voice names, production license declarations, downloaded artifacts or default
catalogs in this slice. Tests use synthetic non-private values only.

`audio_semantic_revision(kind, voices)` produces a canonical, schema-separated
SHA256 digest. Set `ServingContract.semantic_revision` to it **before** pinning
the logical profile to the contract and resolving it. The digest binds backend
kind, ordered-by-alias voice mappings, Japanese locale/language, output format,
license ID and attribution. Deployment identity is independently pinned by the
containing contract. Changed semantics require a new reviewed contract/profile
revision; mere input order of aliases does not. STT has no voices.

Catalog schema: `audio-gateway-catalog-v1`. `to_dict` returns detached data;
`from_dict` rejects unknown top-level, profile, voice and serving identity fields,
unknown versions, duplicate profile IDs/voice aliases and malformed bindings.
Catalogs/profiles/voices are frozen; collections are defensively copied. Catalog
and voice ordering is canonical. No URLs or runtime discovery options are
accepted. Alias lookup is profile-local: the same alias may deliberately have
different reviewed mappings in different profiles, but cannot cause fallback.

## Speech JSON contract

Operation schema: `openai-audio-speech-v1`; capability: `audio.synthesize`.
The serving contract must declare `ja` and `wav` features. Exactly these
effective limits are required, as positive non-boolean signed-64-bit integers:

* `input_bytes`: inclusive upper bound on the original input's UTF-8 bytes;
* `request_bytes`: inclusive raw JSON body limit, greater than `input_bytes` to
  leave envelope space (the caller must provision adequate actual overhead).

`validate_speech_request(body, catalog, request_size_bytes=...)` accepts only a
decoded JSON object containing all four fields:

```json
{"model":"reviewed-profile-id","input":"日本語のテキスト","voice":"reviewed-alias","response_format":"wav"}
```

No defaults: missing fields, unknown fields (including backend URLs, speed,
streaming and profile overrides), invalid IDs, unknown model, STT model on the
speech path, unknown/model-mismatched voice and non-`wav` codecs fail closed.
Input must be a nonblank string and strictly UTF-8 encodable. A cheap code-point
bound precedes encoding, then the UTF-8 byte bound is checked; multibyte input
cannot evade it. Input is preserved exactly, with no trimming or normalization.
The caller-supplied measured request size must be a positive integer within the
profile bound and cannot be smaller than the input's encoded size.

The result carries the exact profile/binding and selected voice mapping for
later #176 adapter work. It is not an executable Job or artifact payload schema.
There is no attempt, retry, fallback or backend invocation policy here.

## Transcription metadata only

Operation schema: `openai-audio-transcription-metadata-v1`; capability:
`audio.transcribe`. This identifier intentionally does not claim a complete
multipart transcription protocol. The serving contract must declare `ja` and
`json` features and exactly `file_bytes`, `metadata_bytes`, `request_bytes`
limits. All are positive non-boolean signed-64-bit integers; file plus metadata
bounds must be smaller than the request bound, reserving envelope space.

`validate_transcription_metadata(body, catalog, metadata_size_bytes=...)` accepts
only the extracted metadata fields `model`, `language=ja`, and
`response_format=json`, all required. It checks the measured metadata byte bound
and returns the selected STT profile and immutable future transport limits.
File content, URLs, filenames, prompts, codec overrides and all other fields are
rejected. No file, multipart body, codec, duration or audio content is validated
or accepted by this function. It is metadata validation only, not evidence of a
bounded/valid transcription upload.

## Errors, integration obligations and dependencies

`AudioGatewayError` exposes `code`, `field_name`, `status_code` and `to_dict()`.
Messages contain only fixed codes and field names: unknown keys, private input,
voice/provider values, URLs and nested configuration exceptions are not echoed.
Request errors use 400, unknown model 404, oversized data 413, and invalid
operator configuration 500. These are future mapping hints, not live responses.

Future ingress must enforce byte caps **before** buffering/JSON decoding, reject
duplicate JSON/multipart field names (decoded dicts cannot detect duplicates),
and supply measured sizes rather than client assertions. The metadata parser
must bound all parts, part count, headers and aggregate request/file bytes. #177
must add codec/container, decoding, duration and model-specific checks before
claiming transcription support. A valid configuration snapshot is not admission
authority; later work must preserve all serving bindings at admission/execution.

Full #175 depends on #174's artifact/transport integration and later #176/#177
adapters. Those dependencies are reported rather than changing `audio/*`,
Control/Worker/DB code, live Gateway APIs, setup or schemas/migrations here.
No unresolved human-owned runtime/model/license selection is made by this
slice: production configuration and approvals remain intentionally absent.

## Offline evidence boundary

`tests/test_audio_gateway_contracts.py` covers immutable catalog round trips,
explicit selection/no fallback, pin and semantic mismatches, malformed limits,
strict fields, model/voice mismatches, formats, empty/overlong/malformed UTF-8,
multibyte limits and the separate bounded metadata contract. Run it with the
existing chat/embedding/decision and serving contract tests, using the existing
project harness. No network install, database, runtime, GPU, production or real
voice/audio data is required. Test commands/results and frozen file/diff SHA256
evidence are supplied in the execution report, not claimed as E2E evidence.
