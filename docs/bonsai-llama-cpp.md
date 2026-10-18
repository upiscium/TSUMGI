# Bonsai 2 — officially supported, opt-in native backend (post-v0.1.0)

TSUMGI supports **only** the explicitly pinned **PrismML Ternary-Bonsai-2-27B-PQ2_0 GGUF** using a separate PrismML-native llama.cpp runtime. This does **not** replace the default upstream `llama-cpp` RuntimeProvider or change already-installed GA Workers.

## Model and source chain

| Element | Immutable acceptance pin |
| --- | --- |
| Native backend | PrismML `llama.cpp`, `prism-b10772-e8fd209`, source `e8fd209c975c80aa7fb36a8423571ff3b1b7e955` |
| Linux CUDA binary | `llama-prism-b10772-e8fd209-bin-linux-cuda-12.4-x64.tar.gz` |
| Binary archive SHA-256 | `f5a46c4edbbc7533e2cac96ce9191204c7a03655eaf74d79639469f50c8d998c` |
| Model source | `prism-ml/Ternary-Bonsai-2-27B-gguf`, revision `b072e1d3b35a0a630cece372c2127528e0994386` |
| Approved model filename | `Ternary-Bonsai-2-27B-PQ2_0.gguf` |
| Model size | 7,206,168,928 bytes (~6,873 MiB) |
| Model SHA-256 | `3907dc1658db1f78a9826bf8d5bcb8dc65db0d466388937af57f2294fae62ec1` |
| Model metadata license | Apache-2.0 |

The runtime release and model are independent third-party sources. The GitHub archive is fixed-output-hashed at Nix build time. TSUMGI **does not automatically download a model in first-run setup**: preparation is `reference_only`. Verify the entire model with `sha256sum` and retain its operator-controlled local path before any activation. Filename validation is **not** a substitute for checking model contents or their SHA-256; a model manifest/reference audit must bind the expected hash.

Only **PQ2_0 of Bonsai 2 27B** is qualified. The 6873 MiB model-size estimate intentionally rounds the exact 7,206,168,928-byte weight file **up** to the next MiB; this is a conservative capacity floor. PTQ1_0, Q2_0, later Bonsai, other sizes, generic TQ/PQ formats, embeddings and native decision heads have no support claim under this profile.

## Separation and installation

The TSUMGI flake contains two independent outputs:

- `#runtime-llama-cpp`: original, unmodified nixpkgs upstream default (unaffected).
- `#runtime-bonsai-llama-cpp`: pinned PrismML native binary with a distinct driver/secret boundary.

On supported `x86_64-linux` generic-systemd hosts, the *same candidate's* installed TSUMGI Installer provides the runtime profile manager:

```bash
sudo /nix/var/nix/profiles/astrumweaver-installer/bin/astrumweaver-runtime-profile \
  ensure bonsai-llama-cpp
sudo /nix/var/nix/profiles/astrumweaver-installer/bin/astrumweaver-runtime-profile \
  verify package bonsai-llama-cpp
```

The default `llama-cpp` profile remains `/nix/var/nix/profiles/astrumweaver-runtime-llama-cpp`; the separate Bonsai profile is **`/nix/var/nix/profiles/astrumweaver-runtime-bonsai`**. The Bonsai executable is **`/nix/var/nix/profiles/astrumweaver-runtime-bonsai/bin/llama-server`**. Profile upgrades require the exact reviewed Installer candidate and refuse unmanaged or mixed profiles. Rollback is separately available:

```bash
sudo /nix/var/nix/profiles/astrumweaver-installer/bin/astrumweaver-runtime-profile \
  rollback bonsai-llama-cpp
```

This does *not* change an active Worker automatically. Select the normal `llama-cpp` Provider, with `runtime_flavor=bonsai2-pq2` in its customized runtime options. TSUMGI then binds the pinned executable and package reference, enables reviewed Jinja Tool Call handling, and enforces the exact supported GGUF basename and dense topology. For the first 11GB-class single-GPU deployment, select `model_format=gguf`, `gpu_topology=single_gpu`, estimated model size **at least 6873 MiB**, and `vram_only` only when the conservative allocation budget and current free VRAM allow it. An unrecognized runtime flavor, package, executable, model filename, topology or understated size is rejected before setup. Runtime startup and native allocation errors remain fail-closed under the existing `llama-cpp` contract.

**NixOS:** the underlying package output can be used with an explicit pinned flake reference, but the generic-systemd installer/profile layout is not a substitute for a reviewed NixOS-specific service/module deployment. Do not copy the generic-systemd executable path into NixOS unless that profile is separately installed and verified. The standard NixOS package-expression wizard does not create a Bonsai profile automatically.

## Native security and lifecycle

The derived `llama-server` wrapper uses **a private 0700 host NVIDIA driver shim with `libcuda.so.1`, matching `libnvidia-ptxjitcompiler.so.1` and `libnvidia-nvvm.so.4`** plus immutable Nix runtime libraries, refuses CUDA stub libraries and fails if no real driver is available. `env -i` prevents accidental direct environment inheritance of Worker/Control secrets, but is not a confidentiality boundary against a compromised same-UID native child; it passes only minimal environment values (selected GPU UUIDs, library paths, basic locale and temporary-dir controls). Host-wide physical GPU exclusivity is infrastructure responsibility; logical GPU ownership and preflight/fail-closed remain TSUMGI responsibilities.

These protections do **not** establish hostile same-UID isolation or secure multi-tenant localhost service access. That stricter trust boundary remains deferred in [#149](https://github.com/upiscium/TSUMGI/issues/149). Release 0.1.0 service behavior, credentials, worker configuration and native model content are not modified by building this package.

## Evidence and known limits

[Feature Request #151](https://github.com/upiscium/TSUMGI/issues/151) recorded the actual deneb GTX1080Ti experiment: Bonsai 2 PQ2_0 14/14 real OpenCode scratch sessions and 129/129 bounded assertions, direct CUDA generation and Tool Call, a real ~6.9k-token long input, and GA Python `LlamaCppSubprocessController` native startup, request interruption, restart and resource cleanup. These are **narrow Coding Leaf trials**, not a representative software-engineering benchmark or a published quality SLA. Runtime GPU support has been tested on SM61, not the entire NVIDIA matrix.

Before promoting this runtime to a live Worker, independently qualify an **actual durable Control→PostgreSQL→Worker→Job→OpenCode** acceptance with reservation/fencing, cancellation, fail-closed VRAM probe and rollback, using a disposable configuration and measured resource headroom. Native subprocess API checks alone do **not** certify durable Job behavior. Upgrading, stopping, installing or redirecting the existing five GA Workers requires separate operator approval.

## Troubleshooting

- `bonsai-specialized-runtime-required`: the selected backend is still the upstream `llama-cpp`; explicitly choose `bonsai2-pq2` and its separate pinned profile.
- `bonsai-model-not-qualified`: the GGUF basename is outside the one qualified model; do not guess an untested packing.
- `bonsai-model-size-understated`: review the actual immutable model file and set a correct conservative capacity estimate.
- `cuda_driver_library_unavailable` or `cuda_stub_is_not_a_driver`: verify native host driver exposure, not a CUDA SDK stub. Do not patch global dynamic linker paths.
- `invalid-profile` or `candidate-mismatch`: keep the existing profile intact, inspect Nix profile provenance and review the exact installer candidate. Do not `nix profile upgrade` an unmanaged path or silently fall back to upstream.

## Verified safeguards for the Coding Leaf

The Bonsai-specific provider validates the entire GGUF by byte count and SHA-256 against the qualified PQ2_0 artifact before every owned native child start. Renaming an unrelated file to the supported basename is insufficient to pass. This check protects the trusted-LXC deployment workflow; it is not a hostile same-UID TOCTOU proof.

Real Control-to-Worker-to-OpenCode trials found that a 4,096-token context can exhaust the generation budget and fail to complete. The supported Coding Leaf configuration therefore requires at least **8,192 context tokens**, Jinja tool templates, full-GPU offload without automatic fitting, and the observed PrismML arguments **--reasoning off** and **--flash-attn on**. The initial 8,192-token Control E2E passed a single-file edit; multi-file E2E completion is a separate acceptance gate and must not be inferred from native direct-CLI successes.
