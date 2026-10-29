# Wave2: opt-in reviewed voice catalog route

Run: `TSUMGI-175-W2-VOICE-20261029-R2`, continued under the R3 source-only
transport constraint. Workstream `WS-175-VOICE-P0`; #175 / Epic #173;
TSUMGI-AUDIO-FC-001/r0, DES-AUDIO-002, AC-AUDIO-01 and AC-AUDIO-05.
Coordinator-verified base: `ef337ace847a2b3cd90cccfa897cd2a370dc553d`.

## Mounting and authentication

`create_audio_voice_router(catalog, client_auth=ClientAuthMode.BEARER,
client_token=...)` returns a FastAPI `APIRouter`. The caller must explicitly
inject an **already-reviewed** `AudioProfileCatalog` and independently mount it.
The factory revalidates a detached immutable snapshot; it does not load operator
files or establish whether the operator actually reviewed the declarations.
There is no default catalog, backend URL, runtime discovery or fallback.

The factory rejects `NONE`, missing/invalid credentials and invalid catalogs.
Tokens must use ASCII HTTP bearer token syntax. Requests require exactly one
Authorization header with a bearer token; malformed, non-ASCII and duplicate
headers fail closed. The existing gateway `_require_client` helper supplies
constant-time comparison, always forced to `ClientAuthMode.BEARER`. Failures
return sanitized `AudioGatewayError` JSON, 401 and `WWW-Authenticate: Bearer`.
Authentication precedes query parsing and profile lookup. Existing permissive
non-audio authentication does not enable anonymous access to this route.

**This wave does not mount the router in Control or any daemon.** It remains
independently test-mounted only; production mounting would require separately
reviewed edits outside this three-file scope. `/v1/models` is neither registered
nor changed by this router, and continues to describe the existing chat catalog.

## GET /v1/audio/voices?model=exact-logical-profile

Exactly one `model` query field is required. Its value is the exact logical
profile identifier, case sensitive, without whitespace normalization. The shared
catalog validates identifier syntax, resolves only the pinned speech operation
(`audio.synthesize` / `openai-audio-speech-v1`) and refuses STT or unknown models.
Only the shared catalog's reviewed `voicevox` and `model-tts` declarations can
serve this projection; missing TTS voices or unknown backend kinds prevent
router construction. These are declaration kinds, not available adapters.

Raw ASGI query bytes are bounded to 1024 before decoding. Strict ASCII transport,
percent-escape and UTF-8 decoding reject malformed inputs; percent-encoded
identifiers are accepted only if their decoded identifier is valid. Parsing
retains duplicates and empty values. Unknown fields (including `voice`,
`backend_url`, `stream`, revisions or auth tokens), duplicates, bare fields and
malformed separators are rejected, never silently selected or ignored. A GET
lists the selected profile's mappings; it does not choose a voice for synthesis.

The successful JSON object has `object: "list"`, the exact `model`, `data` with
only each alias's `alias`, `speaker_id`, `style_id`, `locale`, `license_id` and
reviewed public `attribution`, and `x_astrumweaver` with:

- backend kind, capability and operation schema;
- exact profile, deployment and serving-contract revisions;
- exact voice/locale/license/attribution semantic revision.

Mappings are canonically ordered and detached from immutable configuration.
The route explicitly allowlists response fields rather than serializing a
deployment or full manifest. It exposes no deployment URLs, provider options,
artifact payloads, credentials, private configuration or submitted query values.
Reviewed attribution is deliberately public text; operators must not put secrets
in public declarations. Revisions pin declarations, not readiness or availability.
License IDs and attribution are declarations, **not permission to use a real
voice**, verified licensing, provider authorization or real audio acceptance.

Responses use `Cache-Control: no-store`. Errors have exactly the existing
`AudioGatewayError.to_dict()` shape: `error.code`, `error.param`, `error.message`.
Messages contain only fixed codes and fixed field names, not client values.
Missing/duplicate fields, malformed values and unsupported STT models use 400;
unknown exact profiles use 404; oversized queries use 413. Unknown keys use the
fixed `query` field name rather than echoing an untrusted key. Invalid operator
configuration fails at factory construction, before a public route is exposed.
Unsupported methods use the framework's 405 behavior, not a public write path.

## Synthetic verification and freeze boundary

`tests/test_audio_voice_api.py` uses actual `httpx.ASGITransport` requests with
synthetic pinned deployments, tokens, aliases and licenses. It exercises both
TTS kinds, exact mappings/revisions/attribution, STT refusal, missing/unknown/
malformed model queries, duplicate/unknown fields, malformed encodings, bearer
failures/duplicate headers, no fallback/leakage, revision changes, immutable
catalog round trips, existing validators and the real chat `/v1/models` route
alongside strict audio auth. No repository operation is needed by either catalog
route; the compatibility test uses a repository-access tripwire.

**Tests NOT RUN in this session.** R3 forbids shell/command execution. The
coordinator should run, with existing installed dependencies and no install:

```sh
PYTHONPATH=src python -m pytest -q tests/test_audio_voice_api.py tests/test_audio_gateway_contracts.py
PYTHONPATH=src python -m pytest -q tests/test_serving_contracts.py tests/test_chat_gateway_contracts.py tests/test_chat_gateway_api.py tests/test_embedding_gateway_contracts.py tests/test_decision_gateway_contracts.py
```

Exit codes, installed dependency versions, SHA256 hashes and a frozen patch must
be collected by the coordinator; none were executed/computed here. Preserve
uncommitted additions at exactly these paths:

```text
src/astrumweaver/gateway/audio_voice_api.py
tests/test_audio_voice_api.py
docs/design/audio-voice-api-wave2.md
```

For reproducible collection, hash those files with `sha256sum` and capture each
addition with `git diff --no-index -- /dev/null <path>` (exit 1 normally means a
diff was emitted). Ordinary `git diff` omits new untracked files. Capture patch
and manifest outside the worktree; do not stage files just to collect evidence.

No existing tracked file, sibling ingress/parser/transport code, #174 spool,
Control/Worker/DB implementation, setup, dependency declaration, permission
configuration or deployment is changed. There is no GPU/model/real audio use,
streaming, public write/admission/Job path, network access, release or claim of
full #175 completion or real audio E2E.
