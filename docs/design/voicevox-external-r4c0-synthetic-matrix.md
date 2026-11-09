# TSUMGI #247 R4-C0 — synthetic authority/fencing acceptance matrix (proposal r0)

**Status: design specification only; NO executable test implementation, network/DB access, activation or deployment.**
Base `main`: `334c9bceaba15ea8ffdc69f73955e40afb16c9c1`.
Contract: [R4-C0 external VOICEVOX authority and epoch fencing](voicevox-external-r4c0-authority.md).
Applies to future R4-C1/C2/C3/C4 and R4-D, each requiring a separate explicit approval.

## 1. Test boundary and fixture model

All values here are **disposable, synthetic, non-authoritative**. Use fixed synthetic Worker IDs `audio-cpu-a` and `llm-gpu-b`; E1/E2 distinct canonical UUID strings, separate configured exact ServingDeployment/ServingContract/voice-semantic revisions and reviewed-fake qualification revisions Q1/Q2. No actual user voice, native binary, license file, model download, GPU, real VOICEVOX or live audio. A fake R1 result may represent `127.0.0.1:<ephemeral-port>` in unit tests only; a disposable fake localhost HTTP server is allowed only in a later separately approved source/test stage. It must accept **GET /version and /speakers only**, reject /audio_query, /synthesis and arbitrary paths; no host/network scan.

Fixture actors:
- `observer`: replay-capable private R1 observation with launch `run_generation`, `sequence`, `config_revision`, independent expected digest and operator voices, including style/license/attribution changes.
- `qualifier`: proposed deterministic `QualificationEvidence` fixture distinguishing `unreviewed`, `reviewed-but-live-gate-off`, `revoked`, and future `reviewed-live-gate-on`. **All R4-C0/C1 positive scenarios are simulated and never mint production grants.** Future R4-D provides any real verification.
- `worker`: fake isolated CPU-only audio Worker and a separate existing LLM/GPU Worker; one `RuntimeInstance.epoch` per invocation, explicit `WorkerServingAdvertisement`, fake `RuntimeHealthSupervisor`, synthetic attempt and cancellation.
- `control`: deterministic in-memory ledger storing `(worker_id, E)` and per-Worker monotonic intent; identical fixture contract later run against disposable PostgreSQL transaction implementation. No real Control API/DB access in C0.
- `scheduler`: competing claim/heartbeat/event/finalize actors invoking a *fake* admission repository under injected scheduling.
- `clock`: controllable Control/server UTC/monotonic freshness and Worker local monotonic clock; no trust in Worker-supplied scheduling timestamps.
- `artifact`: synthetic staged WAV reference, commit marker and simulated GC receipt, no real files or private audio.

Default state: `observer=BLOCKED`, `qualification=UNVERIFIED`, `live_gate=false`, `control epoch absent`, `audio capability unadvertised`. Any test that accidentally admits a real Worker, registers a provider or opens an external native engine **fails**.

## 2. Test oracle and invariants

Define `CanClaim(E,t)` as **Control-atomic**:
`E.state == ADMITTED && E == current_worker_serving_epoch && E.qualification/live_gate_valid && Worker.ONLINE && Worker.fresh_at(t) && E.fresh_at(t) && exact_JobServingBinding && worker.capacity_available`.
No cached R4-B1/B candidate, unsigned ACK, heartbeat or unpinned digest may replace a conjunct. This is a **proposed predicate**, not current repository code.

Define `CanPositiveWrite(job,attempt,lease,E,t)`: `CanClaimEpochState(E,t) && current job.status==RUNNING && exact job/attempt/lease/E == Control claim && lease/deadline valid && matching serving/voice revisions`. Stage/finalize, event, lease renewal, success terminal and output commit must recheck this predicate atomically at their respective mutation boundary. Cleanup/negative terminal under FENCED uses a distinct **nonpositive** reconciliation authority. Test oracle checks **post-commit state**, never only request receipt or emitted notification.

Safety assertions common to all rows:
- `G-AUDIO-OPS=STOP` in source-only stages, default disabled; no provider registry/admission/audio-ready promotion, no User engine installation/launch/stop/update/kill or GPU claim. `ready=False`, `can_apply=False`, `rights_verified=False`, `external_engine_verified=False` for all observation-only outputs.
- `FENCE_COMMITTED` and `CLEANUP_COMPLETE` are distinct durable receipts. `retirement_pending` is never proof of either. No prior E can be re-admitted; E2 cannot admit while E1 retirement remains incomplete.
- After any `FENCED(E)`, new claims, lease renewals and **positive** success/artifact/event writes for E reject. Exact aborted/expired attempt accounting is allowed only via privileged nonpositive cleanup flow.
- Redacted error/event/snapshot contains no endpoint, UUID, style, license, credit, transcript, `/audio_query?text=`, native exception, token or raw output.
- In-memory and PostgreSQL fixtures produce identical observable result/error categories and epoch/lease state across the same schedule. Explicit PostgreSQL tests are planned for C2, **not executed now**.

## 3. Deterministic matrix

Legend: **P** = proposed positive simulated test (NOT permission to activate); **N** = negative/fail-closed; **R** = recoverability/idempotency. Expected result is an *oracle*, not a current implementation claim.

| ID | Class / stage | Injected trace | Expected assertion |
| --- | --- | --- | --- |
| C0-01 | N / C1 | Disabled config with trap objects for endpoint/voices, request observer start | Zero foreign data inspection and zero I/O; no qualification, no epoch. |
| C0-02 | N / C1 | No listener / ABSENT, failed poll, malformed/oversized response, timeout | All remain BLOCKED; no provider/Control call. |
| C0-03 | N / C1 | Fake local HTTP 200 with unrelated endpoint or forged canonical digest | R1 observation is not signed identity; no qualification/admission. |
| C0-04 | N / C1 | Matching independently pinned digest, but selected voice UUID/style absent | Advisory only blocked; no capabilities or epoch. |
| C0-05 | N / C1 | Version/style/UUID changes or changed voice license/attribution/policy revision during in-flight probe | Synchronous local revocation first; stale return rejected; no digest auto-pin. |
| C0-06 | R / C1 | Same canonical observation repeated N times | No duplicate planner generation or repeated withdrawal; no Control epoch. |
| C0-07 | N / C1 | Policy `REVOKED` arrives before sample completion | Old completion ignored without reading private result; no admission. |
| C0-08 | N / C1 | New observed voice has no reviewed rights/credit; marker says `review_only_blocked` | Exact `qualification_revision` absent; cannot become READY. |
| C0-09 | N / C1 | Config/user asks for remote LXC, LAN address, wildcard, redirect, proxy or arbitrary endpoint | Fail closed (current R1 only literal loopback); no network scan/remote connection. |
| C0-10 | N / C1 | Unreviewed or expired trust policy; missing/contradictory log privacy evidence | Reject qualification; never trust digests as rights/engine attestation. |
| C0-11 | N / C1 | User-managed engine without manually entered PID or ELF checksum | Observation remains supported; manual hash absence alone is not an R1 error; **no production admission** until explicit R4-D trust mode is reviewed. |
| C0-12 | N / C1 | Fake Q1 appears `reviewed` while `live_gate=false` | No Control PREPARE/ADMIT or Worker Serving Advertisement. |
| C0-13 | N / C1 | Fake Q1 uses wrong `worker_id`, deployment, voice semantic, network namespace or manifest revision | Exact binding mismatch; reject and redact, no fallback. |
| C0-14 | P / C2 | Simulated future live gate ON, exact Q1/E1, trusted Control prepare CAS | Exactly one PREPARED record; admission still false; source-only fixtures non-operational. |
| C0-15 | N / C2 | Same worker E1 PREPARED, `activate` missing exact Worker ServingAdvertisement/READY/freshness/qualification | Reject, remain PREPARED or FENCED, never forge epoch. |
| C0-16 | P / C2 | Q1/E1, Worker ONLINE/ready, Control fresh, exact advertisement + CAS and future gate ON | One durable ADMITTED(E1) in disposable fake backend, never a production capability in C0/C1. |
| C0-17 | R / C2 | Duplicate `prepare/activate` with same idempotency key and identical immutable fields | Same persisted record/revision, no second E or duplicate side effect. |
| C0-18 | N / C2 | Duplicate request ID reused with different E, Q, contract or intent; mismatched CAS | CONFLICT and exact readback requirement, no silent last-writer-wins. |
| C0-19 | N / C2 | Revoke intent at generation 12 races older prepare/activate at generation 11 | Revoke wins; no admission after fence, including delayed old ACK. |
| C0-20 | R / C2 | `fence(E1)` commit succeeds but reply times out; same request readback after Control restart | Readback returns exact FENCED(E1) with committed revision; never blindly retry CAS. |
| C0-21 | N / C2 | `fence(E1)` concurrent with atomic new claim | Serializable outcome: claim either committed **before** fence and later invalidated, or denied; no post-fence claim. |
| C0-22 | N / C2 | `fence(E1)` concurrent with lease renewal/Job event/positive terminal/WAV finalize | All E1 writes **after** fence commit fail; no old artifact publish/success. |
| C0-23 | R / C2 | Existing running E1 attempt at time of fence | FENCE_COMMITTED first; explicit terminal/expiry accounting; CLEANUP_COMPLETE only when all exact attempts resolved. |
| C0-24 | N / C2 | Two old attempts, one still leased; client sends forged cleanup ACK | CLEANUP_COMPLETE rejected; `retirement_pending`/notification not accepted as Control cleanup. |
| C0-25 | N / C2 | E1 FENCED but not clean; E2 exact fresh prep/activate requested | No E2 ADMITTED until E1 cleanup completes. |
| C0-26 | N / C2 | E1 CLEANUP_COMPLETE then stale E1 heartbeat/register/activate arrives | E1 tombstone persists, stays non-admitted; no epoch UUID reuse. |
| C0-27 | R / C2 | E1 cleaned, new Worker invocation + fresh E2 Q2, old E1 fully retired | E2 may be admitted ONLY when all future live gates and Control CAS hold; no implicit recovery. |
| C0-28 | N / C2 | Worker dies after E1 Control prepare/activation DB commit but before client ACK | Durable readback decides outcome; no duplicate E1/activation and no unbound running Worker. |
| C0-29 | N / C2 | Control DB restarts after fence; worker resends previously successful prepare/activate | Persisted FENCED(E1) wins; replay rejected; no local-counter reconstruction. |
| C0-30 | N / C2 | Worker network partition while Control record still ADMITTED | Worker local execution stops immediately; Control's independent freshness deadline bounds claim window; no assertion of zero remote staleness. |
| C0-31 | N / C2 | Control freshness expired but Worker self-reports healthy via late heartbeat | Atomic claim rejects; stale heartbeat does not extend a fenced/superseded E. |
| C0-32 | N / C2 | Two concurrent Control claims E1 or audio Worker registered with conflicting GPU UUID | Existing single-owner capacity and GPU exclusivity preserved under job→worker→epoch lock order; no double claim. |
| C0-33 | N / C2 | New Worker serving epoch E2 registered under same ID while E1 has running lease | Registration/activation must not bypass old E1 cleanup or rewrite E1 identity. |
| C0-34 | N / C2 | Client attempts old E1 artifact output after E2 becomes current | Exact job/attempt/lease/serving epoch fail; staged bytes do not become published output. |
| C0-35 | N / C3 | Bare audio `executor.factory` with false `ja/wav` features and no managed manifest | Existing daemon gate rejects before Control/network/side effects; no bypass in attach mode. |
| C0-36 | N / C3 | External user-managed process accidentally supplied to owned process controller | Must be rejected; never kill, adopt PID, or take lifetime ownership. |
| C0-37 | N / C3 | External engine lost during claim reply or before executor invocation | Local execution denied; remote fence requested; no early assumption that ACK means cleanup. |
| C0-38 | R / C3 | Existing Chat/Embedding/Decision Worker alongside CPU-only audio Worker | Immutable non-audio capabilities and GPU reservations unaffected; no automatic audio on other Worker. |
| C0-39 | N / C4 | Gateway profile references stale E1 semantic/deployment/voice revision or wrong worker | No audio route/claim, no model/style fallback, no cross-profile spoofing. |
| C0-40 | N / C4 | Private transcript, URL text, UUID, credit or native error injected in all failure paths | No exposure in logs, public snapshot, Control response, exception or synthetic artifact receipt. |
| C0-41 | N / C2 | Simulated CAS deadlock, disconnect or serialization failure at commit boundary | No uncertain success claim; exact durable readback + limited explicit retry policy only. |
| C0-42 | N / C2 | Schema migration/rollback attempts to remove E1 tombstone or reactivate FENCED record | Forbidden; reject unsafe rollback, preserve durable no-reuse and audit evidence. |
| C0-43 | N / C3 | Old observer remains positive while Worker/Control qualification becomes revoked | Control FENCE overrides advisory candidate; local `ready` cannot be restored via local planner. |
| C0-44 | R / D | Real operator-owned instance shutdown/restart with **unchanged** approved trust/voice/logging policy | Future separate R4-D acceptance: automatic availability only after fresh observation, qualification, Control E2 and exact Worker ACK; never solely from port-open. |

Rows C0-14, C0-16, C0-27 and C0-44 describe **future simulated positive traces or R4-D qualification goals**, not current implementation/test success. Until a later human operational approval, the expected externally observable result for all R4-C0/C1 runs remains **no usable TTS capability**.

## 4. Schedule/execution matrix for future C2 tests

For each competing operation pair, execute deterministic permutations of **A-before-B**, **B-before-A**, **A commit then lost reply**, **A validation then B commit**, and **restart between commit and response**. Use explicit barriers, no sleeps for correctness. At minimum:

| Schedule | Linearization candidate | Required observable evidence |
| --- | --- | --- |
| `activate(E1)` vs `fence(E1)` | Same Worker+epoch/intent CAS | At most one ADMITTED result, any committed fence dominates future activation |
| `claim(job,E1)` vs `fence(E1)` | Atomic job/Worker/epoch lock | No claim committed after FENCE |
| `renew(lease,E1)` vs `fence(E1)` | Same epoch record checked inside lease update | No renewed lease after FENCE |
| `finalize(output,E1)` vs `fence(E1)` | Same epoch record checked at artifact authority commit | Old completed WAV cannot surface as successful output |
| `activate(E2)` vs `cleanup_complete(E1)` | Cleanup state and unique-ADMITTED invariant | E2 succeeds only after durable cleanup of E1 |
| `register(E2)` vs old `heartbeat(E1)` | Same Worker ID, distinct epochs | Older heartbeat neither overwrites Worker serving nor extends authority |
| `stop` vs `reconfigure` vs late R1 completion | Single owner local intent/generation | No stale old-endpoint probe or stale Planner candidate after revocation |
| Lost/duplicated ACK vs retry/readback | Unique request_id+CAS revision | At most one effect; exact readback resolves uncertainty |

## 5. Test-specific observability, exclusions and staged handoff

Every fixture records: previous/new epoch state and revision; latest per-Worker intent; exact claim attempt/lease/E bindings; Control clock deadline; fake Worker runtime state; redacted error/status; and which mocked external calls occurred. A passing test must assert **both negative effect absence and persisted state**; test solely counting emitted requests is insufficient.

**C1 deliverable:** immutable, deterministic source-only protocol/domain values + fake store, blocked-by-default and 0 actual network/DB/Worker/Control changes, focused tests for C0-01–C0-13 and applicable local guards. Requires separate human implementation approval.

**C2 deliverable:** matching in-memory and disposable PostgreSQL transaction/test suites for C0-14–C0-34, C0-41–C0-42 and all schedule permutations. Data migration, indexes, constraints, credential scope, deadlock order and rollback must receive separate approval. A fixture accepting positive simulated Q1 does not grant operator rights or real engine identity.

**C3 deliverable:** source-only Worker/ManagedRuntime/provider boundary tests for C0-35–C0-38, C0-43; keep #214 managed-only and all existing Worker liveness/epoch/Serving checks. No real Worker or VOICEVOX operations.

**C4 deliverable:** source-only Gateway/profile/audio artifact binding tests C0-39–C0-40 and regression across non-audio paths. No deploy/activation/real synthesis.

**R4-D later:** qualify user-managed engine trust, legal voice use/credit, native HTTP request URL logging/privacy, explicit local namespace placement, CPU/GPU resource acceptance and full operational end-to-end audio. Do **not** require user manual PID/ELF/model hashes merely to observe an external instance. Real acceptance and controlled deployment are separately human-gated.

**Stop conditions:** unknown/uncertain Control commit, missing durable attempt ledger, improper idempotence, unsafe same-worker re-registration, false identity/rights attestation, omitted old-epoch artifact guard, no measured Control TTL bound, or any accidental admission default. Do not waive these to claim R4-C completion.

No executable tests, migrations, DB, Worker, Control, API or host operations were performed in R4-C0. An independent source/docs review of these documents is the only acceptance task for this slice.
