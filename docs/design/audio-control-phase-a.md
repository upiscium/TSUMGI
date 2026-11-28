# Audio Control Gateway — #180 Phase A (source-only)

**Status:** Source integration candidate; not a deployed or provider-qualified audio service. The three providerless factories were merged under #175; this change wires them into the existing Control process and **reuses the exact same AudioInputChannel and AudioOutputChannel** for the worker-facing artifact transport and the periodic maintenance loop. #180 later phases own Worker runtime packaging and operational rollout.

## Authority, configuration and behavior

- `[audio_gateway]` is **disabled by default**. With no audio table, or `enabled = false`, Control neither loads an audio catalog nor creates spools, mounts audio API routes, changes the existing non-audio routes, or advertises a new Worker capability. A disabled table containing a retained policy validates only its shape and never opens the named paths.
- Enabling requires a **strict** complete TOML table with the keys `enabled`, `catalog`, `ingress`, `speech`, `transcriptions`, `pcm`, `input_spool`, `output_spool`; all nested keys are explicit (the optional #318 keys listed at the end are the only exceptions). Unknown/invalid fields and malformed/corrupt/oversized JSON fail closed on startup. Catalog must contain reviewed pinned profiles for both speech and transcription; it is *not* runtime discovery or evidence that a Worker is ready. JSON is UTF-8, at most 1 MiB, with duplicate keys and nonstandard constants rejected.
- **Current safe auth gate:** Enabling audio requires `control.client_auth = "bearer"` and a valid `TSUMGI_CLIENT_TOKEN`, plus an *independent* `TSUMGI_AUDIO_CLIENT_TOKEN`. The audio token must be distinct from the Control client and Worker tokens. Audio Speech, Transcriptions, and Voice require the dedicated token; existing Control client artifact endpoints continue to require the Control token, while Worker input/output routes require the Worker token and lease/attempt/epoch authorization. `control.client_auth = "none"` with audio enabled is **rejected at startup**, not silently downgraded. Supporting separate audio-only authentication while retaining globally anonymous generic Job operations would require a distinct, reviewed per-job authorization redesign, and is **not** in this phase.
- `AudioInputChannel` and `AudioOutputChannel` share exactly the durable Control repository with their corresponding route services and the base Control app. Both channels are passed to `create_app`, ensuring existing `/v1/audio/inputs`, staged Job binding, Worker claim-fenced input/output transfer, published output and maintenance reaping use the *same receipt owners*; mounting only the three public routers would be insufficient.
- Input and output have **different existing private directory roots**, owned by the Control effective UID with mode `0700` and trusted ancestors. The source must not create roots, provision hosts, or change running services. The ArtifactSpool constructor revalidates each root using descriptor-relative checks. Both roots have finite per-artifact and aggregate quotas, artifact count and TTL; input TTL exceeds transcription deadline and output TTL exceeds speech deadline. Same-effective-UID processes are trusted, so the roots are not a hostile same-UID isolation boundary.
- **Synchronous startup rollback**: if creating the second spool or any route fails, or if subsequent non-audio daemon composition fails, the startup owner attempts to remove only the freshly created **empty** per-instance directories. It validates the original root/directory inode identities and does not recursively delete files, reuse names or reclaim unknown directories. If safe removal fails, startup fails with an explicit operator-inspection error rather than deleting an ambiguous occupant. Crash/process-loss cleanup remains a separate #174 operational gap.
- **Non-audio completion isolation**: the common Worker completion route selects the original Repository completion path for non-audio Jobs; only `audio.*` Jobs go through AudioOutputChannel. Job capability is immutable and the Repository still revalidates Worker/lease/attempt during completion. Thus an optional audio-spool I/O failure must not block Chat/Embedding/Decision/generic completions, while audio completion continues to fail closed on invalid/unavailable artifacts.
- All entrypoints retain the strict bounded ingress, PCM16-only initial codec, exact ServingJobBinding/one-attempt Job, deadline, pending-admission and idempotency/error semantics of #174/#175. No WAV bytes are placed in durable Job JSON. An unknown provider/unsupported deployment is not substituted automatically. Audio outputs are complete WAV (not streaming), and STT returns JSON. A configured catalog cannot itself advertise Worker readiness.

## Illustrative review-only configuration

This example is **not** a deploy instruction; paths, token delivery, quotas and qualified catalog must be independently reviewed first. Audio requires pre-existing secure roots, not automatic installation or creation by the service.

```toml
[control]
client_auth = "bearer"

[audio_gateway]
enabled = true
catalog = "/var/lib/tsumgi/audio/reviewed-catalog.json"

[audio_gateway.ingress]
request_bytes = 2097152
input_bytes = 16384
file_bytes = 1048576
metadata_bytes = 4096
part_header_bytes = 1024
total_header_bytes = 8192
receive_events = 256
idle_timeout_seconds = 1.0
total_timeout_seconds = 10.0

[audio_gateway.speech]
output_bytes = 1048576
request_timeout_seconds = 60.0
max_pending_admissions = 4
poll_interval_seconds = 0.05

[audio_gateway.transcriptions]
text_bytes = 8192
request_timeout_seconds = 60.0
max_pending_admissions = 4
poll_interval_seconds = 0.05

[audio_gateway.pcm]
max_decoded_bytes = 1048532
max_duration_seconds = 30

[audio_gateway.input_spool]
root = "/var/lib/tsumgi/audio/input"
ttl_seconds = 180.0
max_total_bytes = 8388608
max_artifacts = 16

[audio_gateway.output_spool]
root = "/var/lib/tsumgi/audio/output"
ttl_seconds = 180.0
max_total_bytes = 8388608
max_artifacts = 16
```

Control reads tokens only from the process environment (`TSUMGI_CLIENT_TOKEN`, `TSUMGI_AUDIO_CLIENT_TOKEN`, `TSUMGI_WORKER_TOKEN`), never TOML. Do not reuse tokens or put them in the catalog.

## Verification and explicit limitations

- Source-only tests use a fake Control PostgreSQL repository, synthetic immutable profiles and private temporary spools. Check default disabled mode, exact configured routes, independent bearer checks, same-repository channels, route-to-Worker transport, strict rejection, fail-closed audio storage versus unaffected non-audio completion, safe synchronous-startup rollback, unchanged Chat/Embedding/Decision routes and model listing. No live database, actual Worker, model, network or host services are involved.
- `#174` still owns Artifact MVI completion and its restart/orphan/retention bounds; this phase does not provide process-independent receipt persistence, atomic spool/Job commit, HA, crash recovery or bounded synchronous PostgreSQL waits. Some local spool directories may remain after process loss; actual operational cleanup policy must be qualified separately.
- `#176/#177` and `#178→#179` own respective provider validation and adapters. `#180` future phases own NixOS/generic-systemd configuration packaging, Worker lifecycle, deploy/rollback, and capability advertisement after genuine readiness. Disabled Control audio support must not be construed as verified provider availability.
- No production config, Control/Worker deployment, database migration, model download, release or GitHub Merge occurs as part of source-only Phase A. The feature contract remains subject to real Job/Artifact integration testing, independent security review and operator approval.

## Lifecycle and admission hardening (#318, optional keys)

These keys are all **optional**: a policy written for the earlier phases stays valid.
They only matter when audio is enabled (audio is off by default and nothing here
changes a non-audio Job). Every value is validated as strictly as the keys above;
unknown keys are still rejected.

| Key | Default | Meaning |
|---|---|---|
| `speech.retry_after_seconds`, `transcriptions.retry_after_seconds` | `2` | Integer seconds (1 to 30) sent as `Retry-After` on every 429 (`overloaded`, `admission_overloaded`). A policy constant, never derived from a Job. |
| `transcriptions.credit` | unset | Model/licence credit sent as `x-tsumgi-credit` on STT responses (the STT catalog defines none). At most 256 characters, no control characters. |
| `transcriptions.accepted_sample_rates`, `transcriptions.accepted_channels` | unset (no early check) | What the selected STT Worker takes, for example `[16000]` and `[1]` for faster-whisper. A WAV outside the lists is refused at admission with 415 `unsupported_audio_format`; no spool file and no Job is created. The serving identity is not changed. |
| `input_spool.min_free_bytes`, `output_spool.min_free_bytes` | `max(1 GiB, 2 x max artifact)` | Free-disk floor of the spool filesystem; `0` disables. Below it, new admission is refused (see below). |
| `output_spool.read_grace_seconds` | `60` | Delete a client-read output this long after its first full read; `0` disables (TTL only). Not accepted on `input_spool` (inputs follow Job state). |
| `claim_lease_seconds` | `min(30, deadline / 2)` | Lease of `audio.*` Jobs only. Whole seconds, at least 1, strictly below both request deadlines (otherwise the policy is rejected). When no whole second fits below the deadlines (deadline of 2 s or less) the generic lease is kept. |
| `text_retention_seconds` | `3600` | Seconds after a Job is terminal before TTS input and STT transcript in the Job row are replaced; `0` disables; at most 30 days. |

**Free disk (G10).** `DiskFloor` measures the spool filesystem on every admission
(`shutil.disk_usage`, injectable in tests). Staging an STT input and admitting a
speech request are refused with **503 `spool_disk_low`** (the existing 503 class for
"not available now"; 507 is not used by any audio route) before any file or Job is
created. A Worker output upload below the floor fails with the usual 503
`StorageUnavailable`. An unmeasurable filesystem counts as low. `disk_status()` on
both channels returns `free_bytes`, `min_free_bytes`, `low`, `refusals` for an
operator or a later metrics hook; `/v1/ready` is deliberately unchanged because it
is not an audio endpoint. The check is advisory between measurement and write.

**Release at terminal state (G3).** The input WAV is deleted as soon as the durable
Job is `succeeded`, `failed` or `cancelled`; a Job that failed with a retry left is
`queued`, not terminal, and keeps its input. The channel re-reads the Job and never
trusts the caller; any repository or deletion error keeps the file (the TTL remains
the backstop). Triggers: end of a transcription request, the maintenance loop,
and staging when the spool quota is tight, so throughput is no longer capped at
`max_artifacts / TTL`. An input whose commit acknowledgement was lost is found by its
idempotency key. The speech service frees the output WAV as soon as it holds the
bytes; for a client that reads `GET /v1/jobs/{id}/audio/output/{artifact}` itself,
the file is kept for re-reads and deleted `read_grace_seconds` after the first full
read. An output that was never read stays until the TTL. After release a read
answers 404 although the Job result still names the artifact.

**Audio lease (G2).** `worker/runtime.py` still does not fail an attempt it cannot
confirm. The lease is separable per capability without a migration, so both
repositories take `audio_lease_seconds` and apply it to `audio.*` Jobs at claim and
heartbeat renewal only. With the default and a 120 s request deadline an
unconfirmed Worker failure now ends the Job `failed` about 30 s after the last
heartbeat (plus up to one maintenance interval), and the Gateway answers 502
`backend_failure` instead of 504. Keep the lease at least three times the Worker
heartbeat interval (5 s by default) or healthy Jobs can lose their lease.

**Text retention (G7).** The maintenance loop calls
`redact_audio_job_text(text_retention_seconds)`. For `audio.synthesize` and
`audio.transcribe` Jobs that have been terminal for at least that long,
`payload.input` and `result.text` become `"[redacted: audio text retention elapsed]"`.
Ids, status, timings, digests, the `audio_input` receipt and all other fields stay.
It is idempotent, bounded per pass and identical on the in-memory and PostgreSQL
repositories. **Disclosure:** until the retention elapses, the spoken text and the
transcript are stored in plaintext in `jobs.payload` and `jobs.result`, are visible
to anyone with database access or backups made in that window, and are returned by
`GET /v1/jobs/{id}`; redaction does not rewrite earlier backups. A later request
with the same idempotency key no longer matches the redacted payload (Gateway keys
are random per request).
