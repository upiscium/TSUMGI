# #180: opt-in NixOS audio Worker manifest wiring

This source candidate wires **pre-provisioned** VOICEVOX, JVNV Style-Bert-VITS2,
and Parakeet through the existing NixOS Worker module. It is not native runtime
packaging, an applicable Audio SetupPlan, a model download, or installed-host
acceptance. The local Parakeet candidate must be included in the selected Worker
package; merely selecting its name does not provide its implementation.

## Explicit configuration

`services.tsumgi.worker.runtime.enable` remains **false by default**.
An audio provider additionally requires literal `runtime.providerConfig.enabled
= true`, the provider's exact execution demand, and a reviewed
`serving.declaration` (the JSON value produced by the matching provider's
`serving_declaration().to_dict()`). The module writes the normal runtime and
serving JSON manifests and connects both to the generated Worker TOML.
It does not create a catalog entry, fabricate review receipts, or infer pins.

Select exactly one of `voicevox`, `style-bert-vits2`, or `parakeet-ja`.
TTS requires a CPU-only single-slot `audio.synthesize` Worker; Parakeet requires
only `audio.transcribe`, one exact canonical GPU UUID, at least 11,264 MiB of
declared total/single-GPU capacity, and the existing device-cgroup isolation
mapping/preflight. The capacity floor is not a measured minimum or qualification
of every GPU. Additional device entries may only name the shared NVIDIA
control/UVM nodes, not another physical GPU. Existing Python compatibility,
asset, serving-identity and native
readiness checks still run before registration.

Provide a dedicated static user (not `root` or the shared `tsumgi` user),
a protected plain `/etc/...` or `/run/...` `environmentFile`, and loopback
Control/health endpoints. User/group values must be names matching
`[a-z_][a-z0-9_-]{0,31}`, not numeric IDs; explicit UID/GID zero is also rejected.
For this bounded lane use only the typed options:
`settings`, `extraArgs`, `executorSettings`, and `runtime.packages` must be empty.
Non-audio package selection and existing untyped settings are unchanged.

All three provider configurations name a separately provisioned, empty `0700`
`private_home` beneath `/var/lib/tsumgi-audio/`. The unit uses that exact
working directory and writable path, `UMask=0077`, no shared `StateDirectory`,
no automatic user-home creation, `Restart=no`, and `KillMode=control-group`.
The module does **not** create, rotate, erase, or repair this private directory
or existing engine/model/dependency trees. Assets must be reachable through
the existing `ProtectHome`/`ProtectSystem` restrictions. Staging and ownership
are separately reviewed operations, not Nix evaluation side effects.

Nix store configuration is world-readable: provider paths and receipts must be
non-secret; never embed tokens, recordings, transcripts, or private report
contents. The environment file's contents are never opened by this module.
Its live ownership/mode, UID exclusivity, assets and host driver closure remain
operator prerequisites. Unit-path assertions do not prove a live sandbox.

## Mismatches and disabled mode

Evaluation rejects mismatched provider/capability/schema/demand, omitted
enablement or serving declaration, unsafe home/environment references,
non-dedicated resources, untyped config overrides, and conflicting effective
service identity/path/restart settings. Nix checks structural agreement;
the Python manifest loaders/provider factory independently verify the complete
canonical declarations and code/model/runtime/semantic/evidence bindings.

A retained audio provider configuration and serving declaration with
`runtime.enable=false` neither generate a serving manifest nor add runtime
packages, private state, or audio unit settings. A non-audio executor remains
the configured execution path. Advertising audio while disabling its managed
runtime is rejected rather than silently falling back.

## Evidence and remaining gates

`system.build.tsumgiAudioWorkerConfig` exposes the three configuration
artifacts for review, with `.reviewed` containing their actual serialization
inputs and `.artifacts` the exact linkFarm entries. Evaluation checks the
Worker's configured paths against those entries and the JSON derivation
outputs against the expected serialization; matching filenames alone do not
suffice. It does not build or read the resulting artifact bytes.
`nix/tests/audio-worker-module.nix` evaluates real NixOS configurations
for positive, negative, disabled and non-audio cases. The separate
`tools/check_audio_worker_nixos.py` performs offline Nix evaluation using real
Python-generated declarations and passes the evaluated manifests back through
the real Python loaders/factories without starting a runtime.

These are configuration/source tests, not native inference or an installation
trial. Full standard Control E2E, native engine/dependency packaging, supported
NixOS and generic-systemd apply/rollback, and real old-epoch rejection remain
separate #180/#181/#247 acceptance gates. Applying a NixOS generation can start
the Worker and consume readiness/inference budget, so activation still needs a
separately bounded approval. No such activation is authorized by this note.

### Local verification

With the repository's Python test environment on `PATH`, the evaluation-only
commands are:

```sh
nix eval --offline --no-update-lock-file --no-write-lock-file \
  --option allow-import-from-derivation false --json "path:$PWD#checks.x86_64-linux" \
  --apply 'checks: builtins.mapAttrs (_: check: check.drvPath) {
    inherit (checks) audio-worker-module audio-control-module runtime-module-eval module-eval;
  }'
PYTHONPATH=src python -B tools/check_audio_worker_nixos.py
env -u TSUMGI_TEST_DATABASE_URL PYTHONPATH=src \
  python -B -m pytest -q -p no:cacheprovider
```

The local candidate based on `c2f3c678` passed the four Nix evaluations and all
three Python/Nix manifest-parity cases. The Python regression run reported
**3,540 passed, 133 skipped, 8 subtests passed**, with one existing Starlette
deprecation warning. The skipped/database-dependent paths were not accepted by
this run. No package realization, native inference, activation, or rollout was
performed. The parity CLI remains a separate check; the ordinary pytest tests
mock Nix execution and do not substitute for invoking it.
