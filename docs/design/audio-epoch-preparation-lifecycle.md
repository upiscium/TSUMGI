# Audio epoch preparation: lifecycle proposal (cleanup and activation)

Status: **approved by the maintainer (A + B); C deferred; implemented in this change; the feature stays off.** It answers the open
question in [audio-epoch-preparation.md](audio-epoch-preparation.md#known-limits-and-decisions-to-confirm):
a Worker that has been *prepared* stays out of registration, heartbeat and claims
for good, because the guard is "any recorded preparation" and no cleanup or release
exists. Until this is decided, `[audio_epoch_preparation] enabled` must stay off
everywhere, and it is off at the operator site.

## The problem

1. A preparation is recorded for a future epoch of an offline Worker.
2. Expiry (`expires_at`) and revocation of the qualification only govern replay and
   whether a *new* intent may be recorded. They do not release the Worker.
3. There is no route and no repository operation that ends a preparation. A prepared
   Worker can only be recovered by editing the database by hand.

So turning the feature on can strand a Worker. The feature also has no consumer yet:
no audio engine is qualified (#173, #178, #215, #216), and activation is out of
scope of the slice.

## Options

| | What changes | Result for a prepared Worker | Cost and risk |
| --- | --- | --- | --- |
| **A. Guard follows validity** | The registration/heartbeat/claim guard becomes "has an *unexpired, unrevoked* preparation" instead of "has any preparation". | Self-heals when `expires_at` passes or the qualification is revoked. No new route. | Smallest change (one predicate in the repository and its PostgreSQL twin, plus tests). The Worker can come back without an explicit operator act, which may be undesirable while a preparation is meant to hold the Worker out. |
| **B. Explicit release** | An admin route, `POST /v1/admin/audio-epochs/{worker_id}/{epoch}/release`, appends an immutable release record; the guard ignores released preparations. Optionally combined with A. | Recovered by an operator decision, with an auditable record; history is never rewritten (same shape as the fence). | One new table (migration `008`), one admin route, CAS on the preparation record, tests on both repositories. Needs the operator console and admin auth to expose it. |
| **C. Activation protocol** | `prepared -> activated`: the prepared epoch is registered and admitted, with a receipt that carries the qualification and the previous epoch's cleanup state. | The Worker leaves the prepared state by *becoming* the new epoch. | Largest. It is the positive grant the current slice deliberately omits; it only makes sense once a real engine is qualified, and it needs the E2 cleanup receipt (`CLEANUP_COMPLETE`) that [audio-epoch-fencing.md](audio-epoch-fencing.md) only proposes. |

## Recommendation

Do **A and B together first** (a "cleanup" slice, migration `008`), and defer **C**
until an engine is qualified:

- A alone leaves Workers out of an operator's control (they reappear on expiry); B alone
  leaves expiry as a dead end. Together a prepared Worker leaves the state by expiry,
  revocation or an explicit, recorded release, and never needs a database edit.
- C has no consumer until #178/#215/#216 qualify an engine, and its receipt design
  depends on what a qualified engine needs from cleanup.
- Keep `enabled = false` by default and do not enable it at the site until the cleanup
  slice has shipped and been exercised against PostgreSQL 17.

## What the cleanup slice would contain (if approved)

- Migration `008_audio_epoch_preparation_releases.sql`: an append-only release table
  keyed by Worker and epoch, with a check that a release references an existing
  preparation (`ON DELETE RESTRICT`, same style as `007`).
- Repository: an `is_prepared(worker)` that ignores released and expired-or-revoked
  preparations, used by every place that currently checks "any preparation"; a
  `release_audio_preparation(...)` with the fence's compare-and-swap semantics.
- Admin route and, optionally, an operator-console row showing prepared Workers.
- Tests mirrored on the in-memory and PostgreSQL 17 repositories, including: expiry
  releases the guard, revocation releases it, release is idempotent for the same
  request id and 409 otherwise, a released Worker registers and claims normally, and
  history stays readable after release.
- Docs: update [audio-epoch-preparation.md](audio-epoch-preparation.md) (remove the
  known limit) and the migration/rollback boundary (`008` is additive).

## Decisions needed from the maintainer

1. Approve the recommendation (A + B now, C deferred), or choose another option.
2. Whether release should be a separate admin credential from fencing.
3. Whether the operator console should list prepared Workers (read-only).

Nothing here changes a running site: the routes stay absent unless the setting is on,
and the setting stays off.
