# Real-audio end-to-end evidence for #181 (scratch stack, one workstation)

**This is evidence for the maintainer's decision. It is NOT a qualification and
NOT a release gate.** It records the first feature-level run of the public audio
Gateway, Control (PostgreSQL), per-Worker authentication and three real
Workers (VOICEVOX, Style-Bert-VITS2 JVNV, faster-whisper-ja) together. Nothing
here approves, activates or releases any provider, voice or deployment, and no
claim is made beyond what was measured.

No audio, transcript, case identifier, token or private path is recorded here.
The synthetic voices are not real speakers: no real-speaker result exists.

## Scope, environment and what this is not

| Item | Value |
| --- | --- |
| Machine | one developer workstation (12 CPU threads, 62 GiB RAM, Linux 6.18, NixOS-style host with a nix-ld shim), **not** a production Ubuntu LXC and **not** the GTX 1080 Ti target |
| GPU | RTX 4080 16 GiB (compute capability 8.9), shared with another tenant that held about 9.7 GiB and CPU time throughout |
| Load | host load average 2.5 to 11 during the run; latency numbers are indicative, not benchmarks |
| Disk | the host disk was 100 % full (3.8 to 4.9 GiB free); the run was sized for that |
| Repository | `origin/main` 550e75b plus the one fix commit named below; CPython 3.12 (Control/Worker), 3.11.14 (engine children) |
| Stack | PostgreSQL 17.12 on a unix socket, migrations 000 to 008; one Control (bearer client auth, `worker_auth.mode = per-worker`, three distinct Worker tokens, separate client and audio tokens, audio epoch preparation and fencing **off**); three Workers with real manifests; everything bound to 127.0.0.1; throwaway tokens created at run time |
| Config of interest | `lease_seconds = 20`, `worker_ttl_seconds = 15` (short, to make lease-loss tests fit; defaults are 300 and 60), `max_pending_admissions = 4`, spool TTL 300 s (input) / 600 s (output) |

Not exercised: a production LXC, systemd/NixOS units (the Workers were started by a
launcher, so `Restart=no`, `KillMode`, `ProtectHome` and the unit sandbox were not
in play), the GTX 1080 Ti, real speakers, Control HA, streaming/first-audio
latency (nonstreaming complete-result time only), PostgreSQL deployment tuning,
and any installer or SetupPlan path.

Test-purpose records: the manifests require digests of an operator-reviewed rights
record and a validation-evidence record. Two **TEST-PURPOSE ONLY** plain-text
records were written (owner approval given in conversation on 2026-11-27 and
28 for test use of the three models/voices, outputs private, no redistribution).
They are stand-ins, **not** a production rights review or a validation record,
and their digests are what the manifests carry:
rights `sha256:070e1a11839b8cfb5570c2b7118e548547e3dd0b6e52d64ae69e656266e85e9f`,
evidence `sha256:4c9f0db3659e271ba3ed5cee984d4550d29341156777d7561f082aeaa0f91938`.

## Identities and pins

| Provider | Identity |
| --- | --- |
| VOICEVOX | engine 0.26.2, archive `sha256:483633de...4c26c0` (the production pin), voice ずんだもん／ノーマル (alias `zundamon-normal`, style 3), credit `VOICEVOX:ずんだもん`, deployment revision `sha256:a545147e0aedd0036d29ea7c7a4138a99f1764ed5ff4e2c9092c85e5bc1614ff`, CPU, one slot |
| Style-Bert-VITS2 | JVNV `jvnv-F1-jp` (alias `jvnv-f1-neutral`), CC BY-SA 4.0, credit `litagin / JVNV (CC BY-SA 4.0)`, engine 2.7.0, deployment revision `sha256:137753d2ca3f587c0940852727781c620f674b219000a16d323ab4f2c02356cf`, CPU, one slot. **Two production byte pins could not be reproduced and were substituted with the digests of the staged interpreter** (`sha256:6ff97f60...820944`) **and lock** (`sha256:a4ba6b8b...7974`), exactly as in the earlier real-engine run (see [S1](audio-tts-real-engines.md)); all other asset pins were the production ones |
| faster-whisper-ja | `deepdml/faster-whisper-large-v3-turbo-ct2` at revision `4df90f75...f8a`, float32, faster-whisper 1.2.1, CTranslate2 4.9.1; model file digests were the source-pinned ones; interpreter `sha256:6ff97f60...820944`, lock `sha256:11717a8c...320e` are **operator-supplied test staging digests** (the install was hard-linked from an earlier environment, not reproduced from the lock); GPU UUID of the 4080 declared with total/single-GPU capacity 16,376 MiB (floor 11,264 MiB satisfied) |
| Public profiles | `voicevox-zundamon`, `jvnv-f1`, `stt-fw-ja` in one reviewed catalog; each profile bound to its own deployment revision |

The faster-whisper child needs the host driver directory appended to its
`LD_LIBRARY_PATH` on this host (defect F1 of the earlier note). This was done in the
**launcher** (a wrapper that adjusts the provider's child environment before the
daemon starts); no repository source was edited for it.

## Results by leg

All checks below went through the public Gateway (`POST /v1/audio/speech`,
`POST /v1/audio/transcriptions`, `GET /v1/audio/voices`) with the audio client token,
Control and Workers authenticating with their own tokens.

### TTS on both profiles (10 Japanese sentences each)

| Check | VOICEVOX | Style-Bert-VITS2 JVNV |
| --- | --- | --- |
| Requests succeeded | 10 / 10 | 10 / 10 |
| Playable RIFF PCM16 mono, canonical 16-byte fmt | yes | yes |
| Sample rate | 24,000 Hz | 44,100 Hz |
| Duration range | 1.06 to 5.72 s | 0.84 to 4.96 s |
| Client digest equals the Job result artifact digest | 10 / 10 | 10 / 10 |
| Job bound to the requested deployment revision (no fallback to the other engine) | 10 / 10 | 10 / 10 |
| Distinct outputs | 10 of 10 | 10 of 10 |
| Client complete-result latency p50 / p95 / max | 0.72 / 0.93 / 0.96 s | 6.81 / 10.85 / 10.89 s |
| Real-time factor p50 / p95 | 0.19 / 0.24 | 2.21 / 2.60 (slower than real time) |
| Queue wait (Job created to started) p50 / p95 | 0.17 / 0.19 s | 0.16 / 0.19 s |
| Execution (Job started to finished) p50 / p95 | 0.51 / 0.71 s | 6.59 / 10.63 s |

The queue wait is the Worker poll interval (0.2 s), not contention. Model start-up
(Worker launch to READY, warm cache, separate from per-job time): VOICEVOX 1.1 to
1.3 s, JVNV 7.8 to 8.6 s, faster-whisper 5.5 to 6.3 s.

### STT (private labelled fixture, 245 clips: 239 FLEURS Japanese read speech, 6 synthetic contrast clips)

Scoring used the repository's normalization (NFKC, lower-case, punctuation and
whitespace removed; MeCab-segmented tokens for WER) from `audio-stt-178.md`.

| Check | Result |
| --- | --- |
| FLEURS clips accepted and transcribed as supplied | 239 / 239 |
| CER, macro (screening threshold 0.10) | 0.048 (clean 0.046, n = 209; noise 10 dB 0.061, n = 30; numeral-heavy 0.049, n = 136) |
| WER, macro, MeCab-segmented (screening threshold 0.20) | 0.062 |
| Exact match after normalization | 62 / 239 |
| Client complete-result latency p50 / p95 / max | 0.80 / 1.02 / 1.33 s |
| Execution (started to finished) p50 / p95 | 0.57 / 0.80 s |
| Aggregate RTF (3,228 s of audio) | 0.059 (threshold 1.0) |
| `散歩` / `3本` contrast clips | **6 / 6 hits, but only after a client-side re-encode**: the six clips as supplied were refused by the Gateway (415 `unsupported_audio`) because they carry a `LIST` chunk, see G5 |
| Round trip: VOICEVOX output resampled to 16 kHz, transcribed | 10 / 10 returned; CER 0.026, 6 / 10 exact |
| Round trip: JVNV output resampled to 16 kHz, transcribed | 10 / 10 returned; CER 0.049, 5 / 10 exact |
| Digital silence (3 s, 10 s) and low-level noise (3 s) | each returns a 14-character stock phrase with status 200 (documented limitation V0, reproduced through the full path) |

The thresholds are screening values only. The round trip is a **pipeline check**
(TTS output into STT through two resamples), not an accuracy claim. The 239
clips are one private fixture; no statement about real speakers or other
conditions is made. The result is in the same range as the earlier direct
measurements (CER about 0.04 to 0.05).

### Voice catalog, selection and fail-closed negatives

| Check | Result |
| --- | --- |
| `GET /v1/audio/voices` returns the exact alias, speaker/style, licence id and credit line per profile | yes |
| Speech responses carry the credit line | **no**: only the catalog does, see G9 |
| Unknown voice, voice of the other profile, unknown model, voice object with another speaker/style | refused 400/404, no Job |
| Vendor voice served only by its own profile; the other TTS profile still served while one Worker is offline | yes |
| VOICEVOX Worker stopped: its profile answers 503 `no_compatible_deployment`, no Job created, **no fallback** to JVNV | yes |
| Worker latched failed (engine killed): 503, no fallback | yes |

### Negative controls (84 request cases, all without fixture audio or text)

76 matched the expected status as written. Of the other 8, three differ only in
status class (JVNV over-length 413 for 400; missing file and missing boundary 415
for 400), four are the admitted-but-unusable audio formats (G5) and one is U+0000
(the fix below). **No rejected request created a Job except the four G5 cases.**

| Group | Result |
| --- | --- |
| TTS: formats mp3/opus/pcm/flac, empty and blank input, over-long input (VOICEVOX 16,385 bytes; JVNV over 256 bytes), unknown fields (`speed`, `stream`, `instructions`), missing fields, non-string input, malformed JSON, duplicate keys, `NaN`, wrong content type, GET, query string | all 400/413/415/405, no Job (JVNV over-length answers 413 rather than 400) |
| Auth: no token, wrong token, Control client token, Worker token and epoch tokens on the public routes | all 401 |
| Oversized bodies (2 MiB JSON, 2 MiB multipart) | refused 400/413 before work |
| STT filenames: `../` traversal, absolute path, backslash, NUL, non-`.wav`, URL | all 400 |
| STT bytes: wrong content type, MP3-like, random, 8-bit, float codec, truncated header, truncated data, zero frames, 31 s, over 1 MiB, `LIST` chunk | all 400/413/415 |
| STT fields: language `en`, formats `text`/`verbose_json`, missing format, `prompt`, `timestamp_granularities`, `temperature`, `stream`, unknown or TTS model, missing model/file | all 400/404/415 |
| Malformed multipart: no boundary, wrong boundary, truncated (no close, mid-file), bare LF, duplicate part, 5 KiB header, `Content-Transfer-Encoding`, urlencoded and JSON bodies, empty body | all 400/415 |
| **Stereo, 8 kHz, 24 kHz, 44.1 kHz PCM16 WAV** | **admitted as Jobs; the Worker refuses them and the client gets 502 `backend_failure`**: see G5 |
| TTS input containing U+0000 | **503 `admission_unavailable` before the fix, 400 after**: see the fix below |

Log and Job hygiene: after the whole run, Control, Worker and PostgreSQL logs
contained no sentence, transcript, token or audio marker (checked against every
sentence and transcript used and every token); error bodies carried only fixed
codes. Job JSON (`payload`, `result`) held no audio bytes, only opaque URIs and
digests, **but it does retain the TTS input text and the STT transcript** (see G7).

### Concurrency, overload, memory

Each Worker has one slot. Control refuses a request immediately when every
compatible Worker is at its `max_concurrency` (no queueing); the Gateway sheds
above `max_pending_admissions`. Clients with a 0.1 s retry loop (the table) showed:

| Profile | Clients | Complete-result p50 / p95 incl. retry wait | 429 retries | Throughput |
| --- | --- | --- | --- | --- |
| VOICEVOX | 1 / 2 / 4 | 1.55 / 2.05 s, 2.59 / 4.73 s, 4.88 / 9.05 s | 0 / 61 / 143 (16 requests each) | about 0.65 to 0.69 per s |
| JVNV | 1 / 2 / 4 | 2.74 / 9.95 s, 11.75 / 20.18 s, 15.12 / 21.58 s | 0 / 179 / 101 (6 requests each) | 0.16 to 0.22 per s |
| faster-whisper | 1 / 2 / 4 | 0.76 / 0.80 s, 1.30 / 1.92 s, 1.91 / 3.63 s | 0 / 30 / 71 (16 requests each) | 1.4 to 1.7 per s |

Without a retry loop, concurrency 2 and 4 against one slot returned 429
`overloaded` for the excess (for example 12 of 16 VOICEVOX requests at 2 clients),
12 clients returned 429 `admission_overloaded` as well. The VOICEVOX run times
here (execution 1.3 s) are slower than the quiet run (0.5 s) because the host was
loaded. A 90 s hot-retry storm (about 9,000 requests, 18 client threads over three
profiles) made Control and one Worker misbehave (G1).

| Memory (Worker process tree) | Value |
| --- | --- |
| VOICEVOX | 0.2 to 0.55 GiB RSS |
| JVNV (CPU, one thread) | 2.9 GiB right after start, 4.5 GiB after 10 sentences, 6.1 GiB peak after about 30 synthesis requests (the S2 4 GiB RAM gate is too low, as before) |
| faster-whisper | 0.86 GiB RSS, 3.3 GiB peak (load phase); device memory about 4.1 GiB for the child |
| Scratch PostgreSQL | 10 MB after about 750 Jobs |
| Spool | bounded by configured quotas; emptied by TTL |

### Deadline, cancellation, restart and fencing

| Check | Result |
| --- | --- |
| JVNV request past a 4 s request deadline | client 504 `deadline_exceeded` at 4.0 s, Job `cancelled`, no artifact |
| STT request past a 0.4 s deadline | client 504 at 0.40 s, Job `cancelled`, no transcript retained |
| Client disconnect mid-JVNV inference | Job `cancelled`; Worker runtime latched failed (cancel is a Worker restart, S5), restart took 7.8 s |
| Retry after cancellation | a new Job is created and succeeds; the cancelled Job keeps no artifact |
| SIGKILL of the JVNV Worker mid-job | owned child exits by itself in 9 s; Job `failed` with `worker lease expired` after 22 s; client 502; no audio published; Worker row goes offline; restart (new epoch) serves again |
| SIGKILL of the faster-whisper Worker mid-job | child exits in 1 s and the GPU memory is released; Job failed after 21 s (no transcript); the stale private run directory is removed on the next start |
| SIGKILL of the VOICEVOX engine mid-job | Job failed after 21.7 s; Worker latched failed and draining, further requests 503 (no fallback), restart needed |
| Control killed (SIGKILL) and restarted twice, down 10 to 12 s | Workers went offline via TTL and re-registered by themselves within about 30 s |
| Control SIGKILL with files in both spools | files stayed (orphans) until the next start, which removed the old spool directories and started empty |
| Spool write failure (file-size limit 150 KiB, errno EFBIG, **not** ENOSPC) | oversized input staging answered 503 `audio_unavailable` in 0.01 s, no partial file; Control stayed up; small requests worked; an oversized JVNV output failed the Job after 29.6 s via lease expiry |
| Spool quota (64 artifacts) exhausted by 63 successful requests | every further request 503 `audio_unavailable` immediately until the TTL expired (see G3) |

Protocol-level negatives were exercised with an adversarial Worker (a script
using the repository's `ControlClient`, a real per-Worker credential and the real
Control, with the real Worker stopped); 33 of 33 checks refused as designed:

| Check | Result |
| --- | --- |
| Another Worker's token on the output upload, completion, input download of a job it does not own (also through its own Worker URL) | 403 / 409 |
| Own Worker, wrong lease token, wrong attempt number, wrong runtime epoch (input and output routes, complete, renew) | 409 |
| Control client token and public audio token on Worker routes | 401 |
| Late complete, output upload and lease renewal after the lease expired | 409; the Job had already failed |
| Claim with an old runtime epoch after a new epoch registered | 409; complete and renew with the stale epoch 409 |
| New epoch registered while a lease is active | 409 |
| Download after completion | 409 |
| Claimant downloads exactly the client's uploaded bytes | yes |
| Client received only the current epoch's audio (an earlier staged WAV was never served) | yes |

Audio Jobs are submitted with `max_attempts = 1`, so lease loss ends the Job
`failed` and no second attempt exists; a "wrong-attempt output" cannot occur end to
end here and was exercised only by presenting wrong attempt numbers.

### Disabled audio leaves the old path working (AC-AUDIO-07, one install path)

A second Control on its own database with `[audio_gateway] enabled = false` (a
complete retained policy) and a plain executor Worker: generic Job submission
completed, `/v1/audio/*` answered 404, no spool directory held anything.
This is one source-run install path; the NixOS and generic-systemd paths were not run.

## Defects and gaps (most important first)

Fixed in this branch (separate commit):

* **F1 (low): U+0000 in speech input reached admission and returned 503.**
  `validate_speech_request` (`src/tsumgi/gateway/audio.py`) accepted U+0000; the
  PostgreSQL `jsonb` payload cannot hold it, so admission failed as
  `admission_unavailable` (a retry-looking server error for a client mistake).
  It is now refused as `invalid_request` (400); regression case added in
  `tests/test_audio_gateway_contracts.py`. Other control characters (for example
  U+0007, U+202E) are still passed to the engine; whether to reject them is a policy
  choice left open.

Described only:

* **G1 (high, not root-caused): the VOICEVOX Worker's runtime latched failed three
  times while the engine was healthy.** Twice during hot-loop concurrency runs and once
  during a Control restart sequence, the Worker logged "Runtime health lost
  (health_not_ready)" and went draining; the engine answered `/speakers` in 2.5 ms
  afterwards. The probe in `voicevox_managed.py` `health()` runs several `/proc`
  scans and two HTTP calls inside a 1.0 s budget (`probe_timeout_seconds`), and any
  failure latches FAILED for good (`worker/supervision.py` `check`). With the NixOS
  module's `Restart=no` this needs an operator. A deliberate stall (kill of Control
  for 12 s) did **not** reproduce it, so the trigger is probably host contention.
  Needs: a timing trace of the probe under load on the LXC, then a decision on a retry
  budget before latching.
* **G2 (medium): every uncertain Worker-to-Control failure is reported only by
  lease expiry.** Engine death mid-job, Worker kill and a failed output upload
  each ended the Job after the lease (20 s here), because
  `worker/runtime.py` deliberately does not fail an unconfirmed attempt. With the
  default `lease_seconds = 300` and a 120 s request deadline, the client would see
  504 instead of 502 after two minutes. The lease must be tuned below the request
  deadline, or the Worker should fail provably-failed Jobs promptly.
* **G3 (medium): terminal Jobs do not release spool files; only TTL does.** Input
  recordings and output WAVs stay until the TTL (300 / 600 s here) even after
  the Job succeeded (`audio/input_channel.py`, `audio/output_channel.py`;
  `control/api.py` maintenance loop reaps expired files only). Two consequences
  were observed: private audio is retained for the TTL, and
  `max_artifacts / TTL` caps throughput (64 artifacts over 300 s stopped the run at
  63 requests with 503 until expiry). Delete-on-terminal, or sizing guidance, is
  needed before an operator runbook.
* **G4 (medium): overload is load shedding, not queueing.** A second concurrent
  request for the same profile gets 429 `overloaded` (`control/postgres.py`
  `submit_job` when all compatible Workers are at capacity). There is no
  `Retry-After` header. Clients must retry; JVNV at 4 clients needed 101 to 179
  retries for 6 requests.
* **G5 (medium): the Gateway admits audio the selected STT Worker cannot take.**
  Stereo, 8, 24 and 44.1 kHz PCM16 WAVs pass the generic PCM16 validation of the
  input channel, become Jobs, are refused by the faster-whisper adapter (16 kHz
  mono only) and surface as 502 `backend_failure` with a failed Job. Conversely the
  canonical-44-byte policy refuses common encoder output (a `LIST` chunk) with 415.
  Both are interop traps; the serving contract does not declare rate or channels.
* **G6 (medium): cancel, deadline or client disconnect during JVNV inference
  latches the Worker** (S5, now seen through the Gateway): one impatient client
  takes the profile offline until a 8 s restart (or longer).
* **G7 (medium): Job rows keep the TTS input text and the STT transcript.**
  `jobs.payload` and `jobs.result` hold them with no retention limit seen in this
  run (the spool TTL does not apply to them). Needs a stated retention or purge
  policy and a disclosure statement.
* **G8 (low): a silent or noise-only clip yields a stock phrase** with status 200
  (decision V0, reproduced here).
* **G9 (low): the attribution lives only in the voice catalog**; speech responses
  carry no credit header or field, so a client must fetch the catalog to credit
  `VOICEVOX:ずんだもん` or the JVNV licence.
* **G10 (low): the host disk was full.** Nothing in the stack checks free disk;
  only spool quotas bound it.

Test limits: the spool "disk-full" case used a file-size limit (EFBIG), not ENOSPC
(a size-limited mount needs privilege, and the spool-root ownership checks reject
the parents seen inside an unprivileged user namespace). No ENOSPC result is claimed.

## Open items for a production-LXC repeat

1. Repeat everything on the production Ubuntu LXC images, with the real NixOS or
   systemd units (`Restart=`, `KillMode`, sandboxing, per-start private homes) and
   without the nix-ld shim and the launcher-side driver path (F1 of the STT note).
2. Repeat the faster-whisper leg on the GTX 1080 Ti (compute capability 6.1,
   float32) with its real latency, VRAM and the exact driver library path.
3. Reproduce the Style-Bert interpreter and lock (S1) and run with the **unmodified
   production pins**; reproduce the faster-whisper install from its lock.
4. Replace the two TEST-PURPOSE records with real operator-reviewed rights and
   validation records, and rerun the manifests.
5. A real-speaker, conversational re-check of the `散歩` / `3本` contrast and
   numerals (required before activation) and a human Japanese listening review of
   both TTS voices.
6. Root-cause G1 on the LXC; decide G2 (lease versus deadline), G3 (spool release
   and quotas), G4 (queueing and `Retry-After`), G5 (declared input format), G7
   (retention), then re-run the lease, restart and overload checks.
7. A real ENOSPC test on a size-limited filesystem for both spools.
8. Control-HA, streaming, the NixOS/generic-systemd disabled-audio path and
   release or rollout approval are out of scope and unverified.

## Tests run

After the fix: `tests/test_audio_gateway_contracts.py`, `test_audio_speech_api.py`
and `test_audio_voice_api.py` passed (262 tests). The full suite ran on this branch
and on `origin/main` against a scratch PostgreSQL 17 with database tests enabled.
Both runs had the same failures, all environmental on this host and none in audio
code: `test_d1_gpu_smoke.py` (2), `test_setup_permissions.py` (7),
`test_setup_scripts.py` (23) and one test in `test_postgres_serving.py`. Two
suites run at the same moment on one PostgreSQL server also produced a few
different `test_postgres_*` failures; rerun one after the other, the branch and
`origin/main` agree (64 passed, the same one failure). The documentation build
(`scripts/build-docs.sh`) succeeded with this page.

## Follow-up 2026-11-28: findings addressed in source (#318)

This section only records what changed afterwards; the measured results above are
unchanged and none of this was re-measured on the scratch stack or a Worker host.
Each item is covered by deterministic unit tests (in-memory and, for the
repository changes, a scratch PostgreSQL 17); behaviour on real engines, real
disks and the production LXC is **not** re-verified. Audio stays off by default
and every new key is optional. Details: Phase A page, "Lifecycle and admission
hardening".

| Finding | Status | How |
|---|---|---|
| G2 lease versus deadline | addressed in source | `audio.*` Jobs get their own lease (`claim_lease_seconds`, default `min(30, deadline/2)`, must be below the deadlines); the Worker still does not fail unconfirmed attempts, lease recovery does, so the client sees 502 instead of 504. |
| G3 spool release | addressed in source | Input WAV deleted at terminal Job state; speech output deleted once served; client-read output deleted `read_grace_seconds` after the first read; TTL stays the backstop. |
| G4 overload | partly addressed | `Retry-After` on 429. Still load shedding, not queueing. |
| G5 admitted audio | addressed in source | Optional declared rates/channels refuse a mismatching WAV with 415 `unsupported_audio_format`; extra chunks get 415 `unsupported_audio_extra_chunk`. The canonical-44-byte policy is unchanged. The faster-whisper value (`[16000]`, `[1]`) must be set by the operator. |
| G7 text retention | addressed in source | Placeholder after `text_retention_seconds` (default 3600) via the maintenance loop; disclosure in the Phase A page. Backups taken earlier are not rewritten. |
| G9 attribution | addressed in source | `x-tsumgi-credit` on speech (catalog attribution); on STT only if `transcriptions.credit` is set. |
| G10 free disk | addressed in source | Floor refuses admission with 503 `spool_disk_low`. A real ENOSPC was not tested (open item 7). |
| G1, G6, G8 and the three status-class differences | not touched | |
