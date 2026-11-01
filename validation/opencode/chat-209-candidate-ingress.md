# TSUMGI #209 — OpenCode 1.18.35 candidate ingress smoke

**Historical scope update (2026-11-01 JST):** This test-only bridge evidence has been superseded for native candidate E2E by the [patched Control acceptance report](chat-209-native-candidate.md). The bridge-only outstanding gates below describe the state *at the time of this earlier test*. Native candidate E2E is now complete, but production rollout/rollback and final Ready/Merge remain separately controlled.

**Date (JST):** 2026-11-01

**Assessment:** **PASS for the isolated ingress-bridge smoke; NOT YET PASS for native candidate Control deployment.**
**Issue:** #209; **PR:** #211; **candidate source commit:** `3dd407a7fe986012692a3c39b4750cd4bec3ec3c`.

## Verified identities and scope

- Client: real OpenCode **1.18.35**, using the
  `@ai-sdk/openai-compatible` provider. The pinned upstream
  `v1.18.35` package manifest declares adapter **2.0.41**
  (Git blob `9def9a322eb3c47435a7b0f0009b4035f0de01d7`);
  upstream `bun.lock` Git blob is
  `481ef0a61ec4ab3335d01e32934cd96589966f56`.
  The compiled binary's actual loaded adapter module identity was
  **not separately introspected**.
- Source commit above was built into a Nix **Control** package without
  changing any installed profile. Candidate Control package NAR hash:
  `sha256-RGsosySfBuEdI+urXKpzsVGKa/5q9liy3wNQsoxzNwo=`.
  Its packaged `gateway/chat.py` SHA-256 was
  `2276e35bae9e83cf523096d4a5b381e138cb80b76a101f7f61f4f5f118051fdf`,
  identical to the candidate worktree source. The packaged module
  includes `reasoning_effort` in its reviewed accepted-field set.
- Existing live Control is a distinct older `v0.1.2` Nix closure with
  `gateway/chat.py` SHA-256
  `1c6a498bf116e8ebbea80beaefe9a7e6179d6a980b836b80da2885a105b8a6a2`.
  It still rejects `reasoning_effort`; **the live Control was not upgraded**.
- Live `GET /v1/models` advertised both `qwen3.8-27b` and
  `bonsai2-27b`, each with the `tools` feature and bounded
  `total_tokens=8192`, `output_tokens=2048`, `request_bytes=65536`.

## Candidate method and limits

A disposable HTTP listener bound **only to loopback** exercised the
actual packaged PR #211 `compile_chat_request` with source-compatible
synthetic local profile identities and the exact live published limits.
It then sent the normalized request to the **unchanged live Chat
Gateway**, which continued through its ordinary durable Job admission,
Worker and GPU model path. It preserved SSE streams and tool result
messages. This was a **test-only ingress bridge**, not a globally
configured relay and **not** the new Control server running independently
on its native HTTP route.

The OpenCode CLI ran with `--pure --agent plan --format json`,
`stdin=/dev/null`, isolated disposable Home/XDG storage and explicit
in-memory provider config. No normal user session, workspace or plugin
configuration was reused. The sole tool fixture was a synthetic text
file in a disposable work directory; no project files were edited.
Only status codes, event types, whether expected synthetic text matched,
and completed Tool event counts are published here, not request bodies,
credentials, prompts, tool inputs/outputs or private paths.

## End-to-end observations

| Model | Test | OpenCode exit | Completed tool events | Expected result | Outcome |
| --- | --- | ---: | ---: | --- | --- |
| `qwen3.8-27b` | streamed plain chat | 0 | 0 | exact `TSUMGI_OK` | PASS |
| `qwen3.8-27b` | read tool → result → final chat | 0 | 1 (`read`) | exact `TSUMGI_TOOL_OK` | PASS |
| `bonsai2-27b` | streamed plain chat | 0 | 0 | exact `TSUMGI_OK` | PASS |
| `bonsai2-27b` | read tool → result → final chat | 0 | 1 (`read`) | exact `TSUMGI_TOOL_OK` | PASS |

All four runs: **zero OpenCode error events**. The complete four-case
smoke was executed a second time using the PR-built Nix Python closure,
not an editable source interpreter: **4/4 PASS**. In that packaged
smoke, the existing live Gateway returned two transient **HTTP 429**
responses before successful bounded requests; OpenCode retried and
eventually exited 0. Treat this as observed admission/availability
behavior, **not** as an error-free upstream trace or a fix for overload.

This smoke used real GPU-served models and real durable backend Jobs.
The synthetic accepted conversations therefore entered the existing
operational Job/event retention path as normal; the test did **not**
disable or remove those production records. It changed no deployed
service, Worker, model, model profile, PostgreSQL schema, global client
config or installed Nix profile.

## Independent supporting evidence

- Exact PR #211 local full tests: **2,195 passed / 128 skipped / 0 failed**;
  focused source/API/ingress tests: **112 passed**.
- Independent final source-only reviewer: **PASS**, no blocking finding.
- GitHub Actions CI for the source commit: unit **SUCCESS**, nix
  **SUCCESS**; run `41299220023`.
- The original two OpenCode 1.18.35 field/content refusals were
  reproduced against the unpatched source with a redacted synthetic
  request-shape fixture. New compiler and ASGI tests exercise the
  corrected strict subset and reject unknown media/fields before Job
  admission.

## Outstanding strict acceptance gates

1. **AC-OC-04:** Native **patched** Control HTTP route on an authorized
   isolated candidate or qualified deployment, with exact package,
   profile and Worker identity, must pass the same pinned OpenCode
   text + structured tool-call/result E2E. The bridge is insufficient
   to close this gate; the live Control still runs the old validator.
2. **AC-OC-06:** Document native deployed Control/Worker identity,
   actual live candidate vs. production boundary and a tested rollback
   path **before** switching production. A built Nix closure alone does
   not prove deploy/rollback.
3. Complete final CI after any later evidence-only changes and obtain
   explicit Ready/Merge authorization. This evidence is nonclosing;
   keep PR #211 Draft and Issue #209 OPEN.

Do not interpret these four successful bridge smokes as proof that
production OpenCode now works without an ingress adapter. No secret or
source prompt content is included in this record.
