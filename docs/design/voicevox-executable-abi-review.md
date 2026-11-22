# #180: VOICEVOX executable ABI — source and exact-byte static evidence

Status (2026-11-22): **static evidence only; executable closure unqualified**.
This follows the [offline content/staging candidate](voicevox-offline-bundle-staging.md).
The source review did not access native inputs; the subsequent, separately
approved local discovery, one extraction and ELF inspection are recorded below.
No loader, engine, or installed service was exercised. No native trial was
repeated. Activation remains unsupported.

## Source identity and its limits

The upstream release-page inspection associated Engine `0.26.2` with short
commit `c66f534`. The source inspected here is
[`c66f534106465c7f3186c031018f6b28dd3c2223`][engine-source].
This is a release-page/source association, **not** independent tag-ref or
source-to-binary attestation. A tag-ref API lookup returned HTTP 403 and was
not retried. The workflow can update both the release assets and version tag.
The existing archive/executable/model hashes remain the required byte pins;
source inspection does not replace them.

Directly inspected upstream source establishes:

| Source | Established fact | Not established |
| --- | --- | --- |
| [Build workflow][build] | `linux-cpu-x64` uses `ubuntu-22.04`; CORE `0.17.0`, ONNX Runtime `1.23.2`, resource `0.26.1`, VVM `0.17.1`. | Actual ELF interpreter, minimum glibc, CPU ISA floor, or dependency closure. |
| [Build dependencies][dependencies] and [spec][spec] | PyInstaller `6.21.0`, one-folder `COLLECT`, with `contents_directory="engine_internal"`; bundled Python requirement `==3.14.9`. | Contents of the exact pinned archive or need for a host Python installation. |
| [Spec][spec] | Collects `pyopenjtalk` data and Linux `pyhwloc` libraries, and copies resources, manifest, CORE, ONNX Runtime, models and optional license file. | Full runtime-loaded library inventory. |
| [Build workflow][build] | Renames Linux ONNX Runtime to `libvoicevox_onnxruntime.so` and sets its versioned SONAME before packaging. The explicit `$ORIGIN` adjustment is in the **CUDA** branch. | CPU archive RPATH/RUNPATH; CUDA instructions must not be applied by analogy. |
| [Path utility][paths] and [core initializer][initializer] | Packaged engine root comes from `sys.executable`, not startup cwd. Default CORE/runtime directories are that root. User CORE directories are also searched beneath the private user-data directory. | Permission/read-only-store compatibility or absence of all runtime writes. |
| [Core wrapper][wrapper] | Loads root libraries with `ctypes.CDLL`, also attempts system `find_library`, and loads CORE separately; CORE initialization temporarily changes cwd. | `run`'s `DT_NEEDED` alone cannot cover these dynamically loaded libraries. |
| [Entry point][entry] | `CancellableEngine` is opt-in; the normal path calls `uvicorn.run(app, host=host, port=port)` without a worker-count argument. | Actual process topology of the shipped binary. |
| [Dockerfile][docker] | Uses Ubuntu 22.04 by default and launches the published engine via `gosu`. | A portable Nix closure, a measured ABI floor, or permission to adopt that container launcher. |

### Important distinction: self-reexec is not image substitution

The version-tagged [PyInstaller `6.21.0` bootloader source][bootloader] describes
Linux one-folder startup as setting its own library search environment, then
calling `execvp` on the same executable with the original argv, **without
forking**, when directly invoked. It also has a separate explicit-loader path.
This explains why stripping inherited `LD_LIBRARY_PATH` need not prevent
PyInstaller from locating its bundled libraries after startup.

This is a source-derived expectation, not observation of the pinned `run`.
Same-file/same-argv reexec is not inherently incompatible with TSUMGI's image
and generation checks. Replacing the image with a different wrapper/loader or
changing argv is a different case. No readiness check should be relaxed on
the assumption that “PyInstaller needs reexec”.

## Current TSUMGI launch contract

[`voicevox_manifest.py`](../../src/astrumweaver/runtime/providers/voicevox_manifest.py)
fixes the executable to `<engine_root>/run` and the argv tail to
`--host 127.0.0.1 --port <port>`. It still rejects activation SetupPlans.

[`voicevox_linux_process.py`](../../src/astrumweaver/runtime/providers/voicevox_linux_process.py)
directly spawns that path, hashes its bytes beforehand, and checks the child's
executable inode/device/size/mtime, UID, start generation, process group, exact
argv and owned IPv4 loopback listener. It sets `cwd="/"` and constructs a new
environment containing only fixed `PATH`, `PYTHONNOUSERSITE` and the validated
private `HOME`. Service environment variables, including `LD_LIBRARY_PATH`,
`LD_PRELOAD` and `NIX_LD*`, are not copied to that child.

Existing source tests reject another ELF even when PID, generation and the
old listener survive exec, while retaining authority to reap that owned
child. Those tests were inspected, not rerun for this review.

| Approach | Decision for the unchanged contract |
| --- | --- |
| Direct original `run` on an already compatible host | Structurally compatible; actual ABI/readiness remains unqualified. |
| `patchelf`, stripping, or replacing `run` with a wrapper | Reject: changes pinned executable bytes. |
| Direct `ld-linux … run`, or a per-engine FHS/bwrap launcher | Not representable by the current fixed executable/argv contract. Do not substitute it or weaken image checks. |
| Set loader environment in the Worker unit | Not a child-closure solution: the controller replaces that environment. |
| Globally enable/change `nix-ld` or host loader paths | Outside this bounded work; do not make host-global changes. |
| Give the whole Worker service a reviewed filesystem namespace, then directly exec original `run` | Candidate for investigation only; no namespace or mount has been implemented or tested. |

## Candidate to investigate, not a qualified launch recipe

Prefer preserving direct execution and supplying any missing interpreter and
libraries in the **same service filesystem view as the Worker**. A
systemd-managed mount namespace is a candidate rather than an engine wrapper.
Keep the pinned executable, original layout, exact argv, static unprivileged
account, private HOME, PID/network visibility and process ownership checks.
This does not authorize a container/PID namespace or a different listener.

Offline evaluation of the repository's locked nixpkgs
`6774f7bc253789b113a4f39285dc0fa100abeacc` reported:

```text
glibcVersion=2.42
systemdVersion=261.2
dynamicLinker=<Nix store glibc output>/lib/ld-linux-x86-64.so.2
```

These are evaluated package attributes, **not** built/installed dependencies.
Inspection of cached nixpkgs module source was permission-denied and was not
retried through another route. No claim about the target's `/lib64` contents
or loader symlinks follows from that evaluation.

The [systemd 261.2 manual source][systemd] says `BindReadOnlyPaths` creates
unit-specific mounts invisible in the host mount table and implies a mount
namespace. It also requires that destinations exist or can be created.
Therefore, even if the actual ELF needs a conventional interpreter path, a
usable mapping still requires review of the destination's symlinks, mount
creation, read-only protection, complete library resolution, and effects on
the Worker itself. Do not hard-code an assumed `PT_INTERP`, install a global
symlink, or claim that binding a single loader proves a complete closure.

## Planned gate: exact-byte static inspection, separately authorized

The following bounded gate was subsequently approved and completed as recorded
below; it does not authorize the later executable-closure or service trials.

The next minimum input is an operator-designated, existing pinned archive
and/or extracted tree. Do not search old private trial directories or assume
their assets still exist. If an input is unavailable, stop rather than fetch
another archive or model.

After explicit approval of the exact inputs and any private extraction
destination, the bounded static-only inspection should:

1. Verify the archive hash when an archive is supplied, and executable/model
   hashes and tree identity for extracted inputs. A tree alone cannot establish
   archive provenance. Preserve original bytes and modes.
2. Record ELF class/machine/type, `PT_INTERP`, `DT_NEEDED`, SONAME, RPATH/RUNPATH,
   version requirements (`GLIBC_*`, `GLIBCXX_*`, `CXXABI_*`) and relevant ELF
   property notes for `run` **and** packaged ELF libraries, including
   `engine_internal`, CORE and ONNX Runtime. Static notes do not fully establish
   CPU instruction requirements.
3. Distinguish bundled providers from unresolved host-library names, account
   for the source-observed `dlopen` paths, and propose an explicit closure and
   service-local path mapping. Do not infer completeness from `run` alone.
4. Retain a private digest-bound report that explicitly remains
   `native_execution_qualified=false`.

Use byte-reading metadata tools such as `readelf`; **do not** use `ldd`, run
the loader, `ctypes.CDLL`, Python imports from the bundle, engine `--help`,
readiness, inference, Nix realization, or any service/host operation.
Extraction, if needed, is a separately specified local write, not implied by
read-only input access. No deletion or permission repair is part of this gate.

Static inspection still will not establish native load/start success,
same-image reexec behavior, socket ownership, loaded libraries/models,
installed-host apply/rollback, standard Control E2E, or production acceptance.
Those require later, separately bounded operational approval. #180 remains
open; this review changes neither #181/#173 nor #247 acceptance.

### Follow-up: existing-input discovery, not extraction or ABI acceptance

The operator subsequently requested locating the existing inputs. A bounded
local search found a retained `voicevox_engine-linux-cpu-x64-0.26.2.7z.001`
and a separate retained `verified-0.vvm`. Their private locations are not
included here. Read-only, bounded, no-follow hashing confirmed:

| Input | Bytes | SHA-256 |
| --- | ---: | --- |
| Archive | 1,908,622,427 | `483633de02700cd918731538543a22ce895877aeb09450a990ff38af6e4c26c0` |
| Separate VVM | 59,308,488 | `ecd35374d4182cd883cba5040376f7f888cc6ba248b1c2f4cea07cdb34bb1318` |

Both matched the existing pins and were unchanged across each read. This does
not establish that the separate VVM was extracted from that archive.
No real extracted `linux-cpu-x64/run` was found in the accessible searched
trial/current-worktree roots; synthetic pytest fixtures were excluded.
An ancestor-worktree search was permission-denied and not bypassed, so this
is not an exhaustive host/fleet inventory.

No acquisition, extraction, ELF parsing, engine execution, or service operation
was performed. The original `run` pin and its actual interpreter/dependencies
remain unverified in this slice.

The operator then approved one extraction into a new private analysis directory
and static ELF inspection, bounded by 16 GiB and 100,000 archive entries, with
no acquisition/build, native execution, service operation, existing-permission
change or deletion. Tool preflight found `readelf`, but neither `7zz`/`7z` on
`PATH` nor the evaluated locked-nixpkgs `7zz 26.02` executable was available.
No package was realized to fill that gap. A new empty `0700` analysis directory
was created and retained; extraction did not start. This gate was then blocked
on an available extractor or separately authorized extractor provisioning,
not on a missing archive or missing static-inspection approval.

### Completed follow-up: Nix extractor and exact-byte static inspection

The operator subsequently authorized using Nix to provision the missing
extractor. The locked nixpkgs above selected `7zz 26.02`. A dry run reported
two outputs, **2.5 MiB download / 5.7 MiB unpacked**; both were substituted from
`cache.nixos.org` with local and remote builders disabled, no fallback, no
profile changes and no result symlink. This realizes the **inspection tool**,
not the VOICEVOX package, an installed Audio unit, or a native runtime closure.

The exact existing archive was rehashed before listing and extraction. The
preflight accepted **1,409 entries**, including the root: **213 directories,
1,192 regular files, 4 leaf symlinks**, **2,320,120,600 declared bytes**. It
checked canonical unique paths, explicit directory parents, no entries below
symlinks, file types/modes, size/depth/count bounds and free space. A fixed
extractor command ran once into a new private directory with time/resource
limits, no overwrite of existing inputs and no cleanup/deletion.

Post-extraction checks matched all listed types and sizes and validated all
four relative symlinks within the tree. The archive, original `run` and
`model/0.vvm` matched the existing pins. The current repository's stdlib
content verifier also scanned the actual extracted tree successfully:

```text
entries_excluding_root=1408
regular_bytes=2320120431
tree_sha256=64925cbaaee53a81b0ccf8c4c1fc918f633951eb61bb9b61c850ddb7dce5eebf
executable_and_model_pins=MATCH
```

This invoked only the verifier's content-inventory routine, not a Worker or
anything imported from the bundle. No rights-review digest was invented and
no rights-bearing bundle receipt was created. This is real-input verification
of the raw tree, **not** a realized Nix VOICEVOX derivation.

#### Observed ELF facts

`readelf` inspected **36 regular ELF files** (157,044,981 bytes). The four
symlinks contribute name aliases, not duplicate ELF observations. The private
structured report has SHA-256
`9bfde6c2ea316f6c47591fec8121f74c1941ffdde7877da688210137808a371e`.
It retains per-file byte hashes, raw-report hashes, headers, interpreter,
dynamic tags, version needs and selected notes. No input ELF was executed.

| Property | Observation |
| --- | --- |
| `run` | 9,315,016 bytes, ELF64 little-endian x86-64, `ET_EXEC`, original executable pin matches. |
| `run` interpreter | `/lib64/ld-linux-x86-64.so.2` |
| `run` direct `DT_NEEDED` | `libdl.so.2`, `libz.so.1`, `libpthread.so.0`, `libc.so.6` |
| `run` RPATH / RUNPATH | Neither present. |
| Largest numeric **required** versions across all 36 ELFs | `GLIBC_2.35`, `GLIBCXX_3.4.30`, `CXXABI_1.3.11` |
| Non-numeric requirement | Bundled `engine_internal/libmvec.so.1` requires `GLIBC_PRIVATE` from `ld-linux-x86-64.so.2`. |
| Additional dynamic-symbol inspection | `libmvec.so.1` has undefined object `_rtld_global_ro@GLIBC_PRIVATE`. |
| CPU ONNX Runtime | SONAME `libvoicevox_onnxruntime.so.1.23.2`, RUNPATH `$ORIGIN`, requires `GLIBCXX_3.4.30` and `CXXABI_1.3.11`. This is now observed in CPU bytes, not inferred from the CUDA workflow. |

No bundled filename/SONAME candidate was found for
`ld-linux-x86-64.so.2`, `libc.so.6`, `libdl.so.2`, `libm.so.6`,
`libpthread.so.0`, `librt.so.1` or `libutil.so.1`.
Other `DT_NEEDED` names have at least one candidate somewhere in the tree;
**that does not prove the loader can reach or select it**. Source-observed
`dlopen` behavior and runtime branches can add further requirements.

The numeric maxima are version-*need* evidence, not exported-version maxima,
a sufficient ABI test, or a general supported-distribution floor. In
particular, the separate `GLIBC_PRIVATE` requirement must not disappear behind
the shorthand “glibc >= 2.35”.

#### Consequences for the next executable-closure design

1. **Bootstrap search is distinct from PyInstaller's later search.** The only
   bundled `libz.so.1` candidate is `engine_internal/libz.so.1`. The kernel
   interpreter must satisfy `run`'s initial dependencies before the bootloader
   can set its own library-search environment and self-reexec. With no
   `run` RPATH/RUNPATH and TSUMGI's sparse child environment, the presence of
   that file alone does not close bootstrap library resolution. A loader-only
   bind is not yet a qualified recipe.
2. **Keep the glibc components coherent.** The bundled `libmvec.so.1` uses the
   loader's private `_rtld_global_ro` object as well as versioned `libm`
   interfaces. Do not bless mixing it with the evaluated Nix glibc 2.42 merely
   because public numeric symbol versions look new enough. Static inspection
   here establishes neither a concrete 2.42 mismatch nor compatibility of
   that private object/layout.
3. **Preserve the reviewed image and tree.** Do not patch `run`, silently
   remove/mask bundled `libmvec`, or switch to an explicit loader/wrapper.
   Supplying a compatible, pinned runtime cohort in a service-local filesystem
   remains the preferred investigation path. Any alternate library mapping
   changes the loaded-code closure and needs its own provenance, serving
   binding, review and native qualification.

At the end of this one-extraction/static-inspection step, a matching
runtime-library cohort had **not** been acquired, built, installed, or tested.
No NixOS/generic-systemd apply/rollback or standard Control acceptance followed.
The status at that gate was:

```text
native_execution_qualified=false
elf_loader_resolution_proven=false
archive_extractions=1
native_engine_executions=0
loader_executions=0
```

## Fixed-cohort follow-up: acquired and statically matched, not executed

The operator subsequently approved the investigation and operations needed
to resolve this ABI boundary. The bounded follow-up selected one exact
runtime-package candidate, without changing the VOICEVOX inputs:

```text
package=libc6_2.35-0ubuntu3.15_amd64.deb
bytes=3234380
sha256=79e35256227e16a607c154cdeb8d76ff12d20e31de286ea7fd9ad3b96fe0452d
```

The [Ubuntu debug-package filelist][libc-debug-files] contains
`.build-id/98/9e0b199bc8fd7ff63102bbb1389e576135aaf0.debug`, matching the
existing bundled `libmvec` Build ID. The [debug-package metadata][libc-debug]
requires exactly `libc6 (= 2.35-0ubuntu3.15)`, and the
[runtime download metadata][libc-download] supplies the size and hash above.
This identified a candidate, not byte equality or the original engine's
build provenance.

### Acquisition failure and the retained official build

The initial primary-archive acquisition failed without a recorded HTTP
status. A newly authorized, instrumented attempt established **HTTP 404**,
zero retained package bytes and no redirect or retry. The official primary
publication records
[Updates][libc-publication-updates] and
[Security][libc-publication-security] identify the same package and
[build `33569442`][libc-build]; both record removal on
`2026-11-14T00:10:55.683273+00:00` after supersession. Thus a package-site
download listing must not be treated as proof that the current archive-pool
URL still retains the artifact.

The retained build's [changes metadata][libc-changes] independently lists
the exact filename, size and SHA-256 above. The bounded acquisition then
used its [official build-file URL][libc-build-file], followed one explicitly
allowed HTTPS redirect to `launchpadlibrarian.net`, and received HTTP 200.
The complete **3,234,380 bytes matched the fixed SHA-256**. This did not add
a PPA, install a package, invoke a maintainer script, run `ldconfig`, or change
host loader settings. The two unsuccessful archive attempts remain separate
from the successful build-file acquisition; none of their markers was reused.

### Actual package bytes and static bridge checks

The package was inspected once in a new private location. Only regular files
were materialized, non-executable; archived links were validated against a
virtual package root and recorded, never followed into the host or created.
The operation stayed within **128 MiB / 2,048 entries / 300 seconds**.
Its summary recorded 881 private entries and approximately 36.3 MB before
the final summary, with a 10.708-second acquisition/inspection run.

`readelf` inspected **274 package ELFs**. An independent parent-side
cross-check matched all 274 file hashes and raw-report hashes. The package
report SHA-256 is
`7dee46ec4629d1fbbee9d5ea3e0cb4bc4d21b20821b1dd263ce0904083356a57`.
The important results are:

| Observation | Evidence |
| --- | --- |
| Package `libmvec.so.1` | **Byte-identical** to the existing bundled file: 1,031,720 bytes, SHA-256 `44e31b0cd6dcb6f8147ffc820f51e7140c429a6e0e7724dd00ad0ea4d18f4f51`, Build ID `989e0b199bc8fd7ff63102bbb1389e576135aaf0`. |
| Same-package loader | 240,936 bytes, SHA-256 `41daf7836f7fce21a40ca63a00c5caf21b05a35a76ccc075d86f1716212cdf01`; exports a 928-byte `OBJECT` named `_rtld_global_ro@@GLIBC_PRIVATE`. |
| Same-package glibc components | Provides candidates for all seven previously unbundled names: loader, `libc`, `libdl`, `libm`, `libpthread`, `librt`, and `libutil`. |
| Static version-name bridge | All **343 recorded version-need entries** from the existing 36-ELF VOICEVOX reports have matching version definitions in the selected package/bundled providers. |
| Loader path constants | Contains `/lib/x86_64-linux-gnu/`, `/usr/lib/x86_64-linux-gnu/`, `/lib/`, and `/usr/lib/`. These are observed strings, **not** an executed search-path test. |

Matching version names does not test relocations, private structure accesses,
IFUNC selection, CPU support, `dlopen` branches, or actual loaded paths.
The package's declared dependencies include `libgcc-s1` and `libcrypt1`;
no additional packages were silently acquired. This is not an installed
Ubuntu environment or a completed runtime dependency closure.

### Candidate filesystem view and remaining gates

The concrete candidate is now the pinned package cohort above, retaining the
original bundled `libmvec`, `libgcc_s`, `libstdc++` and entire engine tree.
A separately prepared, read-only service view would expose its loader at
`/lib64/ld-linux-x86-64.so.2` and its glibc files under the package's
`/lib/x86_64-linux-gnu/` layout. It must also expose the existing bundled
`libz.so.1` (SHA-256
`64c206f0146cc58bbddc4f22054436f4ff278f5a554aa3ce6921ddf7e9133370`)
in a confirmed initial default-search directory, before PyInstaller can
set its environment. No host cache, preload configuration, or unreviewed
alternate/hwcaps provider may silently supply those libraries.

This view has **not** been constructed or exercised. A bounded controller
harness inside such a view would qualify only that harness/child experiment,
not the real Worker daemon or installed unit. Loader `--list` diagnostics
and post-reexec process maps do not by themselves observe the first exec's
loaded zlib; that observation needs separately reviewed instrumentation.
Standard Worker readiness includes synthesis and remains a distinct gate;
the exhausted #292 trial budget is not reused.

The candidate is superseded upstream. Its current security-patch status must
be assessed before any deployment decision; selecting a byte-matched ABI
comparison cohort is not a recommendation to deploy an obsolete glibc.
Neither HTTPS metadata/hash matching nor this content inventory verifies a
signed archive chain, uniquely identifies the engine's original build
environment, grants rights, or attests actual loaded code.

```text
candidate_package_pin_match=true
bundled_libmvec_byte_match=true
static_version_name_coverage=343/343
native_execution_qualified=false
elf_loader_resolution_proven=false
firstexec_loaded_mapping=UNKNOWN
additional_voicevox_archive_extractions=0
loader_executions=0
native_engine_executions=0
```

No Nix VOICEVOX package realization, installed-host apply/rollback,
standard-Control E2E, #247 epoch-revocation acceptance, or Issue closure
follows from this static result.

[engine-source]: https://github.com/VOICEVOX/voicevox_engine/tree/c66f534106465c7f3186c031018f6b28dd3c2223
[build]: https://github.com/VOICEVOX/voicevox_engine/blob/c66f534106465c7f3186c031018f6b28dd3c2223/.github/workflows/build-engine.yml
[dependencies]: https://github.com/VOICEVOX/voicevox_engine/blob/c66f534106465c7f3186c031018f6b28dd3c2223/pyproject.toml
[spec]: https://github.com/VOICEVOX/voicevox_engine/blob/c66f534106465c7f3186c031018f6b28dd3c2223/run.spec
[paths]: https://github.com/VOICEVOX/voicevox_engine/blob/c66f534106465c7f3186c031018f6b28dd3c2223/voicevox_engine/utility/path_utility.py
[initializer]: https://github.com/VOICEVOX/voicevox_engine/blob/c66f534106465c7f3186c031018f6b28dd3c2223/voicevox_engine/core/core_initializer.py
[wrapper]: https://github.com/VOICEVOX/voicevox_engine/blob/c66f534106465c7f3186c031018f6b28dd3c2223/voicevox_engine/core/core_wrapper.py
[entry]: https://github.com/VOICEVOX/voicevox_engine/blob/c66f534106465c7f3186c031018f6b28dd3c2223/run.py
[docker]: https://github.com/VOICEVOX/voicevox_engine/blob/c66f534106465c7f3186c031018f6b28dd3c2223/Dockerfile
[bootloader]: https://github.com/pyinstaller/pyinstaller/blob/v6.21.0/bootloader/src/pyi_main.c
[systemd]: https://github.com/systemd/systemd/blob/v261.2/man/systemd.exec.xml#L481-L517
[libc-debug-files]: https://packages.ubuntu.com/jammy-updates/amd64/libc6-dbg/filelist
[libc-debug]: https://packages.ubuntu.com/jammy-updates/libc6-dbg
[libc-download]: https://packages.ubuntu.com/jammy-updates/amd64/libc6/download
[libc-publication-updates]: https://api.launchpad.net/1.0/ubuntu/+archive/primary/+binarypub/249341623
[libc-publication-security]: https://api.launchpad.net/1.0/ubuntu/+archive/primary/+binarypub/249334198
[libc-build]: https://launchpad.net/~ubuntu-security-proposed/+archive/ubuntu/ppa/+build/33569442
[libc-changes]: https://launchpad.net/~ubuntu-security-proposed/+archive/ubuntu/ppa/+build/33569442/+files/glibc_2.35-0ubuntu3.15_amd64.changes
[libc-build-file]: https://launchpad.net/~ubuntu-security-proposed/+archive/ubuntu/ppa/+build/33569442/+files/libc6_2.35-0ubuntu3.15_amd64.deb
