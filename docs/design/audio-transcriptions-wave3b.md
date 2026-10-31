# Audio transcriptions Wave3B: opt-in, providerless source slice

Tracking: TSUMGI #175; reuses Wave3A lifecycle conventions, Wave2 ingress,
the serving contract/catalog, Control Job authority and #174 `AudioInputChannel`.
Only the new API module, its synthetic test module and this note are added.
Nothing mounts this router in a daemon, changes deployment/configuration,
invokes a provider/model, downloads artifacts, or installs a Worker endpoint.
This is not full #175/#177, real STT qualification, production acceptance, or
evidence that a model/license/runtime has been approved.

## Reconciliation feasibility checked first

The existing channel is sufficient **without modifying shared code**:

1. `stage` validates bounded PCM16 WAV and returns an opaque receipt. The channel
   owns the private scope, spool path and bytes; the gateway receives no path.
2. `submit` pins the complete augmented `JobSubmission` before calling Control.
   A `StorageUnavailable` exception can mean lost COMMIT acknowledgment, so the
   channel retains the receipt and pinned request, even without a known Job ID.
3. `read_for_worker` first authorizes the current Worker/attempt/lease/epoch.
   When local Job ID is unknown, it requires the exact idempotency key and
   repository `_submission_matches` comparison of the full pinned request.
   It reauthorizes after spool I/O. URI/digest equality alone is insufficient.
4. The service can reconstruct the expected augmented request from the public
   staging receipt and its own submission. It uses that same repository
   comparison, plus key equality, for admission acknowledgments, polling and
   reconciliation. It never changes channel receipts, impersonates a Worker,
   reads the spool directly, or resubmits to discover a committed Job.

The tests exercise authorized pending-input rebinding before acknowledgment,
wrong-epoch rejection, and rejection of a different request with the same
URI/digest. This explains feasibility; the tests still require execution.

## Explicit construction and request contract

`create_audio_transcriptions_router(catalog, repository, audio_input_channel,
client_auth=..., client_token=..., ingress_limits=..., limits=...)` returns only
`POST /v1/audio/transcriptions`. Importing this module does not register a route.
The factory requires bearer-only authentication, a valid explicit token, an
exact `AudioProfileCatalog` revalidated into a detached immutable snapshot, a
transcription profile, explicit validated limits, and an input channel belonging
to the same repository. The ingress file cap cannot exceed the channel wire cap.
Authentication and rejection of any query string precede body consumption.
Duplicate Authorization headers fail closed using the existing bearer helper.

Multipart fields are exactly:

* `model`: exact reviewed STT logical profile ID, never a provider/runtime alias;
* `file`: strict safe `.wav` filename, `Content-Type: audio/wav`, bounded PCM16 WAV;
* `response_format`: required `json`;
* `language`: optional, but if present exactly `ja`; omission means `ja`.

No prompts, URLs, arbitrary paths, model overrides, timestamp options, streaming,
FLAC, compressed audio or fallback profiles are accepted. The shared structural
WAV subset is intentionally narrower than all WAV containers supported by #174.
The #174 PCM16 validator additionally checks decoded-size and duration against
the channel's operator limits, before staging; `stage` validates again under its
own authority. This is not model-specific audio qualification or FLAC support.

### Optional-language compatibility adapter

Wave2's existing `parse_transcription_ingress` requires all four parts, including
`language`. The new module bounds and reads the original upload using the
existing header/body helpers and effective operation caps, visits at most four
bounded part headers using the shared strict header parser, and adds a `ja`
part **only** to an otherwise exact three-name set (`model`, `response_format`,
`file`). It then replays through `parse_transcription_ingress` unchanged.
Duplicate/malformed/extra parts are not repaired. No general MIME parser or
unbounded buffering is introduced.

Conservative accounting is deliberate: both original and normalized bodies
must fit the caps. The inserted part consumes request/header budget and two
metadata bytes, so an omitted-language upload very near a limit can be rejected
even if its original wire size alone would fit. Operators must reserve this
small envelope overhead. Actual original transport reads still obey receive
event, idle and total time limits; the replay is one already-bounded body event.
The outer service lifetime bounds both phases together. ASGI servers/proxies
must independently bound transport allocations before handing over a chunk.

## Durable Job and normalized result

After validation, one process-local slot owns staging, admission and polling.
The service stages **before** its single call to `AudioInputChannel.submit`.
The durable Job has:

* capability `audio.transcribe`, exact `ServingJobBinding` from the snapshot;
* `max_attempts=1`, a deadline set at request start (including ingress time);
* an internal random `audio-transcriptions:` key with 256 random bits;
* payload `language=ja`, `response_format=json`, and channel-added `audio_input`
  with only opaque URI, MIME, SHA256 digest and byte count.

No WAV bytes, base64, filename, credential, lease, provider URL or backend model
name enters the Job payload. Identical HTTP requests are independent operations,
not a client-visible idempotency protocol. Matching online workers must satisfy
the exact serving identity; no compatible deployment is a sanitized refusal.

Only an identity-matched durable `SUCCEEDED` Job may produce HTTP success. This
slice defines the narrow canonical result as `JobResult.text`: a strict UTF-8
string within the independent operator text byte cap, with no `outputs` or
artifacts. Missing text, ambiguous output containers, malformed Unicode and
oversized text are rejected. Empty text is valid silence. Unicode and whitespace
are preserved, not stripped or rewritten. HTTP JSON is exactly `{"text": ...}`;
metrics, backend metadata and artifact references are never forwarded. This
does not bound bytes already allocated by the repository while decoding a Job.

Responses are `Cache-Control: no-store`. Errors expose fixed structured codes,
not request audio/text, tokens, provider configuration or repository exceptions.
Authentication adds `WWW-Authenticate: Bearer`; unsupported input uses 400/404/
413/415, capacity 429, unavailable admission 503, invalid result/backend failure
502, durable cancellation 409, and service lifetime expiry 504 (a definite
admission deadline refusal can use 408). A detected disconnect uses 499 if a
response can still be delivered.

## Cleanup, uncertainty, and late outcomes

There is **no atomic transaction between spool and database**.

| State | Ownership / action |
| --- | --- |
| Before staging | Parsing/codec rejection releases the slot; no Job or receipt. |
| Staging still running after HTTP exit | Detached operation keeps its slot, waits for the receipt, then discards it without submission. |
| Receipt exists, submit never entered | Service calls public `discard`; failure increments `cleanup_failures`, with bounded spool quota/TTL as fallback. |
| Definite first-admission refusal | Existing channel attempts deletion. It can suppress a filesystem deletion error; the gateway cannot certify deletion and does not reach into private state to retry it. |
| Submit entered, acknowledgment uncertain | Retain input. Up to three key readbacks; cancel only a full exact match. Never resubmit or interpret missing readback as proof of rollback. |
| Known admission followed by timeout/disconnect/task cancellation or invalid result | Attempt cancellation of the exact acknowledged Job, up to three calls. |
| Successful/failed/cancelled bound Job, or unresolved pending admission | No gateway discard. Channel forbids it; retention lasts until verified TTL reap. |

Uncertain admission always fails HTTP closed, **even if readback finds a
successful Job**. Reconciliation is cleanup, not permission to return a late
result. A mismatched/missing/unavailable readback leaves `unresolved_outcomes`
evidence; the service neither cancels a different identity nor claims rollback.
Cancellation accepts an identity-matched terminal result, including a success
that won the race. It cannot reverse an already committed completion. Control
continues to fence cancelled running attempts against late input access and
completion. Input is not deleted merely because cancellation was requested.

HTTP teardown sets an abort event rather than cancelling the internal operation.
The bounded slot stays occupied until synchronous staging/admission/polling and
the appropriate cleanup finish. Abort/deadline are checked after late I/O and
before consuming results. Cleanup has bounded **call counts**, not guaranteed
wall-clock completion: Python cannot kill a repository/filesystem thread.
Readback/cancellation receive short explicit repository deadlines; the existing
channel does not forward an `admission_deadline_at` argument to `submit_job`.
The Job still carries its deadline, and repository/default connection bounds
and post-call abort handling remain necessary. A stuck call can retain its slot
indefinitely, but cannot create unbounded extra service operations.

The channel checks that remaining spool TTL strictly covers the Job deadline at
first admission. It does not make spool data durable across process restarts.
Receipts and service ownership/counters are process-local; storage quotas and
verified TTL reaping still require owner maintenance. There is no autonomous
reaper, durable reconciliation queue, restart recovery, crash-safe orphan claim,
multi-Control sharing, HA guarantee, shutdown drain hook, or new binary transfer
authority in this slice. Process termination can lose cleanup/reconciliation
state. These limits must not be represented as solved by a durable Job row.

## Bounded offline evidence

`tests/test_audio_transcriptions_api.py` uses synthetic WAV, an in-memory Control
reference, real #174 spool/channel, synthetic exact serving advertisements and
in-process ASGI transport. No DB, HTTP listener, runtime, model, real recording,
provider call or external service is needed. Holds use finite Events/timeouts;
concurrency and uncertain-outcome tests assert one submission and retained slots.
Coverage includes rejection before staging, bearer ordering, profile snapshots,
exact identity, normalized success, TTL retention, admission refusal, lost ACK,
authorized pending rebinding, cancellation fencing and late staging/DB/poll I/O.

Intended command in an already provisioned offline environment:

```sh
PYTHONPATH=src python -m pytest -q tests/test_audio_transcriptions_api.py tests/test_audio_speech_api.py tests/test_audio_ingress.py tests/test_audio_input_channel.py
```

Execution in the implementation session is **unverified**: neither `python` nor
`python3` was available on PATH; a cached-only Nix test-environment attempt was
denied by execution policy and was not retried. Source inspection and Git scope
checks are not a substitute for passing tests. No E2E/restart/HA claim follows.
