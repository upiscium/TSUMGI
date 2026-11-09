# TSUMGI #247 R4-C0 — external VOICEVOX authority and epoch-fencing contract (proposal r0)

**Status: local docs-only proposal; NOT an implemented protocol or operational approval.**
Baseline: `main` `334c9bceaba15ea8ffdc69f73955e40afb16c9c1` (R4-B1/B PR #255 merged; post-merge CI #42621626092 green).
Companion: [R4-C0 synthetic acceptance matrix](voicevox-external-r4c0-synthetic-matrix.md).
Track: #247, #180, #215, #214, #181; prior #247 [R4-C/D0 design](https://github.com/upiscium/TSUMGI/issues/247#issuecomment-6740909381).

## 0. Scope and authority limits

This file specifies a **future** external-engine audio-only Worker and a Control-owned, per-serving-epoch admission/revocation protocol. It changes **no** Python, APIs, schemas, migrations, RuntimeProviders, installations, Worker/Control settings or host state. `G-AUDIO-OPS=STOP`; #247/#215/#180 remain open.

Currently merged R1–R4-B1/B discover a single user-configured literal loopback endpoint and reconcile the *complete* R1 catalog against an independently operator-pinned digest, voice selections and policy declaration. The resulting `qualification_requested`, R4-A `generation_counter`, `retirement_pending` and R4-B1/B state are **non-authoritative in-memory advice**. They are not managed readiness, legal rights, process identity, Control ACK, durable lease fence, `RuntimeInstance.epoch` or a serving-capability advertisement.

**Shipping invariants for all R4-C source-only slices:** feature/default disabled; no runtime can emit a positive `ready` or `can_apply` from an R1/R3/R4-A result, no user-facing audio capability/serving registration, no real synthesis, model download, engine process control, external-origin probing, PostgreSQL migration, deployment or activation. Positive synthetic records below are explicitly test-fixture data, not production grants. R4-D and an independent operational authorization remain necessary.

## 1. Boundaries in existing source (observed, not assumed)

| Existing component | Actual scope and constraint | Proposed R4-C relationship |
| --- | --- | --- |
| `runtime/providers/voicevox_discovery.py`, `voicevox_external_observation.py` | Bounded opt-in GET `/version` and `/speakers` on worker-namespace `127.0.0.1`; complete canonical R1 observation remains private; no native engine attestation. | Observation provider **only**; no conversion of its digest to an admission token. |
| `runtime/providers/voicevox_external_plan.py`, `voicevox_external_reconcile.py` | Pure R3/R4-A advisory candidate/withdrawal; `retirement_pending` is not a Control lease release receipt. | Generate candidate/withdrawal *intent*; Control fencing remains independent. |
| `runtime/deployment.py::_PROVIDER_TYPES` | No external-VOICEVOX provider registered. Existing owned VOICEVOX mode has different process-control guarantees. | No direct registry enable; design separate attach-only ManagedRuntime/executor after approval. |
| `worker/daemon.py::run_worker` | Requires reviewed audio serving manifest and managed runtime path; `_require_audio_ready_health` checks coherent READY, independent of a mere manifest; builds a fresh serving epoch. | Keep these checks. Do not change `executor.factory` audio bypass denial. |
| `worker/supervision.py::RuntimeHealthSupervisor` | Fail-latched, single-use; loss/expiry stops Worker claims and restart is needed for recovery. | Preserve latch; new Worker invocation/epoch for requalification, not a heartbeat promotion. |
| `worker/runtime.py::WorkerRuntime` | WorkerSpec capabilities/ServingAdvertisement fixed for invocation; `ready` requires Control acknowledged ONLINE, freshness and supervisor. Claim and execution check serving/lease. | A separate logical audio-only Worker; no dynamic mutation of another GPU/LLM Worker. |
| `serving.py` | `ServingDeploymentDeclaration`/`WorkerServingAdvertisement`/per-invocation `RuntimeInstance.epoch` and `ServingJobBinding` are existing **identities**. | Retain exact identities; add Control-authoritative *fencing state*, not an unrelated replacement for job binding. |
| `control/repository.py`, `control/postgres.py` | In-memory and transactional backends enforce registered Worker serving epoch and job attempt/lease. Existing Worker registration and TTL/recovery semantics are not an audio-qualified per-epoch tombstone ledger. | Extend under a separate approved stage, with identical semantic checks in both backends. |
| `gateway/audio.py`, `setup/audio_provider_binding.py` | Profile/voice and setup declarations do not establish actual native ownership, trust or health. Binding reviews currently target owned runtime specs. | No Gateway/profile/voice publication until exact external qualification + committed authority and R4-D evidence. |

Source references denote current names as of the baseline; they are **not** a claim that the proposed APIs already exist.

## 2. Proposed allocation: one logical CPU-only audio Worker

Initial MVI uses a distinct, opt-in **logical audio-only Worker identity**, with `capabilities == {"audio.synthesize"}`, explicit reviewed `ServingDeploymentDeclaration` and a **new non-owning external VOICEVOX ManagedRuntime**. It must not advertise LLM/embedding/decision capabilities or share/claim their GPU UUIDs. Use `gpu_count=0` and no GPU UUID reservations for the first reviewed CPU resource profile. CPU process placement/quotas and coexistence with other CPU workers require a separate host resource review; `CPU-only` does **not** imply free or exclusive CPU capacity.

The poller and external engine must share the same network namespace for `127.0.0.1` to work. Cross-LXC/host endpoints, Cloudflare Tunnel/public ports, LAN discovery, `localhost` remapping and arbitrary URI input are **not** authorized by this contract. No automatic installation or ownership of an external engine, its PID, ELF, models, HTTP access logs or license. Never import a discovered process into `VoicevoxOwnedManagedRuntime` or relax its process proof.

No manual PID/ELF/model hash entry is required merely to **observe/attach** user-managed VOICEVOX. This does **not** make a loopback port cryptographically authenticated. The production *trust-mode qualification* must explicitly record operator-controlled namespace/endpoint and accepted residual impersonation risk; if policy requires stronger verified identity, it must be provided by separately reviewed evidence. Public `external_engine_verified=True` is forbidden unless its precise proof has been independently established. Mere `/speakers` presence or a hash cannot attest voice rights or credit compliance. The R4-D user-facing qualification UX, privacy/logging and rights evidence requirements are **open gates**, not invented automatic approvals.

## 3. Three disjoint authorities

**A. Observation (Worker-local, non-authoritative).** Canonical R1 health/catalog, independent observed-digest pin, full immutable selected VoiceAlias declarations, `UNVERIFIED|REVOKED` policy declaration and R4-B1/B current launch token. Observed drift, lost probe, stop or policy revocation immediately cancels local candidacy. No positive registration or epoch.

**B. Qualification (operator/R4-D authority, not an R3 digest).** Proposed immutable `ExternalAudioQualification` keyed by `qualification_revision`. Its private reviewed body binds: `worker_id` and CPU resource context; exact deployment/ServingContract/voice-semantic revision; network namespace and allowlisted endpoint; independently pinned R1 digest; complete voice UUID+numeric style+license/credit declarations; explicit operator trust mode and acceptance revision; rights/attribution decision and privacy/logging evidence; expiry/revocation status; and the explicit authentication assurance level (*operator-accepted* versus *cryptographically verified*). Only a dedicated validation actor may attest the actual evidence; hashing an unapproved operator declaration remains UNVERIFIED. A candidate cannot promote itself, cannot auto-update a changed observed digest, and cannot claim external identity verification on the basis of a matching digest. R4-C0/C1 cannot issue a production-admissible qualification.

**C. Admission (Control-authoritative, durable).** Proposed `AudioServingEpochRecord` keyed by `(worker_id, runtime_instance_epoch)`, plus a persistent monotonically ordered per-Worker `intent_revision` and `record_revision` (CAS token). Fields include exact `qualification_revision`, `deployment_revision`, `serving_contract_revision`, `voice_semantic_revision`, `worker_spec_revision`, `state`, Control-clock `prepared_at/admitted_at/fenced_at`, bounded `fresh_until`, `fence_request_id` and `last_fenced_revision`. Unique epoch identities must not be recycled, including after restart, cleanup or data migration. A tombstone survives all supported replay windows. Only Control's locked transaction grants or invalidates admission; an ACK, local Planner counter, Worker heartbeat or arbitrary client proposal is not itself a grant.

Proposed public diagnostics contain only stable redacted status/opaque revisions, never raw user input, endpoint, speaker UUID/style, native HTTP exception, license/attribution, bearer token or payload. Private evidentiary records require restricted access and independent data-retention policy.

## 4. Versioned state machine, guards and ordering

These are **future proposed states**, not existing source enums.

| State | Entry guard | Permitted effect | Exit |
| --- | --- | --- | --- |
| `BLOCKED` | Default, disabled, lost trust, invalid config, or unknown persisted Control state | No audio serving, no admission | `OBSERVED_UNQUALIFIED` on valid *observation only* |
| `OBSERVED_UNQUALIFIED` | Current R1 observation matches independent pin and selected voices | Local unqualified advice only | `QUALIFICATION_PENDING`; loss returns `BLOCKED` |
| `QUALIFICATION_PENDING` | Explicit review requested, exact source and operator intent frozen | No Control prepare/registration | `QUALIFIED_INACTIVE` only with R4-D-authorized qualification; otherwise `BLOCKED` |
| `QUALIFIED_INACTIVE` | Valid reviewed, nonexpired qualification with exact identities and current health; separate operational gate open | Proposed Control prepare request, not serving | `CONTROL_PREPARED` on committed prepare |
| `CONTROL_PREPARED` | Control persisted unique fresh epoch and exact qualification/deployment/Worker binding under CAS; not yet admitted | No claims/serving; prepare receipt only | `ADMITTED(E)` on atomic activate + exact Worker ACK/health and live gate |
| `ADMITTED(E)` | Control transaction sets E as sole admitted epoch; exact Worker serving epoch/registration, supervisor READY, Control freshness and qualification all hold | Atomic audio claims/lease/attempt authority **only** for E | `FENCED(E)` on loss/revoke/drift or expiry; never directly to new E |
| `FENCED(E)` | Control CAS atomically blocks all new claims/renewals/positive terminal/audio event/artifact authority for E | Reconciliation, negative terminal accounting and secure cleanup only | `CLEANUP_COMPLETE(E)` after exact old attempts resolved/expired |
| `CLEANUP_COMPLETE(E)` | All E attempts and logical artifacts accounted for; no valid lease remains | Retain tombstone; does **not** assert native process stopped or physical blob GC | New distinct E' may be prepared after fresh requalification |

The `BLOCKED` through `QUALIFIED_INACTIVE` phases are **local qualification workflow**; `CONTROL_PREPARED` through `CLEANUP_COMPLETE` are **Control persisted epoch states**. They must not be collapsed to one volatile enum. A detected engine with no reviewed external trust/rights/logging remains local BLOCKED for serving. Every R4-C0 and R4-C1 fixture has `live_gate=false`; no test may interpret synthetic positive transitions as production admission.

The initial design **requires old E to reach CLEANUP_COMPLETE before E' becomes ADMITTED for the same logical Worker**. The exact policy for preparing E' while E is FENCED is configurable at C2, but overlap of two ADMITTED epochs is forbidden. This can temporarily reduce availability; intentional for the MVI.

### Protocol (proposed, not a shipped endpoint)

- `prepare(worker_id, E, expected_intent_revision, qualification_revision, deployment_revision, serving_contract_revision, request_id)`: authenticated and scoped to the logical Worker; validates immutable configuration, operator/R4-D gate, fresh health and Control-clock lease; writes unique PREPARED record with conditional CAS. Never auto-creates a qualification from R1 observation.
- `activate(worker_id, E, expected_record_revision, expected_worker_serving_epoch, request_id)`: atomically checks exact latest intent/qualification, matching registered `WorkerServingAdvertisement.runtime_instance.epoch`, no other ADMITTED epoch, old epochs cleaned, Worker ONLINE and health/freshness. Responds only with persisted commit evidence; response arrival alone cannot create permission.
- `fence(worker_id, E, expected_record_revision, revoke_intent_revision, fence_request_id)`: **monotonic, idempotent Control CAS** marking E FENCED (or no-op if already FENCED/cleaned). Commit and required cleanup/attempt invalidation are in the same atomic boundary; a late prepare/activate/heartbeat cannot restore E. A stale expected revision or different request identity returns CONFLICT and requires readback, never blind retry.
- `readback(worker_id, E, request_id)`: exact authenticated durable read, with `state`, `record_revision`, `last_fenced_revision`, Control commit time and bound `qualification_revision`. This is the safe resolution after response timeout/unknown commit.
- `cleanup_complete(worker_id, E, expected_record_revision)`: privileged, independent Control check that every active attempt/lease for E is terminal or expired and logical artifact authority invalidated. It emits a separate receipt; does not certify native VOICEVOX process termination, external HTTP logs erased or physical spool deletion.

Every operation carries exact `worker_id`, E (canonical UUID), operation-specific immutable idempotency identity, intent/revision preconditions and authenticated actor scope. Different request IDs cannot replay a success for another epoch. All transitions are monotonic; a `FENCED`/cleaned E never returns to `ADMITTED`, and old records may never be deleted merely to permit a reuse.

### Local/Control ordering and in-flight work

1. On R1 loss, config/policy/voice/license/credit change, timeout, probe failure or operator stop: **synchronously** prevent any new local execute/claim and revoke the R4-A candidate before awaiting cancellation. Cancel and join TSUMGI-owned HTTP/work, not the user's external engine. Attempt to fence E in Control using the exact request ID and previous revision.
2. Control's **committed** `FENCE_COMMITTED(E, record_revision)` means E is durably excluded from claim, lease renew, audio event/artifact commit and positive terminal writes. A notification/request delivered or local `retirement_pending` is **not** an ACK.
3. If an existing task produced external WAV while fencing, it must not finalize that result under E. Its still-running attempt may be terminally failed/expired by exact cleanup policy, and disposable staged blobs handled by independently reviewed spool GC. A worker cannot claim user data was removed from the native engine log.
4. `CLEANUP_COMPLETE(E, record_revision)` is issued only after exact `(job_id, attempt, lease_token, E)` attempts have no effective leases and all logical output authorities are invalidated. Failures/expired attempts may be reconciled, but no **positive** old-epoch finalization is permitted. Physical artifact deletion is separately proven, not inferred.
5. Unknown/late ACKs (including older `prepare`, `activate`, `fence` or `cleanup` responses) are accepted only for the exact request ID/epoch/revision; none may rewrite current Control state. At an uncertain write boundary, **read back the durable record first**; do not retry a possibly committed CAS blindly. A retry may use the same idempotency key only under a proven equivalent intent.
6. On Worker crash/restart: start blocked with a *new* Worker invocation/serving E', read exact prior epoch(s) from Control and reconcile FENCED/CLEANUP_COMPLETE before any registration/admission. A restored local R4-A counter or a fresh `/speakers` scan is insufficient. If Control is unreachable, no capability is admitted.
7. On Control/DB restart: authoritative records/tombstones and attempted leases survive. Reconstruct no admission from Worker heartbeat or local observer. A crash after DB commit but before sending ACK is resolved through exact readback, not a second activation.
8. Under network partition: Worker locally fails closed immediately, but **global immediate Control fencing cannot be claimed**. Control must independently stop scheduling E after its own finite server-clock `fresh_until`/Worker TTL and reject stale `ADMITTED` at atomic claim. A stale remote Control view can persist for a bounded window until that deadline; the numeric bound and its invariant to existing `worker_ttl_seconds` must be validated in R4-C2 before any activation.

## 5. Transactional admission and source of truth

Proposed persistent indexing: unique `(worker_id, E)`, unique request ID within operation scope, a monotonic per-Worker latest intent, and at most one `ADMITTED` epoch per audio Worker. Registration/heartbeat must never overwrite an E tombstone, clear a fence, or change capability/serving identity for existing E. A new `WorkerServingAdvertisement` generated at Worker start is only a proposal until Control validates it.

Both in-memory and PostgreSQL repository implementations must have **equivalent observable semantics**. Preserve existing transaction lock order (`job` then `worker` whenever a job participates); new epoch-ledger locks must extend an explicitly documented global order. Claim and fence must contend on the same epoch/worker synchronization boundary. Atomic claim must test `ADMITTED(E)`, matching full serving revisions, valid qualification/live gate, Worker ONLINE/freshness, available capacity and current E **inside the transaction**, then bind `attempt`/`lease_token`/E. Merely testing a prior query, Gateway advertised profile or cached supervisor state is TOCTOU-unsafe.

The following authoritative paths must also re-check E and associated lease/state **at their mutation boundary**: heartbeat lease renewal, job events, staged media input/output authority, artifact finalize/commit, completion/terminal success, cancellation/retry and Worker recovery/register. Negative cleanup reconciliation can use a separate bounded privileged path after fencing; it must not be confused with ordinary success writes. A queued job may wait, fail or be explicitly re-admitted against new E via existing ServingJobBinding rules; it must never silently change model/voice/Worker or inherit a stale lease.

**No schema or migration is specified as already deployed.** R4-C2 must evaluate ledger constraint/index enforcement, migration/rollback, transaction deadlocks and same-identity Worker re-registration behavior across both repository implementations before any source or DB change.

## 6. Open design gates and required decisions

| Gate | Proposed default | Required evidence before enabling |
| --- | --- | --- |
| Trust and operator UX | Explicit opt-in to a **user-managed, non-cryptographically authenticated** loopback engine, no manual PID/ELF/model hash for ordinary attach | R4-D evidence policy, documented identity assurance level, rights/credit acceptance, native `/audio_query?text=` and access-log privacy inspection |
| Worker placement | Separate CPU-only audio Worker in same namespace | CPU/RAM isolation, no GPU UUID conflict, resource preflight and supported host/Nix/systemd setup (#180); no implicit extra LXC |
| Health and recovery | Existing fail-latch; new invocation/epoch after observed loss | Exact fresh supervisor READY with no bypass, controlled drain, old E fenced and cleaned, no old claim admitted |
| Protocol ownership | Control owns durable E CAS and tombstones | Authentication/scoping, transactional lock order, replay/idempotency limits, PostgreSQL restart tests |
| TTL and stale visibility | Finite Control-clock freshness, local immediate fail-closed | Concrete max stale-window measurement, server clock and expiry behavior, bounded lease acceptance |
| Backward compatibility | Feature off; existing owned runtime unchanged | Existing Chat/Embedding/Decision, audio-owned, gateway, spool and Worker serving/claim regressions |
| Release | No R4-C automatic activation | Separate human Ready/Merge for source, R4-D live engine/rights/privacy and explicit operational grant |

**Not resolved by this document:** actual backend authorization/proof transport, numerical TTL, topology/process packaging, schema/migration, measured failover, and whether CPU capacity is truly sufficient. Those belong to later separately gated slices; absence of these values MUST NOT be silently treated as production defaults.
