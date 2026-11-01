{ config, lib, pkgs, ... }:

let
  cfg = config.services.astrumweaver.control;
  astrumweaverPackage = pkgs.callPackage ../package.nix { };
  integrationPackage = pkgs.callPackage ../integration-package.nix { };
  defaultPackage = pkgs.callPackage ../control-support.nix {
    astrumweaver = astrumweaverPackage;
    integration = integrationPackage;
  };
  toml = pkgs.formats.toml { };
  effectiveSettings = lib.recursiveUpdate cfg.settings (
    lib.optionalAttrs (cfg.clientAuth != null) {
      control.client_auth = cfg.clientAuth;
    }
  );
  generatedConfig = toml.generate "astrumweaver-control.toml" effectiveSettings;
  effectiveCommand =
    if cfg.command == null
    then "${cfg.package}/bin/astrumweaver-control"
    else cfg.command;
  execStart = lib.escapeShellArgs ([ effectiveCommand "--config" generatedConfig ] ++ cfg.extraArgs);
  audioEnabled = (cfg.settings.audio_gateway.enabled or false) == true;
  audioInputRoot = "/var/lib/astrumweaver/audio/input";
  audioOutputRoot = "/var/lib/astrumweaver/audio/output";
  service = config.systemd.services.astrumweaver-control.serviceConfig;
  # Validate only the reference, never read a secret into the Nix store. Live
  # ownership/mode and trusted ancestors remain operator prerequisites.
  protectedEnvironmentPath =
    cfg.environmentFile != null
    && !builtins.hasContext cfg.environmentFile
    && builtins.match "/(etc|run)/[A-Za-z0-9_./-]+" cfg.environmentFile != null
    && builtins.all (part: part != "" && part != "." && part != "..")
      (lib.drop 1 (lib.splitString "/" cfg.environmentFile));
in
{
  options.services.astrumweaver.control = {
    enable = lib.mkEnableOption "AstrumWeaver Control Plane service";

    package = lib.mkOption {
      type = lib.types.package;
      default = defaultPackage;
      defaultText = lib.literalExpression "AstrumWeaver control package from this module";
      description = "Control Plane runtime package.";
    };

    command = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "/custom/bin/astrumweaver-control";
      description = "Optional daemon executable override. Defaults to the packaged astrumweaver-control entrypoint.";
    };

    settings = lib.mkOption {
      type = lib.types.attrs;
      default = { };
      description = "Non-secret TOML settings. Secrets must not be placed here because Nix store paths are world-readable.";
    };

    clientAuth = lib.mkOption {
      type = lib.types.nullOr (lib.types.enum [ "bearer" "none" ]);
      default = null;
      description = ''
        Optional explicit Client API authentication mode. null preserves the
        TOML/default contract, where an absent client_auth setting means bearer.
        Use "none" only when the deployment/network boundary supplies the
        intended Client API access control.
      '';
    };

    environmentFile = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "/run/secrets/astrumweaver-control.env";
      description = ''
        Protected EnvironmentFile containing database URL and authority tokens.
        Optional unless audio is enabled, when a required plain /etc or /run
        reference is mandatory. Provision live protection out of band (normally
        root-owned 0600); the module never reads or stores the file contents.
      '';
    };

    migrateOnStart = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Apply packaged PostgreSQL migrations before starting Control. Disabled by default because schema mutation is an explicit authority.";
    };

    user = lib.mkOption {
      type = lib.types.str;
      default = "astrumweaver";
    };

    group = lib.mkOption {
      type = lib.types.str;
      default = "astrumweaver";
    };

    extraArgs = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
    };

    extraPackages = lib.mkOption {
      type = lib.types.listOf lib.types.package;
      default = [ ];
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = lib.optionals audioEnabled [
      {
        assertion = (effectiveSettings.control.client_auth or "bearer") == "bearer";
        message = "Control audio requires effective control.client_auth=bearer (clientAuth overrides settings).";
      }
      {
        assertion = protectedEnvironmentPath
          && (service.EnvironmentFile or null) == cfg.environmentFile;
        message = "Control audio requires a protected environmentFile: a required canonical /etc or /run path outside the Nix store, with no string context or EnvironmentFile override. Provision its permissions out of band.";
      }
      {
        assertion = (cfg.settings.audio_gateway.input_spool.root or null) == audioInputRoot
          && (cfg.settings.audio_gateway.output_spool.root or null) == audioOutputRoot;
        message = "Control audio supports only input_spool.root=/var/lib/astrumweaver/audio/input and output_spool.root=/var/lib/astrumweaver/audio/output.";
      }
      {
        assertion = service.StateDirectory == "astrumweaver"
          && service.User == cfg.user && service.Group == cfg.group
          && !(service.DynamicUser or false);
        message = "Control audio requires the existing astrumweaver StateDirectory and static module user/group without service identity overrides.";
      }
      {
        assertion = cfg.extraArgs == [ ];
        message = "Control audio requires empty extraArgs so the reviewed generated config cannot be overridden.";
      }
    ];

    # No shell, recursive ownership repair, cleanup, or new provisioning unit.
    # The daemon independently rejects unsafe pre-existing paths at startup.
    systemd.tmpfiles.rules = lib.optionals audioEnabled [
      "d /var/lib/astrumweaver/audio 0700 ${cfg.user} ${cfg.group} - -"
      "d ${audioInputRoot} 0700 ${cfg.user} ${cfg.group} - -"
      "d ${audioOutputRoot} 0700 ${cfg.user} ${cfg.group} - -"
    ];

    users.groups.${cfg.group} = { };
    users.users.${cfg.user} = {
      isSystemUser = lib.mkDefault true;
      group = lib.mkDefault cfg.group;
      home = lib.mkDefault "/var/lib/astrumweaver";
      createHome = lib.mkDefault true;
    };

    environment.systemPackages = [ cfg.package ];

    systemd.services.astrumweaver-control = {
      description = "AstrumWeaver Control Plane";
      wantedBy = [ "multi-user.target" ];
      wants = [ "network-online.target" ];
      after = [ "network-online.target" ] ++ lib.optional audioEnabled "systemd-tmpfiles-setup.service";
      requires = lib.optional audioEnabled "systemd-tmpfiles-setup.service";
      path = [ cfg.package ] ++ cfg.extraPackages;

      serviceConfig = {
        Type = "simple";
        User = cfg.user;
        Group = cfg.group;
        ExecStart = execStart;
        Restart = "on-failure";
        RestartSec = "5s";
        RuntimeDirectory = "astrumweaver-control";
        RuntimeDirectoryMode = "0750";
        StateDirectory = "astrumweaver";
        StateDirectoryMode = "0750";
        NoNewPrivileges = true;
        PrivateTmp = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        ProtectKernelTunables = true;
        ProtectKernelModules = true;
        ProtectControlGroups = true;
        RestrictAddressFamilies = [ "AF_UNIX" "AF_INET" "AF_INET6" ];
      }
      // lib.optionalAttrs cfg.migrateOnStart {
        ExecStartPre = "${cfg.package}/bin/astrumweaver-migrate";
      }
      // lib.optionalAttrs (cfg.environmentFile != null) {
        EnvironmentFile = cfg.environmentFile;
      };
    };
  };
}
