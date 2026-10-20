# Qwen3.8 27B — upstream llama.cpp Coding Leaf on sirius

This package adds an **opt-in, pinned, upstream** `ggml-org/llama.cpp` CUDA runtime to TSUMGI without changing the GA default `#runtime-llama-cpp` or modifying PrismML-only Bonsai support. Qwen3.8 Q4_K_M is a regular GGUF and **does not require PrismML's PQ2_0 fork**.

## Immutable supply-chain pins

| Component | Pin |
|---|---|
| Runtime source | `ggml-org/llama.cpp`, tag `b11430`, commit `8345f333951c661d166b00e6f9362e553768f292` |
| Linux release asset | `llama-b11430-bin-ubuntu-cuda-12.8-x64.tar.gz` |
| Release binary SHA-256 | `8f79f75093e8fb6d9c65e59167511e9547776794191073e488ce81efd5ade723` |
| Tested model source | `unsloth/Qwen3.8-27B-GGUF` revision `4ca720788d1e01f1bff70c033e0d0028fd02e502` |
| Model GGUF | `Qwen3.8-27B-UD-Q4_K_M.gguf` (16,464,440,224 bytes) |
| Model SHA-256 | `322e194ff79741c7baa497c240f677f54b201b0efab44ca8e50f122b39123482` |
| License | upstream llama.cpp MIT; Qwen3.8 model Apache-2.0 as declared by model provider |

The Qwen3.8-specific Nix wrapper enforces reasoning=off and flash-attn=on after the Provider arguments, mirroring the tested native/OpenCode configuration. The Nix package uses the SHA-256-pinned CUDA 12.8 binary release and immutable CUDA runtime libraries from this flake's nixpkgs. It uses the host NVIDIA driver `libcuda.so.1` only via a temporary 0700 shim, rejecting missing, stub, non-root-owned or writable drivers and requiring version-matched, root-owned PTX JIT/NVVM companions, stripping inherited Worker secrets from the child process. CUDA 12.9 SONAME-compatible runtime and cublas from the pinned Nix closure were validated on sirius RTX 3060 SM86; do not claim broad architecture support.

## Installation and rollback

On a generic-systemd host, using the *reviewed candidate Installer*:

```sh
sudo /nix/var/nix/profiles/astrumweaver-installer/bin/astrumweaver-runtime-profile ensure qwen38-llama-cpp
sudo /nix/var/nix/profiles/astrumweaver-installer/bin/astrumweaver-runtime-profile verify package qwen38-llama-cpp
```

The new executable is `/nix/var/nix/profiles/astrumweaver-runtime-qwen38/bin/llama-server`. Its Nix profile is managed separately from default `/nix/var/nix/profiles/astrumweaver-runtime-llama-cpp` and refuses unknown/mixed elements on ensure, upgrade and rollback. `rollback qwen38-llama-cpp` recovers a previous *Qwen38 profile generation*; it does not automatically restore the old Qwen3.6 deployment. Preserve backups of the original `worker.toml`, RuntimeDeployment/ServingDeployment manifests, systemd unit/drop-ins and Control Chat catalog for full service rollback.

Use a matching Provider config pair: executable=/nix/var/nix/profiles/astrumweaver-runtime-qwen38/bin/llama-server and package_reference=qwen38-llama-cpp when invoking the packaged installer. A pre-installed operator-managed site may retain package_reference=/nix/var/nix/profiles/astrumweaver-runtime-qwen38 with that same executable; the candidate profile manager now accepts that exact managed path as an alias (and still rejects unmanaged/mixed elements). Do not mix either Qwen3.8 selector with the default llama-cpp executable/package. Existing generic upstream selections remain unchanged.

**Staging before promotion:** verify the complete Qwen3.8 file bytes against the SHA-256 above, then exercise `--model <verified-gguf> --n-gpu-layers 99 --split-mode layer --tensor-split 1,1 --ctx-size 8192 --flash-attn on --reasoning off --jinja` with `127.0.0.1` binding, explicit dual GPU UUIDs and `--fit off`. Require actual Chat/Tool Call and OpenCode file edits, correct token budgets, consistent GPU residency, bounded shutdown and recovery.

**Activation policy:** do not silently repoint a Qwen3.6 model ID to Qwen3.8. Build an entirely new RuntimeDeploymentSpec / ServingDeploymentDeclaration / ChatGatewayCatalog identity pinned to the actual GGUF SHA, and validate equality of the full deployment/contract/profile revisions before Control restart. The runtime executable should be in the separate immutable Nix profile. On each Worker start, verify the model file SHA-256 and size using a root-owned `ExecStartPre` command or a preflight owned by the reviewed deployment; the upstream general Provider by itself does not authenticate arbitrary GGUF bytes. Stage immutable backups for rollback.

A Control catalog update requires a controlled short Control restart and could interrupt other consumers: check active Jobs before restart, perform a local post-restart health and new-model E2E, and roll back both Control catalog and sirius Worker if any acceptance fails. Keep other GPU Workers running.

Safe rollback after another Coding Leaf was added: the current production Control Chat Catalog may contain unrelated entries such as bonsai2-27b. Never replace the full current catalog with the historical Qwen3.6-only snapshot: doing so would remove valid Bonsai routing. Instead prepare a new versioned rollback catalog by replacing only the qwen3.8-27b profile with the exact preserved qwen3.6-35b-a3b profile, keeping every unrelated current profile byte-for-byte unchanged. Validate schema, unique IDs, immutable deployment/contract revisions and all other Worker registrations before any approved Control restart. Restore the old sirius Worker service/config/Runtime and run authenticated durable Jobs on both the restored Qwen3.6 profile and retained Bonsai profiles. A static reconstruction is not a live recovery rehearsal.

## Measured evidence (sirius)

- Prior pinned PrismML-b10772 experiment: Qwen3.8 Q4 on both 12GiB RTX3060s, 14/14 real OpenCode coding sessions vs existing Qwen3.6 Q4 11/14 in a same-host matched series. Quality and reduced rework favored Qwen3.8, but it was ~2.2x slower in short-task median.
- Independently SHA-256 verified upstream `b11430` assets on sirius. Real upstream native dual-GPU load with Context 8192 passed, GPU usage 7,329MiB / 8,401MiB, text Chat and forced named Tool Call / tool-result roundtrip passed.
- Production cutover is only accepted after running this same model through the actual Nix package and the TSUMGI Control/Worker stack, with preserving the old Qwen3.6 profile and full service rollback.

**Scope:** use `llama-cpp` Provider (runtime flavor `upstream`) with this selected executable. This output does not claim the standard `#runtime-llama-cpp` nixpkgs version works with Qwen3.8; that remains untested. It also does not change Bonsai's PrismML-only PQ2_0 support or resolve #149/#150.
