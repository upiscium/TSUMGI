# #231 / B6d — VOICEVOX provider-local source assembly

Status: source-only declaration review and an inert, disposable construction
of B5b/B6b/B6c components. This is NOT a RuntimeProvider, SetupPlan,
operator authorization, real loaded-model attestation, or deployable Worker.
Parent #180; related #215 / #176 / #181.

## Contract and static checks

VoicevoxSourceAssemblyIntent binds the exact logical Gateway profile, immutable
ServingDeclaration, synthesis-only CPU WorkerSpec, SetupHostSnapshot,
VoicevoxOwnedRuntimeSpec, VoicevoxLinuxLaunchSpec, and VoicevoxHttpConfig.

An explicitly disabled review ignores all candidate values and returns an
inert disabled record. Disabled mode never accesses the filesystem, opens
sockets, launches subprocesses, calls Control, stages media or downloads files.

Enabled B6a review binds profile, serving/deployment revision, CPU-only Worker,
exact reviewed voice aliases, speaker UUID, global numeric style, semantic
revision, and provider identity. B6d additionally requires:

- One exact executable path in runtime and launch spec, operator-pinned args
  and SHA-256-pinned auxiliary script whenever referenced.
- Exact canonical loopback endpoint and adapter between runtime and HTTP
  transport, explicit fixed CLI port token or --port=N, and no contradictory
  --port, --host, --listen, or wildcard-address argument.
- Launch stop budget bounded by the runtime stop timeout and HTTP request
  timeout bounded by the adapter synthesis deadline.
- No GPU-backed VOICEVOX Worker until separately qualified GPU ownership.
- No Style-Bert-VITS2 substitution or user-provided backend URL or model
  selection, no automatic download, no implicit fallback.

An assembly_digest hashes the review digest and static launch/HTTP config,
including private path and fixed argv without displaying them. It is not
an HMAC, signature, actual file digest, license proof, or authorization.
Captured mutable inputs are re-reviewed before disposable construction to
reject changes. Review serialization allowlists only public-safe status
fields, selected logical profile/provider IDs and hashes. All enabled reviews
are review_only_blocked and can_apply=False, preserving upstream B6a blockers.

The explicitly named compose_disposable_source_runtime method creates the
LinuxVoicevoxOwnedProcessControl, VoicevoxLoopbackHttpApi, and
VoicevoxOwnedManagedRuntime objects without starting them or doing filesystem,
network, Worker/Control or database I/O. It is a disposable source test helper
only. It is not registered or exported as a shared RuntimeProvider.

## Synthetic evidence and real-world limits

Disposable tests cover disabled-mode noninspection, mismatched profiles,
provider identity, Worker/host and CPU-only resource contract, endpoint,
port and argv mismatch, model/auxiliary SHA pins, no leaked private values,
review drift, and Style-Bert substitution rejection. A synthetic local Linux
HTTP subprocess with actual PID/socket demonstrates STOPPED → READY →
Japanese-text-to-fake-WAV ArtifactRef, then bounded cleanup. Negative tests
also cover occupied foreign listener, wrong model artifact, process UID,
engine version drift and unavailable admission. The test server is NOT VOICEVOX.

The executable RuntimeProvider registry, shared SetupPlan, Worker daemon,
NixOS/systemd packages, Control API, PostgreSQL and deployed hosts are
unchanged. The existing Worker managed-only and coherent READY pre-registration
guards still apply; a static manifest, a digest, mock server and declared
license ID cannot qualify a real engine.

The Linux procfs checks have known TOCTOU and escaped process-descendant
limits, and no pidfd/cgroup/LSM/namespace guarantees. Native VOICEVOX
/audio_query includes input text in URL parameters: native engine, proxy,
access and tracing logs MUST be separately verified disabled/redacted.
Model-loaded identity, speaker rights/attribution, ABI and real quality
are unqualified.

## Operator checklist for a future, separately authorized trial

1. Select exact binary/auxiliary code SHA-256, complete fixed argv, local port,
   dependency/package revisions, platform/ABI, native logging policy and
   isolated disposable host. No automatic model installation or download.
2. Record exact loaded model bytes, voice UUID/style inventory, licensing and
   speaker credits, including the human rights-review evidence and immutable
   serving/adapter/model revisions.
3. Review CPU/GPU residency, Worker resource ownership, VRAM and process
   isolation; explicitly prohibit GPU sharing or silent backend selection.
4. Review native input-text privacy, job/attempt/lease fences, artifact TTL,
   cancellation, PID/descendant cleanup, failure recovery and rollback.
5. Derive a reviewed reversible SetupPlan from existing #24 and #31 for NixOS
   and generic systemd. Do not mutate host services outside that framework.
   An isolated real-engine smoke needs a separate G-AUDIO-OPS grant.

Sequence: B6d Source-only Draft PR → separate Human Ready/Merge → isolated
native binary/model/voice/privacy/rights qualification → separately authorized
RuntimeProvider/SetupPlan integration in #180 → real #181 E2E acceptance →
operator-authorized release and deployment. Style-Bert-VITS2 #216 stays
independent. Keep #175/#176/#177/#180/#181/#215/#216 open.

STOP: no Registry enablement, can_apply change, engine/model download,
host/GPU/service/DB mutation, release, deployment, or automatic rollout.
