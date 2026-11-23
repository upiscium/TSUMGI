# Control audio epoch revocation: negative-only slice

Status: **implemented source and test boundary, not complete #247 or native
Audio qualification**. This extends the ordinary runtime-epoch binding with
a separate, persistent denial record. It does not reinterpret `OFFLINE`,
grant external-engine qualification, activate a Worker, or modify an
installed service.

## Scope and explicit authority

An operator can irreversibly fence the **currently registered**, exact
`(worker_id, runtime_instance_epoch, deployment_revision,
serving_contract_revision)` of a single-contract audio-only Worker.
Non-audio, mixed-capability, unbound and multi-contract Workers cannot be
fenced by this initial interface. Revocation is negative authority, not
evidence of native ownership, health, voice rights or operator qualification.

Management routes are absent by default. The standard Control daemon requires:

```toml
[audio_epoch_fencing]
enabled = true
```

It also requires `ASTRUMWEAVER_AUDIO_EPOCH_OPERATOR_TOKEN`, a distinct
16–512-character bearer token, separate from Worker, Control-client and
Audio-Gateway-client tokens. A token in the environment alone does not enable
the routes. Do not place credentials in TOML, URLs, receipts or public logs.
This source interface is not wired into Nix deployment or external Worker
activation.

| Method / path | Effect |
| --- | --- |
| `POST /v1/admin/audio-epochs/{worker_id}/{epoch}/fence` | Compare the current exact binding and commit an irreversible revocation |
| `GET /v1/admin/audio-epochs/{worker_id}/{epoch}` | Read the exact historical revocation, independently of the current Worker epoch |

Both require the operator bearer, including readback. A POST accepts only
`protocol_version = "v1"`, `expected_deployment_revision`,
`expected_serving_contract_revision`, and `fence_request_id`. Epoch and request
IDs are canonical UUID strings; revisions are canonical `sha256:` digests.
The body is JSON, limited to 2048 bytes and a five-second receive timeout;
unknown/duplicate fields are rejected. Management responses are `no-store`.

A receipt uses `schema_version = "audio-epoch-fence-v1"`, `state = "fenced"`,
`record_revision = 1`, the bound identities, request ID and a Control-assigned
UTC timestamp. The timestamp is not an exact database commit time or ordering
token. The receipt contains no lease token, user payload, engine endpoint,
voice catalog, or qualification claim.

This is a **one-way absent → fenced operation**, not the complete R4-C0
intent/revision state machine:

- An identical request ID and binding return the original receipt, including
  after E2 replaces E1. This resolves an unknown response after a commit.
- Different request IDs or bindings for an existing fence conflict. A request
  ID cannot be rebound to another Worker or epoch.
- A missing record is **not** an admission receipt.
- There is no unfence, delete, refresh, prepare, activate or cleanup-complete
  endpoint.

## Storage and serialization

`006_audio_epoch_fences.sql` adds a table with primary key `(worker_id,
runtime_instance_epoch)`, globally unique fence request IDs, identity-format
constraints, and a restrictive Worker foreign key. Repository methods never
update or delete a fence. This assumes a trusted primary database and
administrators; it does not protect against a DBA directly altering SQL data.

The in-memory reference backend uses its existing repository lock. PostgreSQL
uses the **same Worker row lock** for fencing and authoritative acceptance:

- Fence: lock Worker, validate/read existing fence, insert and commit. It never
  waits for Job locks or rewrites Job/Worker state.
- Claim: lock selected Job, then Worker; recheck the requested/current epoch
  and revocation before issuing a lease.
- Job heartbeat, completion, event append, failure and artifact authority:
  Job → Worker; check revocation before mutation/authority.
- Registration/recovery and state transitions retain the existing identity /
  resource → Worker order and check the proposed epoch. A Worker with fence
  history cannot downgrade to a registration without a serving identity.

This avoids a Worker → Job inversion. The serving-candidate filter excludes
fenced advertisements, but only the locked claim transaction grants a lease.
Admission filtering is not a cached reusable authority check.

The guards apply even when management routes are subsequently disabled.
There is no feature toggle that makes existing fences ineffective.

## In-flight work, cleanup and E2

Fencing does not change `WorkerState`, clear stored lease fields, physically
erase artifacts or stop a native process. It makes the old epoch's lease
**ineffective for further Worker-authorized operations**. Re-registration,
claim, heartbeat/renewal, state changes, success, events, staging authority,
and Worker failure/retry under E1 are denied.

Existing client cancellation and Control expiry/recovery remain accounting
paths. The old running attempt must be resolved before normal registration
can replace it with E2. Cancellation does not erase the tombstone and does
not produce the proposed `CLEANUP_COMPLETE` receipt.

After accounting, normal registration can accept a distinct E2; E1 remains
fenced permanently within its logical Worker identity. New tests exercise
E2-only claim, renewal, completion and output publication after this sequence.
This is **not** new qualification/admission of an external engine.

Output reads also consult the historical fence, including outputs whose Jobs
completed before revocation. The Job result and bytes are retained, but a
post-fence read is unavailable. This is call-time withdrawal, not a claim
that bytes already sent before a concurrent fence can be recalled, or that
DB/filesystem/HTTP effects are one atomic transaction. In-flight pre-fence
private staging cannot grant post-fence success.

Ordinary OFFLINE behavior without an explicit fence is unchanged. Its
reversibility, stateless heartbeat renewal and artifactless completion are
retained as compatibility tests, not treated as epoch revocation.

## Migration and rollback boundary

The migration is additive and introduces no positive admission record. Apply
it only through the existing migration workflow to the intended database.
Missing required schema keeps Control readiness unavailable.

Once fences exist, **do not mix old and new Control binaries** or roll back
to a version that ignores this table. Disabling the management API is safe
because enforcement remains active; dropping the table or reverting to an
unaware binary is not a safe rollback. Stop admission and retain the records
while preparing a forward fix. No installed-host migration, rollback or
production service update is claimed by the local tests.

## Verification and residual gates

Tests include:

- Standard `control.daemon.build_app`, complete Audio catalog and ASGI
  lifespan in `tests/audio_epoch_acceptance.py`, shared by memory and real
  PostgreSQL 17 fixtures.
- Dedicated-token/default-off/strict-parser regressions in
  `tests/test_audio_epoch_fencing_api.py`.
- Equivalent repository effects and real PostgreSQL Job/Worker row-lock
  interleavings in `tests/test_audio_epoch_fences*.py`.

Run against an explicitly disposable PostgreSQL 17 test server:

```sh
ASTRUMWEAVER_REQUIRE_AUDIO_EPOCH_POSTGRES=1 PYTHONPATH=src \
  python3 -m pytest -q --runxfail \
  tests/test_audio_epoch_acceptance.py \
  tests/test_audio_epoch_acceptance_postgres.py \
  tests/test_audio_epoch_fences.py \
  tests/test_audio_epoch_fences_postgres.py \
  tests/test_audio_epoch_fencing_api.py
```

Supply `ASTRUMWEAVER_TEST_DATABASE_URL` securely. Each real-DB case creates,
migrates and drops only its fresh UUID-named database. A private local cluster
may use an owner-only Unix socket with no TCP listener; durability checks
must leave `fsync` and `synchronous_commit` enabled.

### Verified local evidence

On PostgreSQL **17.11**, UTF8, with an owner-only Unix socket, no TCP listener,
and `fsync=on` / `synchronous_commit=on`:

- The eight-file fence/ASGI/output/artifact-authority regression run passed
  **223 tests**, with `ASTRUMWEAVER_REQUIRE_AUDIO_EPOCH_POSTGRES=1` and
  `--runxfail`; no skips or expected failures.
- Full regression passed **3898 tests**, with **8 skips**, 1 Starlette
  deprecation warning and 8 passing subtests. The skips were six separate
  Chat-Gateway private-cluster tests requiring PostgreSQL executables in
  their test PATH, one GPU-host smoke test and one file-ownership test.
  These skips are not acceptance evidence.
- Migration/readiness and connection-pool checks passed **3 tests**. The
  legacy-schema fixture now removes dependent tables before reconstruction
  and checks the fence's restrictive Worker foreign key.
- A separate owned-cluster probe committed E1's fence, closed repositories,
  performed a real `pg_ctl` **fast/clean restart**, and independently reopened
  the database. Historical receipt equality and exact retry equality held.
  E1 registration, claim, renewal and completion each raised
  `audio epoch is fenced` without changing Worker/Job/event state; after
  client cancellation, E2 claimed successfully and E1's receipt remained.
  Durability settings were checked both before and after restart. This is
  not crash/power-loss testing or installed-Control TCP restart acceptance.
  The owned test cluster was stopped afterwards with no remaining client
  connections; its data and private receipts were retained, not deleted.
- Six offline Nix evaluations passed: `voicevox-service-view`,
  `voicevox-bundle`, `audio-worker-module`, `audio-control-module`,
  `runtime-module-eval` and `module-eval`. Evaluation is not realization.
- `git diff --check`, new-file syntax/whitespace checks and the original
  44-file candidate's preservation fingerprint passed.

The initial full run exposed a stale migration-list expectation and a missing
`psycopg-pool` dependency in the ad-hoc Python environment. The latter selected
the repository's documented per-call fallback; it was not an asynchronous
pool-startup defect. Final runs included both declared Postgres dependencies,
including the repository-pinned pool package. The pool test and production
pool implementation were not weakened or changed to obtain these results.

The separate [Worker identity slice](worker-identity-authentication.md) adds
opt-in per-Worker bearer scoping. Default shared mode still identifies only
a role; enabling fencing alone does not enable scoped authentication.

Still open: qualification evidence, monotonically ordered operator intent,
prepare/activate/cleanup receipts, finite admission freshness/partition policy,
real TCP/Worker/network faults, native ABI, installed-host credential
provisioning/apply/rollback and operational authorization. A caller able to
propose a **new** epoch through existing registration has not thereby obtained
external-engine qualification. These bounded slices must not be presented as
completing the full [R4-C0 contract](voicevox-external-r4c0-authority.md).
