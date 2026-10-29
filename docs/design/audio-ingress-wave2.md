# Wave 2 audio ingress: pre-admission only

Run: `TSUMGI-175-W2-INGRESS-20261029-R2`, continuation R3.
Workstream: `WS-175-INGRESS-P0`; TSUMGI-AUDIO-FC-001/r0, #175 / DES-AUDIO-002,
AC-AUDIO-01 and AC-AUDIO-05. Coordinator-verified branch:
`feature/175-ingress-wave2`; base `ef337ace847a2b3cd90cccfa897cd2a370dc553d`.
The executor did not rerun Git preflight in R3: command execution was prohibited.

## Interface and trust boundary

`parse_speech_ingress(request, catalog, limits=...)` and
`parse_transcription_ingress(request, catalog, limits=...)` are helpers, **not
HTTP routes**. Call once on a fresh ASGI/Starlette Request, before any other
body/form/JSON consumer. They consume `scope["headers"]` and `receive()` directly,
never `request.form()`, cached bodies, files, paths, providers or external URLs.
The existing catalog and both shared validators are reused without modification.
There is no model/voice fallback, discovery, production catalog or default limit
configuration. All configuration in tests is synthetic.

An operator must supply every `AudioIngressLimits` field. Counts are positive
non-boolean integers, and deadlines are finite positive numbers, at most 60
seconds, with idle <= total. Hard safety ceilings are: request/file 16 MiB,
speech input 1 MiB, metadata 16 KiB, individual part headers 8 KiB, aggregate
headers 32 KiB, and 65,536 receive events. These are implementation ceilings,
**not approved production settings**. The effective pre-parse body/file/metadata
caps are the minimum of the explicit global cap and the maximum relevant catalog
limit. No caller-selected `model` is trusted until parsing and validation finish.
The exact selected profile's request/content limits are then enforced as well.

## Bounded transport and parsing

* Raw HTTP headers: at most 64, with a byte budget; ASCII token names and printable
  ASCII values only. All duplicate header names are rejected case-insensitively.
  This intentionally rejects some otherwise legal general HTTP requests.
* Content-Length is checked before receiving, must be a 1–10 digit unsigned
  decimal, positive and within the body cap, and must equal measured bytes.
  Without it, ASGI chunks remain bounded. An exposed Transfer-Encoding is allowed
  only as `chunked`, without Content-Length; the ASGI server owns HTTP dechunking.
  Content-Encoding and Content-Transfer-Encoding are never accepted.
* Each chunk is checked against the aggregate cap **before** retaining it. Idle
  and whole-body receive deadlines, a finite event count (including empty
  chunks), disconnection and malformed event rejection prevent indefinite reads.
  Cancellation propagates. There is no draining after rejection.
* The complete raw body is buffered **within that fixed cap**, not streamed to a
  spool. Temporary copies/Unicode decoding consume a bounded multiple of this
  cap; it is not a constant-memory streaming multipart parser. Header/file/value
  slices are checked before creation. The host/ASGI server must separately bound
  incoming chunk allocation, connections, raw headers and concurrency. Helpers
  cannot undo allocation that happened before `receive()` returned.
* Speech accepts `application/json`, optionally with only `charset=utf-8` (also
  quoted). UTF-8 is strict; BOM, bad JSON, nonstandard numeric constants, numbers,
  lone surrogate escapes, arrays, nested objects and more than four members fail
  closed. An iterative string-aware structural guard precedes the recursive JSON
  decoder. `object_pairs_hook` detects duplicate decoded keys, including escaped
  spellings; excessive members can be rejected before that hook. Input text is
  not normalized or trimmed. The shared validator checks the four exact fields.
* Multipart accepts only `multipart/form-data; boundary=...`, with a single
  optionally quoted 1–70 character ASCII alphanumeric/underscore/hyphen boundary.
  CRLF framing, a final `--boundary--\r\n`, and no preamble/epilogue are required.
  A boundary-prefix collision inside a value is rejected, not ambiguously
  reinterpreted. This is a strict supported subset, not general MIME compliance.
* Exactly four parts, in **any order**: required `model`, `language`,
  `response_format`, and one `file`. No duplicates, extras or omitted defaults.
  Part headers are searched only within the individual cap, and counted against
  an aggregate cap including their terminating CRLFCRLF. HTTP and multipart
  header budgets are checked separately against the configured aggregate cap.
  Metadata size is the sum of raw value bytes, excluding envelope/headers.
* Part headers allow only one Content-Disposition and optional Content-Type.
  Disposition is `form-data; name="..."`, with `; filename="..."` only for file.
  No folded headers, extra parameters, extended filenames, transfer encodings
  (even `binary`) or unknown headers. Metadata is strict UTF-8; its optional MIME
  is only `text/plain` or `text/plain; charset=utf-8`. The filename is an ASCII
  basename matching `[A-Za-z0-9][A-Za-z0-9_-]{0,63}\.wav`, checked then discarded.
  URLs, traversal, absolute paths, percent escapes and backslashes cannot pass.

## WAV checks are not audio admission

Only file MIME `audio/wav` without parameters is accepted. FLAC, compressed or
extended WAV and all other MIME types fail closed; there is no sniff-and-transcode
fallback. A nonempty file must structurally contain exactly RIFF/WAVE, a 16-byte
PCM format chunk and one nonempty data chunk. PCM16, one or two channels, sample
rate 8,000–48,000 Hz, consistent byte rate/block alignment, frame-aligned data,
exact RIFF/data lengths and no extra bytes/chunks are required. Samples are not
decoded. These checks define a parser subset, not a production codec decision.

`PreAdmissionTranscription` retains immutable **untrusted** bytes and measured
sizes with shared validated metadata. It is not a Job or artifact. Future #174
binding/artifact integration and #177 codec qualification, duration, decoded-size
and model-specific checks remain mandatory **before any admission**. No control,
worker, DB, spool, adapter or host service is changed. Speech parsing does not
synthesize WAV; multipart parsing does not transcribe it. This is not completion
of #175, either acceptance criterion, or real-audio E2E.

## Verification handoff and frozen source manifest

Source edits were accepted by the file-edit tool. **Tests NOT RUN**: R3 permits
only file inspection/edits and expressly forbids all command execution, including
test runs and command substitutes. Runtime versions, exit codes, SHA256 hashes
and a generated patch are therefore **not measured**, not fabricated. Source was
read back for static inspection; execution correctness remains unverified.

The frozen uncommitted source manifest is exactly these three new files:

1. `src/astrumweaver/gateway/audio_ingress.py`
2. `tests/test_audio_ingress.py`
3. `docs/design/audio-ingress-wave2.md`

No existing tracked file was edited. The coordinator must collect these untracked
files explicitly: a normal `git diff` alone does **not** include them. For each
path in manifest order, `git diff --no-index -- /dev/null PATH` produces its
reproducible addition patch (exit 1 means a diff, not a test failure). Concatenate
those three patches in manifest order for the frozen patch; measure SHA256 of
each file and that concatenation during authorized coordinator collection. No
index operation or commit is needed. Hash/patch evidence remains pending there.

Required synthetic checks, from the worktree with the already-provisioned Python
environment (no install, network, model, GPU or host service):

```sh
python -m pytest -q tests/test_audio_ingress.py tests/test_audio_gateway_contracts.py tests/test_serving_contracts.py
```

Optional neighboring contract-only regression command:

```sh
python -m pytest -q tests/test_chat_gateway_contracts.py tests/test_embedding_gateway_contracts.py tests/test_decision_gateway_contracts.py
```

The new tests include ASGI Request and stubs, all 24 multipart field orders,
bytewise Unicode/body chunking, explicit omission/duplicate checks, raw and
selected-profile limits, malformed JSON/MIME/framing/headers, dangerous filenames,
transfer-encoding ambiguity, empty/spoofed WAV, disconnect, empty-event floods,
cancellation, idle/total timeout, fixed error redaction and exact size edges.
Coordinator execution and independent review remain outstanding; do not infer a
passing result from test source. Preserve the worktree without commit/add/push,
PR/Issue writes, merge, release, deployment or permission configuration changes.
