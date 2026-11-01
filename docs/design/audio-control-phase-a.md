# Audio Control Gateway — #180 Phase A (source-only)

**Status:** Source integration candidate; not a deployed or provider-qualified audio service. The three providerless factories were merged under #175; this change wires them into the existing Control process and **reuses the exact same AudioInputChannel and AudioOutputChannel** for the worker-facing artifact transport and the periodic maintenance loop. #180 later phases own Worker runtime packaging and operational rollout.

## Authority, configuration and behavior

- `[audio_gateway]` is **disabled by default**. With no audio table, or `enabled = false`, Control neither loads an audio catalog nor creates spools, mounts audio API routes, changes the existing non-audio routes, or advertises a new Worker capability. A disabled table containing a retained policy validates only its shape and never opens the named paths.
- Enabling requires a **strict** complete TOML table with the keys `enabled`, `catalog`, `ingress`, `speech`, `transcriptions`, `pcm`, `input_spool`, `output_spool`; all nested keys are explicit. Unknown/invalid fields and malformed/corrupt/oversized JSON fail closed on startup. Catalog must contain reviewed pinned profiles for both speech and transcription; it is *not* runtime discovery or evidence that a Worker is ready. JSON is UTF-8, at most 1 MiB, with duplicate keys and nonstandard constants rejected.
- **Current safe auth gate:** Enabling audio requires `control.client_auth = "bearer"` and a valid `ASTRUMWEAVER_CLIENT_TOKEN`, plus an *independent* `ASTRUMWEAVER_AUDIO_CLIENT_TOKEN`. The audio token must be distinct from the Control client and Worker tokens. Audio Speech, Transcriptions, and Voice require the dedicated token; existing Control client artifact endpoints continue to require the Control token, while Worker input/output routes require the Worker token and lease/attempt/epoch authorization. `control.client_auth = "none"` with audio enabled is **rejected at startup**, not silently downgraded. Supporting separate audio-only authentication while retaining globally anonymous generic Job operations would require a distinct, reviewed per-job authorization redesign, and is **not** in this phase.
- `AudioInputChannel` and `AudioOutputChannel` share exactly the durable Control repository with their corresponding route services and the base Control app. Both channels are passed to `create_app`, ensuring existing `/v1/audio/inputs`, staged Job binding, Worker claim-fenced input/output transfer, published output and maintenance reaping use the *same receipt owners*; mounting only the three public routers would be insufficient.
- Input and output have **different existing private directory roots**, owned by the Control effective UID with mode `0700` and trusted ancestors. The source must not create roots, provision hosts, or change running services. The ArtifactSpool constructor revalidates each root using descriptor-relative checks. Both roots have finite per-artifact and aggregate quotas, artifact count and TTL; input TTL exceeds transcription deadline and output TTL exceeds speech deadline. Same-effective-UID processes are trusted, so the roots are not a hostile same-UID isolation boundary.
- All entrypoints retain the strict bounded ingress, PCM16-only initial codec, exact ServingJobBinding/one-attempt Job, deadline, pending-admission and idempotency/error semantics of #174/#175. No WAV bytes are placed in durable Job JSON. An unknown provider/unsupported deployment is not substituted automatically. Audio outputs are complete WAV (not streaming), and STT returns JSON. A configured catalog cannot itself advertise Worker readiness.

## Illustrative review-only configuration

This example is **not** a deploy instruction; paths, token delivery, quotas and qualified catalog must be independently reviewed first. Audio requires pre-existing secure roots, not automatic installation or creation by the service.

```toml
[control]
client_auth = "bearer"

[audio_gateway]
enabled = true
catalog = "/var/lib/astrumweaver/audio/reviewed-catalog.json"

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
root = "/var/lib/astrumweaver/audio/input"
ttl_seconds = 180.0
max_total_bytes = 8388608
max_artifacts = 16

[audio_gateway.output_spool]
root = "/var/lib/astrumweaver/audio/output"
ttl_seconds = 180.0
max_total_bytes = 8388608
max_artifacts = 16
```

Control reads tokens only from the process environment (`ASTRUMWEAVER_CLIENT_TOKEN`, `ASTRUMWEAVER_AUDIO_CLIENT_TOKEN`, `ASTRUMWEAVER_WORKER_TOKEN`), never TOML. Do not reuse tokens or put them in the catalog.

## Verification and explicit limitations

- Source-only tests use a fake Control PostgreSQL repository, synthetic immutable profiles and private temporary spools. Check default disabled mode, exact configured routes, independent bearer checks, same-repository channels, route-to-Worker transport, strict rejection, unchanged Chat/Embedding/Decision routes and model listing. No live database, actual Worker, model, network or host services are involved.
- `#174` still owns Artifact MVI completion and its restart/orphan/retention bounds; this phase does not provide process-independent receipt persistence, atomic spool/Job commit, HA, crash recovery or bounded synchronous PostgreSQL waits. Some local spool directories may remain after process loss; actual operational cleanup policy must be qualified separately.
- `#176/#177` and `#178→#179` own respective provider validation and adapters. `#180` future phases own NixOS/generic-systemd configuration packaging, Worker lifecycle, deploy/rollback, and capability advertisement after genuine readiness. Disabled Control audio support must not be construed as verified provider availability.
- No production config, Control/Worker deployment, database migration, model download, release or GitHub Merge occurs as part of source-only Phase A. The feature contract remains subject to real Job/Artifact integration testing, independent security review and operator approval.
