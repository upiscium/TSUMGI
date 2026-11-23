# #180: explicit owned VOICEVOX CPU Worker (local integration)

The standard `runtime.manifest` loader can construct one pre-provisioned
VOICEVOX backend. No audio engine is selected by default. There is no
`runtime.voicevox_trial` switch, automatic install, SetupPlan, attach to a
user-managed process, GPU path, STT, or fallback. This is separate from #247.

A later [offline bundle/staging candidate](voicevox-offline-bundle-staging.md)
can prepare review files via a separate `goal="stage"` plan. It does not enable
this provider's ordinary installation/activation SetupPlan or qualify its ABI.

## Two manifests and one slot

The Worker must be unprivileged, on Linux x86-64, dedicated to
`audio.synthesize`, with zero GPUs, `max_concurrency = 1`, a loopback health
listener and a `http://127.0.0.1:<port>` Control URL. Keep the existing
independent Worker bearer credential out of both manifests.

1. Configure `[runtime].manifest` and `[serving].manifest` in the normal
   Worker TOML, without an `executor.factory`. The runtime JSON uses schema
   `v1`, provider `voicevox`, and exactly the following demand:

   ```json
   {"model":{"model_ref":"model/0.vvm","model_format":"vvm","topology":"dense"},
    "residency_policy":"cpu_gpu_hybrid","gpu_topology":"none"}
   ```

2. The exact `provider_config` fields are `enabled` (literal `true`),
   `engine_root`, `private_home`, `port` (1024–65535), `archive_sha256`,
   `rights_review_sha256` and `validation_evidence_sha256`. No setup intent,
   command line, URL, model selector or arbitrary environment is accepted.
   The root is the extracted, independently verified official Linux CPU x64
   0.26.2 bundle, containing `run` and `model/0.vvm`. Pin the archive to
   `sha256:483633de02700cd918731538543a22ce895877aeb09450a990ff38af6e4c26c0`.
   The two review digests refer to real operator-reviewed records: dummy
   hashes are for tests only. Declaring hashes neither verifies the entire
   extracted bundle nor grants voice rights.
3. From the same reviewed TSUMGI package, construct the expected serving
   document using `VoicevoxProviderConfig(**provider_config).serving_declaration().to_dict()`
   (`tsumgi.runtime.providers.voicevox_manifest`). Review it against
   the Gateway catalog **before** starting anything. The declaration helper
   reads the installed adapter source bundle but starts no service. The
   deployment and contract bind the executable, VVM, adapter bundle, port,
   execution policy, reviewed evidence, voice semantics and input limits;
   neither host paths nor tokens are part of the public identity. A change
   of adapter bundle requires a new reviewed serving revision. This is not
   the old local trial's identity.
4. Use an independently provisioned **empty**, private `0700` HOME owned by
   the engine's service user, separate from the engine tree. The launcher
   checks the path, does not inherit the operator's HOME/environment, and
   never creates, empties or chmods this directory. If engine state remains,
   provision a fresh HOME before the next start rather than reusing it. The
   entire extracted engine tree, its dependencies and the Worker environment
   must be trusted and protected against concurrent modification.

Only the fixed owned loopback process is used. It verifies the executable
and VVM before launch; READY requires matching process/listener ownership,
version `0.26.2`, and exactly the reviewed `ずんだもん／ノーマル`,
speaker UUID `388f246b-8c41-4ac1-8e2d-5d79f3ff56d9`, global style `3`,
type `streaming_talk`. The public alias is `zundamon-normal`, credit
`VOICEVOX:ずんだもん`. Neither the manifest nor a prior readiness observation
replaces the independent health check immediately before registration.

## Evidence boundary

`tests/test_voicevox_manifest_worker.py` runs the real TOML/JSON loaders,
provider selection, managed lifecycle, supervisor and standard Worker. It
holds startup and catalog observation open, counts registration requests,
tests internally consistent but mismatched declarations, drift at the final
supervisor check, and reads back a successful registration via in-process
ASGI Control. The native process, byte observations and engine responses are
synthetic; Control storage here is in memory. This is not a native synthesis
test for the new manifest path, a package/ABI or service qualification,
a production deployment, or proof that all audio providers coexist.
