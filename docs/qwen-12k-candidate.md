# Qwen3.8-27B 12K Context — offline Stage A (TSUMGI #230)

This is a **source-only proposal and safety contract**, not a production
upgrade instruction. Sirius, its GPUs, installed model, systemd units,
Worker/Control configurations, and OpenCode Agents remain on **8K**.
GPU incident observation is independently tracked by TSUMGI #229.

## Capacity evidence (not an allocation guarantee)

Pinned Qwen GGUF metadata: 65 layers, 4 KV heads, key/value head lengths
256/256. Assuming FP16 K and V, its theoretical KV cache costs
65 x 4 x (256 + 256) x 2 = **266,240 bytes per token**, excluding
fragmentation, CUDA graph/compute buffers, and other GPU users.

| Candidate | Native context | Input | Output | Total | Added FP16 KV |
| --- | ---: | ---: | ---: | ---: | ---: |
| Current | 8,192 | 6,144 | 2,048 | 8,192 | 0 |
| Stage A | **12,288** | **10,240** | **2,048** | **12,288** | **1,040 MiB total** |
| Stage B (not authorized) | 16,384 | 14,336 | 2,048 | 16,384 | 2,080 MiB total |

At the November 5 baseline, Sirius GPUs had **4,350** and **3,349 MiB**
free. In a worst-case allocation of the additional 1,040 MiB KV wholly
on the more constrained GPU, idle free memory falls to **2,309 MiB**,
only 261 MiB over the proposed 2 GiB floor. This is **not a peak VRAM
qualification**. Measure each GPU at sustained 12K prefill, keeping
parallel slots at one; require **>=2,048 MiB free per GPU at peak**, no
OOM, CUDA errors, Worker restarts or serving identity drift.

Native tokenizer-only synthetic OpenCode first-main-request counts on
Qwen: OpenAI-oriented plan override **7,841 input tokens**, dedicated
tsumgi-qwen **1,504**. A proposed 10,240 input cap accommodates the
specific synthetic plan request. This does **not** prove long-horizon
planning quality, production source attribution, or Tool correctness.

## Unqualified candidate generator (no GPU access)

The operator tool is tools/qwen_12k_candidate.py. It consumes three
**existing reviewed 8K JSON inputs**: Sirius runtime.manifest, Sirius
serving.manifest, and the complete Control ChatGatewayCatalog. It also
requires a **fourth, independently reviewed Qwen baseline pin**, NOT
generated from the three supplied source files. The pin binds the
approved model artifact SHA-256, SHA-256 of the exact runtime model_ref
UTF-8 string, old deployment revision and qwen3.8-27b logical alias.
This guards against mutually consistent but wrong-model manifests.
The operator must independently establish the model artifact and
reference provenance; merely supplying a self-generated pin is NOT
an attestation of the actual GGUF file bytes.

The pin has schema reviewed-qwen-baseline-pin-v1 and exactly these
JSON properties: schema_version, profile_id, model_artifact_sha256,
model_ref_sha256, deployment_revision. All three hash values have
canonical sha256: plus 64 lowercase hexadecimal digits. Source files
may contain private model paths and deployment details: **do not
commit, publish or share the raw inputs, pin or generated drafts**.

In a trusted isolated checkout with the project Python environment:

    PYTHONPATH=src python3 tools/qwen_12k_candidate.py \
      --runtime /private/8k/runtime.json \
      --serving /private/8k/serving.json \
      --control /private/8k/control-chat-catalog.json \
      --reviewed-pin /private/approved/qwen8k-pin.json

Default mode prints only bounded metadata, with **zero file writes**.
An optional, explicitly requested output directory must be a NEW direct
child of /tmp with a name prefixed tsumgi-qwen12k-:

    PYTHONPATH=src python3 tools/qwen_12k_candidate.py \
      --runtime /private/8k/runtime.json \
      --serving /private/8k/serving.json \
      --control /private/8k/control-chat-catalog.json \
      --reviewed-pin /private/approved/qwen8k-pin.json \
      --output-dir /tmp/tsumgi-qwen12k-local-private-review

The private directory is owner-only (0700), files 0600, and must be
deleted after review. Atomic directory creation, an O_NOFOLLOW directory
file descriptor and exclusive O_NOFOLLOW child file creation protect the
output path against final-component symlink replacement and redirects.
Files are not securely erased by ordinary deletion. The tool emits
intentionally **NON-DEPLOYABLE** artifacts:

- runtime.UNQUALIFIED.json: both provider_config.context_size and
  setup_intent.configuration.context_size are 12,288.
- serving.INVALID.json: proposed 10,240 input, 2,048 output, 12,288
  total, 65,536 request bytes. The new execution-config digest,
  validation evidence and contract revision are **PENDING**, invalid.
- control.INVALID.json: replacement Qwen deployment/contract/profile
  revisions are **PENDING**; unrelated logical model profiles are
  preserved byte-for-byte.
- review-gates.json: input fingerprints and outstanding human/evidence
  checkpoints (not proof of artifact provenance).

Inputs are validated using TSUMGI's real RuntimeDeploymentSpec,
LlamaCppProviderConfig, ServingDeploymentDeclaration and
ChatProfileCatalog. The two INVALID drafts are deliberately rejected
by the real TSUMGI loaders. No new execution digest is invented from a
local path and the old validation-evidence digest is never reused.
Even successful generation does **not** authorize deployment.

The tool requires the precise 8K Qwen profile, matched immutable
identities, two-GPU runtime topology, correct setup intent, matching
8K limits and an unambiguous catalog. It rejects incompatible source
inputs or shared Qwen deployment aliases. It uses neither network nor
GPU calls and never modifies any original files.

## Steps for a separate, approved maintenance canary

1. Complete the already scheduled 24-hour GPU incident observation.
   Verify 5/5 Workers Ready and Sirius CUDA stable. Preserve existing
   8K manifests, hashes, Worker/Control serving bindings, Nix generations
   and current OpenCode provider settings; ensure no nonterminal Qwen
   Jobs before any source-revision cutover.
2. Independently review exact 12K runtime execution config and compute
   a **new execution_config_sha256** excluding paths and secrets. Do
   not invent digests from partial path/config excerpts.
3. During a **separately authorized maintenance window**, drain Sirius,
   stop its native Qwen process under the approved lifecycle controls,
   and launch the same pinned model at native 12K. Do **not** assume a
   second concurrent 27B server fits in the two 12 GiB GPUs.
4. At runtime, measure each GPU's idle and peak VRAM during prolonged
   prefill and Tool use. Validate tokenizer counting of the synthetic
   7,841-token first-main request, bounded real Chat, streaming,
   Tool Call/Result, over-limit fail-closed, cancellation, CUDA
   initialization and restart-free operation. Require **>=2 GiB free
   per GPU at peak**. If not met, restore the 8K runtime immediately.
5. Only after real evidence, calculate new DeploymentIdentity,
   ServingContract and LogicalServingProfile revisions with the
   existing canonical classes. Worker readiness and the Control
   catalog must agree on exactly that revision. Do not migrate
   admitted old Jobs or reuse old evidence.
6. Once new Worker/Control serving is consistent, update the
   OpenCode TSUMGI model limit.context to 12,288 and preserve
   output 2,048. The canonical OpenAI plan Agent, model assignments,
   permissions and explicit no-fallback policy remain unchanged.
7. Test actual public Chat/Tool E2E, observe errors/VRAM/restarts, and
   preserve a documented **reverse-order rollback** of native runtime,
   Worker serving, Control profile and client config. Any failure
   returns **all affected surfaces together** to exact old 8K.
8. Only after successful Stage-A canary and measured headroom consider
   16K in a separate decision. Bonsai 12K is separately tracked in
   TSUMGI #232; the Qwen change cannot silently expand Bonsai.

**Authority boundary:** passing source-only tests does not permit
restarting Sirius, merging code, changing a production manifest,
modifying the hypervisor/Kernel, or writing to the Control DB.


## R2: exact-byte 8K rollback preservation (source-only)

The historical Qwen Runtime generation 2 from #157 is **not present**
on Sirius. The currently running generation 3 and its Nix Store closure
remain available (25 store paths by read-only closure query, 2026-11-05).
The current generation's profile link is not independent protection:
a future profile replacement or garbage collection may make the old
runtime unreachable unless the operator separately pins that closure. A new 12K experiment must therefore preserve and
prove restoration of the **current** 8K configuration, not assume the old
generation-2 package can be recovered.

The companion operator tool,
tools/qwen_8k_rollback_snapshot.py, handles **only an offline snapshot
and disposable byte-for-byte restore rehearsal**. It does not pin Nix
closures, download the GGUF, edit Worker/Control files, switch profiles,
stop processes, restart systemd, or restore production installations.

### Inputs (sensitive: assemble only in a trusted, private directory)

Collect these nine exact 8K source files through already-authorized
read-only access. Control and Sirius files may be copied privately to
one operator machine for offline review, but **do not commit or print
their contents**:

| CLI argument | Exact source |
| --- | --- |
| --runtime | Sirius RuntimeDeploymentSpec JSON |
| --serving | Sirius ServingDeploymentDeclaration JSON |
| --catalog | Entire Control ChatGatewayCatalog JSON (including Bonsai) |
| --reviewed-pin | Independently reviewed original Qwen 8K model/reference/revision pin |
| --worker-toml | Sirius Worker configuration TOML (opaque exact bytes) |
| --worker-service | Sirius systemd Worker unit file (opaque exact bytes) |
| --worker-dropin-runtime | Sirius 30-coding-runtime.conf (opaque exact bytes) |
| --worker-dropin-identity | Sirius 40-qwen38-identity.conf (opaque exact bytes) |
| --control-toml | Control daemon TOML (opaque exact bytes) |

The last five opaque configuration inputs are snapshotted and hashed,
**not semantically audited**. The two drop-ins are the **actual** ones
observed under the active Sirius Worker (2026-11-05): 30-coding-runtime.conf
and 40-qwen38-identity.conf. Compare against the live systemd
DropInPaths listing before every capture; reject or separately protect
any added/changed drop-in. Separate environment/secret files, Nix profile
references and the GGUF itself are **not included** and require separate
inventory, protection and restoration testing.
The reviewed pin is an operator trust boundary and does not rehash
16GB of model bytes or independently sign provenance.

First check without writing:

    PYTHONPATH=src python3 tools/qwen_8k_rollback_snapshot.py check \
      --runtime /private/runtime.json \
      --serving /private/serving.json \
      --catalog /private/control-catalog.json \
      --reviewed-pin /private/reviewed-pin.json \
      --worker-toml /private/worker.toml \
      --worker-service /private/astrumweaver-worker.service \
      --worker-dropin-runtime /private/30-coding-runtime.conf \
      --worker-dropin-identity /private/40-qwen38-identity.conf \
      --control-toml /private/control.toml

After separate operator approval for private backup file creation,
replace check with capture using the same arguments. The tool creates
an unpredictable new /tmp/tsumgi-qwen8k-rollback-* directory with
owner-only permissions and writes the nine exact input byte streams
plus a digest/size receipt. Files are opened without following
final-component symlinks; directory and files use exclusive creation.
Sources are re-read after copying to detect changes in that interval.
Standard deletion **does not securely erase** these contents. /tmp is
not a durable backup across reboots, so an operator-managed durable,
protected copy is required before any live 12K cutover. Do **not**
create a public CI artifact or commit the snapshot to Git.

Then verify the chosen private snapshot:

    PYTHONPATH=src python3 tools/qwen_8k_rollback_snapshot.py verify \
      --snapshot-dir /tmp/tsumgi-qwen8k-rollback-EXAMPLE

    PYTHONPATH=src python3 tools/qwen_8k_rollback_snapshot.py rehearse \
      --snapshot-dir /tmp/tsumgi-qwen8k-rollback-EXAMPLE

verify compares each file with the receipt's size and SHA-256,
checks the required file set and permissions, and loads the baseline
through TSUMGI's actual 8K Serving/Runtime/Control validators.
rehearse also copies those nine files into a temporary directory,
verifies bytes and Qwen identity, and removes that disposable directory.
It **never** writes to /etc, a systemd unit, Nix profile or Control.

**The receipt is not authenticated.** Someone who can rewrite both
files and receipt can make hashes agree. Independent file provenance
review and a protected backup repository remain necessary. A successful
rehearse proves byte-level restoration **only into a sandbox**, not
that a live Worker/Control will restart after replacing those files.

### Mandatory independent rollback gates

- Confirm exact installed Sirius native Qwen generation-3 Nix profile
  and **pin its Store closure against garbage collection** through
  the approved operator mechanism. Nix generation history alone does
  not guarantee the old build remains accessible. Never assume
  generation 2 or its earlier package still exists.
- Independently preserve the approved Qwen model artifact, original
  model SHA-256 receipt, all active systemd drop-ins/environment, current Worker
  unit/config, Runtime/Serving manifests, Control Catalog and
  compatible client limit/Agent settings. Avoid bearer secrets in
  public logs, issues or PR artifacts.
- Record pre-cutover nonterminal Jobs and immutable
  DeploymentIdentity/ServingContract/LogicalProfile revisions.
  **Stop admissions/drain** under a separately authorized maintenance
  procedure before changing any live serving identity.
- Rehearse the **actual** 8K runtime restoration, Worker registration,
  Control catalog consistency, OpenCode limit reconciliation,
  Chat/Tool E2E and rollback under a separately authorized
  service-impacting window. The source-only CLI cannot do that.
- Any 12K candidate with peak free VRAM below 2 GiB per GPU,
  CUDA/OOM/restart, identity mismatch or Chat/Tool failure must abort
  and restore the full affected Worker/Control/client revision set
  to 8K; never silently fall back to Bonsai or another model.

The receipt explicitly records signed_or_authenticated=false,
nix_runtime_closure_backed_up=false and
BACKUP_ONLY_NOT_RESTORED. These are **deliberate negative claims**
and may not be silently waived. **Production remains at 8K.**


## R3: read-only Nix closure availability preflight

The running Sirius Qwen 8K native Nix profile currently points to the
generation-3 store output. A direct, read-only Nix query on November 5
found **25 current requisites, zero missing Store paths and one current
profile root reference**. This is a CURRENT reachability result only:
after an operator switches that profile to a new generation, the
old generation may lose its root and become eligible for garbage collection.
The historical Qwen generation 2 remains unavailable.

The source-only companion preflight
tools/qwen_8k_nix_preflight.py is restricted to the known exact
Sirius runtime profile path and to Nix read-only query operations:

    PYTHONPATH=src python3 tools/qwen_8k_nix_preflight.py --read-only

On a host where that profile is installed, it resolves the current
profile Store output, calls nix-store --query --requisites and
nix-store --query --roots, checks each referenced Store path exists,
rejects empty/duplicated/malformed closure data, and reports counts
without printing Store filenames or model paths. It **does not**
run garbage collection, add or remove Nix GC roots, switch generations,
realise Store outputs, change model files or restart services.

By default the result reports independent_gcroot_verified=false;
the explicit --require-independent-root flag verifies the separately
installed direct root before reporting true. The fields
old_generation_rollback_verified and live_service_restore_verified
remain false. A current profile root is not
an independently preserved rollback root. The script does not
cryptographically attest the runtime model or guarantee that another
user will not change the profile immediately after the check.

Before an authorized 12K maintenance window, the operator must
separately select and protect a stable direct Store root for the
currently accepted **8K generation-3 output**, verify the exact root
still points to that original Store item, establish a durable
configuration/model backup, and complete a real 8K restoration
rehearsal. Those operations are NOT performed by this source tool
or authorized by this PR.

This preflight is intentionally independent from the seven/nine-file
snapshot command: it verifies the native Nix closure existence,
not the completeness of Worker/Control configuration snapshots.


## R4: independent 8K GCroot now installed (2026-11-05)

The operator explicitly prioritized 12K work without waiting for the
separate 24-hour GPU monitoring result. The current accepted Qwen
generation-3 Nix Store output was protected on Sirius by a **new direct
GCroot**, without switching the running profile:

    /nix/var/nix/gcroots/tsumgi-qwen38-8k-pre12k-20261105

The root points at the exact same generation-3 Store profile already
used by the 8K Worker. At installation, 25 closure paths existed and
the count of current Nix root references increased from one to two.
No Nix build, garbage collection or service restart was performed.

To independently re-check the **currently installed** root and full
closure without modifying anything:

    python3 tools/qwen_8k_nix_preflight.py \
      --read-only --require-independent-root

The flag validates root ownership, the **direct literal symlink** to
the independently accepted generation-3 **8K** Store output, trusted
GCroot-parent permissions, and the exact root-to-pinned-Store record in
the read-only Nix roots query. It queries the **pinned old output**,
not the replaceable active profile. Even after the active profile is
switched to 12K, the old 8K root must still be independently verified.
It rejects a missing, indirect, mismatched, duplicate or malformed root. Only when all checks pass does it
report independent_gcroot_verified=true. Without that flag, the tool
continues to report the independent-root status as unverified.

This means the 8K **native Runtime Store closure is currently
protected from Nix garbage collection** independently of the
replaceable runtime profile. It does NOT imply that a privileged
operator cannot remove the GCroot, that GGUF weights or configuration
are in the Nix closure, or that real Worker/Control rollback succeeds.

Separate protected, root-owned **host-local** source backups were
also created under the following private backup basename on Sirius
and Control:

    qwen12k-8k-fallback-20261105-r1

The Sirius snapshot contains 8 exact files (Worker TOML, Runtime and
Serving Manifests, systemd unit, two drop-ins, Worker environment and
Qwen model-hash receipt). Control contains 4 (TOML, complete Qwen+Bonsai
catalog, unit, environment). All 12 were SHA-256 verified against
their current source files and copied into disposable sandbox restore
directories, with 12/12 exact-byte comparisons PASS. Raw source data
are not embedded in the repository, public issue or CI artifacts.

**Remaining boundary:** These backups are host-local rather than
off-host backups, and the model's 16GB GGUF is not copied. The real
service-impacting 8K restore and full 12K native runtime canary still
require a separately bounded maintenance/rollback procedure. The
separate GPU 24-hour monitoring automation is still scheduled but is
not used as a precondition for this source and backup work.

## R5: read-only two-GPU VRAM observation (source-only, no 12K launch)

`tools/qwen_12k_vram_probe.py` is an *optional telemetry probe for an
already approved, active* 12K native canary. This source change **does
not** authorize starting that canary, changing the Runtime, stopping
systemd, or admitting a new logical profile. The probe invokes only the
fixed read-only NVML query `nvidia-smi --query-gpu=uuid,memory.total,memory.free,memory.used --format=csv,noheader,nounits`.
It never sends prompts or Tool requests to the model, changes GPU
settings, writes files or calls Control. Only run it within a separately
approved maintenance window when the exact native 12K runtime is already
active, with an *operator-controlled* known long-prefill stimulus and
the exact two reviewed Sirius GPU UUIDs. Do not use `--phase prefill`
as an assertion that the interval actually overlapped a prefill.

A bounded example, intentionally using redacted placeholders:

    python3 tools/qwen_12k_vram_probe.py --read-only \
      --expected-gpu-uuid GPU-REVIEWED-UUID-0 \
      --expected-gpu-uuid GPU-REVIEWED-UUID-1 \
      --phase prefill --samples 300 --interval-ms 200

The placeholders must be replaced with the **full matching NVIDIA GPU
UUIDs** from a reviewed read-only mapping. The process samples exactly
these two GPUs and rejects extra, missing or reordered-inconsistent
metrics, malformed UUIDs, impossible memory totals, NVML failures and
partial series. Its standard output is a bounded JSON summary with
per-reviewed-GPU minimum free MiB in the supplied UUID order, **without
raw UUIDs or model paths**. It includes **unauthenticated host-clock
start/end Unix timestamps** to aid manual comparison against an
independently recorded prefill request, not to prove temporal overlap.
Exit 0 indicates only the *observed*
VRAM gate was not violated, exit 3 a measured sub-2048 MiB minimum,
and exit 2 a blocked/invalid observation. If either GPU reaches only
2047 MiB free, the observed VRAM gate fails.

An idle-only run deliberately reports `NOT_APPLICABLE_IDLE_ONLY`.
Even a `PASS` prefill run is **NOT production qualification**:
sampling may have missed a short CUDA allocation peak, the declared
prefill interval is not independently attested, and the probe cannot
verify actual 12K context, pinned model/build identity, 7,841-token
tokenizer input, real Chat/Tool outcomes, strict over-limit rejection,
systemd restarts, queued Job fencing, Control contract revisions, or
actual 8K restoration. These mandatory acceptance gates must be
reviewed **separately** with genuine live canary evidence and the
operator-approved reverse-order rollback.
