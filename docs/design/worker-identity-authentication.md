# Worker-scoped bearer authentication

Status: opt-in source implementation for #247. This establishes a **logical
Worker identity at the Control HTTP boundary**, not engine qualification,
native process attestation, activation, cleanup completion or full #247
acceptance. The default shared-role mode remains unchanged.

## Configuration and credentials

On Control, explicitly select:

```toml
[worker_auth]
mode = "per-worker"
credentials_file = "/run/credentials/tsumgi-control/worker-identities.json"
```

`control.client_auth` must be `bearer`. The credential file is operator-owned
runtime secret material, **not** a Nix-store file, inline TOML secret, URL
parameter or public artifact. Its exact JSON structure is:

```json
{
  "schema_version": "worker-credentials-v1",
  "workers": {
    "audio-worker-a": "<private-random-token-a>",
    "audio-worker-b": "<different-private-random-token-b>"
  }
}
```

The angle-bracket values above are deliberately invalid placeholders, not
usable credentials. Generate each real token independently from at least
32 cryptographically random bytes. Never reuse a token between Workers or
between Worker, client, Audio client, shared Worker and epoch-operator roles.
The loader requires distinct, header-safe ASCII tokens of 32–512 characters.
It does not infer entropy from a string's length or issue credentials.

Worker IDs must be exact ASCII identifiers matching
`[A-Za-z0-9][A-Za-z0-9._:-]{0,255}`. No normalization, whitespace trimming,
numeric coercion or wildcard identities are accepted in this mode.

The loader accepts at most 1024 Workers and a 1 MiB file. The path must be
absolute, without `..`; the final path component must not be a symlink.
The opened file must be regular, have one hard link, be owned by root or
Control's effective user, and have mode `0400` or `0600`. Validation uses
the opened descriptor; nonblocking/no-follow open prevents FIFO hangs and
final-component symlink following. Unknown/duplicate JSON fields, duplicate
IDs/tokens, unsupported versions, unsafe files or missing configuration fail
startup with a generic error. The loader does not repair permissions.

The validated registry is an immutable startup snapshot containing only
token digests and Worker IDs. Its representation does not disclose credentials
or IDs. The local file and process boundary remain trusted; this is not
protection against an administrator or code executing inside Control.

On **each Worker**, continue using its existing
`TSUMGI_WORKER_TOKEN`, but set it to only that Worker's assigned token.
The standard `ControlClient` bearer header and request formats are unchanged.
Control does not require the old shared `TSUMGI_WORKER_TOKEN` in strict
mode. If it is still configured there, it is **not a fallback** and must not
equal any scoped token.

This source slice does not install secret files, change Nix/systemd units,
generate real credentials or activate hosts. Secret provisioning and
installed-host validation remain explicit deployment work.

## Authorization boundary

Authentication determines a Worker principal independently of the requested
Worker ID:

- Invalid/missing credentials, duplicate authorization headers and other
  role tokens receive `401`.
- A recognized Worker B token targeting Worker A receives generic `403`,
  even if A is not yet registered. No Worker record is auto-created by
  authentication.
- Registration/recovery requires the raw `spec.worker_id` and the decoded
  registration identity to equal the authenticated principal.
- Every Worker-ID URL is checked before request-body consumption, repository
  calls or spool access: claim, heartbeat/lease renewal, state, completion,
  failure/retry, events, job inspection and audio input/output transfer.
- The credential only establishes **who** is calling. Existing lease,
  assignment, attempt, serving epoch and durable-fence guards still decide
  whether the operation is currently allowed. B's own URL plus A's known
  lease cannot complete or renew A's Job.
- Worker credentials confer neither client cancellation nor epoch-operator
  fencing/readback authority.

The credential belongs to a logical Worker, not a specific engine, epoch,
deployment or voice. Its legitimate owner can still propose new epochs
through ordinary registration. The separate
[negative-only fence](audio-epoch-fencing.md) remains authoritative for
revoked epochs; scoped identity is not positive qualification/admission.

Use authenticated TLS or an equivalently protected transport on non-loopback
connections. Possession of a copied valid bearer token impersonates that
Worker; this is not mTLS, hardware identity or an engine ownership proof.
Privileged client/operator/admin-console access and trusted in-process
repository callers are separate authorities, not constrained by Worker
identity authentication.

## Compatibility, rotation and rollback

An omitted section, or `mode = "shared"`, uses the existing shared-role
Worker token. A credentials file in shared mode is rejected rather than
silently ignored. There is no mixed shared/per-worker enrollment mode:
provision every Worker that connects to a strict Control before switching.
Legacy-only Workers will fail authentication against that Control.

All Control instances serving a cluster must use the same strict policy.
Routing to an old/shared-mode Control bypasses this identity boundary.
Changing the mode back to shared is a security downgrade, not a safe rollback.
Pause admission and retain strict policy while preparing a forward fix.

Credential files are loaded once, not watched or reloaded on each request.
Removal/rotation requires a coordinated Control restart; already authenticated
in-flight requests can finish. Removing a credential is not a durable epoch
revocation or native stop. Use the independent epoch fence and existing
attempt accounting where revocation is required. No installed-host
rotation, multi-Control upgrade or rollback acceptance is claimed here.

## Test boundary

- `test_worker_credentials.py`: immutable registry, strict file/schema and
  token validation, collision rejection and secret-redacted failures.
- `test_worker_auth_config.py`: standard daemon configuration, no shared
  fallback, role separation, default compatibility and denial before reading
  bodies or accessing storage.
- `worker_identity_cases.py`, shared by memory and strict PostgreSQL 17
  fixtures: production daemon ASGI assembly, cross-Worker registration and
  claim denial, correct-lease cross-identity rejection, unchanged
  Worker/Job/event/fence rows and staged files, owner-positive controls,
  and E1 fencing followed by E2 operation.
- `test_worker_identity.py` additionally exercises the unchanged real
  `ControlClient` wire implementation over ASGI transport.

These tests use only synthetic credentials, profiles, payloads and WAV bytes.
They do not start an engine, qualify a managed Worker, or claim real TCP /
installed-host E2E acceptance. Real-DB runs must use the dedicated disposable
server and `TSUMGI_REQUIRE_AUDIO_EPOCH_POSTGRES=1`; skips are not evidence.

### Local verification

With both declared Postgres dependencies available and the owned PostgreSQL
17.11 UTF8 test cluster (private Unix socket, no TCP listener,
`fsync=on`, `synchronous_commit=on`):

- Credential/configuration/identity, existing transport and epoch target:
  **117 passed**, 1 warning, no skips or xfails, with PostgreSQL required and
  `--runxfail`.
- Full regression: **3970 passed, 8 skipped**, 1 warning and 8 passing
  subtests. The skips remain six separate Chat-Gateway private-cluster tests
  requiring PostgreSQL executables in PATH, one authorized-GPU-host smoke
  test and one disposable file-ownership test.
- The warning is Starlette's `httpx` TestClient deprecation, not an
  authentication failure.
- Security review of the actual credential/API/daemon files and separate
  review of the effect oracles found no issues within their scoped reviews.
- Six offline Nix evaluations passed; no package realization or host
  activation was attempted. `git diff --check`, source syntax/whitespace
  and documentation-link checks passed.
- The previous candidate's 57 files outside the five explicitly extended
  API/daemon/test-helper/document files were hash-checked unchanged,
  including the original 44-file native/packaging candidate.

The target command was:

```sh
TSUMGI_REQUIRE_AUDIO_EPOCH_POSTGRES=1 PYTHONPATH=src \
  python3 -m pytest --runxfail -q --tb=short -rs -p no:cacheprovider \
  tests/test_worker_credentials.py tests/test_worker_auth_config.py \
  tests/test_worker_identity.py tests/test_worker_identity_postgres.py \
  tests/test_transport_daemons.py tests/test_audio_epoch_acceptance.py \
  tests/test_audio_epoch_acceptance_postgres.py
```

`TSUMGI_TEST_DATABASE_URL` selected only the private disposable server.
The full suite used the same environment with `pytest -q --tb=short -rs
-p no:cacheprovider`; some existing fixtures reset its base database, so a
shared or production database is never an appropriate target.
The owned cluster was stopped after verification with no other clients;
its data and private receipts were retained.

Qualification evidence, prepare/activate/cleanup protocol, native ABI,
installed-host credential provisioning, apply/rollback and operational
approval remain open. These results do not close #247 or all Audio issues.
