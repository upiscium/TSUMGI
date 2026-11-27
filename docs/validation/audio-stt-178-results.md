# Issue #178 — Japanese STT comparison: measured results (public-safe summary)

**This is evidence for the maintainer's decision, not a selection.** It
summarises one private run of the [#178 harness](audio-stt-178.md). No
transcripts, case IDs, audio or paths are published here; the full report stays
private. The first measurement (RTX 4080) is not valid for the GTX 1080 Ti target; a
second measurement on a GTX 1080 Ti follows below.

## Dataset

- `google/fleurs`, config `ja_jp`, **test** split (CC-BY-4.0, public, read speech).
- Subset rule: test rows sorted by file name; every 7th row, plus **all** rows
  whose text contains digits followed by a counter or unit (本, 人, 個, 枚, 台,
  匹, 冊, 回, 杯, 軒, 頭, 羽, つ, 倍, %, パーセント, キロ, メートル, 年, か月, 日間,
  歳). Result: 209 clean utterances (136 of them counter/numeral cases).
- Noise: 30 of those utterances copied with synthetic white or pink noise at
  10 dB SNR (fixed seed).
- Contrast: 6 short contextual sentences (3 with 散歩, 3 with 3本, tagged
  `homophone:散歩-vs-3本`) synthesised with VOICEVOX engine 0.26.2 (Linux CPU
  x64), speaker ずんだもん ノーマル. **These are synthetic speech.**
- Total 245 cases. References are the FLEURS normalised transcription;
  references and hypotheses are NFKC-normalised, lower-cased, stripped of
  punctuation and MeCab-segmented (WER on segmented tokens; CER ignores
  whitespace). Metrics are macro-averages per utterance.

## Candidates and runtime

| Candidate | Model / revision | Precision | Runtime |
|---|---|---|---|
| Parakeet | `nvidia/parakeet-tdt_ctc-0.6b-ja` @ `44edb27e…` (CC-BY-4.0) | FP32, TF32 off | NeMo 2.4.0, torch 2.7.1+cu126, CPython 3.11.14, TDT `greedy_batch`, batch 1 (no CUDA graphs: `cuda-python` absent) |
| Faster-Whisper large-v3 | `Systran/faster-whisper-large-v3` @ `edaa852e…` (MIT) | float16 | faster-whisper 1.2.1, CTranslate2 4.9.1, CPython 3.11.14, beam 5, language ja, no VAD |
| Faster-Whisper large-v3-turbo | `deepdml/faster-whisper-large-v3-turbo-ct2` @ `4df90f75…` (MIT, third-party CTranslate2 conversion) | float16 | same as above |

Host: NVIDIA GeForce RTX 4080 16 GiB, driver 595.45.04, 12 logical CPUs.
Candidates ran sequentially, one process each, one untimed warm-up call, one
timed call per case, in-process sampler at 50 ms (psutil, NVML). The weights'
SHA-256 values are recorded in the private results.

## Results (245 cases)

| Candidate | CER | WER | Exact match | Target hits (散歩 / 3本) | RTF mean | Warm p50 / p95 (s) | Cold p50 / p95 (s) |
|---|---|---|---|---|---|---|---|
| Parakeet | 0.063 | 0.066 | 0.314 | 3/3 / 0/3 | 0.016 | 0.21 / 0.41 | 3.58 / 3.78 |
| Faster-Whisper large-v3 | 0.047 | 0.058 | 0.286 | 3/3 / 3/3 | 0.147 | 1.88 / 2.85 | 2.43 / 3.41 |
| Faster-Whisper large-v3-turbo | 0.047 | 0.061 | 0.274 | 3/3 / 3/3 | 0.075 | 0.86 / 1.77 | 1.74 / 2.65 |

| Candidate | Peak VRAM (MiB) | Peak RAM (MiB) | Mean CPU util | Mean GPU util* |
|---|---|---|---|---|
| Parakeet | 3212 | 2005 | 17% | 93% |
| Faster-Whisper large-v3 | 4256 | 764 | 12% | 99% |
| Faster-Whisper large-v3-turbo | 2400 | 758 | 17% | 95% |

\* device-level, polluted by another job (see limitations). CPU util is
process CPU time as a share of all logical cores. Peak VRAM is this process's
NVML usage including resident weights.

Observation on the contrast rule: Parakeet matched 散歩 in all 3 items but
produced a kana reading of the counter instead of `3本` in all 3 contrast
items, so it earns 0/3 on the 3本 side under the harness rule (digit 3 followed
by 本). Both Whisper variants matched all 6. With only 6 synthetic items this is
an indication, not a rate.

## Proposed screening thresholds (CER ≤ 0.10, WER ≤ 0.20, mean RTF ≤ 1.0)

All three candidates are within all three thresholds on this fixture, on the
RTX 4080 and on the GTX 1080 Ti. The thresholds are screening only; the choice
is a human decision.

## GTX 1080 Ti measurement (production LXC, GPU lent for the run)

Same fixture, same harness and the same pinned models, run on a GTX 1080 Ti
(11 GiB, driver 580.173.02, Ubuntu LXC) while its Worker was drained and the
GPU was otherwise free. Candidates ran one at a time, sequentially.

| Candidate | CER | WER | Exact match | 散歩 / 3本 hits | Mean RTF | Warm p50 / p95 (s) | Cold p50 / p95 (s) | Peak VRAM (MiB) | Peak RAM (MiB) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Parakeet ja (FP32) | 0.063 | 0.066 | 0.314 | 3/3, 0/3 | 0.008 | 0.090 / 0.118 | 2.26 / 2.29 | 3,214 | 1,800 |
| Faster-Whisper large-v3 (float32) | 0.047 | 0.058 | 0.285 | 3/3, 3/3 | 0.144 | 1.72 / 2.57 | 2.28 / 3.12 | 7,964 | 1,698 |
| Faster-Whisper large-v3-turbo (float32) | 0.047 | 0.061 | 0.273 | 3/3, 3/3 | 0.068 | 0.795 / 0.996 | 0.945 / 1.15 | 4,284 | 808 |

All three are within all three screening thresholds. Accuracy is the same as on
the RTX 4080 (same decoding; differences only from numerics).

- **Whisper runs in float32, not float16.** CTranslate2 refuses float16 on this
  GPU (compute capability 6.1: `Requested float16 compute type, but the target
  device or backend do not support efficient float16 computation`); float32 is
  the closest choice that keeps the accuracy. This differs from the RTX 4080 run
  (float16), so the Whisper latencies and VRAM are not comparable across the
  two GPUs. int8 variants were not measured.
- **VRAM is device-level.** Inside an LXC the driver cannot attribute memory to
  a process, so peak VRAM is the device's used memory during the run (the GPU was
  otherwise free, 2 MiB before). A first attempt sampled per-process memory and
  recorded 0 MiB for every case; it was discarded and the run repeated.
- Mean GPU utilisation during calls: about 71 % (Parakeet), 96 % (large-v3),
  89 % (turbo); mean CPU utilisation (share of the 8-core host) 13 to 26 %.
- Cold latency per case is an estimate, as above. FLEURS, noise and contrast
  limitations below apply unchanged.
- The 1080 Ti identity requirement of the gate is met for the measurement;
  the selection gate itself stays `DECISION_REQUIRED` (a human choice).

## Limitations

- The first measurement ran on an RTX 4080 (Ada); its latency, VRAM and
  utilisation do not transfer to the 1080 Ti, which is measured separately
  above.
- During the RTX 4080 runs the GPU was shared with an unrelated job (about 10.6 GiB VRAM and 70–100%
  device utilisation) during every run. Latency and device GPU utilisation are
  contaminated, probably pessimistically.
- FLEURS is clean read speech, not conversation. The noise condition is
  synthetic noise on read speech. The 6 contrast items are synthetic speech and
  exercise the contrast rule and pipeline, not real-speaker robustness.
- Subset of 245 cases from one split: small; confidence intervals are wide and
  exact-match differences between candidates are within noise.
- Cold latency per case is an estimate (one cold first request per process; its
  overhead added to each case's warm time), not a per-case restart.
- Reference numeral/script style follows FLEURS normalisation; style mismatches
  (kana vs kanji, digits vs kanji numerals) count as errors.
- Whisper decoding uses beam 5 without VAD or temperature tuning; Parakeet ran
  without CUDA graphs. Other settings could change speed.
- The harness still reports `empirical_completion: false` and the human
  selection gate stays `DECISION_REQUIRED`.

## Maintainer decision (2026-11-27)

The maintainer selected **`deepdml/faster-whisper-large-v3-turbo-ct2`** (MIT, a
third-party CTranslate2 conversion of Whisper large-v3-turbo) at the pinned revision
above, **float32** on the GTX 1080 Ti, as the Japanese STT candidate for DES-AUDIO-006.

Reasons recorded: accuracy equal to large-v3 on this fixture (CER 0.047, WER 0.061,
both contrast targets 3/3) at about half its peak VRAM (about 4.2 GiB against 7.8 GiB
of 11 GiB) and under a second of warm p95 latency; Parakeet is much faster but wrote
the counter in the `3本` items as a kana reading (0/3).

Conditions attached to the selection:

- **Real-speaker re-check before any activation.** The evidence is read speech plus six
  synthetic contrast items; a conversational, real-speaker check of the `散歩` / `3本`
  contrast and of numerals is still required.
- The conversion is third-party; the pinned revision and weight SHA-256 are the identity
  to deploy, not the model name.
- float32 is what was measured on this GPU; int8 is unmeasured and would need its own
  measurement because it changes accuracy.

This note records the decision; the harness itself still reports
`empirical_completion: false` and a `DECISION_REQUIRED` gate by design.
