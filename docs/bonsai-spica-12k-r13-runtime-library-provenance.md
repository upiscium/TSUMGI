# TSUMGI #232 R13 — Loaded Native dependency identity (source-only)

**State: REVIEW_ONLY / BLOCKED.** This phase is a source/test/docs
extension of Draft PR #250, not approval for a physical managed 12K
Worker, Control Catalog publication, Ready/Merge, or any service change.

## Actual read-only observation from Spica 8K

During the current 8K service's normal operation, the configured
provider executable at the pinned Nix profile is **a Bash wrapper
(4,433 bytes)**. The running Native process's /proc/PID/exe resolves
to a **26,656-byte llama-server executable**. Those are different
files and have different SHA-256 digests. The Native process dynamically
maps substantial runtime libraries, including:
- libggml-cuda.so: 692,921,504 bytes;
- libcublasLt.so.12.9.2.10: 749,232,240 bytes;
- libcublas.so.12.9.2.10: 105,186,528 bytes;
- libllama-server-impl.so: 7,437,400 bytes.

The selected runtime-relevant /proc/PID/maps subset contained **14
distinct Nix-provided file basenames**, and those selected existing
files were successfully hashed *read-only* on Spica. A hash of only
the 4.4KiB wrapper or the tiny 26KiB executable is not sufficient
to characterize which CUDA/GGML/llama.cpp implementation is loaded.

The active 8K Runtime/Serving source SHA-256 values remained
f0c20685800548151197c13b9e9418f29f62e4739fffb72c66545bb3379a4623 /
6cad463e2e07c41850db29aeb6afc53e24b54932190472ecf41040049f84d01b
at this observation. This was **not** a new 12K launch, an independent
signature, a full model re-hash, or a trusted off-host forensic record.

**Important scope limit:** the 14 files are a selected
GGML/CUDA/Bonsai-related memory-map subset, not a complete ELF dependency
closure, firmware or driver measurement; libraries can also load later.
An actual future managed Canary must bind the exact current binary,
mapped object and process identity from that *new run*, not re-use
these observations as if they attested the future process.

## R13 pure source-only validator

The new R13 module computes a **review-only path-neutral fingerprint
of caller-reported bytes**, including:
1. a Bash entrypoint wrapper's claimed SHA-256 and file size;
2. the distinct Native process executable's claimed SHA-256 and size;
3. the selected 14 mapped runtime objects' claimed SHA-256, size
   and SONAME, including an exact match between the Native executable
   and its corresponding mapped file.

Unknown, duplicate, missing or malformed SONAMEs and digests fail
closed. The set is deliberately pinned to the **currently observed
Spica Bonsai 8K runtime**. An actual Nix/CUDA/runtime library change
must trigger a new review rather than silently inherit the previous
identity. Mapping order does not change the canonical digest; changing
any dependency hash or size does.

Paths and PIDs are deliberately excluded from this *byte identity*
fingerprint, but an independent future process provenance receipt
MUST bind the runtime identity to exact PID-starttime, cgroup,
Wrapper/Native process ancestry, loaded object paths/inodes, hostname,
GPU UUID, actual current Nix closure and an authenticated capture time.
The path-neutral byte identity is **not** a substitute for those
instance/host claims.

R13 also checks that R11's reported "runtime" SHA is the **whole
selected dependency-bundle review fingerprint**, not the wrapper
hash alone. It verifies R11's canonical record fingerprint,
the pinned Bonsai GGUF digest claim, correct 12K opt-in profile,
and rejects attempts to flip its blocked source/host/activation flags
to true. An untrusted caller could fabricate the entire R11+R13
bundle, so the output remains explicitly blocked.

Neither the new source module nor its isolated tests performs
filesystem, /proc, GPU, HTTP, Native, Control, PostgreSQL, Nix or
Worker I/O. No runtime artifact is installed, signed or trusted.

## What R13 does NOT prove

- Who measured the runtime libraries or whether they were actually
  loaded by a particular serving process.
- That every dependency, kernel/driver module, firmware component,
  template, tokenizer and model weight was included.
- Which exact binary, options and CUDA device a **future 12K**
  managed Worker will launch.
- Real Native HTTP request bytes, R6B stage/UUID NVML provenance,
  sustained peak VRAM floor, Gateway Tool Result or cancellation.
- An independently authenticated 8K rollback or production
  Serving Contract/RuntimeInstance epoch.

These remaining gaps are not waived by internally consistent SHA-256
strings or Green synthetic CI.

## Next acceptance gates (separately authorized)

Obtain an independent source review; establish a trusted capture
procedure, capture role, immutable evidence receipts and verification
anchor. During a separately approved Spica-only maintenance window,
first drain admissions and prove old Native descendants/cgroup and
GPU owners released. For the new managed 12K process, capture the
actual Wrapper -> Native -> mapped-library dependency chain and
full GGUF/template/binary bytes; match it to the operator-approved
source revision, then bind the exact native prompt/Tool/SSE/cancellation
and Spica GPU UUID/time-window samples with >=2,048MiB observed free
and complete original 8K restoration.

Only then consider separate approval to mint reviewed Serving
identities and publish a new opt-in bonsai2-27b-12k Control profile.
Never silently migrate the old Bonsai8K alias or route its 12K calls
to Deneb8K. Keep #232 OPEN and PR #250 Draft/unmerged.
