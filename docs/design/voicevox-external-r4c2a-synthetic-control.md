# TSUMGI #247 R4-C2-A — Synthetic Control epoch authority adapter

**Status:** local, source-only contract and deterministic isolated tests. **NOT** a Control repository implementation, real admission protocol, authoritative epoch tombstone, production Worker integration, database migration, deployment or activation.

**Baseline:** `main` at `51536d383811e00ddc8db9ea5bda52e0ec08487b` (R4-C1 Merge Commit, post-merge CI green). Source authority specification: [R4-C0](voicevox-external-r4c0-authority.md), with [R4-C0 acceptance matrix](voicevox-external-r4c0-synthetic-matrix.md). Read-only R4-C2/D0 design captured in Issue #247 comment `6762112185`.

## Scope and integration isolation

The source-only `astrumweaver.audio.epoch_control_synthetic` module is **not imported by** `control.repository`, `control.postgres`, `control.api`, `worker.daemon`, `worker.runtime`, the serving registry or the audio Gateway. Placement under the audio package preserves the architectural rule that the generic Control plane must **never depend on first-class RuntimeProvider modules**. The Control-epoch contract is simulated outside Control; the future generic implementation must accept provider-agnostic serving/qualification authority values through a reviewed boundary. It constructs no connections, does not register a Worker, write the DB, open sockets or call a user-managed VOICEVOX engine.

- `SyntheticPrincipal` is an **untrusted test assertion of intended actor scope**, not a credential, authenticated Worker token, signed authorization or operator identity. The real Control currently accepts a shared Worker bearer token, which does not cryptographically identify the URL Worker ID. Resolving that actor-provenance blocker is a separately authorized prerequisite for live C2/Worker integration.
- `SyntheticWorkerWitness` is a **fake snapshot** with exact Worker/epoch/Serving identity and reported health/freshness fields. Its construction is no health or engine attestation. It cannot establish that the engine is genuinely VOICEVOX, its voices are licensed, logs are safe, or Worker resources are available.
- R4-C1 `ExternalAudioQualification` is only an immutable `UNVERIFIED` or `REVOKED` declaration. R4-C2-A binds its reviewed-*fake* value to the exact `AudioEpochIdentity` but cannot create or verify real production rights/identity evidence. A plain digest (including the observed `/speakers` digest) never becomes a license grant.
- `SyntheticAudioControlEpochAdapter` stores volatile in-memory records under a single `RLock`. It intentionally uses simulation states named `PREPARED`, `ADMITTED`, `FENCED` and `CLEANUP_COMPLETE` to exercise ordering. Its `ready`, `can_apply`, `control_authoritative`, `can_claim` and `can_positive_write` are **always false**, including when an internal record says ADMITTED.
- **No real Job or Lease model is imported into this adapter.** The outstanding-attempt counter and the explicit `outputs_revoked_for_test` flag are deliberately fake test inputs. They are not proof of terminal Job state, artifact revocation, privileged cleanup or actual filesystem garbage collection.

## Exact synthetic transitions

| Method | Synthetic guard | Synthetic effect |
| --- | --- | --- |
| `prepare_for_test` | Worker/operator-scope test context, full R4-C1 qualification binding, expected per-Worker intent floor, unique global E, no unclean older E, valid synthetic control clock | One PREPARED record at revision 1 |
| `activate_for_test` | Same E and full Serving identity, PREPARED, CAS revision and intent, online/READY fake witness, both fake Control and Worker deadlines not expired, **explicit `synthetic_gate=True`** | One simulated ADMITTED record, **not** authority |
| `fence_for_test` | Scoped actor and exact R4-C1 `AudioEpochFenceIntent`, same E, current record revision, strictly increasing revoke intent, globally unique request ID | FENCED record and monotonic floor, stored idempotent receipt; **not** cleanup |
| `set_outstanding_for_test` | Operator-scoped fake, exact count CAS, no increments except under simulated ADMITTED, no increase after FENCED | Fake unresolved-attempt count adjustment only |
| `cleanup_for_test` | Operator-scoped fake, FENCED revision + intent CAS, **zero fake outstanding attempts** and explicit `outputs_revoked_for_test=True` | CLEANUP_COMPLETE fake receipt, tombstone remains |
| `readback_for_test` and `receipt_for_test` | Scoped fake actor, exact E and request ID for receipts | Defensive immutable snapshots, no state change |
| `report_for_test` | No external authority | Redacted fake status with all real authority flags false |

A request ID is a globally unique canonical UUID in this disposable ledger and is bound to its **operation, actor, Worker, E and exact immutable input**. An exact replay returns the original receipt without reverting later FENCED/CLEANUP state. Reusing an ID with changed intent, changed request, different operation, or different scope conflicts. A request can be committed while its response is lost; exact readback is required to resolve that uncertainty.

A stale activation cannot revive a FENCED E. A distinct E2 is only prepared after E1 has reached CLEANUP_COMPLETE and is never allowed to reuse E1's UUID. The fake's internal witness validity and clock checks are not replacements for PostgreSQL `clock_timestamp()` measured **after** row-lock waits.

## Test oracle and exclusions

`tests/test_audio_epoch_control_synthetic.py` supplies deterministic synthetic fixtures and verifies: disabled-by-default actual authority; exact qualification and Worker identity binding; scoped fake actors (not real authentication); two activate/fence orderings; monotonic CAS; mutated request/replay; late ACK; cleanup distinct from fence; outstanding-count refusal under PREPARED/FENCED; expiry under supplied synthetic clocks; epoch nonreuse; redaction of endpoint/voice/license/credit/native text; no socket/native-process calls.

Passing this test suite **does not** establish PostgreSQL durability, transaction serialization, real Worker auth, stage/finalize atomicity, bounded partition staleness or compatibility with existing production Job lease paths. No tests touch an actual Control server, PostgreSQL, Worker, AudioOutputChannel or native VOICEVOX process.

## Future separately approved gates

- **R4-C2-B:** Introduce a reviewed additive `006_*.sql` schema and PostgreSQL repository implementation **only in a separately authorized isolated disposable test database**, with exact durable request idempotency, tombstone persistence, Worker identity and Job→Worker→E lock-order tests, migration replay and rollback. Database access and migration are not permitted by this document.
- **R4-C2-C:** Guard Control's actual Worker registration/recovery, Job claim/heartbeat lease renewal, event append, audio artifact authorization, durable terminal success and output publication paths with atomic epoch checks; bind exact scoped authenticated actor provenance. No real operations without separate authorization.
- **R4-C3/C4 and R4-D:** Worker/provider/Gateway integration, host/Nix setup, user-managed VOICEVOX engine trust, voice license/credit and native `/audio_query?text=` URL logging privacy review with separately approved live tests.

The external engine remains **user-installed and user-managed**. A normal attach must never require manual PID/ELF/model-hash entry merely to detect an instance, but loopback observation alone is not cryptographic authentication.

**Operational gate: `G-AUDIO-OPS=STOP`.** Keep Issue #247/#215/#180 OPEN. No Commit/Push/PR/Ready/Merge/Deploy/Activation or actual DB/Worker/Control operations are authorized by the source-only R4-C2-A implementation and review.
