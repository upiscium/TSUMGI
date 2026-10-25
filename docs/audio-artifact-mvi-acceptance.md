# Audio artifact MVI: contract, evidence, and acceptance gaps

Status: **AC-AUDIO-05/06 final acceptance pending**. This is an evidence
inventory for Issue [#174](https://github.com/upiscium/TSUMGI/issues/174),
not a declaration of release or production readiness.

Source baseline: `main` merge `16f25698e4ce9594fded3b756a987c0298bdd53a`
(PR #192). Subsequent focused admission-route guard and test evidence are
tracked in the PR that adds this document.

## The supported boundary

The reviewed artifact transfer is **opt-in, single-Control-local**. A caller
must create private `0700` roots and `ArtifactSpool` instances with finite
per-file and aggregate byte/count quotas, then pass `AudioInputChannel` and/or
`AudioOutputChannel` to `create_app`. This is a temporary byte-transfer
bridge around **durable PostgreSQL Job metadata**, not an object store or
persistent file service.

The existing `control/daemon.py` invocation does **not** construct or pass
audio channels by default. Tests instantiate the channels explicitly. This
code-level MVI acceptance does not constitute live daemon deployment.

The input path is:

1. Client-Bearer `POST /v1/audio/inputs`: receive a bounded `audio/wav` body,
   validate strictly decodable 16-bit PCM (mono/stereo, 8–48 kHz) with
   independently bounded decoded bytes and duration; stage private bytes and
   receive an opaque metadata reference.
2. Client-Bearer `POST /v1/audio/inputs/jobs`: bind that reference to an
   `audio.transcribe` Job with Serving/Deadline/Idempotency Key. The durable
   Job payload stores reference metadata, not input WAV bytes.
3. Worker-Bearer `GET /v1/workers/{worker_id}/jobs/{job_id}/audio/input`:
   require current Job, Worker, attempt, lease, runtime epoch and Serving
   authority; revalidate after Spool I/O.

When an input channel is enabled, the ordinary Client-Bearer `POST /v1/jobs`
must not accept `audio.transcribe` or any untrusted `payload.audio_input`
reference. This ensures opted-in audio requests cannot bypass staged-input
admission. With no input channel, generic Job submission stays unchanged.

The output path is:

1. Worker-Bearer `POST /v1/workers/{worker_id}/jobs/{job_id}/audio/output`:
   stage at most one bounded WAV-looking byte sequence per current attempt.
   Identical retransmission is idempotent; a distinct replacement is rejected.
2. Worker completion requires the exact staged `ArtifactRef` and a live
   authoritative claim before durable Job `SUCCEEDED`.
3. Client-Bearer `GET /v1/jobs/{job_id}/audio/output/{artifact_id}`:
   return intact bytes **only after** the durable Job references that output
   in a `SUCCEEDED` result.

The output channel checks RIFF/WAVE signatures and size; it does **not**
decode/validate all generated PCM content. Input and output policy are
deliberately separate.

## Acceptance coverage (evidence, not blanket certification)

| Criterion | Evidence / disposition |
| --- | --- |
| Authenticated private stage/read | **Verified in test scope.** Current claim (Worker, attempt, lease, epoch and Serving) is required before/after read and on output stage. See `test_audio_artifact_authority*.py`, `test_audio_input_channel.py`, `test_audio_output_channel.py`. |
| Strict input wire/codec/decoded size/duration | **Verified in test scope** after #192. `pcm16.py` checks complete RIFF/chunk boundaries, PCM16 header fields, rate/channels and independent ceilings before sample reads or file staging. |
| Output published only after terminal success | **Verified in test scope.** See `test_audio_artifact_end_to_end.py` and output-channel tests with real PostgreSQL and private files. |
| Opaque refs, digest, size, filesystem safety | **Verified in test scope.** Job JSON uses URI/digest/MIME/size metadata. Spool checks digest, ownership, inode/permissions and symlink/path safety. `test_audio_artifact_spool.py`. |
| Bounded quotas, TTL, reaper, stale/failed cleanup | **Verified in test scope** for per-file and aggregate quotas; expiry and abandoned output reaping; failed unlink retains quota. Input is reclaimed at TTL; it is not guaranteed to be immediately deleted at every malformed admission request. |
| Upload interruption | **Partially verified.** Truncated `Content-Length` over in-process ASGI is rejected with no staged file. An actual TCP peer disconnect has **not** been exercised. |
| Filesystem faults | **Partially verified.** Partial `os.write` followed by injected `ENOSPC` leaves no visible output receipt or quota charge, and healthy retry can stage. Physical disk/power failure is **not** tested. |
| Post-crash orphan cleanup | **Verified for exclusive-root Spool lifecycle.** A separately hard-exited Spool process leaves a file that a new owner can reclaim. Simulated state loss refuses to restore private bytes from durable Job metadata. Full Control process restart with live job recovery is **not** tested. |
| Commit-response uncertainty | **Partially verified.** Real PostgreSQL can commit before a deliberately injected `StorageUnavailable`; receipt survives, idempotent client replay and a current Worker can reconcile from canonical durable request metadata. Actual network COMMIT ACK loss is **not** tested. |
| AC-AUDIO-06 no binary in persistent JSON/logs/metrics | **Narrowly verified, not universally proven.** Audio endpoints persist only opaque ref metadata; tests compare Job payload/result against raw test WAV. Generic JSON Job payloads outside the opted-in contract and downstream text/transcripts remain caller-dependent. Log/metric/backups-wide binary and sensitive-data absence has **not** been exhaustively audited. |

## Operational invariants and known limits

- **Exclusive Control ownership:** the same root cannot be shared by two
  concurrently active Control instances. Startup orphan reclamation is an
  explicit exclusive-root operation; no HA/replication or persistent receipt
  recovery is offered.
- **Expiry and crash:** PostgreSQL metadata is durable, but input/output bytes
  and their scope/receipt maps are process-local. After restart, a Job may
  retain a reference whose WAV is no longer readable. Correct behavior is to
  fail closed; this is not a promise of durable data availability.
- **Concurrency and atomicity:** Job commit and file staging are not one
  PostgreSQL/filesystem transaction. Idempotency protects identical admission
  retries and ambiguous response handling while local state survives.
- **Authentication:** Client and Worker Bearer tokens must differ; Worker
  Bearer grants a role, not per-Worker cryptographic identity. Authorization
  binds Worker claims using the durable lease/attempt/runtime epoch.
- **Retention and privacy:** raw input/output WAV should not enter the
  artifact-specific Job JSON, but ordinary Job text, transcription outputs,
  events, logs, PostgreSQL backups, and external Worker components may retain
  sensitive content under their separate policies. No full erasure claim.
- **Non-goals:** no S3, multi-Control replication, arbitrary files/codecs,
  output transcoding, archival, long-term WAV durability, or deployed TTS/STT
  model-quality assertions are part of this MVI.

## Final decision gate

A maintainer must explicitly decide whether the **partially verified items**
above are acceptable as documented MVI constraints or require a narrow
additional acceptance test, especially actual socket disconnection,
full-Control restart, and logs/metrics retention. **Do not close #174 or claim
full AC-AUDIO-05/06 PASS solely from this inventory.** Deployment and release
also require a separate operational decision.

Related merges: #183–#187, #189–#192. Older Draft
[#188](https://github.com/upiscium/TSUMGI/pull/188) implements an alternate
overlapping input API; its distinct PCM16 validation was extracted into merged
#192. The duplicate Draft must not be merged as-is.
