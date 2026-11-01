# #180 Phase B1 — opt-in Control audio host integration (source-only)

**Status:** source change only, not deployment approval or provider qualification.
Phase A's daemon contract is unchanged. B1 adds only NixOS declarative directory
provisions and configuration assertions; the existing generic-systemd helper is
unchanged. No model/runtime downloads, Worker capability advertisement, new
provisioning service, database migration, or live host change is authorized here.

**An explicit operator GO is required before actual setup**, including a NixOS
activation/rebuild, live helper invocation (even without `--start`), directory or
secret provisioning, service restart, or network exposure. Review the target,
effective identity/unit, storage, secrets, catalog, rollback and readiness evidence
first. The examples below are review material, not permission to execute them.

## Shared prerequisites and fail-closed boundaries

1. Use the reviewed Control daemon containing Phase A. Provide a complete strict
   `[audio_gateway]` policy, with every nested field, finite quotas and deadlines,
   and a reviewed pinned catalog containing both speech and transcription
   profiles. See [Phase A](audio-control-phase-a.md) for the TOML and contract.
   A catalog is not evidence that any Worker/provider is ready.
2. Effective `control.client_auth` **must be `"bearer"`**. On NixOS the non-null
   `clientAuth` option overrides `settings.control.client_auth`; when both are
   absent the daemon defaults to bearer. `clientAuth = "none"` with audio enabled
   is rejected by the module; generic-systemd's `client_auth = "none"` is rejected
   by the daemon before spool allocation. It is not a supported audio-only-auth
   mixed mode. Anonymous non-audio operation remains available with audio off.
3. Deliver `ASTRUMWEAVER_DATABASE_URL`, `ASTRUMWEAVER_CLIENT_TOKEN`,
   `ASTRUMWEAVER_WORKER_TOKEN`, and **a separate protected
   `ASTRUMWEAVER_AUDIO_CLIENT_TOKEN`** through the protected environment file.
   All three tokens must be nonempty and pairwise distinct. The audio token must
   match the daemon's bearer-token syntax, cannot be `anon` (case-insensitive),
   and must never fall back to either existing token. Distribute only the audio
   token to audio API clients. Do not put tokens/secret database credentials in
   Nix settings, TOML, catalogs, unit text, derivation environment variables,
   `writeText`, `readFile`, committed fixtures, or any Nix store object.
4. At daemon startup, **both spool roots must already exist** as separate real
   directories owned by the effective Control UID with mode exactly `0700`.
   They cannot coincide, alias one inode, or nest. Each ancestor must be a real
   directory owned by root or Control, without group/world write access (the
   daemon's narrowly allowed root-owned sticky temporary ancestors are not used
   by these deployment layouts). No symlink component is supported for roots.
   The daemon checks both roots before constructing either spool; ArtifactSpool
   repeats descriptor-relative checks. The daemon never provisions these roots.
5. Root and same-effective-UID processes are trusted. Do not share the Control
   identity/private group or secret access with an untrusted Worker or workload.
   NixOS retains its existing default `astrumweaver` user/group: B1 does **not**
   silently migrate identities. For a co-located Worker using that identity,
   require a separately reviewed stopped-service identity/state migration or a
   separate host before enabling audio. The generic helper already defaults to
   separate Control and Worker identities and rejects legacy/shared layouts.
6. Provision a protected regular environment file and trusted ancestors out of
   band. For NixOS use root-owned `0600` (PID 1 reads it); for the generic helper
   the installed contract is `root:<Control group> 0640`. No untrusted reader or
   writer, including through group membership, ACLs or secret-manager indirection,
   may gain access. Audit the actual target/mount and secret delivery ordering.
   Neither Nix evaluation nor the daemon audits environment-file permissions:
   an accepted path is **not proof** of live file protection.

## NixOS: exact supported opt-in

Import `nixosModules.control` (or the existing default module), enable Control,
and set the existing **boolean** `settings.audio_gateway.enabled = true`.
There is no second audio enable option and no inferred enablement from a catalog
or path. With the Control service itself disabled, this module provisions nothing.

The only supported audio root strings are exactly:

| Purpose | Path | Mode / owner |
| --- | --- | --- |
| Audio parent | `/var/lib/astrumweaver/audio` | `0700`, configured Control user/group |
| Input root | `/var/lib/astrumweaver/audio/input` | `0700`, configured Control user/group |
| Output root | `/var/lib/astrumweaver/audio/output` | `0700`, configured Control user/group |

These are `systemd.tmpfiles.rules` type `d` entries, with no age/cleanup policy,
recursive chown, shell hook, or new provisioning unit. Control explicitly requires
and starts after the existing `systemd-tmpfiles-setup.service`. NixOS activation
can also apply tmpfiles rules: merely starting a previously active tmpfiles unit
is not a substitute for a reviewed activation. The existing
`StateDirectory=astrumweaver`, `StateDirectoryMode=0750`, runtime directory,
sandbox and migration defaults are unchanged. The roots are writable inside
Control's existing StateDirectory sandbox exception; no broader write path is
added. Do not treat tmpfiles as recovery of unknown existing spool contents.

Module assertions require effective bearer auth, both exact root strings, a
static matching service/module user and group, the existing StateDirectory, and
empty `extraArgs` (no alternate `--config`). Keep the packaged daemon/default
command; custom package/command/unit overrides require independent review and
are not evidence that the daemon's checks ran.

`environmentFile` is mandatory for this opt-in. It must be a **plain string**
under `/etc/` or `/run/`, with only ASCII letters, digits, `_`, `.`, `/`, `-`,
nonempty components and no `.` or `..` components. Relative/optional (`-`)
references, whitespace, systemd specifiers, store paths, Nix string contexts and
service-level EnvironmentFile overrides are rejected. Use, for example,
`"/run/secrets/astrumweaver-control.env"`, **not** a Nix path literal or an
interpolated secret derivation. The assertion verifies this lexical reference
and requirement only; the operator must establish live protection and arrange
secret delivery before Control starts. B1 does not install a secret manager.

Complete illustrative non-secret policy (catalog must be supplied separately):

```nix
services.astrumweaver.control = {
  enable = true;
  clientAuth = "bearer";
  environmentFile = "/run/secrets/astrumweaver-control.env";
  settings.audio_gateway = {
    enabled = true;
    catalog = "/etc/astrumweaver/reviewed-audio.json";
    ingress = {
      request_bytes = 2097152;
      input_bytes = 16384;
      file_bytes = 1048576;
      metadata_bytes = 4096;
      part_header_bytes = 1024;
      total_header_bytes = 8192;
      receive_events = 256;
      idle_timeout_seconds = 1.0;
      total_timeout_seconds = 10.0;
    };
    speech = {
      output_bytes = 1048576;
      request_timeout_seconds = 60.0;
      max_pending_admissions = 4;
      poll_interval_seconds = 0.05;
    };
    transcriptions = {
      text_bytes = 8192;
      request_timeout_seconds = 60.0;
      max_pending_admissions = 4;
      poll_interval_seconds = 0.05;
    };
    pcm = { max_decoded_bytes = 1048532; max_duration_seconds = 30; };
    input_spool = {
      root = "/var/lib/astrumweaver/audio/input";
      ttl_seconds = 180.0;
      max_total_bytes = 8388608;
      max_artifacts = 16;
    };
    output_spool = {
      root = "/var/lib/astrumweaver/audio/output";
      ttl_seconds = 180.0;
      max_total_bytes = 8388608;
      max_artifacts = 16;
    };
  };
};
```

Before operator GO, review any existing occupants/ancestors and confirm the
effective account, exact paths, permissions and catalog readability. After an
approved activation, verify those facts again **before accepting audio traffic**;
directory creation alone does not imply successful daemon startup or readiness.

With the audio table absent or `enabled = false`, B1 contributes no audio rules,
no extra dependencies or assertions, and changes no service defaults. A retained
disabled policy does not cause path/catalog access. Disabling does not remove
previously created directories or artifacts: retention, inspection and any
deletion require a separate explicit operator decision.

## Generic systemd: existing helper, pre-existing secure directories

Use `setup/setup-control-plane.sh` or the packaged
`astrumweaver-setup-control-plane`. It already accepts a complete `--config FILE`
and `--environment-file FILE`, copies them to `/etc/astrumweaver/control.toml`
and `control.env`, rejects symlink destinations and differing existing contents,
and on live installs reconciles files to `root:<Control group> 0640`. The config
directory is `root:astrumweaver-config 0710`; Control gets traverse-only membership
and its private group reads its own files. Protect the source files and ancestor
directories too; this helper is not a general adversarial-path resolver or a
secret validator. Changed existing content requires a separate reviewed update,
not an overwrite bypass.

The helper defaults to user/group `astrumweaver-control` and
`StateDirectory=astrumweaver-control`, mode `0750`. **These differ from NixOS.**
For the unchanged generic unit sandbox, use the complete Phase A TOML policy but
set these roots:

- `audio_gateway.input_spool.root = "/var/lib/astrumweaver-control/audio/input"`
- `audio_gateway.output_spool.root = "/var/lib/astrumweaver-control/audio/output"`

An authorized operator must pre-create the audio parent and both roots with
`0700` and the actual Control user/group, with trusted ancestors, **before daemon
start**. The helper only provisions the state parent; it never parses audio
policy or creates audio roots. Do not use Phase A's NixOS-root illustration
unchanged under this generic unit, or add a broad sandbox write exception to
work around it. A missing/unsafe root causes the actual daemon to fail closed.
Use `control.client_auth = "bearer"` in TOML and supply all four required
environment variables, including the independent audio token, in the protected
environment file. Missing/invalid/reused audio credentials fail closed even
though the generic unit retains its existing optional EnvironmentFile directive.

For a separately approved staging exercise, the existing helper interface is:

```text
astrumweaver-setup-control-plane \
  --config /secure/source/control.toml \
  --environment-file /secure/source/control.env \
  --executable /reviewed/profile/bin/astrumweaver-control \
  --root /reviewed/staging
```

Staging requires an explicit executable and does not prove live identity,
permissions, sandbox or readiness. Never combine staged `--root` with `--start`.
On the live root the helper creates/reconciles accounts and files and calls
`systemctl daemon-reload` **even without `--start`**; `--start` additionally enables
and starts Control. It is not a dry run. Keep Control stopped during approved
setup until secrets, catalog and pre-existing roots are verified, then obtain
the appropriate operator GO for startup/exposure. No helper invocation is part
of this source-only continuation.

## Verification boundary and remaining work

`nix/tests/audio-control-module.nix`, exposed as
`checks.x86_64-linux.audio-control-module`, evaluates the actual NixOS module:
default/explicit bearer acceptance and option precedence; anonymous, missing or
unsafe environment references, overridden state/identity/environment, alternate
arguments and unsupported roots rejection; absent/disabled/retained-policy
non-provisioning semantics. It checks real NixOS assertion values and a rejected
system toplevel, not textual source matching. The **new B1 audio-control-module check** is **NixOS evaluation only**, deliberately not a VM
or live filesystem test: it verifies generated private tmpfiles rules and
service configuration values, but it does **not** prove directory ownership,
mode or sandbox write access on an actual host. Those require separately
authorized isolated-VM/host qualification before operational acceptance.

Source verification entrypoint: `nix build .#checks.x86_64-linux.audio-control-module`.
The resulting lightweight derivation exercises real NixOS **configuration
evaluation**, not runtime execution. It never provisions a real host or VM.
Do not treat it as a successful boot or permission test. The repository already had a distinct VM-based `setup-permissions` check in default `nix flake check` before B1; this PR neither adds nor removes that existing check. B1 itself introduces no VM requirement. Passing evidence must be obtained
separately; the presence of the evaluated-module test is not a claim it passed.
Existing `tests/test_audio_gateway_daemon.py` covers daemon auth, root rejection
and disabled no-I/O behavior; B1 does not replace that contract.

Phase A/#174 restart/orphan/retention limitations remain. No automatic cleanup,
crash recovery, Worker lifecycle, runtime/model installation, readiness
advertisement, deployment/rollback execution or production qualification is
added. Actual setup and audio traffic remain explicitly operator-gated.
