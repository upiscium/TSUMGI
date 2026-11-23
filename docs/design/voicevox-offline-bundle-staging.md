# #180: offline VOICEVOX content package and staging-only SetupPlan

This local source candidate adds two bounded pieces, **not an activated native
installation**:

1. A Nix recipe which verifies an explicitly supplied, already acquired
   VOICEVOX Linux CPU x64 **0.26.2** archive before extraction, preserves the
   complete engine/model/license tree, and emits a content inventory.
2. A separate `SetupPlan(goal="stage")` which verifies that pre-provisioned tree
   and writes review artifacts beneath a caller-owned private staging directory.
   It reuses the existing digest-bound approval and failure rollback mechanism.

The ordinary LLM activation planner, `VoicevoxProvider.setup_intent`, and the
old `AudioSetupReview.can_apply=False` boundary are unchanged. Neither selecting
VOICEVOX nor obtaining a review fingerprint grants installation or activation.

## Offline content package

`nix/voicevox-cpu-bundle.nix` is exposed as
`flake.lib.mkVoicevoxCpuBundle { pkgs; archive; rightsReviewSha256; }`.
There is deliberately no default archive, fetcher, model downloader or default
`packages.voicevox` output. `pkgs` must explicitly permit the conservatively
unfree content package. Supply a real rights-review digest as
`sha256:<64 lowercase hex>`; declaring it is not a license grant.

The recipe verifies the existing release pins before extraction/copy:

- Archive: `483633de02700cd918731538543a22ce895877aeb09450a990ff38af6e4c26c0`
- `run`: `acf74848282c0ff6f0aaf8514427c5f44b0992c26b48448d76b35a97dbe81a25`
- `model/0.vvm`: `ecd35374d4182cd883cba5040376f7f888cc6ba248b1c2f4cea07cdb34bb1318`

Outputs are `lib/voicevox/linux-cpu-x64/` and
`share/astrumweaver/voicevox-bundle.json`. No engine command is executed during
the build. There is no ELF patching, stripping, launcher wrapper or automatic
`nix-ld`/host-loader modification: changing the executable would invalidate the
standard provider's byte pin and owned-process identity check.

**This means executable ABI support is still unresolved.** The original
interpreter/library requirements must be qualified on NixOS and generic
systemd before any activation plan can be admitted. The package exposes
`nativeExecutionQualified=false`; its receipt similarly says
`native_execution_qualified=false`. The raw content recipe is not evidence
that the engine can start in a Nix sandbox or installed service.

The follow-up [executable ABI source review](voicevox-executable-abi-review.md)
narrows the next gate to exact-byte static inspection. It explains the
PyInstaller self-reexec distinction and service-local namespace candidate;
neither changes this package's unqualified status or authorizes activation.

The standalone stdlib verifier bounds archive reads at 8 GiB, individual
installed files at 4 GiB, total installed bytes at 16 GiB, entries at 100,000,
depth at 32, each scan at 120 seconds, and the receipt at 16 MiB. It rejects
changed bytes, special files, writable/foreign-owned entries and escaping
symlinks. Contained relative links are recorded without traversing directory
links. Mutable hardlinks are refused; read-only root-owned Nix-store files may
use Nix's deduplication hardlinks. Nix permission canonicalization does not
change the receipt's byte and executable/non-executable inventory.

Receipts are **content descriptions**, not signatures, origin attestations,
voice-rights grants or loaded-runtime observations. The reviewed Nix recipe
establishes archive verification before extraction; a receipt manufactured
elsewhere cannot independently prove that provenance. Trusted operators and
the current UID must not concurrently mutate the inputs. Host loaders/drivers
and an OS sandbox are outside this check.

## Staging-only plan and driver

`build_voicevox_staging_plan(enabled=False, ...)` returns `None` without
inspecting optional inputs. Enabled construction requires the real
`VoicevoxProviderConfig`, its exact serving declaration, a dedicated zero-GPU
`audio.synthesize` Worker, an x86-64 target snapshot, named non-root service
identities, a loopback Control endpoint, and plain protected environment/private
HOME references. It neither opens secret files nor allocates private HOME.

The new action kinds are `VERIFY_AUDIO_BUNDLE` and `STAGE_AUDIO_CONFIG`.
The generic systemd driver rejects them. Use only `VoicevoxStagingDriver(plan,
root=...)` with an existing caller-owned `0700` staging root, never `/` or an
overlapping engine/receipt/private-HOME tree. The plan contains no package
installation, download, conversion, Worker stop, runtime start, health probe,
service reload or service-enable action.

Under `<staging-root>/voicevox/`, the driver stages:

- `runtime-deployment.json`, `serving-deployment.json`, and `worker.toml`;
- `nixos-worker-options.json` for NixOS, with the service explicitly disabled;
- or `worker.service` for generic systemd, without an automatic enable target.

These are **review artifacts**, not files installed into the live `/etc`.
Their eventual canonical configuration references are beneath
`/etc/astrumweaver/audio/voicevox/`. Native assets and their Nix GC roots remain
separately provisioned. The private HOME must remain separately provisioned,
empty and protected. There is no implicit ownership repair, credential copy,
runtime fallback, or inference.

Call `dry_run_plan` first, review the **private** exact plan, and apply only with
`SetupApproval(plan.digest, allow_confirmation_actions=True)`. No network,
privileged-operation, model-download or model-conversion approval is requested.
In contrast to the old public-safe audio review summary, this plan contains
local paths and configuration and must not be posted as a public receipt.

File replacement is individually atomic and idempotent. Failure rollback
restores prior bytes/modes or removes only the new file, using in-process
receipts; a detected intervening edit is preserved and reported as
`ROLLBACK_FAILED`. A failure after a replacement attempts recovery of that
current action too. Empty staging directories may remain. This is not a
multi-file transaction, persistent crash journal, guaranteed erasure, installed
service rollback, or authorization to delete assets or user data.

## Verification and remaining gates

`tests/test_voicevox_release.py` uses small synthetic bytes and test-local pin
substitutions, and separately checks the production pins against the standard
provider. `tests/test_voicevox_staging.py` exercises both target paths, real
manifest loading/inert factory construction, approval rejection, idempotence,
unsafe-path rejection, prior/new-file rollback, partial-write recovery and
intervening-edit refusal. It never executes the synthetic engine.

`checks.x86_64-linux.voicevox-bundle` evaluates the recipe using a deliberately
invalid synthetic archive. The check has **no native bundle build dependency**;
building that archive as a bundle would correctly fail its pin. Evaluate it
with `nix eval --offline --no-update-lock-file --no-write-lock-file --option
allow-import-from-derivation false --raw
"path:$PWD#checks.x86_64-linux.voicevox-bundle.drvPath"`.

Real archive materialization, package/ABI qualification, installed NixOS and
generic-systemd apply/rollback, standard Control integration, native readiness
and inference, and production acceptance remain separate approval/evidence
gates. This source/staging step does not close #180 or #181.

Local verification on the candidate based on `c2f3c678` (2026-11-22):

- Targeted verifier/staging tests: **54 passed**.
- Full Python regression: **3,594 passed, 133 skipped, 8 subtests passed**;
  one existing Starlette deprecation warning. PostgreSQL was not enabled.
- Five offline Nix evaluations passed: `voicevox-bundle`,
  `audio-worker-module`, `audio-control-module`, `runtime-module-eval`,
  `module-eval`.
- Actual plan-generated NixOS options evaluated as disabled by default.
  Evaluation-only enablement passed module assertions and produced runtime and
  serving values identical to the staged manifests.
- The bundle test's inspected derivation graph had no native bundle build
  dependency. No real archive, engine, service, or native package was exercised.

Subsequent, separately approved [exact-byte static inspection](voicevox-executable-abi-review.md#completed-follow-up-nix-extractor-and-exact-byte-static-inspection)
used the retained pinned archive, one private extraction, and `readelf` over
36 ELFs. The content verifier accepted the actual tree (1,408 entries excluding
the root); archive/run/VVM pins matched. It exposed bootstrap library-search
and bundled `libmvec`/`GLIBC_PRIVATE` constraints. Only the `7zz` inspection tool
was realized through Nix, not this VOICEVOX derivation. No engine/loader ran,
no rights receipt was fabricated, and executable ABI and activation remain
unqualified.
