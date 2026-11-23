# VOICEVOX service-local library view candidate

Status: **offline content construction only; activation unsupported**.
This is the next implementation boundary after the
[fixed-cohort ABI investigation](voicevox-executable-abi-review.md), not
completion of #180 or a supported deployment profile.

## Explicit inputs, separate outputs

`flake.lib.mkVoicevoxServiceView` takes `pkgs`, `libcArchive`, `libcRoot`,
and `engineRoot`. All three content inputs must be explicit local paths:

- `libcArchive`: the already acquired
  `libc6_2.35-0ubuntu3.15_amd64.deb`, 3,234,380 bytes,
  SHA-256 `79e35256227e16a607c154cdeb8d76ff12d20e31de286ea7fd9ad3b96fe0452d`.
- `libcRoot`: the already inspected regular-file payload of that package.
  The builder checks fixed per-file sizes and hashes, including the glibc
  copyright and `libmvec` witness. It does not materialize archived links.
- `engineRoot`: the separately preserved complete VOICEVOX Linux CPU x64
  0.26.2 tree. Only its pinned `run`, `engine_internal/libmvec.so.1`, and
  `engine_internal/libz.so.1` are read by this utility. This limited read is
  **not** a new verification of the entire engine/model tree.

The recipe has no fetcher, extractor, `ldconfig`, `patchelf`, stripping,
wrapper, or native-library execution. It does not call
`mkVoicevoxCpuBundle`, re-extract the original archive, or require a
fabricated rights-review receipt. Existing bundle content/rights review
remains a separate requirement. The original engine, modes and argv remain
untouched; neither `run` nor the model is copied into the new view.

The standalone stdlib utility is
`src/tsumgi/runtime/providers/voicevox_service_view.py`. Its `build`
command takes `--libc-archive`, `--libc-root`, `--engine-root`, and
`--output`; `verify` takes `--output`. There is no activation command and
no command-line override of the pins.

## Fixed layout

Under the new output, `rootfs` contains only the fixed library layout,
copyright and empty mount-point directories:

```text
rootfs/lib/x86_64-linux-gnu/
  ld-linux-x86-64.so.2
  libc.so.6
  libdl.so.2
  libm.so.6
  libpthread.so.0
  librt.so.1
  libutil.so.1
  libz.so.1
rootfs/lib64/ld-linux-x86-64.so.2
  -> ../lib/x86_64-linux-gnu/ld-linux-x86-64.so.2
rootfs/usr/share/doc/libc6/copyright
share/tsumgi/voicevox-service-view.json
```

The zlib bytes come from the original engine, not another package:
108,936 bytes, SHA-256
`64c206f0146cc58bbddc4f22054436f4ff278f5a554aa3ce6921ddf7e9133370`.
They are available in the candidate loader's multiarch default directory
before PyInstaller can set its environment. **Placement is not evidence
that the initial exec actually maps this file.** Loader path strings,
`--list`, and post-reexec maps remain insufficient for that claim.

The original bundled `libmvec`, `libgcc_s`, and `libstdc++` are not masked,
replaced, or copied into this root. No host `ld.so.cache`, `ld.so.preload`,
hwcaps provider, or unreviewed extra file belongs in the view.

Inputs must be canonical, protected, bounded and non-symlinked on the
selected paths. Output must be new, protected and disjoint from the
inputs. Content verification rejects extra entries, changed modes, bytes,
links or receipt flags. Only newly created output modes are changed:
directories and loader are read-only/executable; other files are read-only.
The content receipt contains relative paths, not host paths or credentials.
Failures do not overwrite or clean up a partial output. This is not a
transactional installer, resume operation or rollback implementation.

The protection boundary trusts root and the current owner, not a hostile
same-UID process or host. A narrow exception permits the canonical,
root-owned `01775` `/nix/store` directory used by multi-user Nix; selected
entries must still pass their own checks. Root-owned, read-only store files
may be hardlinked by store optimisation. Neither exception permits arbitrary
group-writable inputs, symlink ancestors or writable hardlinked staging files.

## Candidate systemd data, not a unit

The derivation's `passthru.candidateServiceConfig` is review data:

- `RootDirectory` is the new `rootfs`, for all execution phases.
- Only `/nix/store` is proposed as a read-only host bind.
- API filesystems, private devices, strict system protection, protected
  home directories, and no-new-privileges are requested.

It deliberately supplies **no** `ExecStart`, engine substitution, writable
private HOME bind, user/account, credential source, DNS/NSS configuration,
Control networking, install target, or runtime/Serving declaration. It is
not a NixOS module and is not connected to `tsumgi-worker`.
Do not merge it into a running unit as an installation recipe.

The factory is absent from default packages. Existing Audio module
assertions, fixed Worker `ExecStart`, manifest/artifact binding, sparse
child environment, owned-process identity, LLM planning, and
`AudioSetupReview.can_apply=False` are unchanged.

The receipt and Nix metadata keep native execution, loader resolution,
activation and Serving binding unqualified. In particular:

```text
native_execution_qualified=false
elf_loader_resolution_proven=false
firstexec_loaded_mapping=UNKNOWN
activation_supported=false
serving_binding_supported=false
```

## Verification and remaining gates

`tests/test_voicevox_service_view.py` uses synthetic, non-native bytes with
test-local pins. It exercises construction, exact verification, preservation,
unsafe paths and tampering. It is not a package realization or native trial.
`checks.x86_64-linux.voicevox-service-view` evaluates the recipe and fragment
using deliberately invalid synthetic inputs. Its resulting check derivation
must not depend on those inputs or the native candidate.

An evaluation-only check is:

```sh
nix eval --offline --no-update-lock-file --no-write-lock-file \
  --option allow-import-from-derivation false \
  "path:$PWD#checks.x86_64-linux.voicevox-service-view.drvPath"
```

This does not build the recipe. Building the check must also remain
independent of native content; inspecting its derivation dependencies
guards against accidental realization in CI.

The selected glibc cohort is **superseded upstream** and is an ABI
investigation candidate, not a security-patch or deployment recommendation.
Per-file/archival hashes and the copyright file do not prove a signed
archive chain, redistribution rights, complete dependencies, CPU/IFUNC
compatibility, relocations or `dlopen` coverage.

Further gates include a separately authorized package realization, measured
first-exec library mapping, the actual owned Worker under the intended
service root, explicit binding of this execution view to deployment/Serving
identity, current security review, and installed-host apply/rollback.
Standard Control/PostgreSQL E2E and real old-epoch revocation for #247 are
also still separate. No synthesis or Job from the exhausted #292 trial is
repeated, and no Issue closure or rollout follows from these offline tests.
