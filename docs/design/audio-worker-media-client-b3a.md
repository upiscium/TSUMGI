# TSUMGI #180 B3a — Worker-local bounded audio media transport

**Status:** source-only client transport implementation. This is not a TTS/STT
provider, WorkerExecutor connection, Control deployment, model qualification,
or feature acceptance.

## Problem and ownership

The reviewed Control audio transport (Issues #174/#175) already exposes
Worker-authorized input download and output staging, but the generic Worker
`ControlClient` previously only implemented JSON Job/claim/complete/event RPCs.
`JobExecutor.execute(JobRequest)` does not receive raw WAV, the Worker bearer
token, or the current lease. An adapter cannot safely obtain/commit binary data
by treating the Job's public payload or an arbitrary URL as file authority.

B3a provides two **explicit opt-in** methods on the existing Worker Control
client; neither changes the default registration/heartbeat/non-audio code path:

- `download_audio_input(worker_id, claimed, max_bytes=...)`: accepts only
  `audio.transcribe`, an explicit positive current attempt, nonblank lease and
  serving epoch, plus a bound Job payload `audio_input` reference containing
  the exact opaque URI, `audio/wav`, expected SHA-256 and size. It requests the
  dedicated Control Worker input route using the existing Worker bearer token
  and `x-audio-attempt`, `x-audio-lease-token`, `x-audio-runtime-epoch` headers.
  The response is streamed under a finite Control-client timeout, with an
  explicit caller-supplied byte ceiling **no greater than 16 MiB** and strict
  Content-Length, media-type, size and digest checks. No file path, URL or raw
  binary data enters durable Job JSON.
- `stage_audio_output(worker_id, claimed, wav_bytes, max_bytes=...)`: accepts
  only `audio.synthesize` and exact `bytes` with bounded RIFF/WAVE framing.
  It posts to the Worker output staging endpoint with the same current attempt
  authority, parses a bounded JSON receipt, and verifies the canonical opaque
  artifact ID, media type, byte count and SHA-256 against the actual body.
  Returns `ArtifactRef` for a later **separate** durable Job completion.

No method invents claim identity, performs provider discovery, retries an
outcome-uncertain POST, fabricates a successful Job or copies private response
bodies/headers into public errors. HTTP status and bounded fixed error text
are retained for callers; cancellation propagates. Control still revalidates
Worker ownership, lease, attempt, epoch and artifact bytes at effect time;
client checks are *additional* fail-closed validation, not trusted authority.

## Security and practical bounds

- Explicitly require caller-provided media byte limits; no inferred model
  default, no unbounded read. Incremental streaming rejects excess data even
  without `Content-Length` and refuses compressed responses.
- Only the existing Control base URL and authenticated Worker media endpoints
  are used. Public inputs cannot choose Worker transfer URLs. No direct
  VOICEVOX/Style-Bert/STT server access is introduced.
- A successful output staging receipt is **not** a completed or published Job.
  The existing `ControlClient.complete` and Control's durable status/Artifact
  checks remain authoritative, with lease fencing. Unknown commit acknowledgement
  is not retried automatically; Control-local receipt state remains ephemeral.
- A bad/missing Worker token, stale claim, wrong server payload, malformed
  receipt or media mismatch fails closed. Errors should not reveal the WAV
  content, bearer token, internal path, speaker, model or transcript.
- Base Control request timeout is a finite upper bound per transfer; there is
  no new audio-specific background task or retry queue.

## Coverage and remaining integration

Synthetic tests use the real Control ASGI routes with `InMemoryControlRepository`,
private temporary `ArtifactSpool`, and the same previously reviewed channels.
They check authorized input read, bound input integrity, authorized output upload,
published WAV visibility *only after* durable completion, stale attempt/lease/
epoch rejection, malformed/oversized responses, receipt mismatches, and zero
extra attempts after uncertain output receipts. No real PostgreSQL, server
network, Worker runtime or provider process is launched.

**Next B3b remains required:** integrate these helpers at the active
`WorkerRuntime` attempt boundary into a narrowly reviewed audio-aware Executor
interface. The generic `JobRequest.metadata` must not be repurposed to carry
raw Worker tokens, lease secrets, mutable WAV data or serialized callbacks.
Bind an active claim to the Worker-controlled transport, retain runtime
heartbeat/cancellation/deadline supervision, and validate audio-specific
`JobResult` before calling `complete`. Until B3b and the separately authorized
provider work, B3a alone does not make Audio Gateway publicly functional.

Provider adapters remain independently gated: #176 VOICEVOX, #177
Style-Bert-VITS2, #178 Japanese STT comparison and explicit model selection
before #179. No dynamic engine install, model download, GPU/host changes,
deploy, release, or production feature claim is authorized by this slice.
