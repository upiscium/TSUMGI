# Audio artifact MVI: contract, evidence, and acceptance gaps

Status: **AC-AUDIO-05/06 final acceptance pending**. This is an evidence
inventory for Issue [#174](https://github.com/upiscium/TSUMGI/issues/174),
not a declaration of release or production readiness.

Source baseline: `main` merge `a62d026c3a38c47125f9d48133f15fbc782a5429`
(PR #193, including the accepted generic admission-route guard). The
additional loopback transport-fault tests in this change add evidence without
changing production code.

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
| Upload interruption | **Verified on test-owned loopback TCP.** `test_audio_real_transport_faults.py` starts a real localhost Uvicorn HTTP/1.1 server, sends part of a declared WAV body over a socket, then closes the peer; ASGI observes `http.disconnect` and no bytes, receipt or quota remain. A fresh full upload succeeds afterward. This does not establish behavior behind an external load balancer, TLS proxy or production Gateway. |
| Filesystem faults | **Partially verified.** Partial `os.write` followed by injected `ENOSPC` leaves no visible output receipt or quota charge, and healthy retry can stage. Physical disk/power failure is **not** tested. |
| Post-crash orphan cleanup | **Verified for an explicitly exclusive root.** `test_audio_control_restart.py` hard-exits a process hosting the Control ASGI app after staging output for a running PostgreSQL Job, confirms the WAV remains on disk, then constructs a fresh app/repository after the old process has exited. Explicit cold recovery removes the old directory. The Job remains RUNNING, the old receipt cannot complete it, and no output can be downloaded. This is not an installed daemon restart, automatic Job resumption, or general service recovery. |
| Commit-response uncertainty | **Verified on test-owned localhost protocol proxy and real PostgreSQL.** `test_audio_real_transport_faults.py` suppresses the backend's successful `CommandComplete COMMIT` and following `ReadyForQuery` over a real TCP connection, then closes the client socket. A separate direct connection to the same isolated database (Unix socket locally, TCP in CI) independently finds exactly one committed Job; the caller raises `StorageUnavailable`, but its original input receipt is retained and can be read by the current Worker **without replay**, and identical replay creates no duplicate. This is a deterministic loopback response-loss test, not a general proof for arbitrary network partitions. |
| AC-AUDIO-06 no binary in persistent JSON/logs/metrics | **Verified on specific surfaces.** The PostgreSQL tests compare Job payload/result against the test WAV. `test_audio_artifact_observability.py` separately transfers distinct input/output PCM16 WAVs over loopback, confirms Uvicorn emitted access records for the transfers, and checks captured logs, stdout/stderr and the authenticated Job JSON (including nonempty result metrics) for raw and Base64 WAV/sample bytes. A second test cancels a Job after Control receives the first chunk of a Worker retransmission. The upload returns 409 and an authenticated GET for the previously staged receipt returns 404; neither error body nor the captured logs contain the WAV or either transmitted chunk, raw or Base64-encoded. These tests do not cover third-party exporters, reverse proxies, native engine logs, backups or all failure paths. |

## Operational invariants and known limits

- **Exclusive Control ownership:** the same root cannot be shared by two
  concurrently active Control instances. Startup orphan reclamation is an
  explicit exclusive-root operation; no HA/replication or persistent receipt
  recovery is offered.
- **Transport-fault test containment:** the PostgreSQL fault proxy is limited
  to a test-owned loopback TCP listener. Before allocating its disposable
  database, the test rejects nonlocal host/hostaddr and service or environment
  redirection; both proxy-side host and hostaddr are pinned to 127.0.0.1.
  This remains a test harness, not a deployable PostgreSQL proxy.
- **Expiry and crash:** PostgreSQL metadata is durable, but input/output bytes
  and their scope/receipt maps are process-local. After restart, a Job may
  retain a reference whose WAV is no longer readable. Correct behavior is to
  fail closed; this is not a promise of durable data availability.
- **Live TTL maintenance:** local expiry runs before database-dependent Job
  recovery. `test_audio_maintenance.py` checks physical input-file removal
  when either database maintenance or the output reaper raises. A blocked
  thread or a local filesystem that refuses deletion can still delay cleanup.
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

### Review boundary at `6c81ebbf` plus working-tree changes

The #174 production diff covers live maintenance in `control/api.py` and
exclusive root ownership and startup recovery in `audio/artifact_spool.py` and
`control/audio_gateway_config.py`. `test_audio_gateway_daemon.py` exercises the
standard configuration across process boundaries.
Its review also includes this inventory, the loopback server helper in
`test_audio_real_transport_faults.py` and the three new tests
`test_audio_{maintenance,control_restart,artifact_observability}.py`.
It neither imports nor requires the #178 scorer (`validation/audio_stt_178.py`),
its unit tests or its evaluation guide. The #181 trial checklist is separate.

| #174 acceptance condition | Result in the recorded local run |
| --- | --- |
| AC-AUDIO-05: only the current authorized Worker/attempt/epoch can transfer media; invalid or interrupted transfers cannot publish | Passed the artifact authority suites (memory and PostgreSQL), input/output channel negatives and real TCP interruption. Cancellation during retransmission returns 409 and the GET for the actual staged receipt returns 404. |
| AC-AUDIO-06: bounded, integrity-checked references rather than raw audio in durable records or public diagnostics | Passed PostgreSQL Job and disk readback, digest/size/quota tests and the two observability cases above. Covers the tested Control surfaces, not all external logs/backups. |
| Admission and publication: failed admission cleanup, ambiguous COMMIT, retry without double-visible output | Passed `test_postgres_input_admission_and_failure_cleanup`, `test_real_postgres_commit_ack_lost_after_successful_commit`, and the output-channel idempotent retry and previous-attempt reclamation tests. Visibility requires durable success; there is no atomic filesystem/DB transaction. |
| TTL and crash cleanup | Live TTL under independent failures passed. Standard enabled Control startup now reclaims abandoned managed input/output WAVs after acquiring their respective root locks. |

**Startup boundary:** On local Linux storage the enabled Control acquires a
nonblocking exclusive `flock` on each validated root directory inode before
scanning it, and retains that lock for the lifetime of its spool. A busy
root rejects the competing startup. The kernel releases the lock on process
death. A restarted Control reclaims only its managed `control-spool-v2-*`
namespace; it never restores lost receipts. `test_audio_gateway_daemon.py`
starts a separate process using `daemon.build_app`, stages input and output
via HTTP endpoints, and tests contention on each root, then kills the owner
and checks that standard startup removes both old directories.

**Upgrade/rollback:** legacy `artifact-spool-*` directories do not attest that
their owner participated in this locking protocol. They are deliberately left
alone even when the lock is available; plan an offline inspection of legacy
bytes after stopping all prior owners. Do not rename or replace root paths
while Control is running. This is not cross-host locking or secure erasure.

### Regression evidence (shared suite, not a per-issue test count)

Local regression run (2026-11-19): `audio/acceptance-backlog`, based on
`6c81ebbf` with the uncommitted maintenance, restart, observability and STT
scorer changes present. Against a dedicated PostgreSQL 17.11 instance on a
private Unix socket (explicit `host`, `port=55464`, `dbname=postgres`, and
test-cluster owner `user` in `TSUMGI_TEST_DATABASE_URL`), ran:

```sh
PYTHONPATH=src python -m pytest -q -rs tests/test_audio*.py tests/test_voicevox*.py tests/test_style_bert*.py
```

After adding the standard startup and legacy compatibility cases:
**1854 passed, 0 failed, 0 skipped in 26.01 s**. One Starlette
TestClient/httpx deprecation warning. This includes the PostgreSQL COMMIT
response-loss test and the in-flight cancellation/privacy test; it is not
evidence of real model inference or installed-service qualification.

A maintainer must explicitly decide whether the **remaining partially
verified items** above are acceptable documented MVI constraints or require
narrow further tests, particularly full-Control service restart with running
Jobs, physical power failure and logs/metrics/backups retention. The actual
localhost TCP disconnect and a successful PostgreSQL COMMIT with a withheld
wire-level response are now covered as isolated tests; they do not certify an
external TLS/production Gateway or arbitrary network failures. **Do not close
#174 or claim full AC-AUDIO-05/06 PASS solely from this inventory.** Deployment
and release also require a separate operational decision.

Related merges: #183–#187, #189–#193. Older
[#188](https://github.com/upiscium/TSUMGI/pull/188) was CLOSED/NOT MERGED
as superseded: its distinct PCM16 validation was extracted into #192 and
its alternate input API should not be merged over the accepted contract.
