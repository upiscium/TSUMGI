# Real-engine evidence for the `faster-whisper-ja` provider (#179, #181 prerequisite)

**This is evidence for the maintainer's decision. It is NOT a qualification.**
It records the first run of the source-only, default-off provider added in #314
(`docs/design/faster-whisper-explicit-worker-manifest.md`) against the real
engine and the real pinned model. Nothing here approves, activates or releases
the provider. Every result comes from one developer workstation, **not** from
the Ubuntu LXC with the GTX 1080 Ti that is the selected deployment target, and
must be repeated there (see [Open items](#open-items-to-repeat-on-the-ubuntu-lxc-with-the-1080-ti)).

No audio, transcript, case identifier or private path is recorded here. The
clips were a subset of the private #178 fixture (FLEURS Japanese read speech
plus six synthetic contrast clips).

## Environment (the RTX 4080 workstation, not the 1080 Ti LXC)

| Item | Value |
| --- | --- |
| Machine | one workstation, 12 CPU threads, 62 GiB RAM, Linux 6.18, NixOS-style host |
| GPU | NVIDIA RTX 4080 16 GiB (compute capability 8.9), driver 595.45.04; roughly 9.6 GiB were in use by another tenant during the runs |
| Host loader | `libcuda.so.1` lives in `/run/opengl-driver/lib` (not in a default loader path); glibc 2.44 |
| Python for TSUMGI | CPython 3.12 from nixpkgs, repository at `origin/main` 84eba1a |
| Child interpreter | CPython 3.11.14 (python-build-standalone), see staging below |
| Packages | faster-whisper 1.2.1, ctranslate2 4.9.1, nvidia-cublas-cu12 12.9.3.13, nvidia-cudnn-cu12 9.28.1.4 (all as the provider pins them), PyAV 18.1.1 |
| Compute type | float32 (the provider's only mode) on a compute-capability 8.9 GPU; the 1080 Ti (6.1) is a different case |
| Load | other tenants shared the GPU and CPU; latency numbers are indicative, not benchmarks |

## Artifact identities (verified before anything ran)

Model `deepdml/faster-whisper-large-v3-turbo-ct2`, revision
`4df90f75321148c3a29a9e2351b7ddf8f5b115a8`. The five files were copied
(hard-linked) into a directory holding exactly them; SHA-256 recomputed and
compared with `faster_whisper_native.FILE_SHA256`, and the provider's own
`preflight()` accepted them:

| File | SHA-256 | Result |
| --- | --- | --- |
| `config.json` | `b0253ea6c0d3bea6b1e19e91a02acfd3b53f4467362efcb5a3e6b16c9b3a9b7e` | match |
| `model.bin` | `e76620f83d5f5b69efd3d87e3dc180c1bd21df9fbebacfd4335e5e1efcc018da` | match |
| `preprocessor_config.json` | `7ccc62c6f2765af1f3b46c00c9b5894426835a05021c8b9c01eecb6dfb542711` | match |
| `tokenizer.json` | `297b13372ac43916285644fb9687add3cc62ee2a1adb60da3dc25cc94c1871fd` | match |
| `vocabulary.json` | `c69260f2ab26d659b7c398f9a2b2b48ed0df16c3b47d7326782fd9cba71690c1` | match |

Operator-supplied manifest values generated for this run (these identify the
test staging only, not a reviewed production installation):

| Value | How it was produced |
| --- | --- |
| `interpreter_sha256` `6ff97f602038740073dca96714310a30e303332326268e0f1bb2767edc820944` | SHA-256 of the copied `bin/python` of the staged venv; it is byte-identical to `base/bin/python3.11` (`preflight` requires both to equal the same value) |
| `dependency_lock_sha256` `11717a8c20b133ce4267de367fd5189f1fbe9e3d526571462e282d6d60c3320e` | SHA-256 of a 28-line `name==version` file written from the dist-info directories of the staged site-packages |
| `rights_review_sha256`, `validation_evidence_sha256` | SHA-256 of two fixed placeholder strings (no real review exists yet) |

## How the repository's own components were used

Manifests were written with `RuntimeDeploymentSpec.to_dict()` and
`FasterWhisperManifestConfig.serving_declaration().to_dict()`, read back with
the Worker daemon's own loaders (`_load_runtime_deployment`,
`_load_serving_deployment`, including the canonical-subset and duplicate-key
checks), and turned into a runtime by `managed_runtime_from_deployment`
(compatibility check, serving-declaration equality). Then the real
`FasterWhisperManagedRuntime.start()`, `FasterWhisperProcess` (fixed-argv owned
child, framed pipes, flock-held run directory), `executor().execute_audio()`
(receipt, digest, PCM16 validation) and `stop()` ran with no mock. Only one
harness-side change was made: the child's `LD_LIBRARY_PATH` got
`/run/opengl-driver/lib` appended (defect F1, NixOS-specific).

Staging that the provider's environment scan required (defect F3):

* The base CPython tree was copied; its python-build-standalone
  `site-packages/distutils-precedence.pth` (an executable `import` line) and
  `_distutils_hack` were removed.
* The venv was created with `python -m venv --copies --without-pip`
  (a symlinked `bin/python` and the setuptools `.pth` are refused).
* Packages were hard-linked from the earlier private #178 environment rather
  than freshly resolved (the disk had about 50 MiB free during staging); they
  are the pinned versions, but the install itself was **not** reproduced from the
  lock file. Reproduce it on the LXC.

## Results

Subset: 30 clips (24 FLEURS: 20 clean and 4 with white noise at 10 dB, of which
12 are numeral/counter-heavy; plus the 6 synthetic contrast clips), 2.0 to 25.2 s
each, 327 s of audio in total, sent as 16 kHz mono PCM16 WAV bytes.

| Check | Result |
| --- | --- |
| Manifest loads, compatibility accepted, serving declaration equal | yes |
| Runtime reaches `READY` through the real chain | yes |
| All outputs valid UTF-8 Japanese text, no control characters, none empty | yes (30 of 30) |
| `散歩` / `3本` contrast clips decode to the intended word | 6 of 6 (synthetic voice, not a real-speaker re-check) |
| CER on the 24 FLEURS clips (NFKC, punctuation and spaces removed) | 0.040 macro, 0.044 micro; clean 0.035, noise 0.064, numeral 0.044 |
| Reference: earlier 4080 run, same model, full 245 clips | 0.047 macro |
| Five identical requests give identical text | yes (1 distinct output); not claimed in general |

The subset CER is in the same range as the earlier measurement; 24 clips are
too few to claim equivalence, and no statement beyond "same order" is made.

### Resources and latency (RTX 4080, float32, beam 5)

| Metric | Value |
| --- | --- |
| Boot to `READY` (warm page cache) | 5.5 s |
| Boot to `READY` after evicting model, venv and base files with `posix_fadvise(DONTNEED)` | 6.1 s (NVMe; eviction is best effort) |
| Manifest boot timeout / process readiness deadline | 180 s / 155 s |
| Child RSS after load / peak RSS (`VmHWM`) | about 0.5 GiB / 3.2 GiB (the peak is the load phase) |
| Device memory of the child after load / peak | 4.0 GiB / 4.0 GiB (declared floor 11,264 MiB is the 1080 Ti class, not a measurement) |
| First request after start | 0.52 s |
| Warm latency, 29 requests | p50 0.53 s, p95 0.70 s, max 0.88 s |
| Real-time factor | aggregate 0.048 (p50 per request 0.050) |

### Call forms checked against faster-whisper 1.2.1

| Check | Result |
| --- | --- |
| `WhisperModel(path, device="cuda", device_index=0, compute_type="float32", local_files_only=True)` matches the installed signature and loads | yes |
| `transcribe(io.BytesIO(wav), beam_size=5, language="ja", vad_filter=False, condition_on_previous_text=False)` matches the signature; returns segments plus info | yes |
| PyAV decodes an in-memory WAV (`BytesIO`) to float32 at 16 kHz | yes |
| cuBLAS 12 loaded from the staged venv's `nvidia` wheel through the child's own `LD_LIBRARY_PATH`, without ambient environment | yes |
| cuDNN loaded during a float32 transcription | **no** (not mapped in the child after inference; only cuBLAS is) |
| `libcuda.so.1` found with only the venv libraries on `LD_LIBRARY_PATH` | **no on this NixOS host** (F1); yes once the host driver directory was appended |
| `CUDA_VISIBLE_DEVICES=<GPU UUID>` selects the GPU for CTranslate2 | yes |
| Child core dumps disabled (`RLIMIT_CORE` 0) | yes |
| Child environment holds only the eight designed variables | yes |

### Negative controls (each on a fresh runtime where the control revokes it)

| Control | Result |
| --- | --- |
| Silent clip (digital zeros, 0.2 to 29 s) returns empty text | **no**: a stock 14-character phrase every time (F2) |
| Runtime stays usable after a silent or low-noise clip | yes |
| Non-WAV bytes, random bytes, truncated header, truncated data, 24 kHz, stereo, 8-bit, over 30 s, over 1 MiB, zero frames, digest mismatch | all rejected before the child (`invalid_input` or `invalid_receipt`): 11 of 11 |
| Runtime still `READY` and transcribing after those rejections | yes |
| Cancel mid-inference | `CancelledError`, child killed and reaped in 0.11 s, health `failed`, later calls `runtime_unavailable`, GPU memory released: yes |
| Deadline exceeded (executor timeout shortened to 0.25 s for the test) | `transcription_timeout`, child killed: yes |
| `SIGKILL` of an idle child | health `failed`, calls refused, `stop()` reaches `STOPPED`: yes |
| `SIGKILL` during inference | `transcription_failed`, `stop()` clean: yes |
| `SIGSTOP` of the child (executor timeout 1.5 s) | `transcription_timeout` after 3.6 s (includes the 2 s `SIGTERM` grace), child reaped, `STOPPED`: yes |
| No IP socket at any time (strace of the whole process tree through load and 5 inferences) | yes: no `AF_INET`/`AF_INET6` socket; the child opened two `AF_UNIX` sockets (CUDA driver) during load |
| Python-level network guard blocks `socket`, `create_connection`, `getaddrinfo` after load | yes; a raw `libc` `socket()` call is **not** blocked (as the design says: defence in depth, not a sandbox) |

### Private-HOME lifecycle and orphan behaviour

| Check | Result |
| --- | --- |
| Parent is empty before the first start | yes |
| Start creates exactly one `run-<hex>` directory, mode 0700, flock held while live | yes |
| Stop removes it, no child remains | yes |
| Second start uses a new name and reaches `READY` | yes |
| Run directory content while running | only the CUDA compute cache (`.nv/ComputeCache`); the model copy is deleted after load, so the 1.6 GB copy is transient |
| `SIGKILL` of the parent while the child is idle | child exits by itself in 0.2 s (stdin EOF); GPU memory released; the run directory stays |
| `SIGKILL` of the parent during model load | child keeps loading, then exits 3.8 s later (readiness write fails); GPU memory released afterwards |
| Next start sweeps the stale run directory and reaches `READY` | yes |

## Defects and gaps (most important first)

None was fixed in this branch; each needs a decision or is environment-specific.

* **F2: a silent clip does not return an empty transcript.** The design note
  records the decision that a silent or no-speech clip yields an empty,
  valid result. With `vad_filter=False` (frozen decoding identity) the model
  returns a fixed 14-character stock phrase for digital silence at every length
  from 0.2 s to 29 s, and the runtime treats it as valid text. A caller cannot
  tell this from speech. Pointers: `faster_whisper_native.py` `DECODING` and
  `FasterWhisperNative.transcribe`; `faster_whisper.py` `valid_transcript`.
  Options (each changes identity or adds a gate): a Worker-side energy check
  before the child, `vad_filter=True`, or a no-speech/log-probability check of
  the returned segments. Needs a decision before any activation.
* **F1: the owned child cannot find `libcuda` on a host whose driver is outside
  the default loader path, and the failure is undiagnosable.**
  `FasterWhisperProcess._env` (`faster_whisper_process.py`) replaces
  `LD_LIBRARY_PATH` with cuBLAS and cuDNN only. On this NixOS host
  `ctranslate2.get_cuda_device_count()` is 0, `FasterWhisperNative.load`
  raises `wrong CUDA runtime`, and the caller sees only
  `Faster-Whisper child did not confirm readiness` (the child redirects
  descriptors 0 to 2 to `/dev/null`, and `start()` discards the cause). On
  Ubuntu the `ldconfig` cache normally provides `libcuda`; not yet verified there.
  Suggested: a reviewed optional driver-directory setting, and a fixed
  non-sensitive failure code in the ready frame.
* **F3: the staged-installation rules reject every default tool output.**
  Refused: a symlinked venv `bin/python` (`_canonical` requires the path to
  resolve to itself), the uv-created venv from #178 (`pyvenv.cfg` keys
  `implementation`, `uv`, `version_info` are not allowed; `home` is a symlink
  path), executable `.pth` files including the one shipped in
  python-build-standalone's own `site-packages`, and a model directory with
  the usual Hugging Face extras (`README.md`, `.gitattributes`, `.cache`).
  Working recipe: the staging above. #180 provisioning must produce exactly this
  layout and the design note should list it.
* **F4: cuDNN is pinned, version-checked and put on the search path, but is not
  loaded** on the float32 Whisper path of CTranslate2 4.9.1 here. The pin is
  harmless but its necessity is unproven; the LXC may behave differently.
* **F5: disk and memory headroom.** The child copies 1.6 GB into the run
  directory at every start and its load-phase RSS peaks near 3.2 GiB; the
  design mentions the copy but there is no free-space check, and a full
  filesystem would show only as the generic readiness failure (F1).
* **F6 (observation): every cancel or timeout is a Worker restart** (child
  killed, runtime latches `FAILED`), as designed; a restart cost 5 to 6 s here.

### Assumptions the real engine confirmed

Interpreter and lock digest rules; five-file model identity; `WhisperModel` and
`transcribe` signatures; in-memory WAV decode; owned-child isolation, kill and
reap on every revocation; per-start run directory with flock, stale sweep and
orphan exit; no IP network use.

## Tests run

The faster-whisper test files and the full suite pass unchanged (no source file
was modified in this branch). The documentation build succeeds.

## Licence and credit

Model: MIT; Whisper large-v3-turbo by OpenAI (MIT); CTranslate2 conversion by
deepdml. FLEURS clips are CC BY 4.0. No audio, weights or environment files are
redistributed by this repository.

## Open items to repeat on the Ubuntu LXC with the 1080 Ti

1. The same chain on the LXC with float32 on compute capability 6.1: boot,
   warm latency and RTF (expected much slower than here), device memory against
   the 11,264 MiB floor, host RSS against the LXC allocation.
2. `libcuda` discovery without any harness change (F1) and whether cuDNN is
   loaded (F4).
3. A reproducible installation from the lock file (not hard links) and the
   interpreter and lock digests of the reviewed staging (F3).
4. Free-space behaviour of the 1.6 GB per-start copy on the real filesystem (F5).
5. Cold page cache boot against the 180 s timeout on the LXC storage.
6. The F2 decision and a real-speaker, conversational re-check of `散歩` / `3本`
   and numerals (the contrast clips here are synthetic).
7. Orphan, cancel and `SIGKILL`/`SIGSTOP` behaviour under the real service
   manager, and egress restriction by the deployment (the in-process guard is
   not a sandbox).

## Follow-up: F2 silent-clip options measured (2026-11-28)

Maintainer decision: **keep V0 (the measured decoding, no silence handling) and document
the limitation.** Same real model on the RTX 4080 (shared with another tenant, so absolute
latencies are inflated and comparable only between rows), float32, beam 5, language
ja. The comparison rule was fixed before measuring: a variant is eligible only if the
macro CER on the 245-case fixture is at most 0.052, both contrast targets stay 3/3, and
no speech clip (the 245, and 30 selected clips attenuated by 20, 30 and 40 dB) comes
back empty; among eligible variants the one returning empty for the most of 40
non-speech clips wins.

| Metric | V0 (current) | V2: `vad_filter=True`, default parameters |
| --- | --- | --- |
| Non-speech clips returned empty (silence, ±2 LSB noise, white and pink noise at -70/-50/-30/-20 dBFS; 0.2/1/5/29 s) | 0 of 40 | 40 of 40 |
| CER / WER (macro, 245 cases) | 0.0471 / 0.0608 | 0.0484 / 0.0615 |
| Exact match | 0.273 | 0.261 |
| `散歩` / `3本` hits | 3/3 and 3/3 | 3/3 and 3/3 |
| Fixture cases whose text differs from V0 | n/a | 63 (34 higher CER, 20 lower) |
| Selected speech clips empty at 20 / 30 / 40 dB attenuation | 0 / 0 / 0 | 1 / 2 / 7 of 30 |
| Warm latency p50 / p95, mean RTF | 1.34 / 1.96 s, 0.103 | 1.41 / 2.02 s, 0.106 |

- **V1 (a Worker-side RMS gate) cannot be defined.** The loudest near-silent non-speech
  clip is at -69.7 dBFS RMS; the quietest speech at 40 dB attenuation is at -101.0 dBFS
  and already at 20 dB attenuation at -81.3 dBFS, so no threshold separates them. Loud
  noise would not be caught by any RMS gate.
- **Whisper's own no-speech heuristic cannot work with this model:** `no_speech_prob` is
  0.000 for every clip, silence included, and `avg_logprob` for silence (-0.05 to -0.91)
  overlaps speech (-0.04 to -0.40).
- V2 fails the quiet-speech clause (the empties are at about -55 to -65 dBFS RMS,
  far below normal speech). Redefining the clause by absolute level was offered and not
  taken. V2's network use was not checked.

The design note records the resulting limitation: silent or noise-only audio is
transcribed as a short stock phrase.
