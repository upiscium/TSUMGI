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

A Worker with any preparation is not admitted by registration or heartbeat, and
its claims and state changes are refused (it is checked in the same places as a
fenced epoch); a prepared epoch cannot be registered.

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

## Not part of this slice

Activation, cleanup, a positive grant of any kind, native or external-engine
qualification, an HTTP way to store or revoke qualifications, wiring into Nix or
Worker activation, and any claim that a voice or model may be used.

## Verification

`tests/test_audio_preparation.py` (in memory), `tests/test_audio_preparation_postgres.py`
(PostgreSQL 17 only; a different server version skips as `NOT_VERIFIED`) and
`tests/test_audio_preparation_config.py` (configuration, strict parsing,
no-store). All qualification material in the tests is synthetic.

## Known limits and decisions to confirm

- **A preparation is not undone by expiry.** Any recorded preparation keeps the
  Worker out of registration, heartbeat and claims for good, whatever
  `expires_at` or a later revocation says; `expires_at` only governs replay and
  whether a *new* intent may be recorded. There is no cleanup or release yet, so
  a Worker that was prepared can only be brought back by hand in the database.
  That follows the draft's explicit "any preparation" guard and the activation
  and cleanup protocol being out of scope, but it is an operational risk to
  decide before this is enabled anywhere: either cleanup/activation lands first,
  or the guard becomes "unexpired and not revoked".
- **Migration 007 was edited in place** (bare hex to `sha256:` checks) before it
  was ever applied. It must not exist in any database in its first form; if one
  does, replace it with a new migration instead of re-running this file.
- The in-memory repository now uses the same Worker match as PostgreSQL
  (`worker_matches_fence`). It is still a rehearsal only.
- A second preparation with the same request id, and a preparation of a fenced
  epoch, are 409 on both repositories (PostgreSQL used to answer 503 and to
  accept a fenced epoch; both are covered by tests that fail on the old code).
