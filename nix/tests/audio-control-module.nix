{ pkgs, nixpkgs, system }:

let
  inherit (pkgs) lib;
  controlModule = ../modules/control.nix;
  inputRoot = "/var/lib/astrumweaver/audio/input";
  outputRoot = "/var/lib/astrumweaver/audio/output";
  # This is a module/storage test, not a provider, database, or daemon smoke.
  # No secret fixture is generated in the store.
  base = {
    imports = [ controlModule ];
    nixpkgs.pkgs = pkgs;
    system.stateVersion = "26.05";
    services.astrumweaver.control = {
      enable = lib.mkDefault true;
      package = pkgs.coreutils;
      command = "${pkgs.coreutils}/bin/true";
    };
  };
  # Complete policy shape; the catalog is intentionally absent. The module
  # must not open it or invent provider/runtime configuration.
  policy = {
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
      root = inputRoot;
      ttl_seconds = 180.0;
      max_total_bytes = 8388608;
      max_artifacts = 16;
    };
    output_spool = policy.input_spool // { root = outputRoot; };
  };
  audioOptions = {
    environmentFile = "/run/secrets/astrumweaver-control.env";
    settings.audio_gateway = policy;
  };
  evaluate = options: extra: (nixpkgs.lib.nixosSystem {
    inherit system;
    modules = [ base {
      boot.isContainer = true;
      fileSystems."/" = { device = "none"; fsType = "tmpfs"; };
      services.astrumweaver.control = options;
    } extra ];
  }).config;
  enabled = evaluate audioOptions { };
  baseline = evaluate { } { };
  disabled = evaluate {
    clientAuth = "none";
    settings.audio_gateway.enabled = false;
  } { };
  anonymousBaseline = evaluate { clientAuth = "none"; } { };
  retained = evaluate {
    clientAuth = "none";
    settings.audio_gateway = lib.recursiveUpdate policy {
      enabled = false;
      input_spool.root = "/not-present/input";
      output_spool.root = "/not-present/output";
    };
  } { };
  serviceOf = cfg: cfg.systemd.services.astrumweaver-control;
  unchanged = cfg:
    # ExecStart's generated TOML path necessarily differs; all other service
    # configuration and dependencies, and all tmpfiles rules, must be identical.
    builtins.removeAttrs (serviceOf cfg).serviceConfig [ "ExecStart" ]
      == builtins.removeAttrs (serviceOf anonymousBaseline).serviceConfig [ "ExecStart" ]
    && (serviceOf cfg).after == (serviceOf anonymousBaseline).after
    && (serviceOf cfg).requires == (serviceOf anonymousBaseline).requires
    && cfg.systemd.tmpfiles.rules == anonymousBaseline.systemd.tmpfiles.rules;
  accepts = cfg: builtins.all (entry: entry.assertion) cfg.assertions;
  rejects = prefix: cfg: builtins.any
    (entry: !entry.assertion && lib.hasPrefix prefix entry.message)
    cfg.assertions;
  change = overrides: evaluate (lib.recursiveUpdate audioOptions overrides) { };
  badAuth = change { clientAuth = "none"; settings.control.client_auth = "bearer"; };
  badRoots = [
    { input_spool.root = "/tmp/input"; }
    { input_spool.root = "relative/input"; }
    { input_spool.root = "/var/lib/astrumweaver/audio/../audio/input"; }
    { output_spool.root = inputRoot; }
    { output_spool.root = "${inputRoot}/nested"; }
    { input_spool.root = outputRoot; output_spool.root = inputRoot; }
  ];
  badEnvironmentPaths = [
    null "" "relative.env" "-/run/secrets/control.env"
    "/nix/store/example-control.env" "/run/../etc/control.env"
    "/run//secrets/control.env" "/run/secrets/control.env/"
    "/run/secrets/%n.env" "/run/secrets/control.env other.env"
    # A runtime-looking string must not carry a store dependency/context either.
    "/run/secrets/${pkgs.coreutils.name}-${pkgs.coreutils}.env"
  ];
  rules = map (path: "d ${path} 0700 astrumweaver astrumweaver - -") [
    "/var/lib/astrumweaver/audio" inputRoot outputRoot
  ];
in
# These assertions inspect genuinely evaluated NixOS configurations, not module
# source text or a stand-in option schema. Negative fixtures force the relevant
# module assertions; toplevel evaluation additionally proves NixOS rejects them.
assert accepts enabled;
assert accepts baseline && accepts disabled && accepts retained;
assert accepts (change { settings.control.client_auth = "bearer"; });
assert accepts (change { clientAuth = "bearer"; settings.control.client_auth = "none"; });
assert unchanged disabled && unchanged retained;
assert builtins.all (cfg: !(builtins.any
  (rule: lib.hasInfix "/var/lib/astrumweaver/audio" rule)
  cfg.systemd.tmpfiles.rules)) [ baseline disabled retained ];
assert !((evaluate (audioOptions // { enable = false; }) { }).systemd.services ? astrumweaver-control);
assert baseline.systemd.tmpfiles.rules == anonymousBaseline.systemd.tmpfiles.rules;
assert (serviceOf enabled).serviceConfig.StateDirectory == "astrumweaver";
assert (serviceOf enabled).serviceConfig.StateDirectoryMode == "0750";
assert builtins.all (rule: builtins.elem rule enabled.systemd.tmpfiles.rules) rules;
assert builtins.attrNames enabled.systemd.services == builtins.attrNames baseline.systemd.services;
assert rejects "Control audio requires effective" badAuth;
assert rejects "Control audio requires effective" (change { settings.control.client_auth = "none"; });
assert !(builtins.tryEval badAuth.system.build.toplevel.drvPath).success;
assert builtins.all (path: rejects "Control audio requires a protected"
  (change { environmentFile = path; })) badEnvironmentPaths;
assert rejects "Control audio requires a protected" (evaluate audioOptions {
  systemd.services.astrumweaver-control.serviceConfig.EnvironmentFile = lib.mkForce "/run/other.env";
});
assert builtins.all (roots: rejects "Control audio supports only"
  (change { settings.audio_gateway = roots; })) badRoots;
assert rejects "Control audio supports only" (evaluate {
  inherit (audioOptions) environmentFile;
  settings.audio_gateway.enabled = true;
} { });
assert rejects "Control audio requires the existing" (evaluate audioOptions {
  systemd.services.astrumweaver-control.serviceConfig.StateDirectory = lib.mkForce "elsewhere";
});
assert rejects "Control audio requires the existing" (evaluate audioOptions {
  systemd.services.astrumweaver-control.serviceConfig.User = lib.mkForce "nobody";
});
assert rejects "Control audio requires the existing" (evaluate audioOptions {
  systemd.services.astrumweaver-control.serviceConfig.Group = lib.mkForce "nogroup";
});
assert rejects "Control audio requires the existing" (evaluate audioOptions {
  systemd.services.astrumweaver-control.serviceConfig.DynamicUser = true;
});
assert rejects "Control audio requires empty extraArgs" (change { extraArgs = [ "--config" "/other.toml" ]; });
# Every assertion above forces actual NixOS module evaluation. Keep the
# default CI check evaluation-only: it must not require KVM, instantiate a
# VM, or exercise host systemd. A later isolated host/VM qualification is
# separately reviewed and gated.
pkgs.runCommand "astrumweaver-audio-control-module-evaluation" { } ''
  mkdir -p "$out"
  printf '%s\n' 'NixOS audio Control source-module assertions passed at evaluation' > "$out/evidence"
''
