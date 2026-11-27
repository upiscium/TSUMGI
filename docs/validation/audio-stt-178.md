# Issue #178 — private Japanese STT comparison harness

**State: harness only; no model was run and no empirical result is claimed.**
This is an offline scorer for operator-provisioned labelled fixtures and
measured candidate results. It does not access the network, install or download
models, open audio, or start runtimes. The report is a **private local
artifact**, not public-safe: free-form conditions, candidate identities,
support evidence, failures, or limitations can contain sensitive information.
No real recording or private transcript belongs in this repository.

## Inputs and run

Use `audio-stt-178-v1` JSON fixture with explicit `normalization`,
`segmentation`, and `test_conditions`. Each case contains a private local
audio reference (kept outside version control), duration, gold transcript,
unique ID, and tags. The fixture must contain at least one tagged case whose
target is `散歩` and at least one whose target is `3本` (both, or it is
rejected), each tagged `homophone:散歩-vs-3本` with a `target` field.
Represent two contextual sentences with gold reference text containing the
distinct target words `散歩` and `3本`; isolated-word-only items are rejected.
The report scores target hits independently for each target and in aggregate,
separately from full-utterance exact match. After the fixture's Unicode
normalization and whitespace removal, only an answer containing the expected
string and not its alternative earns a hit. An answer containing both
`散歩` and `3本` counts as a miss, not an excluded case. This checks for the
strings, not Japanese morphological word boundaries, with one exception that
matters for the numeral: `3本` preceded by another digit (ASCII or full-width) or
by a decimal or thousands separator (`13本`, `23本`, `0.3本`, `1,3本`) is a
different number and earns no hit, so a model that gets the number wrong is not
credited. Kanji numerals before the `3` (`十3本`) are not recognised; the
fixture should not rely on them. Include natural conversation,
numerals, names, noise, pauses, and short/long utterances in the *real*
curated fixture. The synthetic unit fixture is deliberately not such evidence.

The operator runs pinned Parakeet and Faster-Whisper independently under
identical conditions, records raw per-case hypotheses and actual measured
resources in a local results JSON, then scores:

```sh
PYTHONPATH=src python -m tsumgi.validation.audio_stt_178 \
  --fixture /private/local/stt-fixture.json \
  --results /private/local/stt-results.json \
  --report /private/local/stt-report.json
```

`results.json` has a `candidates` array. Each item supplies `name`, `model`,
`release`, `quantization`, `runtime`, `support_evidence`, a `predictions`
object mapping every fixture ID to transcript, and `measured` arrays with
exactly one value per case: `warm_latency_seconds`,
`cold_latency_seconds`, `cpu_utilization_percent`,
`gpu_utilization_percent`, `peak_vram_mib`, `peak_ram_mib`. Include
`failures` and `limitations` arrays when applicable. Missing identity,
predictions, or measured metrics is an error, not a pass. Support evidence
must describe what was actually verified; `unknown` is allowed, model-card
claims alone do not qualify. The harness refuses to overwrite the report. It
writes a private temporary file and hard-links it into place (atomic, mode
`0600`); on a file system that reports hard links as unsupported (`EPERM`, `EOPNOTSUPP`,
`ENOTSUP`, `ENOSYS`, `EMLINK`, `EXDEV`) it falls back to an exclusive create,
which still never replaces a file and removes a partial one it created. Any
other error (`ENOSPC`, `EIO`, `EACCES`) is a failure, not a reason to try again.
The fallback is not atomic: a crash during it can leave a truncated report,
which the no-overwrite rule then refuses to replace, so delete it by hand. The report
parent must already exist. The report records `fixture_sha256`, the hash of the fixture's canonical
re-serialised JSON (not of the file bytes, so `sha256sum` will not match it),
and `results_sha256`, the hash of the results file's raw bytes. Input files
must be regular files: symbolic links, FIFOs and devices are refused (a hard
link to a regular file is not detected). JSON must be UTF-8 without a byte-order
mark, and must not contain `NaN`, `Infinity` or duplicate keys,
identity fields are capped at 256 characters, support evidence at 4096, and
`failures` / `limitations` must be lists of at most 100 strings of 2000
characters. Fixture size is capped at 4 MiB, results at 32 MiB, cases
at 500, candidates at 8, normalized text at 10,000 characters, and aggregate
edit-distance work per candidate at 10 million cells. Keep fixture, results,
and report private: they can contain transcripts, paths, and free-form
metadata.

Do not publish this report. If a public summary is needed, create a separate,
manually reviewed and redacted summary containing only approved aggregate
metrics and non-sensitive model identifiers. Do not copy free-form conditions,
support evidence, case IDs, paths, failure details, or transcripts without
review.

## Metric interpretation

The fixture fixes Unicode normalization (`NFC`, `NFKC`, or `none`) and whether
whitespace is removed for exact match and character error rate (CER). CER
uses normalized characters under that policy. WER preserves whitespace and uses
whitespace-delimited tokens; `segmentation` is a required, non-empty statement
of the rule the operator used (its content is not checked) and WER has
limited meaning for unsegmented Japanese. CER and WER are explicitly
macro-averages of per-utterance rates, not corpus-weighted rates. Exact match
compares normalized strings under the configured whitespace policy. Warm/cold
p50/p95 use linear-interpolated percentiles. RTF is warm elapsed time divided
by each case's supplied audio duration and then macro-averaged.

CPU/GPU utilization and peak host/GPU memory are operator measurements. The
scorer validates and reports supplied measurements but does not sample
hardware itself; record sampling tool, interval, driver, host, GPU (including
the GTX 1080 Ti target), power/configuration, concurrency, warm-up and runtime
conditions in `test_conditions`. Those conditions and the traceable private
source evidence must accompany any actual human review. Synthetic test values
are never actual measurements.

All report output is local and mode `0600`; error messages on failure are
generic and do not print exception text, transcript, or input path.

## Human selection gate

Current gate: **a candidate was selected on 2026-11-27 (Faster-Whisper
large-v3-turbo, float32), with a real-speaker re-check required before activation**; see
[the results note](audio-stt-178-results.md#maintainer-decision-2026-11-27). The harness
itself still reports `DECISION_REQUIRED`, by design. The options before the decision were the pinned Japanese Parakeet candidate,
pinned Faster-Whisper candidate, or neither / gather more evidence. Initial
measurable candidate thresholds are CER ≤ 0.10, WER ≤ 0.20 and mean RTF ≤
1.0 on the agreed labelled private fixture, with the agreed GTX 1080 Ti
hardware identity and complete measurements. These are proposed screening
thresholds, not a pass claim or automatic selection. Compare correctness
separately from speed/resources; document per-case observed failures and
limitations. A human explicitly chooses only among actually tested and
identified candidates. Untested models, unsupported combinations, or
incomplete evidence cannot pass based on documentation/model-card claims.

Unknown model support, model/release/quantization/runtime identity, fixture
hash, test conditions, observed failures, and limitations remain explicit
report evidence. The tool always marks `empirical_completion: false` and
leaves the gate at `DECISION_REQUIRED`; actual experiments and human decision
are outside this code change.
