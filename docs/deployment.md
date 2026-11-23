# Existing-Node Deployment

TSUMGI deploys onto an already-created Linux node. VM/LXC/bare-metal creation and GPU passthrough remain outside the project boundary.

## Separation of responsibilities

Before setup:

```text
infrastructure owner
  ├─ creates VM/LXC/bare metal
  ├─ configures networking/storage
  ├─ configures PCI/GPU passthrough when needed
  ├─ installs the operating system
  └─ makes the required NVIDIA GPU visible
           ↓
TSUMGI setup
  ├─ validates prerequisites
  ├─ installs role configuration
  ├─ installs systemd integration
  ├─ validates exact GPU identity for workers
  └─ optionally enables/starts the role service
```

TSUMGI setup never invokes `qm create`, `pct create`, or equivalent infrastructure creation.

## Package/runtime prerequisite

Issue #6 owns **host integration**, while the Nix/release package supplies the daemon executable.

The packaged role executable must already be available on the target node:

- `tsumgi-control`
- `tsumgi-worker`

The Nix flake provides immutable TSUMGI Control/Worker daemon packages and integration assets; see [Nix Packaging and NixOS Modules](nix.md).

Keeping these concerns separate allows the same setup contract to work with a Nix package, a release artifact, or another reviewed packaging mechanism.

For the combined generic-systemd installer profile, the packaged TUI launcher
explicitly supplies its lexical profile `bin` directory to first-run setup.
Transient setup helpers and the persistent daemon `ExecStart` values are
resolved through that explicit authority, not through Python console-script
`sys.argv[0]`, a resolved store sibling, or an ambient `PATH`. Profile
upgrades consequently keep the unit text stable while the profile symlink
selects the new daemon closure. A pre-existing `/nix/store/...` daemon path is
treated as a legacy unit and is refused until the operator performs the
reviewed migration; it is never silently overwritten.

RuntimeBackend ownership is separate from the role profiles. The standard
generic-systemd llama.cpp path is
`/nix/var/nix/profiles/tsumgi-runtime-llama-cpp`; the installer-packaged
runtime-profile manager reconciles it to the reviewed candidate and the Worker
manifest persists
`/nix/var/nix/profiles/tsumgi-runtime-llama-cpp/bin/llama-server`.
Consequently a root-shell `PATH` entry cannot satisfy or replace the reviewed
runtime package. See
[Installation — RuntimeBackend Nix profile](installation.md#runtimebackend-nix-profile).

## Generic systemd role layout

The role separation below applies only to the generic systemd setup helpers.
NixOS modules are unchanged and keep their existing configurable `user`,
`group`, and state-directory behavior; this section does not claim global
NixOS isolation.

The helpers share `/etc/tsumgi` as `root:tsumgi-config` with mode
`0710`. `tsumgi-config` is a **traverse-only** supplementary group:
neither role is granted the other role's private group. Each role-managed file
is `root:<role-group>` with mode `0640`.

- Control defaults to `User=Group=tsumgi-control` and state/home
  `/var/lib/tsumgi-control`.
- Worker remains `User=Group=tsumgi` and state/home
  `/var/lib/tsumgi`, preserving its runtime layout.

Distinct custom `--user` accounts are supported, but equal Control and Worker
values are rejected. An existing account must have the same-name group as its
primary group, with the matching GID. If an empty same-name group was
pre-provisioned, the helper reuses it with `useradd --gid`; peer/private groups
are not reused. Both units contain
`SupplementaryGroups=tsumgi-config`. The exact base units are the
[Control template](../systemd/tsumgi-control.service.in) and [Worker template](../systemd/tsumgi-worker.service.in).

## Control Plane

Example:

```sh
sudo ./setup/setup-control-plane.sh \
  --config ./control.toml \
  --environment-file ./control.env \
  --start
```

The script:

- requires an existing Linux/systemd host
- installs configuration under `/etc/tsumgi/`
- creates the `tsumgi-control` service account and same-name group when
  needed (or uses the reviewed `--user` account)
- installs `tsumgi-control.service`
- creates/uses `/var/lib/tsumgi-control/`
- optionally enables and starts the service

It does not provision PostgreSQL itself. The configured Control Plane must point at an already-available durable database.

Before first Control startup, apply the packaged schema explicitly:

```sh
TSUMGI_DATABASE_URL='postgresql://...' tsumgi-migrate
```

NixOS deployments may instead opt in to `services.tsumgi.control.migrateOnStart = true`; the default remains `false` because schema mutation is an explicit authority.

## GPU Worker

Example:

```sh
sudo ./setup/setup-gpu-worker.sh \
  --config ./worker.toml \
  --environment-file ./worker.env \
  --start
```

The Worker GPU ownership set is read exclusively from
`[worker].gpu_uuids` in `worker.toml`. The setup CLI has no separate GPU UUID
argument, so preflight/isolation state cannot diverge from the Worker contract.

The script installs:

- `/etc/tsumgi/worker.toml`
- optional `/etc/tsumgi/worker.env`
- `/etc/tsumgi/gpu-uuids` derived from `worker.toml`
- `/usr/local/libexec/tsumgi/gpu-preflight`
- `/etc/systemd/system/tsumgi-worker.service`

The Worker account and `/var/lib/tsumgi/` layout are intentionally
preserved. Its role files remain readable by the Worker private group, while
the shared configuration directory grants only traversal through
`tsumgi-config`.

For a live install it verifies `nvidia-smi` before the worker can be started.

The systemd unit repeats exact UUID preflight as `ExecStartPre`, so reboot/service restart cannot silently start a worker against a changed GPU set.

When the live host exposes additional GPUs, `--gpu-isolation auto` (the
default) derives the selected UUID→`/dev/nvidiaN` mapping, verifies it outside
the Worker cgroup, and installs a systemd drop-in with
`DevicePolicy=closed` plus exact physical `DeviceAllow` entries. The original
exact-set preflight then runs inside that restricted cgroup.

For staged `--root` installs, isolation requires reviewed
`--gpu-device UUID=/dev/nvidiaN` mappings because live GPU discovery is not
available.

### RuntimeProvider manifest

A generic systemd Worker can persist an already-reviewed
`RuntimeDeploymentSpec` alongside the Worker configuration:

```sh
sudo tsumgi-setup-gpu-worker \
  --config ./worker.toml \
  --environment-file ./worker.env \
  --runtime-manifest ./runtime-deployment.json
```

The manifest is installed as
`/etc/tsumgi/runtime-deployment.json` and passed explicitly to the
Worker daemon. It contains provider identity, non-secret provider
configuration and execution demand; secret values remain in the protected
EnvironmentFile.

The Worker starts the ManagedRuntime and waits for its readiness before
registering with Control. Worker service shutdown also stops/releases the
ManagedRuntime. A cleanup failure is surfaced rather than treated as a
successful release.

For interactive application of a SetupPlan, use the first-party systemd
driver:

```sh
sudo /nix/var/nix/profiles/tsumgi-installer/bin/tsumgi-setup-tui \
  --driver tsumgi.setup.systemd:create_systemd_driver
```

The runtime driver does not guess installers. Package installation and
download/conversion actions remain BLOCKED unless the operator supplies a
reviewed argv command through
`TSUMGI_RUNTIME_INSTALLERS_JSON`,
`TSUMGI_RUNTIME_DOWNLOADERS_JSON`, or
`TSUMGI_RUNTIME_CONVERTERS_JSON`.

## Exact GPU identity

The worker preflight compares sets, not ordinals.

These are equivalent:

```text
expected:
GPU-a
GPU-b

observed:
GPU-b
GPU-a
```

This is rejected because an unexpected GPU is also unsafe:

```text
expected:
GPU-a
GPU-b

observed:
GPU-a
GPU-b
GPU-c
```

`/dev/nvidia0` ordering is never used as identity. Device nodes are accepted
only after their UUID mapping is verified against `nvidia-smi`.

See [Runtime Deployment GPU Isolation Acceptance](runtime-deployment-acceptance.md)
for the private-safe real-host proof procedure.

## Conflict-safe idempotency

Setup scripts are convergent for reviewed identical inputs.

Running the same command again:

- preserves identical configuration
- corrects expected file modes
- regenerates an identical systemd unit
- succeeds without replacing unrelated state

If an existing managed file differs, setup **fails** instead of silently overwriting it.

The helpers compare an existing role unit before writing it. An old unit from
the shared-account templates therefore refuses replacement; use the manual
migration below rather than deleting the unit to bypass the comparison. For
ordinary fresh/retry runs, the same reviewed inputs repair expected modes and
converge without replacing unrelated state.

This policy prevents a generic bootstrap command from unexpectedly changing worker identity or service authority.

## Migrating an older generic systemd installation

This procedure is for an older generic install where Control and Worker used a
shared `tsumgi` identity/state layout. It is intentionally manual:
preserve the existing executable paths, credentials, state, and reviewed
drop-ins. Do not silently remove secrets or state, and do not remove the
existing Worker account. In a full Control + Worker migration, the old
`/var/lib/tsumgi` remains Worker state; do not blanket-rename or
`chown` it. Any Control-owned data must be identified and moved deliberately.

1. Stop **both** units and verify that each is inactive before restarting
   anything:

   ```sh
   sudo systemctl stop tsumgi-control.service tsumgi-worker.service
   for unit in tsumgi-control.service tsumgi-worker.service; do
     state="$(sudo systemctl is-active "$unit" 2>/dev/null || true)"
     printf '%s: %s\n' "$unit" "$state"
     test "$state" = inactive || exit 1
   done
   ```

2. While both are stopped, protect the old Control files, then make a local
   root-only backup:

   ```sh
   sudo chown root:root /etc/tsumgi/control.toml /etc/tsumgi/control.env
   sudo chmod 0600 /etc/tsumgi/control.toml /etc/tsumgi/control.env
   sudo install -d -o root -g root -m 0700 /root/tsumgi-role-migration
   sudo install -o root -g root -m 0600 \
     /etc/tsumgi/control.toml /root/tsumgi-role-migration/control.toml
   sudo install -o root -g root -m 0600 \
     /etc/tsumgi/control.env /root/tsumgi-role-migration/control.env
   ```

3. Edit the existing Control unit; do not replace it with a newly generated
   unit:

   ```sh
   sudoedit /etc/systemd/system/tsumgi-control.service
   ```

   Change the role identity and state, inserting the supplementary group
   **immediately after `Group=`** exactly as in the [Control unit template](../systemd/tsumgi-control.service.in):

   ```ini
   User=tsumgi-control
   Group=tsumgi-control
   SupplementaryGroups=tsumgi-config
   ```

   Change the existing `StateDirectory=` value to
   `StateDirectory=tsumgi-control` in its existing location.

   Preserve the existing `ExecStart=` command arguments and every other line.
   If its executable token is an immutable `/nix/store/.../tsumgi-control`
   path, replace only that token with the stable installer-profile path:

   ```ini
   ExecStart=/nix/var/nix/profiles/tsumgi-installer/bin/tsumgi-control --config /etc/tsumgi/control.toml
   ```

   Otherwise preserve the reviewed existing executable path. Do not add a
   peer private group.

4. Edit the existing Worker unit and insert the same line immediately after
   its existing `Group=` line, matching the [Worker unit template](../systemd/tsumgi-worker.service.in):

   ```sh
   sudoedit /etc/systemd/system/tsumgi-worker.service
   ```

   The relevant lines must be:

   ```ini
   Group=tsumgi
   SupplementaryGroups=tsumgi-config
   ```

   Keep `User=Group=tsumgi`, its `StateDirectory=tsumgi`, and all
   existing `ExecStart=` command arguments. If its executable token is an
   immutable `/nix/store/.../tsumgi-worker` path, replace only that
   token with:

   ```ini
   ExecStart=/nix/var/nix/profiles/tsumgi-installer/bin/tsumgi-worker --config /etc/tsumgi/worker.toml
   ```

   Otherwise preserve the reviewed existing executable path. Preserve all preflight,
   RuntimeProvider, and GPU drop-ins. No role-identity drop-in may override
   `User=`, `Group=`, `StateDirectory=`, or `SupplementaryGroups=`; remove any
   old identity supplementary override during this deliberate review while
   retaining the non-identity drop-in behavior.

5. Reconcile Control **first**, without `--start`, using the existing
   executable path. Because the Worker unit is present, its live
   `tsumgi` account must still exist; the Control helper then creates the
   new `tsumgi-control` account/group and state:

   ```sh
   sudo /nix/var/nix/profiles/tsumgi-installer/bin/tsumgi-setup-control-plane \
     --config /etc/tsumgi/control.toml \
     --environment-file /etc/tsumgi/control.env \
     --user tsumgi-control \
     --executable /nix/var/nix/profiles/tsumgi-installer/bin/tsumgi-control
   ```

   Then reconcile Worker, again without `--start`, with the original reviewed
   runtime manifest/isolation inputs when applicable:

   ```sh
   sudo /nix/var/nix/profiles/tsumgi-installer/bin/tsumgi-setup-gpu-worker \
     --config /etc/tsumgi/worker.toml \
     --environment-file /etc/tsumgi/worker.env \
     --user tsumgi \
     --executable /nix/var/nix/profiles/tsumgi-installer/bin/tsumgi-worker
   ```

   The Worker helper now sees the live Control peer. Pass the existing
   `--runtime-manifest`, `--gpu-isolation`, and reviewed `--gpu-device` values
   as applicable; do not change the stable `ExecStart` or drop-in behavior just
   to make comparison pass. For a customized installation, substitute the
   distinct account names present in the edited units; never use the same
   `--user` value for both roles.

6. Only after both helpers succeed, reload and start Control before Worker:

   ```sh
   sudo systemctl daemon-reload
   sudo systemctl start tsumgi-control.service
   sudo systemctl start tsumgi-worker.service
   ```

### Control-only legacy state

If there is no Worker unit but `/var/lib/tsumgi` exists, the Control
helper rejects it as orphaned legacy state. This is not permission repair: do
not delete it or blanket-`chown` it. Stop Control, verify it is inactive, and
confirm there is no Worker unit, process, or operator-managed Worker before
continuing. Do not run the `mv` below until that confirmation is complete. Make
a root-only local backup of the legacy directory, then—and only then—rename
that directory to a non-existing review/quarantine path:

```sh
sudo systemctl stop tsumgi-control.service
test "$(sudo systemctl is-active tsumgi-control.service 2>/dev/null || true)" = inactive
sudo install -d -o root -g root -m 0700 /root/tsumgi-role-migration
sudo tar -C /var/lib/tsumgi \
  -cf /root/tsumgi-role-migration/legacy-state.tar .
sudo chown root:root /root/tsumgi-role-migration/legacy-state.tar
sudo chmod 0600 /root/tsumgi-role-migration/legacy-state.tar
sudo test ! -e /var/lib/tsumgi-legacy-review || exit 1
sudo test ! -L /var/lib/tsumgi-legacy-review || exit 1
sudo mv -- /var/lib/tsumgi /var/lib/tsumgi-legacy-review
sudo chown root:root /var/lib/tsumgi-legacy-review
sudo chmod 0700 /var/lib/tsumgi-legacy-review
```

After the Control unit edit above, rerun only the Control helper. It creates
`/var/lib/tsumgi-control`; manually review the quarantine and move only
data confirmed to be Control-owned into the new path, with deliberate
per-entry ownership/mode changes. If a Worker cannot be ruled out, stop here
and use the full migration procedure instead.

## Staged installs

Both setup scripts support:

```text
--root DIR
```

This writes the target filesystem layout under a staging directory without:

- creating system users
- calling systemd
- touching GPUs
- requiring root

It is used by CI and can also be used by packaging/integration tooling.

`--start` is intentionally rejected with staged installs.

## Existing Proxmox VM

A Proxmox VM is eligible when, before setup:

- the VM already exists
- GPU passthrough is already configured if required
- the guest OS is operational
- the role executable is installed
- for a GPU worker, guest `nvidia-smi` reports exactly the intended UUID set

No Proxmox API access is required by TSUMGI.

## Existing Proxmox LXC

A Proxmox LXC is eligible when, before setup:

- the container already exists
- NVIDIA device mapping is already configured by the operator
- driver/userspace compatibility is already resolved
- `nvidia-smi` works inside the container
- the TSUMGI service account can access the devices

TSUMGI does not alter LXC privilege mode, cgroup device rules, bind mounts, or host drivers.

## Secrets

Environment files may contain deployment secrets and therefore:

- must not be committed to the public repository
- are installed mode `0640`, owned by `root:<role-group>`
- are readable by root and only their corresponding role account

The shared configuration group can traverse `/etc/tsumgi` but cannot
read these files. Splitting files does not revoke values already exposed:
rotate existing database or authority credentials if compromise is suspected.

Non-secret role configuration belongs in the TOML configuration file.

## systemd lifecycle

Setup defaults to installing/reloading without starting the service.

Use `--start` only after configuration and credentials have been reviewed.

GPU Worker ordering is:

```text
systemd start
    ↓
exact UUID preflight (privileged ExecStartPre)
    ↓
worker process starts
    ↓
worker may register with Control
```

For RuntimeProvider-backed Workers the ordering is stricter:

```text
exact UUID preflight
    ↓
ManagedRuntime start + provider health/model readiness
    ↓
Worker registration
    ↓
job claims
```

Therefore Worker registration cannot occur before either the local GPU identity
gate or selected runtime readiness passes.
