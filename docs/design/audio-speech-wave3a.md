# #175 Wave3A: opt-in providerless speech Job bridge

Scope: `src/astrumweaver/gateway/audio_speech_api.py`,
`tests/test_audio_speech_api.py`, and this note only. The pre-existing uncommitted
API implementation was continued in place. No separate W3A approval packet was
found in this worktree; this note describes the implementation against the
existing audio catalog, ingress, pinned serving, durable Job, and
`AudioOutputChannel` contracts. It does not claim independent verification of an
external approval or completion of all of #175.

## Explicit injection and mounting

`create_audio_speech_router(catalog, repository, audio_output_channel, *,
client_auth, client_token, ingress_limits, limits)` returns an `APIRouter` with
exactly one route: `POST /v1/audio/speech`. An integrator must explicitly mount
it. Neither Control, a daemon, `/v1/models`, the voice route, nor transcription
routes are modified or implicitly enabled.

Construction requires:

* A reviewed `AudioProfileCatalog`, revalidated through a detached immutable
  serialization round trip, with at least one speech profile.
* Explicit bearer authentication and an ASCII bearer-syntax token. Anonymous
  mode and missing/malformed tokens fail at construction. Request authentication
  uses the existing strict audio bearer helper, before consuming the body or
  acquiring a service slot. Duplicate Authorization headers fail closed.
* Explicit `AudioIngressLimits` and `AudioSpeechLimits`. The latter separately
  bound output bytes, total HTTP request lifetime, concurrent service slots, and
  polling interval. Booleans, nonfinite values, and out-of-policy limits fail.
* An injected `AudioOutputChannel` owning the **same repository object**. Its
  output cap must cover the service cap; the existing channel constructor
  requires a finite aggregate spool byte/count quota and attempt authority.
  Submission, Job read, idempotency readback, and cancellation methods must be
  callable on the repository.

There is no provider object, backend URL, runtime discovery, default catalog,
model loading, real voice selection, or fallback. `voicevox` and `model-tts` are
reviewed catalog declaration kinds, not adapters supplied by this wave.

## Request and pinned durable Job

The request is the existing bounded speech JSON contract, with exactly four
required fields and no defaults:

```json
{"model":"exact-reviewed-profile","input":"日本語のテキスト","voice":"reviewed-alias","response_format":"wav"}
```

The router rejects nonempty query strings rather than silently ignoring query
overrides. The existing ingress parser enforces raw-body/header/event/time
limits, content type and declared-length consistency, strict UTF-8/JSON and
duplicate-field rejection before admission. Shared validation enforces the
selected profile's raw request and UTF-8 input caps, exact model/voice matching,
speech rather than STT, and WAV only. Unknown fields, URLs, streaming, speed,
codec and deployment overrides are not accepted. Input is preserved exactly.

One `JobSubmission` contains:

* `capability="audio.synthesize"` and the selected profile's exact
  `ServingJobBinding` (profile, deployment, serving-contract revisions, capability
  and operation schema). Voice/locale/license/attribution semantics remain pinned
  by that reviewed serving contract.
* A JSON-only payload: `input`, `voice` with `alias`, `speaker_id`, `style_id`, and
  `response_format="wav"`. No WAV bytes, base64, transport credentials, spool
  paths, URLs, or provider configuration enter the Job payload.
* `max_attempts=1`, a finite UTC deadline, and an opaque server-generated
  `audio-speech:` idempotency key. Repository admission receives the same deadline.

The monotonic HTTP lifetime starts before ingress and is independent of the UTC
durable deadline. Polling does not extend either. Client-supplied idempotency
keys are not used; separate HTTP requests create independent internal keys.
There is no transparent resubmission, provider retry, unpinned Job, or fallback
when the repository refuses admission. Existing repository/Worker code remains
the only admission, lease, capacity, claim, and completion authority.

## Successful output and error boundary

Polling validates the Job ID and immutable submission identity (key, capability,
binding, deadline, payload, attempt limit, requirements and priority). A running
or queued Job cannot expose a staged WAV. Only `SUCCEEDED` with exactly one
`audio/wav` artifact is eligible. The reference must have a nonempty
`tsumgi-audio:` ID and an exact integer byte count within the service output cap.

Bytes are read only through `AudioOutputChannel.read_published(job_id, id)`.
That authority rechecks durable success, exact staged-reference membership and
Job ownership, and the spool checks file identity, size, digest and TTL. This
router does not open paths, fetch URIs, trust inline result bytes, or stage or
complete output itself. It additionally rechecks exact `bytes` type, byte count,
and the existing narrow PCM16 RIFF/WAVE structure. No audio samples are decoded;
this is not perceptual quality, duration, model acceptance, or licensing evidence.
Abort/deadline checks occur after polling and after output reading, so a late
read cannot become a successful HTTP response.

Successful responses are raw `audio/wav`, with `Cache-Control: no-store`. Routed
errors use the existing sanitized `AudioGatewayError.to_dict()` shape with fixed
codes and fields, never raw exception text, Job IDs, input, credentials, backend
configuration, or result metadata. Error classes include:

| Condition | Status / fixed code |
| --- | --- |
| Invalid bearer credentials | 401 `unauthorized`, plus `WWW-Authenticate: Bearer` |
| Nonempty query | 400 `unsupported_query` |
| Busy service slot budget | 429 `admission_overloaded` |
| Repository admission deadline / capacity / no exact deployment | 408 `deadline_exceeded` / 429 `overloaded` / 503 `no_compatible_deployment` |
| Conflicting / uncertain admission | 409 `admission_conflict` / 503 `admission_unavailable` |
| Durable cancellation / failure | 409 `request_cancelled` / 502 `backend_failure` |
| Mismatched Job / missing, corrupt, expired or invalid WAV | 502 `invalid_job_result` / 502 `invalid_audio_output` |
| Service deadline / observed post-ingress disconnect | 504 `deadline_exceeded` / 499 `client_disconnected` |
| Other repository/channel exception | 503 `audio_unavailable` |

Ingress errors retain their existing fixed codes and 400/404/408/413/415 policy.
Framework-generated 404/405 responses are not part of the routed error policy.

## Cancellation, late work and uncertainty

A per-service slot covers ingress, submission, polling, output reading, and any
late cleanup. There is no waiting admission queue: excess requests receive 429.
Repository/channel operations run in threads so synchronous calls do not block
the event loop. Timing out, disconnecting, or cancelling the HTTP coroutine sets
an abort flag but does **not** cancel the independently retained operation task.
Its slot remains owned until the underlying call returns and cleanup settles.
New requests cannot create unlimited detached repository operations.

After a known admission, failure/abort triggers cancellation of that exact Job.
A confirmed `CANCELLED`, `FAILED`, or `SUCCEEDED` result settles cleanup; already
terminal success/failure is not falsely reported as an unresolved cancellation.
It is also not evidence that success was rolled back. If submission raises an
ambiguous exception, readback uses its original generated idempotency key only
after that submission call has stopped. Only an exact matching record can be
cancelled. Another identity is never cancelled, and submission is never retried.

Readback and cancellation each allow at most three calls with fresh 0.6-second
repository deadline hints. Missing readback after a lost COMMIT acknowledgement
is **not proof of rollback**. Exhaustion, unavailable readback, identity mismatch,
or unconfirmed cancellation increments the service's process-local
`unresolved_outcomes` counter. This is not a new durable reconciliation ledger,
public metric, or guarantee of successful rollback. Raw failures of detached
tasks are retrieved without logging private repository exceptions.

These are finite call counts, **not a hard wall-clock bound on arbitrary injected
synchronous code**. A repository ignoring its deadlines may keep a slot occupied
indefinitely; it is not silently released or replaced. Shutdown/process loss is
not solved by these in-memory tasks or counters. Durable Job deadlines and the
existing recovery mechanisms remain essential. The channel/spool are explicitly
process-local: durable Job success does not guarantee bytes survive restart or
TTL expiry. Existing abandoned-output/TTL reaping owns spool reclamation; this
router neither promises immediate deletion nor changes cleanup implementations.

## Synthetic tests and explicit execution report

`tests/test_audio_speech_api.py` test-mounts the router with an actual in-memory
Control repository, synthetic pinned Worker advertisements, and a bounded real
`AudioOutputChannel`/private spool. Synthetic callbacks exercise claim, staging,
and durable completion without invoking any provider. Fault injection covers
auth-before-body, strict ingress, exact payload/pins, independent keys, definitive
admission refusal, mismatched admission/poll identities, terminal failures,
missing/forged/foreign/expired/corrupt output, malformed WAV, ambiguous COMMIT
readback, finite cancellation retries, and gated late submission/poll/output
calls. Timeout, disconnect and coroutine cancellation tests check slot ownership
and eventual settlement rather than assuming thread cancellation stops a write.

**2026-10-30 execution report: tests were NOT RUN.** `python -m pytest --version`
and `python3 -m pytest --version` both failed with command-not-found; `command -v
pytest` found no executable. The subsequent focused command below was denied by
the command permission boundary before launch, with no test exit code:

```sh
PYTHONPATH=src python3 -m pytest -q tests/test_audio_speech_api.py --basetemp=.pytest_cache/w3a-speech
```

No permission workaround, environment bootstrap, dependency installation,
external-path search, subagent, network service, or PostgreSQL run was attempted.
There are no collected pass/fail/skip totals and no claim that the new suite
passes. When an authorized installed offline Python test environment is
available, run these as **separate** commands (test temporary files stay inside
the worktree):

```sh
PYTHONPATH=src python3 -m pytest -q tests/test_audio_speech_api.py --basetemp=.pytest_cache/w3a-speech
PYTHONPATH=src python3 -m pytest -q tests/test_audio_ingress.py tests/test_audio_voice_api.py tests/test_audio_gateway_contracts.py --basetemp=.pytest_cache/w3a-ingress
PYTHONPATH=src python3 -m pytest -q tests/test_audio_output_channel.py -k 'not postgres' --basetemp=.pytest_cache/w3a-output
PYTHONPATH=src python3 -m pytest -q tests/test_serving_contracts.py tests/test_chat_gateway_contracts.py tests/test_chat_gateway_api.py tests/test_embedding_gateway_contracts.py tests/test_decision_gateway_contracts.py --basetemp=.pytest_cache/w3a-compat
```

The final three additions remain untracked and uncommitted; ordinary `git diff`
does not include untracked files. File hashes and final repository status are
reported separately at freeze. No commit, push, PR, deployment, provider adapter,
schema/migration, setup change, real audio E2E, production mount, real voice
license approval, or full #175 completion is claimed.
