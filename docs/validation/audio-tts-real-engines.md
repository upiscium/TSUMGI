# Real-engine evidence for VOICEVOX and Style-Bert-VITS2 (#215, #216, #247)

**This is evidence for the maintainer's qualification decision. It is NOT a
qualification.** Nothing here approves, activates or releases any audio
provider, voice or deployment. Every result below was produced on one
developer machine, not on the production Ubuntu LXC hosts, and must be repeated
there (see [Open items](#open-items-to-repeat-on-an-ubuntu-lxc)).

Test-only use: the repository owner approved running the real engines and
voices below for TEST purposes. Generated audio, model weights and the engine
tree were kept private and are not in this repository. No audio content or
transcript is recorded here; the 20 test sentences were generic Japanese
sentences (short greetings through about 40 characters).

## Environment (not the production LXC)

| Item | Value |
| --- | --- |
| Machine | one workstation, 12 CPU threads (AMD Ryzen 7 8700G), 62 GiB RAM, Linux 6.18, NixOS-style host |
| GPU | NVIDIA RTX 4080 16 GiB, driver 595.45 (about 11 GiB already used by another tenant during the runs) |
| Host loader | glibc 2.44; `/lib64/ld-linux-x86-64.so.2` is a **nix-ld** shim (a host-loader modification) |
| Load | the host was shared with unrelated CPU-heavy jobs; latency numbers are indicative, not benchmarks |
| Python for TSUMGI | CPython 3.12.16, repository at `origin/main` 391bc13 plus the fix commit below |

## Artifact identities (digests verified before anything ran)

VOICEVOX Linux CPU x64 0.26.2 (single `7z.001` release asset):

| Artifact | SHA-256 | Result |
| --- | --- | --- |
| Archive | `483633de02700cd918731538543a22ce895877aeb09450a990ff38af6e4c26c0` | match |
| `run` | `acf74848282c0ff6f0aaf8514427c5f44b0992c26b48448d76b35a97dbe81a25` | match |
| `model/0.vvm` | `ecd35374d4182cd883cba5040376f7f888cc6ba248b1c2f4cea07cdb34bb1318` | match |

Style-Bert-VITS2 JVNV, `litagin/style_bert_vits2_jvnv` at
`205830ca1d49e666ddfbf2a755f0108e9cade4dd`, model `jvnv-F1-jp`:

| Artifact | SHA-256 | Result |
| --- | --- | --- |
| `jvnv-F1-jp_e160_s14000.safetensors` | `a90fa6c9444d9235c9ec4db99daf7c5c6a21cc26ca141b4c48455d66a3257d01` | match |
| `config.json` | `d310da5c1e9b095a833591b4bc07209631d6edece263da88e597aaf7f9984d3d` | match |
| `style_vectors.npy` | `1f959bb45ed0922efc31ff24e9147253814f42cb1d2d1e2bb10391a9df368489` | match |
| encoder `model.safetensors` (ku-nlp deberta-v2-large-japanese-char-wwm @ `547b0e8b`) | `2630f547d018524a7b03506a42c700cbac49e29bdc441845b0615bfb3b5d74d2` | match |
| encoder config/tokenizer files | git blob ids in `jvnv_assets.py` | accepted by `JvnvInputs.preflight` |

Open JTalk dictionary `open_jtalk_dic_utf_8-1.11` (GitHub release tarball) was
obtained separately; its inventory digest was computed with
`jvnv_assets.dictionary_inventory_sha256` and accepted. Its provenance review
is still an operator task.

## Versions

* VOICEVOX engine 0.26.2 (`/version`), 44 speakers / 128 styles in `/speakers`.
* Style-Bert-VITS2 2.7.0 at `d8148f3090ee5038ca7b4e4b327116c64467f952`,
  `pyopenjtalk-dict` 0.3.4.dev2, CPython 3.11.14 (python-build-standalone,
  copied binary), torch 2.3.1+cpu (CPU run) and 2.3.1+cu121 (GPU run),
  transformers 4.44.2, tokenizers 0.19.1, huggingface-hub 0.25.2,
  accelerate 0.34.2, setuptools 75.8.0, numpy 1.26.4, safetensors 0.8.0.
  The transformers, tokenizers, huggingface-hub, accelerate and setuptools
  versions had to be chosen by hand (see defect S1).

## How the repository's own components were used

* **VOICEVOX**: `VoicevoxProvider.create_manifest_runtime` (real
  `LinuxVoicevoxOwnedProcessControl`, real `VoicevoxLoopbackHttpApi`, real
  `VoicevoxOwnedManagedRuntime`) against the real pinned engine. Synthesis went
  through `runtime.executor().execute_audio` with an in-memory media stub that
  only records the WAV. No source was mocked.
* **#247 attach mode**: `VoicevoxDiscoveryProbe`, `ExternalVoicevoxManagedRuntime`
  and the local preview app (`voicevox_local_preview`) against a user-style
  engine on 127.0.0.1:50021.
* **Style-Bert-VITS2**: `StyleBertManifestProvider.create_manifest_runtime`
  with the real owned child `jvnv_child.py` over its framed IPC. Only the two
  production byte pins that cannot be reproduced from the repository
  (`INTERPRETER_SHA256`, `LOCK_SHA256` in `jvnv_assets.py`) were substituted in
  the test harness with the hashes of the interpreter and lock actually used.
  All other asset, dictionary and encoder pins were the production ones.
  The repository child accepts CPU only, so the GPU numbers come from direct
  `TTSModel.infer` in a separate CUDA environment, not from the Worker chain.

## Results: VOICEVOX 0.26.2 (CPU)

| Check | Result |
| --- | --- |
| Pinned unmodified `run` starts here | yes, via the nix-ld loader; the PyInstaller bootloader re-execs the same PID; 29 of 38 mapped files came from the engine tree, 8 from the host glibc |
| Production-host ABI verdict | **not established** (this host has nix-ld; the LXCs are Ubuntu) |
| Start to READY | about 0.6 s warm; stop about 0.14 s; no process left after stop (25 of 25 cycles after the fix) |
| Listener | exactly one socket, 127.0.0.1 only |
| `/version` | `0.26.2` |
| `/speakers` | 44 speakers, 128 styles, 14,624 bytes, keys `name,speaker_uuid,styles,supported_features,version` (speaker) and `id,name,type` (style); all types `streaming_talk`; style ids globally unique |
| Approved voice (UUID `388f246b-...`, style 3, `ノーマル`, `streaming_talk`) | present exactly once; READY accepted it |
| Anything else accepted | no: other style ids of the same speaker (for example 1), a wrong UUID, an unknown alias, a wrong format and a wrong semantic revision are all refused before any engine call (`voice_mismatch`, `invalid_request`, `serving_mismatch`) |
| 20 sentences through the executor | 20 of 20 succeeded |
| WAV | RIFF PCM16, mono, 24,000 Hz, canonical 44-byte header (16-byte fmt chunk); `_wav_meta` accepted all; 1.06 to 8.96 s |
| Determinism | same text twice gave the same SHA-256 in 20 of 20; 20 distinct outputs |
| Latency (executor, includes `/version`, `/speakers`, `/audio_query`, `/synthesis`) | p50 0.70 s, p95 1.21 s, max 1.29 s |
| RTF | p50 0.14, p95 0.19 |
| Engine memory | RSS 735 MiB (peak sampled equal), 33 threads, one process |
| Process death (SIGKILL) | next `health()` is FAILED (`voicevox_probe_failed`), executor refuses jobs, `stop()` returns in under 0.1 s, no leftover process, restart of the same runtime refused |
| Foreign listener on the port before start | refused, engine never launched |
| Foreign well-formed fake engine (correct version and `/speakers`) on the port | refused (port occupied), engine never launched |
| Malformed or bad raw requests to the engine (bad speaker, no text, malformed or empty synthesis body) | engine answers 422 each time |
| Oversized text (8,193 bytes) | `text_exceeded`, no engine call |
| Reuse of a non-empty private HOME | start refused, no process launched (but see D3) |

### #247 attach mode (observation only; no admission exists)

| Check | Result |
| --- | --- |
| No engine on 127.0.0.1:50021 | `absent` |
| Real engine | `compatible_observed`, version 0.26.2, 128 styles, digest stable across probes; `ready=False`, `can_apply=False` |
| `ExternalVoicevoxManagedRuntime` | `degraded`, `ready=False`, `executor()` raises `voicevox_external_not_admitted` (as designed) |
| Engine killed afterwards | `degraded` / `voicevox_external_absent`, then `absent` |
| Listener that accepts and stays silent | `incompatible` after the 2.0 s timeout |
| Foreign server returning non-conforming JSON | `incompatible` |
| Foreign server returning a well-formed `/speakers` with version `0.14.4` | **`compatible_observed`** (see D7) |
| Local preview app (list voices, speech) | list 200; three sentences 200 `audio/wav` RIFF; latency 0.3 to 1.1 s; wrong bearer 401; wrong speaker UUID for style 3 refused (503 `voice_unavailable`); texts of 60, 100, 150 and 300 characters all 200 (compare D2) |

## Results: Style-Bert-VITS2 JVNV `jvnv-F1-jp`

CPU rows use the real owned child (single thread by design). GPU rows use
direct inference (the repository child cannot run on GPU, see S7).

| Check | CPU (Worker chain) | GPU (RTX 4080, direct) |
| --- | --- | --- |
| Boot to READY, includes the readiness synthesis | 7 to 9 s warm; 116 s once under heavy I/O contention | model load 4.8 s |
| 20 sentences | 20 of 20 succeeded | 20 of 20 |
| WAV | RIFF PCM16 mono 44,100 Hz, 16-byte fmt, accepted by the child and parent validators; 0.86 to 6.72 s | same sample format; 0.88 to 6.71 s |
| Latency p50 / p95 / max | 9.2 s / 11.6 s / 14.3 s | 0.24 s / 0.26 s / 0.26 s |
| RTF p50 / p95 | 2.17 / 2.40 (slower than real time) | 0.053 / 0.142 |
| Memory | child RSS 2.3 GiB after boot, 3.4 GiB after one long inference, 7.5 GiB after about 25 inferences | process VRAM 2.08 GiB (torch peak allocated 1.80 GiB), host RSS 2.15 GiB |
| Same text twice, same digest | 0 of 5 | 0 of 5 |
| Voice, style, speaker, format negatives | all refused (`audio_voice_mismatch`, `invalid_audio_job`) | not applicable |
| Text limit (256 bytes) | 86 Japanese characters (258 bytes) refused (`audio_input_exceeded`) | not applicable |
| Event-loop lag while the child infers | max 8.7 ms over the 25-inference run, 0.8 ms during a 21.7 s inference | in-process `infer` blocked the loop for 0.174 s, the full inference time |
| Child SIGKILL while READY | health FAILED (`stylebert_process_lost`), jobs refused, no leftover child | not applicable |
| Child SIGKILL mid-inference | job fails (`audio_synthesis_failed`), FAILED, no leftover child | not applicable |
| Child SIGSTOPped mid-job, then `stop()` | `stop()` took 1.08 s, child reaped | not applicable |
| Job cancelled mid-inference | child killed, runtime FAILED (no restart), later jobs refused | not applicable |
| Runtime parent killed mid-inference | orphan child exited by itself within seconds, after its in-flight inference ended | not applicable |

## Defects and gaps found (most important first)

### Fixed in this branch

* **D1: the real VOICEVOX bootloader re-exec made roughly 1 in 6 starts
  fail.** `run` is a PyInstaller bootloader that re-execs itself about 1 ms
  after spawn. During `execve` the same PID reports an empty
  `/proc/PID/cmdline`. `_proc_fixed_command`
  (`src/tsumgi/runtime/providers/voicevox_linux_process.py`) treated that as an
  identity failure; `inspect()` raised, `VoicevoxOwnedManagedRuntime.health()`
  (`voicevox_managed.py`) caught it as `voicevox_probe_failed`, and the first
  post-launch probe latched FAILED. Observed 4 failures in 24 and 2 in 6 starts
  before; 0 in 25 after. Fix: retry an empty cmdline for at most about 50 ms.
  Regression tests added in `tests/test_voicevox_linux_process.py`. The adapter
  source bundle digest in the serving declaration changes with this edit, so a
  new reviewed serving revision is needed.

### Described only (not changed)

* **D2: the declared input limit does not match what the adapter accepts.**
  `voicevox_manifest.py` declares `input_bytes: 8192` (about 2,700
  characters) and `VoicevoxAdapterConfig.max_text_bytes` is 8,192, but
  `_validate_query` in `voicevox.py` rejects an `/audio_query` answer above 2,048
  JSON nodes or any list above 256 items. Real answers carry about 20 nodes per
  character, so natural text of 80 characters passed, 120 failed
  (`invalid_query`) and everything above about 1,200 characters fails at the
  256 KiB query cap, reported as the generic `engine_unavailable`. The local
  preview app already uses 16,384 nodes (`_MAX_QUERY_NODES`). A decision is
  needed: raise the node and list caps, or lower the declared limit (either
  changes the serving identity).
* **D3: a launch refusal before spawn leaves the runtime cleanup-uncertain.**
  When `LinuxVoicevoxOwnedProcessControl.launch` refuses before spawning (for
  example a non-empty private HOME), no nonce is recorded, so the runtime's
  `start()` error path calls `stop_owned` with an unknown nonce, which fails
  closed. The runtime stays `cleanup_uncertain` and a later `stop()` raises
  `voicevox_owned_stop_failed`, although no process ever existed.
  Pointers: `voicevox_managed.py` `start()` (`except BaseException` block) and
  `voicevox_linux_process.py` `stop_owned` (nonce check). Needs a design choice
  on how a pre-spawn refusal is recorded.
* **D4: the engine writes state into the private HOME on every start**
  (`.local/share/voicevox-engine/` with presets, compiled user dictionary,
  library directories), while `_launch_environment` refuses any non-empty HOME.
  So each start needs a fresh HOME (tmpfs or a per-start directory). A systemd
  `Restart=` policy on a fixed HOME would fail every restart; together with D3
  the failure is also hard to diagnose. The same applies to the Style-Bert child
  (two entries appeared in its HOME after start).
* **D5: the managed launch does not pass `--disable_mutable_api`.** The engine
  exposes 27 non-GET operations on loopback, including `/setting`, user
  dictionary and preset endpoints, so any local process can alter
  pronunciation. The engine's default CORS policy did refuse a foreign Origin
  (403). `voicevox_manifest.py` `create_manifest_runtime` fixes the argv; adding
  the flag changes the launch identity.
* **D6: the stock engine logs request URLs including the percent-encoded
  text.** Confirmed with a user-run engine. The managed controller discards the
  engine's stdout and stderr, so it is contained there, but not for #247 attach
  mode, where the user's own engine log receives every sentence.
* **D7: #247 discovery has no identity or version authority.**
  `VoicevoxDiscoveryProbe.probe_once` / `_catalog` (`voicevox_discovery.py`)
  accepts any well-formed `/version` and `/speakers`, so a foreign server
  reporting version 0.14.4 is `compatible_observed`. The version only enters the
  observed digest. Refusal of a wrong version or foreign server therefore
  depends on a later layer that does not exist yet (the external runtime never
  admits).
* **S1: the Style-Bert dependency set is not reproducible from the
  repository.** The reviewed lock (`LOCK_SHA256`) and interpreter
  (`INTERPRETER_SHA256`) bytes are not published, and an unconstrained resolve
  fails: transformers 5.x needs torch 2.6 or newer; `accelerate` (needed
  because `jvnv_child.py` passes `device_map`) and `setuptools` below 81
  (`pyopenjtalk` imports `pkg_resources`) are not requirements of
  `style-bert-vits2`. The working set is listed under Versions. CPython must be
  exactly 3.11.14 (`jvnv_child._native_loader`); the current nixpkgs 3.11 is
  3.11.18 and would be refused.
* **S2: the 4,096 MiB RAM gate is too low.** `required_host_ram_mb=4096`
  (`style_bert_vits2_manifest.py`) against a child RSS of 3.4 GiB after one
  long inference and 7.5 GiB after about 25 inferences on CPU. Whether the
  growth plateaus was not measured.
* **S3: CPU inference is slower than real time under the pinned settings.**
  The child fixes one thread (`jvnv_child.py`: `torch.set_num_threads(1)` and
  the `OMP_`/`MKL_`/`OPENBLAS_NUM_THREADS=1` environment). RTF 2.2 here. Boot
  was 116 s once under I/O contention against `boot_timeout_seconds=120`, so
  cold-start headroom is thin.
* **S4: the 256-byte input limit does not bound cost.** An in-limit 255-byte
  ASCII request ran past the 90 s inference timeout on CPU, which latches the
  runtime FAILED; the same class of text raised CUDA out-of-memory on the GPU.
  One legal request can therefore take the instance down until the Worker is
  restarted.
* **S5: every cancel or timeout is a Worker restart.** By design the child is
  killed and the runtime latches FAILED (no restart), so a normal client cancel
  costs a restart plus the 7 to 116 s boot.
* **S6: output is not deterministic**, unlike VOICEVOX. Digest equality cannot
  be used to compare runs; the readiness probe is validated structurally, not
  against a fixed digest.
* **S7: no GPU path.** The B5c design note allows a Worker-owned GPU UUID, but
  `jvnv_child.py` accepts only `device == "cpu"` and torch `2.3.1+cpu`. The GPU
  figures above are therefore outside the Worker chain.
* **S8: the Style-Bert library logs the raw input text at INFO** to stdout.
  The child redirects descriptors 0 to 2 to `/dev/null`, so it is contained
  there; any in-process or debug use would log user text.

### Assumptions that the real engines confirmed

The speaker UUID, global style id 3, `ノーマル`, type `streaming_talk` and exact
single occurrence; loopback-only listener; canonical 44-byte PCM16 WAV for both
engines; engine re-exec keeps the PID and executable inode so the
inode-and-argv identity design holds; wildcard or foreign listeners refused;
stop and reap leave no process; owned-child isolation keeps the event loop
responsive during native inference; `SIGKILL` and `SIGSTOP` of the child are
detected and reaped.

## Tests run

`tests/test_voicevox_*.py`, `tests/test_jvnv_*.py` and
`tests/test_style_bert_vits2_*.py`: all pass after the D1 fix (including three new
tests). The documentation build was run as described in the repository.

## Licence and credit

* VOICEVOX engine and voice: usage was test-only with the owner's approval;
  credit line `VOICEVOX:ずんだもん`. The engine and voice terms are those of
  upstream and are an operator review item.
* JVNV `jvnv-F1-jp`: CC BY-SA 4.0, credit `litagin / JVNV`. Encoder and
  Style-Bert-VITS2 licences are separate operator review items
  (see the Style-Bert manifest design note).
* No audio, weights or engine files are redistributed by this repository.

## Open items to repeat on an Ubuntu LXC

1. The VOICEVOX `run` ABI/loader verdict on the actual Ubuntu LXC image
   (no nix-ld there); confirm the bootloader re-exec and the D1 fix hold.
2. Cold-start, latency, RTF and memory for both engines on the LXC CPU
   allocation; Style-Bert RAM and boot budget (S2, S3).
3. Decisions on D2 to D7 and S1 to S7 above, then a new reviewed serving
   revision.
4. A reproducible, hashed Style-Bert dependency lock and interpreter, then the
   unmodified production pins (no substituted pins).
5. Process-group and descendant cleanup, privilege, resource limits and
   private-HOME lifecycle under the real service manager.
6. Rights review and Japanese quality listening review by a human; none of
   that was done here.
7. GPU Style-Bert only if a child GPU path is implemented (S7).
