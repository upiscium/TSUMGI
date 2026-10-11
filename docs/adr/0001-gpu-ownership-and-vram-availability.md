# ADR 0001 — Worker GPU Ownership and VRAM Availability Boundaries

- **Status:** Accepted (operator decision 2026-10-11); implementation tracked by #125
- **Decision date:** 2026-10-11
- **Scope:** TSUMGI Worker/Control/RuntimeProvider contracts for existing GPU nodes
- **References:** #1, #2, #3, #22, #31, #57, #91, #122, #123, #124, #125

## Context

TSUMGI is a deployment-agnostic compute fabric, not an infrastructure
provisioner. The foundational architecture (#1/#2) assigns VM/LXC provisioning,
PCI passthrough, device mapping, external workload placement and host drivers to
the infrastructure/operator layer. A Worker is nevertheless the smallest
*scheduler-visible exclusive compute unit*. These statements address
**different scopes of exclusivity**.

The GPU-subset deployment work (#31/#57) also established a separate, stricter
in-Worker device selection contract: if an operator selects one GPU from a
larger GPU inventory visible to the Worker, TSUMGI must actually prove that
unselected physical devices are inaccessible to that Worker service, or reject
the subset configuration. Mere `DevicePolicy` text and
`CUDA_VISIBLE_DEVICES` are not access-denial evidence.

Issue #124 proposed proving that **no external process or LXC or hypervisor
identity** can open the assigned physical GPU. That is not a capability of
TSUMGI's Control or Worker, and was not within the initial project boundary.
Such proof must not be silently elevated to a universal TSUMGI core or feature
merge requirement.

However, external processes can consume memory on an assigned GPU. Comparing a
runtime's declared VRAM requirement only to a physical GPU's *total* capacity
does not demonstrate that the required memory is currently available.

## Decision

### 1. Three distinct ownership boundaries

1. **Logical TSUMGI ownership (TSUMGI MUST):**
   - Control prevents the same GPU UUID from being simultaneously reserved by
     incompatible TSUMGI Workers while ONLINE/DRAINING or until outstanding
     fenced attempts release their ownership.
   - Worker advertises its exact configured stable GPU identities and resource
     capacities; lease, claim, drain, retry and recovery remain authoritative.
   - TSUMGI does **not** thereby reserve the physical device against unregistered
     processes, a different Control deployment or host administrators.

2. **Worker-accessible GPU set (TSUMGI MUST):**
   - In `exact-visible` mode, configured GPU UUIDs must match the observed raw
     visible UUID set as in #57. A 1-visible/1-selected guest is supported
     without requiring proof about every unrelated process outside that guest.
   - In explicit `isolated-access` GPU-*subset* mode, TSUMGI must verify the
     reviewed mapping and enforce actual selected-access/unselected-denial
     inside the Worker service context. If the environment cannot enforce the
     subset, deployment fails closed. The pre-existing #57 tests and invariant
     **remain in force**.
   - TSUMGI must not silently replace an exact GPU choice with another GPU.

3. **Physical host-wide exclusion (Infrastructure/operator MUST provide when
   required by site policy):**
   - Mapping or excluding unrelated LXC/VM/host GPU consumers, Proxmox device
     cgroups, driver installation, VFIO and passthrough remain external.
   - TSUMGI must not mutate those settings or demand hypervisor credentials
     merely to operate on a pre-provisioned compatible node.
   - A validated infrastructure guarantee may be declared in site-specific
     operator acceptance, but TSUMGI cannot independently certify every
     external workload from guest-local observations.
   - A stricter untrusted multi-tenant deployment may require host isolation as
     **operator policy**, not an automatic requirement for all TSUMGI Workers.

### 2. Fail-closed VRAM availability for resident workloads

A runtime requiring fully resident GPU memory must have a reviewed
conservative **required VRAM budget** that accounts for model weights,
KV/context buffers, scratch/CUDA runtime overhead and a safety margin. The
configured static Worker VRAM capacity is only an upper bound, **not a free
memory or hard reservation guarantee**.

For opt-in VRAM-only runtimes:

- Identify the exact configured GPU UUID and query **fresh** device-local free
  memory at runtime start, immediately before starting owned native model
  subprocesses. Do not substitute the free memory of an arbitrary first GPU.
- Reject the start **before either child runs** when the measured free memory is
  below the reviewed required budget; reject malformed/ambiguous/duplicate
  devices, failed/time-out telemetry and missing selected UUID as UNKNOWN.
- Do not lower the required budget, silently use CPU offload, evict other
  processes, kill other GPU consumers or substitute a model to make it fit.
- On failed readiness, CUDA allocation/OOM, a lost child or an invalid partial
  start, mark the whole composite UNREADY, stop owned children, deny new
  serving-bound admission and let existing Control fencing/recovery decide
  outstanding work. Never report partial service as ready.
- The free-memory check is an **admission snapshot, not an atomic reservation**:
  between inspection and CUDA allocation an external workload can consume
  memory. Actual CUDA model-start failures and RuntimeProvider health/supervision
  remain necessary. Do not promise prevention of arbitrary later contention,
  automatic forced recovery, precise external-process attribution or long-term
  QoS.
- Runtime families with `prefer_vram` or `cpu_gpu_hybrid` policy require their
  own reviewed rules; do not impose the fully resident threshold on every GPU
  executor.

### 3. Production trust and responsibility

The operator remains responsible for selecting an appropriate GPU assignment,
ensuring any necessary external no-sharing policy, and deciding which processes
may run under a Worker service UID. A native API key stored under a private
Worker UID is *not* isolated from arbitrary processes sharing that same UID.
Composite native credentials, path ownership, UDS permissions and worker
auth/serving identity remain TSUMGI responsibilities in their stated threat
boundary. Persistent deployment, service upgrade and rollback require
operator-reviewed rollout procedures separate from experimental code merge.

## Consequences

- The #124 requirement to prove complete Proxmox-host/sibling-guest physical
  exclusion **does not gate merge** of an independently reviewed opt-in
  `llama-cpp-dual` implementation. #124 retains its service identity,
  credential compartment and live rollout/rollback gates.
- Existing #31/#57 GPU-*subset* isolation and exact-visible behavior is **not
  relaxed**. A single visible, matching GPU is not rejected just because
  guest-local systemd cannot enforce arbitrary GPU device denial.
- #125 owns prestart resident-memory admission, deterministic failure tests,
  independent review and actual disposable GPU pressure tests.
- Runtime compatibility/capacity claims must distinguish **configured total
  VRAM**, **fresh measured free VRAM**, **observed resident usage**, and
  **guaranteed physical allocation**. The last of these cannot be inferred
  from the first three without infrastructure support.
- No PVE host mutation, feature/main merge, automatic code approval or
  production cutover is implied by this ADR.

## Rejected alternatives

1. **TSUMGI enforces Proxmox-wide GPU access exclusion:** violates the
   provisioning boundary and couples the fabric to hypervisor privileges.
2. **Ignore VRAM contention because external ownership is out of scope:**
   allows a ready-advertising runtime to attempt an infeasible full-residency
   startup, contrary to fail-closed serving semantics.
3. **Treat an `nvidia-smi` free-memory snapshot as a guaranteed reservation:**
   unsound under concurrent external allocations; retain CUDA allocation and
   health/fencing safeguards instead.
4. **Remove the existing subset-access checks:** would break #31/#57's
   intentionally reviewed Worker-local device selection invariant.
