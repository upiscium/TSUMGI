# #179 / #180: explicit Parakeet Japanese STT Worker

This source candidate adds an opt-in standard runtime/serving manifest path.
It does not install dependencies, acquire weights, enable a service, or qualify
a package. The old `runtime.parakeet_trial` switch is not supported. Existing
trial source and native results are not acceptance of this new path.

## Frozen selection and configuration

The selected model is `nvidia/parakeet-tdt_ctc-0.6b-ja` revision
`44edb27eea9317daf89333e75eb830db4b1cc298`, CPython 3.11.14, NeMo 2.4.0,
torch/torchaudio 2.7.1+cu126, FP32 TDT `greedy_batch`. No CPU/model/decoder
fallback is allowed. The Worker itself remains Python 3.12; native dependencies
load only in the owned subprocess.

Use ordinary `[runtime].manifest` and `[serving].manifest`, provider
`parakeet-ja`, and the exact `DEMAND` exported by `parakeet_manifest`.
Required configuration:

- Literal `enabled=true` (omission is not opt-in).
- Canonical `model_path` to the pinned `.nemo`, `interpreter_path` to the copied
  pinned venv `bin/python`, and `dependency_lock_path` to the reviewed lock.
- Explicit `base_python_root` for the separately staged CPython 3.11.14 prefix.
  Its `bin/python3.11` must match the interpreter pin; `pyvenv.cfg` must name its
  `bin` as `home`, declare version `3.11.14`, and disable system site-packages.
- A separately provisioned, empty, non-root-owned `0700` `private_home`.
  It is the child's working, HOME and temporary directory. No implicit reuse,
  erasure or HOME rotation is performed.
- Exactly the dedicated Worker's `gpu_uuid`, one GPU, one slot, only
  `audio.transcribe`, GPU preflight enabled, loopback Control and health ports.
- `rights_review_sha256` and `validation_evidence_sha256` from separately
  approved records. A syntactically valid checksum is not a rights grant.

The declared capacity floor is 11,264 MiB, conservatively retaining the
previous trial GPU's capacity. It is not a measured minimum, a reservation of
free VRAM, or qualification of every GPU with that capacity. Existing GPU
isolation and exact-device preflight remain mandatory. A native load/OOM
failure does not allow registration.

Generate the declaration with
`ParakeetManifestConfig(**provider_config).serving_declaration().to_dict()`.
Its identity binds model/runtime/lock/code/decoder and review receipts, not
machine-local paths or physical GPU UUIDs. Those remain private deployment
inputs and are checked against the Worker. Preserve inputs and installed
dependencies against concurrent modification: a matching lock file and package
version do not prove every installed dependency byte.

Before `Popen`, a bounded read-only scan checks both the venv and declared base
Python tree (at most 200,000 entries, depth 32, 30 seconds per scan). All entries
and ancestors must be root/Worker-owned and not group/world-writable; only
root-owned sticky temporary-directory ancestors are exempt. No devices/FIFOs,
foreign filesystem entries, set-ID entries, or escaping symlinks are accepted.
Relative symlinks must stay inside their own protected tree. CUDA search
directories must exist under that venv and cannot redirect elsewhere.

This lane accepts CPython `venv` configuration, not alternate launchers or
system site-packages. `.pth` files may contain comments and existing in-tree
relative directories only: executable import hooks and external paths are
rejected. Provisioning must stage a compatible reviewed installation separately;
the Worker never removes hooks, repairs permissions, or downloads dependencies.
These checks prevent writable/external startup paths, but **do not attest every
installed package byte against the lock**. Trusted operator/Worker-UID mutation
and the host dynamic loader/driver remain outside this check; protect those
through the separately approved immutable installation and OS sandbox. No
installed-package or production acceptance is established by the scan.

## Safety and evidence boundary

The parent verifies inputs before launching the fixed child. A bounded private
bootstrap handshake follows actual native loading, before READY and Worker
registration. Requests are Japanese, JSON output, canonical mono 16 kHz PCM16
WAV, at most 30 seconds / 1 MiB; transcript UTF-8 is capped at 16 KiB.
Input audio/text, tokens and private paths are not included in public errors.
No Control credential is passed to the child. Offline environment variables
are not an OS-level network sandbox; deployment must separately restrict egress.

Cancellation/timeout revokes result delivery and terminates the owned process.
A failed/cancelled instance is not reused or automatically restarted.
Readiness and health do not authorize another GPU or another provider.
The private child IPC epoch is not the Control's Worker runtime-instance epoch.
Synthetic same-attempt/registered-epoch checks do not establish #247's real
PostgreSQL old-epoch revocation, reconnect, or publication-denial acceptance.

Tests use tiny fake inputs and mock/native-boundary or standard-library
children only. They do not establish Japanese quality, native latency, native
GPU cancellation, PostgreSQL acceptance, standard Control installation,
NixOS/generic-systemd packaging, rollback or production acceptance.
Nix provider selection and an applicable Audio SetupPlan remain separate work.
Any new native run requires a separately approved scope and inference budget.
