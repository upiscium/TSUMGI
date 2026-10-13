# #137 — No-mixed-Control activation gate preflight

Status: **PROPOSED, test-only / operator design**. No production code, SQL migration,
service restart, role revocation, or Owner protocol activation is authorized.

Stacked on [Draft PR #142](https://github.com/upiscium/TSUMGI/pull/142)
(real Control scheduler and legacy-role/RLS compatibility preflight). This
extends [Stage 3C design](137-scheduler-compatibility-preflight.md) without
editing it. Existing #137 Stage 3A UNKNOWN_COMMIT work is handled by another
isolated worktree and is not in scope here.

## Why an absence-of-sessions check is not an activation fence

TSUMGI Control reads its PostgreSQL connection from
`ASTRUMWEAVER_DATABASE_URL`. The current unmodified Control
`PostgresControlRepository.claim_next_job` does not check a durable Gateway
Owner: Draft #142 reproduced that an old Control can claim an Owner-linked
`CANCEL_REQUESTED` Job, and an old Gateway can omit the proposed origin
marker and submit as ordinary `direct`.

A proposed durable-owner release cannot infer that old writers are gone from
`pg_stat_activity` alone. It needs **both** an enforceable no-reconnect
authority boundary and a versioned inventory of **all** processes or direct
SQL actors that can mutate the shared `jobs` table, including processes that
are stopped now but may be restarted by systemd, an orchestrator, a host or an
operator.

### Actual isolated PostgreSQL17.11 experiments

[Opt-in legacy-role preflight tests](../../tests/test_gateway_legacy_role_fence_preflight.py)
use the **same verified disposable database** and real TSUMGI migrations as
the previous scheduler tests. A unique temporary `LOGIN` role has only
`USAGE` on the test schema, sentinel `SELECT`, and
`SELECT, UPDATE` on jobs. A separate authenticated connection uses the
private Unix socket, proves its role/database/transport identity and matches
the independently provisioned 256-bit nonce. All scratch role changes are
reversed on teardown.

| Controlled change | Actual observed behavior | Safety conclusion |
| --- | --- | --- |
| `REVOKE CONNECT ON DATABASE ... FROM old_role` | A new role session still connects because the database's default `PUBLIC` CONNECT grant remains | A per-role REVOKE does not revoke access inherited through PUBLIC |
| `ALTER ROLE old_role NOLOGIN` | New login fails, but the **already authenticated session still UPDATEs a genuine TSUMGI Job** | NOLOGIN alone is **not** a cutover; old sessions can retain writer authority |
| After NOLOGIN, terminate only the backend matching **the test role, observed PID and backend-start timestamp**, rejecting a mismatched identity | Existing session stops accepting SQL and a new role login remains denied | Role-scoped revocation + deliberate session termination is a feasible *component* of fencing, not proof every actor was eliminated |

Both opt-in real PostgreSQL tests currently PASS. They demonstrate unsafe
negative controls deliberately; passing these cases does **not** make
TSUMGI's current Control Claim safe.

This experiment uses `trust` authentication *only inside a fresh Unix-socket
test cluster*. Actual production credentials, pg_hba.conf, TLS/auth, RLS and
role inheritance require a separate site audit. The tests do not grant a
role superuser privileges or read a site DSN.

## Proposed release gate: no older PostgreSQL writers

Every item is **mandatory** and an unknown / stale observation is
**BLOCKED**, not an implicit PASS.

| Gate | Operator-provided evidence required |
| --- | --- |
| Complete actor inventory | Every Control/Gateway instance, maintenance tool, manual SQL writer, service manager and host capable of reaching this database, with installed revision and configured DB role; include powered-off/restartable instances |
| Unambiguous role mapping | Which database roles are used by old and new Control, and whether any role is shared with a privileged migration account, superuser/table owner, BYPASSRLS, inherited group or another SQL writer |
| Stop & restart fence | Old instances drained/stopped, service managers and auto-restart prevented, old deployments not deployable with unchanged credentials; identity and process evidence from every relevant host |
| Durable credential fence | DB-level revocation or rotation that prevents every obsolete login path from re-establishing an unreviewed writer; verify PUBLIC/inherited grants, role membership, alternate identities and host authentication policy |
| Existing connection teardown | Explicitly terminate or drain old backend sessions **after** reconnect denial and verify zero sessions for all identified old roles, without terminating unrelated or upgraded Control sessions |
| Read-only reconnect probe | Old role reconnect must fail using the intended transport; new reviewed Control identities must still authenticate and execute only the approved operations |
| Control binary revision | All remaining Claim, submit, cancel, heartbeat, complete/fail, lease recovery and event-writer paths implement the reviewed Owner-aware contract (not provided by the current #142 test branch) |
| Origin/writer authority | An old Gateway cannot silently create a claimable `direct` Job when its HTTP consumer is gone; Job-origin constraints and privileged bypass controls are qualified |
| Fail-closed activation | Feature OFF unless **all** gates are independently reviewed and a durable signoff references the exact revisions, migration checksum, DB identity, roles, observed time and rollback drill |
| Rollback and canary | On failure, disable new Gateway admission, preserve Owner tombstones/claim fencing, cancel and drain owned QUEUED/RUNNING Jobs, verify native Worker stop/capacity reuse before considering an older Control rollback |

A query to `pg_stat_activity` is **evidence of current sessions**, not a
certificate that old writers cannot reappear. Likewise a version handshake
from one Control does not prove other instances sharing the database use the
same code. Changing a role to NOLOGIN can stop *new* sessions without stopping
an already-authenticated writer. Revoking only a role-level CONNECT grant can
be ineffective if PUBLIC still grants access.

Do not implement an automatic sitewide `pg_terminate_backend()`,
`ALTER ROLE`, `REVOKE`, or credential rotation based solely on this
preflight. Those would be production operational actions requiring a
separate approval and protection of unrelated database users.

## No-mixed-Control versus role/RLS transition

For the first opt-in durable-owner activation, **recommend a single updated
Control authority cohort** with obsolete writers fenced before any Owner-linked
Job is admitted. The RLS option evaluated in #142 only offers conditional
isolation for a non-owner/non-BYPASSRLS legacy identity; privileged sessions
can bypass it, and a legacy Gateway may insert as default `direct`. RLS is
therefore a separate migration/security design, not a simpler substitute for
the no-mixed-Control gate.

Even when an old role is NOLOGIN, the old process may have a separate shared
database credential or privileged connection. Enforceable authority depends
on the **complete credential and process inventory**, not solely on
`rolcanlogin` or server-side session counts.

## Explicitly unproved; no go-live claim

- This stage **does not** implement `gateway_admissions`, versioned
  connection admission, Owner-first Worker Claim, an Owner deadline/commit
  boundary, durable UNKNOWN_COMMIT reconciliation, or an actual deployment
  gate.
- It does not demonstrate scheduler fairness after post-lock Owner rejection,
  immediate recovery after Gateway crash, or bounded native Worker cancellation.
- The checked PostgreSQL role is short-lived and belongs **only to the
  ephemeral test database/cluster**, never to the user's site.
- A Human architecture choice is still required: bounded best-effort with
  deadline recovery versus stronger durable-owner/claim fencing, and whether
  that stronger guarantee is part of TSUMGI's core responsibility.

**Disposition:** Keep #137 OPEN, Stage 1/2/3 Draft PRs unmerged, feature OFF,
no main branch change, no production PostgreSQL, Control/Worker, GPU, release
or credential modification from this preflight.

## Focused preflight evidence (disposable database only)

On a fresh PostgreSQL **17.11** instance with `listen_addresses=''`, a
private Unix socket, verified UTF-8 database and an independently seeded
test-only nonce:

- Two new legacy-login tests: **2/2 PASS**; repeated ten separate
  pytest invocations for **20/20 PASS**.
- These two tests plus the previous real-Control scheduler and stand-in
  Owner/Claim probes: **23/23 PASS** in a single opt-in run.
- Normal Python 3.12 suite with the disposable PostgreSQL env unset:
  **1,046 PASS / 8 SKIP / 0 FAILED**. The opt-in database tests are
  skipped in this ordinary run, not represented as executed there.
- `nix flake check --no-build` and local Git whitespace check: **PASS**.

The first test is intentionally a **negative proof** that CONNECT revocation
can fail open through a `PUBLIC` grant. The second is a **negative proof**
that `NOLOGIN` does not revoke an already authenticated SQL writer. Even a
successful role-specific session shutdown does **not** establish that no
privileged or alternate Control credential remains.

No test server or role configuration from this preflight is a production
activation recommendation. Exact new code revision and any later
independent reviews must be recorded before using these results for an ADR
decision.
