# TSUMGI #209 — Native patched Control / OpenCode 1.18.35 acceptance

**Date:** 2026-11-01 JST

**Source HEAD tested:** `232c60542e9fad56c04f118779cda875ca442f0a`

**Issue:** #209; **Draft PR:** #211

**Disposition:** **PASS for the native isolated candidate**. Production remains on its previous immutable package; no production upgrade or merge is asserted.

## Execution boundary

This run is distinct from the earlier [temporary ingress-bridge experiment](chat-209-candidate-ingress.md).
The candidate was the **actual packaged `astrumweaver-control`** binary from
the pinned PR commit, running the native `build_app`/FastAPI
`/v1/models` and `/v1/chat/completions` routes. There was **no
payload-rewriting shim** between OpenCode and candidate Control.

The candidate ran as a separate, explicitly time-bounded process on a
private network address and non-production port, with an independent
high-entropy bearer token and access logging disabled. Its cloned Control
config changed only the listening address/port, client authentication
mode, access logging, and maintenance interval. The candidate maintenance
interval was **3,600 seconds**; the first process expired cleanly after a
**330-second** upper bound, and the brief negative-test process was
stopped gracefully within its separate **120-second** bound.
Neither process modified the installed Nix profile, production systemd
unit, Worker service, model deployment, schema, or global OpenCode
provider configuration. No NPM/proxy mapping was created.

**Important storage boundary:** The native candidate used the existing
authorized PostgreSQL Job backend and existing live Workers. Accepted
synthetic chat/Tool requests therefore created operational Jobs/events
under the ordinary TSUMGI retention contract. These durable records
were **not** deleted or rolled back as part of candidate shutdown.

## Immutable package and serving identity

- Pinned source: `upiscium/TSUMGI@232c60542e9fad56c04f118779cda875ca442f0a`.
- Candidate built on the Control node from the exact pinned GitHub flake
  revision `#control`, without profile installation or switch.
- Candidate Nix Control package NAR hash:
  `sha256-N9yM/WvehM7boEnJTaJOr+7sGbhW9FZ0SSJmqmaFg04=`.
- Candidate packaged `gateway/chat.py` SHA-256:
  `2276e35bae9e83cf523096d4a5b381e138cb80b76a101f7f61f4f5f118051fdf`;
  matches the previously reviewed PR source/packaging and admits
  `reasoning_effort` in the restricted subset.
- Existing **production** Control `v0.1.2` packaged
  `gateway/chat.py` SHA-256:
  `1c6a498bf116e8ebbea80beaefe9a7e6179d6a980b836b80da2885a105b8a6a2`;
  this older code still lacks `reasoning_effort` acceptance.
- Existing production Control profile Nix store identity:
  `6iyl99ba1p5zbrmill50fy0lildkw3gx-profile` (unchanged).
- Existing Worker installation profile identity (both tested serving
  nodes): `7yvqpnn4zqc9xmd49ndf9ynr4cbd8mq8-profile` (unchanged).
  Qwen and Bonsai retained their separate preexisting runtime profiles;
  no Worker or GPU process was upgraded or restarted.
- Both candidate `GET /v1/models` logical serving profiles and their
  complete advertised `x_astrumweaver` metadata matched production
  exactly before chat admission. Each advertised `tools`,
  `total_tokens=8192`, `output_tokens=2048` and
  `request_bytes=65536`.

| Logical model | Serving profile revision | Deployment revision | Serving contract revision |
| --- | --- | --- | --- |
| `qwen3.8-27b` | `sha256:2784a5f0e900c236e1ae83c23d8d61c809686e49ed0348597a746f8a174c01e5` | `sha256:08d412ed5aaeebc1f6100cc7513f78e053fac88af08d5c6db859aed06b67a5d8` | `sha256:3b143b3d566d1df01e01c8a11dd4acbd6dcf156b257f5ef981427b87c5da1f9e` |
| `bonsai2-27b` | `sha256:38504021e9ec2216fa36d24b0e37b26229758521f1b055c9a8d2b0211884a964` | `sha256:a1a5e5def283b99ce1d7a0d272470fb1f0e0c66b54cfa4157f8606bfcb28cc25` | `sha256:46d01e808efc8e8aa0b255815a82ad764580454a2d07e542204916a748f07ef7` |

## Pinned client and real-model E2E

Client: real **OpenCode 1.18.35** with
`@ai-sdk/openai-compatible` selected in disposable
`OPENCODE_CONFIG_CONTENT`. The pinned upstream release manifest
declares adapter **2.0.41**; the actual loaded module version of the
compiled CLI was **not independently introspected**. All invocations
used `run --pure --agent plan --format json`, an isolated temporary
workspace, isolated HOME/XDG roots and an environment-referenced
ephemeral bearer token. No ordinary user/project OpenCode session,
plugins or credentials were loaded.

| Model | Case | Client exit | Completed read-tool events | Final synthetic answer | Verdict |
| --- | --- | ---: | ---: | --- | --- |
| `qwen3.8-27b` | Streamed plain chat | 0 | 0 | Exact `TSUMGI_OK` | PASS |
| `qwen3.8-27b` | Read tool → tool result → final chat | 0 | 2 | Exact `TSUMGI_TOOL_OK` | PASS |
| `bonsai2-27b` | Streamed plain chat | 0 | 0 | Exact `TSUMGI_OK` | PASS |
| `bonsai2-27b` | Read tool → tool result → final chat | 0 | 1 | Exact `TSUMGI_TOOL_OK` | PASS |

**Four of four successful**. Every run had zero OpenCode error
events and the marker file remained byte-identical. A preliminary
tool-case assertion concatenated pre-tool narration with the terminal
answer and incorrectly labeled one run a failure; the corrected
acceptance evaluates the assistant's **final step**, not interim
narration. The complete suite was then rerun from the start and passed
4/4. This was a test-harness-only correction; no TSUMGI source fix
was needed.

## Fail-closed native HTTP proof

Against the same native packaged candidate, without the rewrite
bridge, all **8/8** negative/security cases matched their expected
status/error code:

| Case | Native HTTP result |
| --- | --- |
| Missing client bearer | 401 |
| Unsupported `reasoning_effort=xhigh` | 400 / `unsupported_feature` |
| Non-string `reasoning_effort` | 400 / `invalid_request` |
| Unknown top-level field | 400 / `unsupported_field` |
| Unsupported image content part | 400 / `invalid_request` |
| `store: true` | 400 / `unsupported_feature` |
| Tool result with unbound call ID | 400 / `invalid_request` |
| Raw HTTP body exceeding original profile limit | 413 / `request_too_large` |

These are schema/authentication rejections before the corresponding
Job admission code path. A separate database row-count audit was
**not** performed and is not claimed here.

## Recovery, CI and outstanding boundaries

- The native candidate was terminated cleanly; the private candidate
  listening port was confirmed closed.
- The original production Control remained **active with the same PID**
  throughout the experiment. The installed Control profile and its
  previous Gateway code hash were unchanged after candidate shutdown.
- Both preexisting serving Workers remained active; the Qwen and
  Bonsai model-local API endpoints remained responsive.
- This constitutes a successful **candidate teardown / no production
  switch** check. It does **not** prove a rollback after upgrading the
  production service, because no such upgrade occurred.
- The earlier source/test review was PASS. Full local regression:
  **2,195 pass / 128 skip / 0 fail**. GitHub unit+nix CI on the
  preceding PR HEAD `232c60542e9fad56c04f118779cda875ca442f0a`
  was **SUCCESS** (run `41302286147`).
- Logs/session event bodies, disposable candidate credentials and
  private test fixtures are not part of this published evidence.

**AC-OC-04:** Native candidate real-model plain+structured-tool
E2E **met for both advertised models**.

**AC-OC-06:** Exact source/package/serving profile and candidate
rollback boundary documented. The production cutover/rollback
verification remains a separate operator-controlled deployment
activity; no GA or production-fix claim is made.

**PR lifecycle:** Keep #211 Draft and #209 OPEN until the
evidence-only follow-up commit's CI and the explicitly authorized
Ready/Merge decision. Even after a merge, production deployment is a
separate action requiring an approved rollout/rollback procedure.
