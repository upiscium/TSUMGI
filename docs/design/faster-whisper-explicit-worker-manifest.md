# #179: explicit Faster-Whisper Japanese STT Worker

This source candidate adds an opt-in standard runtime/serving manifest path for
one maintainer-selected Japanese STT model. It does not install dependencies,
acquire weights, enable a service, or qualify a package. Nothing is enabled by
default; the provider is unreachable without an explicit `enabled=true`
manifest pair. It mirrors the Parakeet lane
([Parakeet manifest](parakeet-explicit-worker-manifest.md)) and leaves that
code untouched; shared logic is duplicated with a reference, not refactored.

## Selection and credits

On 2026-11-27 the maintainer selected, from the DES-AUDIO-005 evidence
(`docs/validation/audio-stt-178-results.md`), **`deepdml/faster-whisper-large-v3-turbo-ct2`**
at revision `4df90f75321148c3a29a9e2351b7ddf8f5b115a8`, float32, on a GTX
1080 Ti. This is the decision recorded there; this note adds no new selection.
No other model, no Parakeet path and no fallback exist in this provider.

- Model licence: MIT. Original model: Whisper large-v3-turbo by OpenAI
  (MIT). The CTranslate2 conversion is third-party work by deepdml; the
  pinned revision and file digests are the identity to deploy, not the name.
- Runtime: faster-whisper 1.2.1, CTranslate2 4.9.1 (MIT), CPython 3.11.14,
  `nvidia-cublas-cu12` 12.9.3.13, `nvidia-cudnn-cu12` 9.28.1.4, as measured.
- float32 only: CTranslate2 refuses float16 on compute capability 6.1.
  int8 was not measured and is not offered.

## Frozen configuration

Provider id `faster-whisper-ja`; ordinary `[runtime].manifest` and
`[serving].manifest`; the exact `DEMAND` exported by `faster_whisper_manifest`.
Decoding is identity, never a request or configuration option: beam 5,
language `ja`, no VAD, `condition_on_previous_text=False`, default temperature
fallback (the settings of the measurement). Required configuration:

- Literal `enabled=true` (omission is not opt-in). Unknown keys (for example
  `compute_type`, `language`, `beam_size`, `model_ref`, a fallback model) make
  the configuration invalid.
- `model_dir`: canonical directory holding **exactly** `config.json`,
  `model.bin`, `preprocessor_config.json`, `tokenizer.json` and
  `vocabulary.json`. Source constants pin each file's SHA-256 (the weight
  digest is `e76620f8...018da`) and the repository revision. Extra, missing,
  renamed, symlinked, empty, oversized or writable entries are refused; a
  changed byte is a stale hash. The child copies the files into a private
  directory while hashing them and loads that copy, so a later change to the
  source cannot alter what runs.
- `interpreter_path` (venv `bin/python`), `base_python_root`,
  `dependency_lock_path` and the operator-supplied `interpreter_sha256` /
  `dependency_lock_sha256`, with the same staged-installation scan as Parakeet
  (only the required CUDA libraries differ: cuBLAS and cuDNN).
- `private_home`: a dedicated, non-root, `0700` **parent** directory (see
  below). `gpu_uuid`, `rights_review_sha256`, `validation_evidence_sha256`.

The declared capacity floor is 11,264 MiB, the capacity of the selected GPU
class, not a measured minimum (device-level peak was about 4.2 GiB) and not a
reservation or a qualification of other CUDA hardware.

`FasterWhisperManifestConfig(**provider_config).serving_declaration().to_dict()`
produces the declaration. Its identity binds the weight and tokenizer digests,
all five file digests, revision, package versions, decoding, compute type,
interpreter and lock digests, the adapter source digests and the rights
receipt; not machine-local paths or the physical GPU UUID.

## Owned child and failure handling

The Worker never imports native code. A fixed-argv child (`python -I -B -u`)
runs in its own session with a minimal environment (offline flags, one
visible GPU, private HOME/TMPDIR/cwd) and talks over private framed pipes
(bounded frames, duplicate JSON keys and non-finite constants rejected, epoch
and request id echoed). Readiness is confirmed against the pinned revision,
weight digest, compute type and package versions before registration.
Requests: Japanese, JSON, canonical mono 16 kHz PCM16 WAV, at most 30 s and
1 MiB; the result is UTF-8 text of at most 16 KiB with no control characters
other than newline/tab. Any failure, timeout, cancellation or invalid output
is an explicit error, revokes the instance, kills and reaps the process group,
and is never retried on another model or device. Public errors carry fixed
codes only.

Deliberate differences from earlier providers (docs/validation/audio-tts-real-engines.md):

- **D3**: a launch refused before any process exists is a complete, clean
  failure. `close()` has nothing to stop and does not raise; the runtime
  reaches `STOPPED`, never a cleanup-uncertain state.
- **D4**: `private_home` is a parent, not the child's HOME. Every start creates
  a fresh `run-<32 hex>` `0700` directory as HOME/TMPDIR/cwd, removes it after
  the child is reaped, and removes stale `run-*` directories of a crashed
  predecessor (each live instance holds an exclusive `flock` on its own run
  directory, so a second start never removes a live one, and an unremovable
  leftover never blocks a start). Any other entry in the parent (or more than 16 run directories,
  or a symlink) refuses the launch. A restart policy therefore works without
  provisioning a new HOME, and no state is reused between runs. The parent
  must be dedicated to this one Worker; the model copy (about 1.6 GB) is
  written under it, so size the filesystem for that.
- Core dumps are disabled in the child; the Python-level network guard denies
  socket creation after the model is loaded. This is defence in depth, **not**
  a sandbox: egress still has to be restricted by the deployment.

Open points for the maintainer: a silent or no-speech clip yields an empty
transcript, which (like Parakeet) is treated as a failure that revokes the
instance; the temperature fallback schedule is the faster-whisper 1.2.1
default, as measured, so output is not guaranteed bit-deterministic.

## What this does not do

- No NixOS or systemd packaging, SetupPlan or provisioning (#180); no model or
  dependency download; no activation on any host.
- No real-engine qualification. Tests use fakes and tiny fixtures. Japanese
  quality, GPU latency, the exact PyAV decode path, `WhisperModel` argument
  compatibility with 1.2.1, native cancellation and the cuBLAS/cuDNN library
  search have not been run through this adapter.
- Not done by the selection and still open: the real-speaker, conversational
  re-check of the `散歩` / `3本` contrast and numerals required before any
  activation; the acceptance thresholds of AC-AUDIO-04; separately approved
  Control/Worker/ASR execution with p50/p95 latency; #247 PostgreSQL
  old-epoch acceptance.
- The interpreter and lock digests are operator-supplied manifest values
  because no staged installation for this runtime exists yet; #180 provisioning
  decides the reviewed values.
