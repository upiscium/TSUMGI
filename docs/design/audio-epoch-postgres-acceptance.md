# #247 — Standard Control / PostgreSQL epoch acceptance preparation

**Status: negative-only epoch revocation regressions, not #247 closure.**
The explicit [fence implementation](audio-epoch-fencing.md) is separate from
the proposed external-audio qualification/admission ledger. SSH access and
the unqualified VOICEVOX library view are separate workstreams; neither is
needed for these providerless tests.

## Existing evidence and the missing guarantee

| Existing tests / contract | What they cover | What they do not establish |
| --- | --- | --- |
| `test_voicevox_external_epoch.py`, `test_audio_epoch_control_synthetic.py` | Deterministic proposed qualification/CAS ordering, explicitly non-authoritative | Real Control API, PostgreSQL, durable tombstones, authenticated Worker identity |
| `test_serving_control.py`, `test_postgres_serving.py` | Registered runtime epoch, exact attempt/lease binding and rejection after replacement | External-audio revocation/readback, permanent nonreuse of E1 |
| `test_audio_artifact_authority_postgres.py` | Real transactional artifact-authority predicates and row-lock tests when a test DB is configured | Complete standard-daemon HTTP revocation/reconnect/publication sequence |
| `test_audio_gateway_daemon.py` | Standard audio composition, including separate real-PG spool-owner tests | An admitted/fenced/cleaned epoch ledger |

Ordinary `register_worker` replaces an idle Worker's current advertisement.
The new explicit fence operation adds a persistent historical denial record
and readback; it does not implement `prepare/activate/cleanup` or a positive
qualification grant. `OFFLINE` remains an existing Worker lifecycle state,
**not** an implicit call to the fence operation.

Without an explicit fence, these shortcuts remain insufficient for #247:

- Replacing E1 with E2 does not prevent a later idle E1 registration from
  replacing E2 again.
- An OFFLINE heartbeat that omits its `state` can still renew an existing
  lease. Merely observing OFFLINE is not an epoch lease-revocation receipt.
- An OFFLINE attempt can still complete without an artifact through the
  generic completion path. A rejected WAV publication does not prove every
  positive-write path is fenced.

These were the three pre-implementation reproduction cases. They are not
authorization to change the generic LLM/Worker lifecycle semantics by treating
every OFFLINE transition as a new revocation protocol.

## New test layers

`tests/audio_epoch_acceptance.py` defines one shared effect oracle.
Both test layers call the production `control.daemon.build_app`, parse an
actual TOML configuration and a catalog containing **both** speech and
transcription schemas, and run the standard ASGI lifespan and routes.
They do not assemble a private one-profile router.

- `test_audio_epoch_acceptance.py`: explicitly labelled in-memory rehearsal
  plus prerequisite-gate tests. It substitutes only the repository factory.
  This is not PostgreSQL acceptance.
- `test_audio_epoch_acceptance_postgres.py`: real PostgreSQL 17, UTF8, existing
  migrations in a uniquely created disposable database. It does **not** mock
  the repository, SQL, locks, clocks, transaction results or authority checks.
  Fresh database connections/repository instances independently read back
  Worker/Job/lease/event/fence state after HTTP effects. Negative checks also
  compare raw rows, including SQL-only columns, and staged-file hashes.

All protocol actors, profiles, hashes, voice declarations, text and the
single-frame WAV are synthetic. No RuntimeProvider, engine, inference, GPU,
production credential or private recording is involved. Operator credentials
are synthetic test tokens. The Worker sender is a protocol test actor, not
an actually qualified managed Worker.

### Observable scenarios

| Scenario | Required observation |
| --- | --- |
| Register/claim E1 | Exact E1, deployment, contract, attempt and lease survive independent readback; staged WAV is not publicly readable |
| OFFLINE with a running E1 attempt | A second compatible queued Job and spare slot exist, but no new claim; staged WAV cannot be restaged or published; E2 cannot replace an active attempt |
| Release E1 attempt, OFFLINE, register E2 | Durable old lease/claim identity cleared before E2 claims; E2 read back as current |
| Stale E1 requests after E2 claim | Claim, heartbeat renewal, completion and staging rejected without changing records/files; test both old credentials and E1 with the **current E2 lease/attempt** |
| E2 success | E2 lease expiry strictly increases; stage and completion succeed; durable success references only E2 output; E2 WAV readable, E1 WAV still unavailable |
| Explicit E1 fence and historical readback | Dedicated operator commits the exact immutable identity; same request returns the original receipt, conflicting intent cannot rewrite it |
| Late E1 registration after explicit fence/E2 | Registration and claim rejected, fence remains readable after E2 |
| Fenced E1 renewal without heartbeat state | Rejected without extending the lease, even while Worker state is ONLINE |
| Fenced E1 completion without artifacts | Rejected just like WAV completion, staging, events and failure/retry |
| Fence, client accounting, then E2 | Old attempt cancelled explicitly; only E2 claims, renews, completes and publishes; E1 remains rejected |
| Pre-fence completed output | New reads are withdrawn after the fence without erasing the stored result or bytes |
| OFFLINE without a fence | Existing lifecycle behavior remains compatible; no false revocation claim |

The rotation scenario uses the existing explicit retryable-failure path to
release the old attempt before registration of E2. **It is not a simulation
of successful durable epoch revocation or `CLEANUP_COMPLETE`.** That unfenced
scenario reads current Worker/Job rows and creates no historical E1 fence.
The separate fence sequence uses the operator HTTP endpoint, existing client
cancellation for old-attempt accounting, and then E2 registration. It does
not invent a `CLEANUP_COMPLETE` grant. No SQL update is used to manufacture
epoch transitions.

The OFFLINE scheduling case has a positive control: returning the same E1 to
ONLINE admits the unchanged queued Job. This excludes empty-queue/capacity
exhaustion as the reason for the earlier rejection and explicitly demonstrates
why the reversible lifecycle state is not an epoch tombstone. E1/E2 claim
identities are also checked against the independently configured deployment
and contract, not just equality between the HTTP result and repository decoder.

The old `MissingEpochFence` xfails have been replaced by real regression
assertions using the explicit fence endpoint. OFFLINE compatibility is
asserted separately. There are **no expected failures** in this suite:
fixture, migration, connection, SQL, authorization or rejection failures
block this slice's verification.

## Execution and interpretation

The ordinary offline rehearsal does not contact PostgreSQL:

```sh
env -u ASTRUMWEAVER_TEST_DATABASE_URL \
  -u ASTRUMWEAVER_REQUIRE_AUDIO_EPOCH_POSTGRES \
  PYTHONPATH=src python3 -m pytest -q -r sxX \
  tests/test_audio_epoch_acceptance.py \
  tests/test_audio_epoch_acceptance_postgres.py
```

A real acceptance invocation requires an explicitly selected **test-only**
PostgreSQL 17 server, a test role with `CREATEDB`, and the project's Postgres
dependencies. Supply its DSN securely in `ASTRUMWEAVER_TEST_DATABASE_URL`;
do not publish credentials or substitute a production database.

```sh
ASTRUMWEAVER_REQUIRE_AUDIO_EPOCH_POSTGRES=1 PYTHONPATH=src \
  python3 -m pytest -q --runxfail \
  tests/test_audio_epoch_acceptance_postgres.py
```

Strict mode fails on missing driver/DSN/isolated-database permissions or an
unsupported server version rather than falling back to memory or reporting
a skip as acceptance. Connection and migration errors remain failures.
Ordinary unconfigured runs report **NOT_VERIFIED** skips.

The shared isolated-database fixture creates a fresh UUID-named database,
migrates only that database, and closes/terminates only its connections before
dropping that newly created database. It does not truncate shared tables or
change an installed Control service. Do not run against a test server without
permission for these bounded database operations.

### Historical pre-implementation preparation verification

With the PostgreSQL test DSN unset, after strengthening the spare-capacity
positive control, independent binding assertions and strict lease extension:

- The two targeted test files: **10 passed, 6 skipped, 3 xfailed**, 1 warning.
  All six PostgreSQL skips state `NOT_VERIFIED`.
- The three memory gap cases with `--runxfail`: exactly **3
  `MissingEpochFence` failures**, observing E1 re-registration and claim after
  E2, OFFLINE E1 lease renewal, and OFFLINE E1 artifactless completion.
- Required-PostgreSQL mode with no DSN: **one setup error before connection**,
  not a PostgreSQL execution result.
- Full regression: **3676 passed, 139 skipped, 3 xfailed**, 1 warning and
  8 passing subtests. `git diff --check` passed.

Those results preceded the fence implementation and are not its current
acceptance result. The implementation now uses real PostgreSQL tests as
described in [audio epoch fencing](audio-epoch-fencing.md). The original
44-file native/packaging candidate remains a separate preserved workstream.

### Current implementation verification

With an explicit disposable PostgreSQL 17.11 cluster and both declared
Postgres dependencies available:

- Fence/ASGI/output/artifact-authority target: **223 passed**, 1 warning,
  no skips or xfails, with PostgreSQL required and `--runxfail`.
- Full regression: **3898 passed, 8 skipped**, 1 warning, 8 passing subtests.
- Separate actual clean PostgreSQL restart: the immutable E1 receipt
  survived; E1 registration, claim, renewal and completion were rejected;
  E2 claimed after client accounting.

See [verification evidence and limitations](audio-epoch-fencing.md#verified-local-evidence)
for skip reasons, dependency correction, migration checks and the precise
restart boundary. None of these results activates a native Audio engine.

## Still required

Even an all-pass PostgreSQL run of these cases does not alone prove:

- the full durable qualification/admission/intent-CAS ledger beyond the
  implemented negative-only, idempotent fence;
- complete admission-ledger reconstruction across Control restart, upgrades
  and delayed acknowledgements beyond the negative-only tombstone;
- every claim/publication/activation race in the full proposed protocol;
  the separate repository tests exercise this slice's actual Job → Worker
  serialization for renewal, completion, event append and failure;
- deployed enforcement and coordinated rollout of
  [opt-in Worker-scoped authentication](worker-identity-authentication.md);
  default shared-role mode is not Worker identity proof;
- a real network partition/reconnect or installed TCP daemon acceptance;
- actual managed/native Worker health, external-engine identity/rights,
  acoustic quality, physical log erasure, deployment/apply/rollback, or HA.

This is standard-app **ASGI** testing, not an installed-host or native Audio
E2E claim. The [R4-C0 authority contract](voicevox-external-r4c0-authority.md)
and [synthetic Control boundary](voicevox-external-r4c2a-synthetic-control.md)
remain the design inputs for the remaining implementation. The new migration
and revocation protocol do not change native activation or grant external
qualification.
