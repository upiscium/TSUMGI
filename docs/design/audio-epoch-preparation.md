# Control audio epoch preparation: non-admitting intent slice

Status: **implemented source and test boundary, not activation, not native
Audio qualification**. A Worker can record that a *future* runtime epoch is
prepared against a reviewed qualification. The record admits nothing: it does
not register an epoch, claim a Job, activate a Worker, start an engine, or
grant rights to a voice or model. `can_claim` and `can_activate` are always
`false` in a receipt.

This is the positive counterpart of the negative-only
[epoch fence](audio-epoch-fencing.md). The previous session left it half
built (storage and migration without routes, config or a working harness); the
behaviour below is what the code and `tests/audio_preparation_cases.py` now
establish, written down because no design document existed.

## Authority and configuration

- Routes are **absent by default**. Enable with:

  ```toml
  [audio_epoch_preparation]
  enabled = true
  ttl_seconds = 60        # 1-300, an integer, optional
  ```

  Unknown keys, non-boolean `enabled` and out-of-range or non-integer
  `ttl_seconds` stop Control at start-up.
- It requires `[worker_auth] mode = "per-worker"`. With one shared Worker token
  any Worker could prepare another's epoch, so Control refuses to start (and
  `create_app` refuses to build the routes) without per-Worker credentials.
- Callers authenticate as the Worker named in the path: the principal must equal
  `worker_id` (403 otherwise; 401 without a known token).
- **There is no HTTP route that creates or revokes a qualification.** A
  qualification is stored by an owner-controlled process through the repository
  (`store_audio_preparation_qualification`, `revoke_audio_preparation_qualification`).
  The routes only *consume* qualifications that already exist.

## Routes

| Method / path | Effect |
| --- | --- |
| `POST /v1/workers/{worker_id}/audio-epochs/{epoch}/prepare` | Record the preparation of `epoch` (compare-and-swap on the intent revision) |
| `GET /v1/workers/{worker_id}/audio-epochs/{epoch}` | Read the exact historical record, independently of what is current |
| `POST /v1/admin/audio-epochs/{worker_id}/{epoch}/release` | Operator only: record that the preparation no longer holds the Worker out |
| `GET /v1/admin/audio-epochs/{worker_id}/{epoch}/release` | Operator only: read the stored release record (404 if none) |

A POST body is exactly `protocol_version = "v1"`, `qualification_revision`,
`expected_intent_revision` (a non-negative integer, not a boolean) and
`prepare_request_id`. Epoch and request ids are canonical lower-case UUIDs,
revisions canonical `sha256:` digests. At most 2048 bytes within five seconds;
duplicate or unknown fields, a wrong protocol version, bad types or a malformed
body are 422. The body is judged on its content, not its content type: the route
is bearer-only, with no ambient browser credentials, like the other Worker
routes. Every response is `Cache-Control: no-store`.

A receipt has `schema_version = "audio-epoch-preparation-v1"`,
`state = "prepared"`, `record_revision = 1`, the bound identities, the
qualification, the request id, `expected_intent_revision`, `intent_revision`
(always the former plus one), and `prepared_at` / `expires_at` (the earlier of
the qualification's expiry and `ttl_seconds` after the request).

## Release and the active preparation

A preparation is **active** iff it has no release record, its `expires_at` is
later than the request time, and its qualification is not revoked. The guard
that keeps a Worker out of registration, heartbeat, claims, state changes and
job candidate selection is "has an *active* preparation", no longer "has any
preparation". A Worker therefore leaves the prepared state by expiry, by
revocation of the qualification, or by an explicit release, and never needs a
database edit. The request time is passed by the caller (not SQL `now()`), so
the in-memory and PostgreSQL repositories agree.

The release routes sit in the same block as the fence routes: the same operator
bearer credential (`TSUMGI_AUDIO_EPOCH_OPERATOR_TOKEN`, so they exist only when
`[audio_epoch_fencing]` is enabled), the same strictness and `no-store`. A POST
body is exactly `protocol_version = "v1"` and `release_request_id` (a canonical
UUID); JSON content type (415), at most 2048 bytes within five seconds (413,
408), duplicate or unknown fields and bad values are 422.

The release receipt has `schema_version = "audio-epoch-preparation-release-v1"`,
`state = "released"`, `worker_id`, `runtime_instance_epoch`,
`release_request_id` and `released_at`. Semantics: the preparation must exist
(404); the same `release_request_id` again returns the stored receipt unchanged;
a different one for an already released preparation, or an id already used for
another preparation, is 409. An already expired or revoked preparation may be
released (it only records the release). History is never rewritten: the
preparation record reads back exactly as before. A released preparation is not
replayed as active and does not block recording the next intent (which still
needs the right `expected_intent_revision`).

## What must hold (all failures are one generic 409)

- The qualification is stored, belongs to this Worker, is `reviewed`, is not
  revoked, and the request time is inside its validity window.
- The Worker's current advertisement has exactly one contract, and it is an
  audio contract; its deployment revision, serving contract revision and the
  Worker's spec revision equal the qualification's.
- The Worker is `offline` with no active Job and none running.
- The epoch is not the Worker's current epoch, is not fenced, and no Job was ever
  claimed under it.
- `expected_intent_revision` equals the Worker's highest recorded intent
  revision (0 for the first), the request id is unused, and the Worker has no
  other unexpired preparation whose qualification is still valid.
- A replay with the same qualification, intent revision and request id, while
  the record is unexpired and still the latest, returns the same receipt.
  After the qualification is revoked the same request is 409, but the
  historical receipt stays readable and unchanged: revocation never rewrites
  history.

A Worker with an active preparation is not admitted by registration or heartbeat,
and its claims and state changes are refused (it is checked in the same places as
a fenced epoch); a prepared epoch cannot be registered while its preparation is
active.

## Digest convention

Every revision is the canonical `sha256:<64 hex>` form used everywhere else in
Control (including the fence table). A qualification's revision is the digest of
its canonical payload, so changing any field changes it. The migration enforces
the same form. (The first draft mixed bare hex and the prefixed form, so no
qualification could ever match a Worker's advertised revisions.)

## Persistence

Migration `007_audio_epoch_preparations.sql`: `audio_preparation_qualifications`
(the stored, immutable qualification body), `..._revocations` (an append-only
revocation mark) and `audio_epoch_preparations` (one row per Worker and epoch,
unique request id, unique intent revision per Worker, a check that
`intent_revision = expected_intent_revision + 1`). Rows reference `workers` with
`ON DELETE RESTRICT`. The in-memory repository implements the same rules and is a
rehearsal only, never durable evidence.

Migration `008_audio_epoch_preparation_releases.sql` is additive: an append-only
`audio_epoch_preparation_releases` table keyed by Worker and epoch, with a unique
request id and a foreign key to the preparation (`ON DELETE RESTRICT`). Control
now requires migrations up to `008`; the older migrations are unchanged and
nothing is dropped.

## Not part of this slice

Activation (the cleanup receipt of the previous epoch is still only proposed), a positive grant of any kind, native or external-engine
qualification, an HTTP way to store or revoke qualifications, wiring into Nix or
Worker activation, and any claim that a voice or model may be used.

## Verification

`tests/test_audio_preparation.py` (in memory), `tests/test_audio_preparation_postgres.py`
(PostgreSQL 17 only; a different server version skips as `NOT_VERIFIED`) and
`tests/test_audio_preparation_config.py` (configuration, strict parsing,
no-store). All qualification material in the tests is synthetic.

## Known limits and decisions to confirm

- **Release and expiry (cleanup slice, implemented).** The earlier limit that a
  preparation kept the Worker out forever is gone: see "Release and the active
  preparation". Activation (option C of
  [audio-epoch-preparation-lifecycle.md](audio-epoch-preparation-lifecycle.md)) is
  deferred. The feature stays default-off and is not enabled anywhere.
- **Migration 007 was edited in place** (bare hex to `sha256:` checks) before it
  was ever applied. It must not exist in any database in its first form; if one
  does, replace it with a new migration instead of re-running this file.
- The in-memory repository now uses the same Worker match as PostgreSQL
  (`worker_matches_fence`). It is still a rehearsal only.
- A second preparation with the same request id, and a preparation of a fenced
  epoch, are 409 on both repositories (PostgreSQL used to answer 503 and to
  accept a fenced epoch; both are covered by tests that fail on the old code).
