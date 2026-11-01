# #180 Phase B3b — active-claim-bound audio Executor interface

**Status:** Source-only synthetic integration. This is not a real VOICEVOX,
Style-Bert-VITS2 or STT model/backend implementation, an approved Worker
installation, or production audio feature acceptance.

## Ownership and opt-in

B3a PR #207 introduced bounded audio input/output methods on the existing
Worker `ControlClient`, using the current Worker bearer and strict
Claim/Attempt/Lease/Epoch headers. Those source methods are a **dependency**:
B3b is developed as a stacked Draft branch based on B3a while PR #207 is
unmerged. B3b must not be merged into main before B3a is integrated and the
base/diff and combined CI are revalidated.

The generic `JobExecutor.execute(JobRequest)` has no media-transfer authority.
A new optional typed `AudioJobExecutor.execute_audio(job, media)` protocol
adds the missing boundary. Audio-capable Workers must have a callable **async**
`execute_audio` before registering, in addition to their already-reviewed
serving declaration, Executor capability/feature checks and runtime readiness.
Direct construction of `WorkerRuntime` with audio capabilities but without
serving advertisement is rejected. Non-audio Executors continue to use their
original `execute`/`execute_stream` path with unchanged error/retry behavior.

## Scoped media session

The Worker creates a private one-shot `_AudioJobMediaSession` only after
verifying the active Job's serving binding and current runtime identity. An
Executor is passed only an `AudioJobMedia`-shaped façade exposing:

- `read_input(max_bytes=...)` for `audio.transcribe` only;
- `stage_output(wav_bytes,max_bytes=...)` for `audio.synthesize` only.

Both call the B3a `ControlClient` helpers with the Worker-held actual claim,
without serializing WAV into Job metadata or exposing the raw Worker bearer,
lease token or runtime epoch credential to the audio Executor. Audio
`JobRequest.metadata` is an explicit allowlist (`attempt`, `deadline_at`,
`serving`, plus the resolved semantic revision). The context retains no
publicly transferable credential, closes after the attempt, permits one media
operation, and checks current active-claim identity, Control ACK freshness,
stop state and runtime availability before/after I/O. The authoritative
Control endpoint revalidates Worker/Attempt/Lease/Epoch during I/O and terminal
commit. Python Executors remain trusted code, not security sandboxes.

No `_control_lock` is held for the network transfer. The existing execution
owner can continue lease heartbeats, respond to drain, and detect runtime
health failure while awaiting audio. Cancellation interrupts the media task;
a stop, reallocation or stale attempt cannot use its old façade to publish
an output.

## Job result and uncertainty

- **Speech synthesis:** exactly one returned `ArtifactRef` must equal the
  reference actually staged by the same one-shot media context, with no text
  or generic output payload. Control still requires matching Job authority,
  staged binary and durable terminal commit before a public WAV is readable.
- **Speech transcription:** text must be a built-in UTF-8 string bounded to
  8,192 bytes, and the executor must have obtained the authorized staged input
  through the media context. No output artifacts or generic `outputs` allowed.
- For both capabilities, metadata and numeric metrics are recursively checked
  for finite bounded JSON (8,192-byte combined bounds per map; limited depth
  and item count). Binary values, NaN/Infinity, oversized text, unsupported
  artifact/result shapes, and raw WAV in Job JSON are rejected before complete.
- Audio Executor exceptions and invalid results are converted to fixed,
  nonretryable errors without leaking provider output, raw file paths, speaker
  data or input audio. A media transport fault or uncertain output-upload
  acknowledgement does **not** fabricate terminal failure, auto-resubmit, or
  allow an adapter to swallow the failure and return successful text. It is
  abandoned to the existing durable lease/attempt recovery fencing, and
  Control remains authoritative. The explicitly configured Gateway uses
  one-attempt Jobs for this route.

## Verification and limitations

Source tests exercise real in-process Control ASGI media routes, private
`ArtifactSpool`, `InMemoryControlRepository`, synthetic Serving/Claim values,
`WorkerRuntime` execution, and fake audio Executors. They verify TTS
WAV stage→claim completion→publication, STT input download→bounded text,
invalid/secret-bearing output rejection, wrong/unpublished artifacts, duplicate
stage attempts, stale/stop/cancelled attempt handling, outcome-uncertain upload
without re-send/terminal write, non-audio compatibility and missing extension
rejection. The tests use no actual voice model, hosted VOICEVOX engine,
PostgreSQL installation, GPU, or deploy/test target.

Real provider-specific interfaces, licensing, model identity/selection,
performance and readiness remain under #176/#177 and #178→#179. Process-local
artifact receipt restart/cleanup requirements remain under #174; #175 final
functional API acceptance awaits qualified providers and a separately approved
real Worker/Control integration exercise. #180 must remain OPEN until the
remaining provider packaging/setup matrix and approved host smoke are complete.
No model download, host/service operation, release or real-world deployment is
performed in this source-only phase.

## B3c — delayed media transfer and real Worker-loop regression

Additional **source-only** tests drive `WorkerRuntime.run_forever()` with the
actual in-process Control ASGI transport, active Worker registration, finite
lease/heartbeat intervals and an intentionally delayed media response. Unlike
the original B3b direct-entry tests, these tests do not inject an artificial
Control acknowledgement or suppress periodic heartbeat scheduling.

- During delayed TTS output upload and STT input download, an independent
  heartbeat advances the Worker `last_seen_at` and renews the active Job's
  lease before the transfer response is released; media I/O does not hold the
  generic Control lock.
- Drain while TTS output is in flight preserves completion of the **already
  owned** attempt and prevents admission of new audio Jobs to that draining
  deployment.
- Stopping the Worker before a delayed output is acknowledged, or cancelling
  the Job before the delayed output/input arrives, never produces a public
  artifact or successful transcript. The current durable Job remains RUNNING
  after a local stop and is subject to the Control's separate lease recovery;
  a cancelled Job remains CANCELLED. The tests explicitly observe the
  cancelled transfer's HTTP 409 response and assert no cancelled STT WAV
  reached the Executor. Checking terminal status alone would not prove that
  input access was fenced; a late public Job completion could be denied even
  if an erroneous input response had exposed bytes.
- An output POST already admitted by Control before Worker stop may leave a
  bounded **private staged receipt** behind. That is neither Job completion
  nor publication; the original #174 spool TTL/reaping and crash/orphan
  retention responsibilities still apply. Do not claim synchronous rollback
  of uncertain filesystem/DB effects.

These tests use controlled delays inside `httpx.ASGITransport`, not an actual
network socket or provider process. They improve interleaving coverage, but
do not establish OS-level scheduling, real-network fragmentation/backpressure,
NVIDIA GPU execution, model identity, provider licensing, or production E2E.
