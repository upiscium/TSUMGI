# TSUMGI #177 — Style-Bert-VITS2 model TTS adapter: isolated source slice

**Status:** Provider-local source adapter, mock model IPC, and tests. **No
Style-Bert-VITS2 weights, executable, backend library or inference engine was
loaded, downloaded or run. No actual audio quality or latency was measured.**

## Contract

`runtime/providers/style_bert_vits2.py` is intentionally isolated from
VOICEVOX, the generic Worker, runtime registry and setup/deployment files.
It defines a bounded, immutable `StyleBertVits2ModelPin` with one operator
approved logical model ID, safe model filename (.safetensors / .onnx),
model SHA-256, `config.json` SHA-256, `style_vectors.npy` SHA-256 and
runtime/adapter revisions. The `StyleBertVits2ProviderConfig` pins the
required serving semantic revision, a public voice alias mapped to exact
numeric native speaker ID and style name, and strict text/WAV/timeout limits.

These digests are **declared only**: no files are inspected or actual artifacts
verified in this phase. No arbitrary paths/URLs/model names, reference audio,
`assist_text` or forced model reload parameters can be submitted by a public
Job. An unknown alias/style/speaker, mismatched semantic revision, malformed
request, unsupported capability or invalid pinned config is rejected.

The adapter implements generic `JobExecutor.execute/cancel/residency` and
audio-only `execute_audio(job, media)`. Model output must be a bounded,
canonical PCM16 WAV, 8–48 kHz, mono/stereo, matching the already approved
Gateway transport subset. The single valid `ArtifactRef` is returned from
the Worker-owned `AudioJobMedia.stage_output`; WAV or private model contents
never enter durable Job JSON or public error text. Duplicate Job IDs,
timeouts and corrupted receipts fail closed with fixed error messages;
automatic fallback to VOICEVOX or different model versions is prohibited.

## Synchronous native inference and process safety

The upstream `style_bert_vits2.tts_model.TTSModel` accepts
`model_path, config_path, style_vec_path, device`; its `infer(...)` is
**synchronous**, returns sample rate and PCM samples, and can load weights
lazily. Calling it on the Worker event loop would block Worker heartbeat,
lease renewal and cancellation. Using `asyncio.to_thread` alone does **not**
ensure that a cancelled inference stops consuming CPU/GPU or memory.

This slice instead requires an **injected asynchronous** model session. Its
future concrete implementation must own a separately supervised, bounded
process, verify artifact hashes/model IDs, convert inference PCM to the
canonical WAV format, enforce IPC byte bounds, and kill/reap the subprocess
on timeout/stop. Cancelling a fake coroutine in the tests is **not evidence
of terminating a model process**. No process launcher is implemented here.

The stock upstream `server_fastapi.py` can log `request.query_params`,
including source text; TSUMGI must not expose that route as a raw text proxy
without reviewed redaction and access restrictions. A process-local,
fixed-protocol IPC with safe logging is the preferred integration strategy.

## Future acceptance gates

- Pin and independently verify the exact engine/model/config/style-vector
  artifacts, all runtime package/ABI and tokenizer/phoneme dependencies,
  license and voice redistribution terms.
- Independently approve actual model download or sample weights, CPU/GPU
  device selection, engine process creation and host access. No implicit
  model sharing or arbitrary framework compatibility.
- Integrate provider-specific ManagedRuntime readiness, process health,
  cancellation and resource reporting **before** Worker advertisement.
  Runtime registry and #180 deterministic NixOS/generic-systemd SetupPlan
  are separate shared-file integration units, not changed here.
- Demonstrate actual Japanese Control→durable Job→Worker→WAV playback,
  quality/latency/VRAM residency and failure/rollback under separate Human
  host/operational authorization. No AC-AUDIO-03 acceptance from mocks.

Source tests use only fake in-process async model sessions and fake media.
They exercise pinned request semantics, no fallback, safe WAV staging,
invalid models/speakers/styles, missing output, bad artifact receipts,
timeouts, cancellation and no raw text/secret exposure.

Official upstream:
https://github.com/litagin02/Style-Bert-VITS2/blob/master/style_bert_vits2/tts_model.py
https://github.com/litagin02/Style-Bert-VITS2/blob/master/server_fastapi.py
https://github.com/litagin02/Style-Bert-VITS2

#177, #175 and #180 remain OPEN. Draft PR does not authorize Ready/Merge,
model acquisition, live inference, deployment or Release.
