# Actual NixOS evaluation of Python-generated synthetic manifest cases.
# Exports production serialization inputs, never starts a runtime or reads an asset.
{ flake, manifest }:
let
  system = "x86_64-linux";
  inherit (flake.inputs.nixpkgs) lib;
  pkgs = import flake.inputs.nixpkgs { inherit system; };
  evaluate = case:
    let
      d = case.runtime.demand;
      gpu = case.provider == "parakeet-ja";
      uuid = case.provider_config.gpu_uuid or "";
      host = lib.nixosSystem {
        inherit system;
        modules = [
          flake.nixosModules.worker
          {
            nixpkgs.pkgs = pkgs;
            system.stateVersion = "26.05";
            boot.isContainer = true;
            fileSystems."/" = { device = "none"; fsType = "tmpfs"; };
            services.astrumweaver.worker = {
              enable = true;
              package = pkgs.coreutils; # Not executed: no fake native qualification.
              workerId = case.worker.worker_id;
              workerClass = case.worker.worker_class;
              controlUrl = case.worker.control_url;
              user = case.worker.user;
              group = case.worker.group;
              capabilities = case.worker.capabilities;
              environmentFile = case.worker.environment_file;
              gpuUuids = lib.optional gpu uuid;
              totalVramMb = if gpu then 11264 else 0;
              maxSingleGpuVramMb = if gpu then 11264 else 0;
              gpuIsolation = {
                enable = gpu;
                deviceMap = lib.optionalAttrs gpu { ${uuid} = "/dev/nvidia0"; };
              };
              nvidiaSmiPackage = if gpu then pkgs.coreutils else null;
              runtime = {
                enable = true;
                provider = case.provider;
                providerConfig = case.provider_config;
                modelRef = d.model.model_ref;
                modelFormat = d.model.model_format;
                modelTopology = d.model.topology;
                estimatedModelSizeMb = d.model.estimated_size_mb;
                modelMetadata = d.model.metadata;
                residencyPolicy = d.residency_policy;
                gpuTopology = d.gpu_topology;
                minGpuCount = d.min_gpu_count;
                minTotalVramMb = d.min_total_vram_mb;
                minSingleGpuVramMb = d.min_single_gpu_vram_mb;
                minHostRamMb = d.min_host_ram_mb;
                preferredHostRamMb = d.preferred_host_ram_mb;
                demandMetadata = d.metadata;
              };
              serving.declaration = case.serving;
            };
          }
        ];
      };
      cfg = host.config;
      unit = cfg.systemd.services.astrumweaver-worker.serviceConfig;
      bundle = cfg.system.build.astrumweaverAudioWorkerConfig;
      inherit (bundle) reviewed artifacts;
      # Exact store derivation outputs, not suffixes: these same artifacts are
      # the linkFarm entries. This remains evaluation, not artifact realization.
      referencesMatch = settings: command:
        settings.runtime.manifest == artifacts."runtime.json"
        && settings.serving.manifest == artifacts."serving.json"
        && command == lib.escapeShellArgs [
          "${cfg.services.astrumweaver.worker.package}/bin/astrumweaver-worker"
          "--config" artifacts."worker.toml"
        ];
      wrongReference = field: path: lib.recursiveUpdate reviewed.settings {
        ${field}.manifest = path;
      };
    in {
      inherit (case) name;
      inherit reviewed;
      assertionsPassed = builtins.all (entry: entry.assertion) cfg.assertions;
      serviceContracts =
        referencesMatch reviewed.settings unit.ExecStart
        && artifacts."runtime.json" == pkgs.writeText "astrumweaver-runtime-deployment.json"
          (builtins.toJSON case.runtime)
        && artifacts."serving.json" == pkgs.writeText "astrumweaver-serving-deployment.json"
          (builtins.toJSON case.serving)
        && !(referencesMatch
          (wrongReference "runtime" "/unrelated/astrumweaver-runtime-deployment.json") unit.ExecStart)
        && !(referencesMatch
          (wrongReference "serving" "/unrelated/astrumweaver-serving-deployment.json") unit.ExecStart)
        && !(referencesMatch reviewed.settings "/unrelated/astrumweaver-worker.toml")
        && unit.User == case.worker.user && unit.Group == case.worker.group
        && unit.EnvironmentFile == case.worker.environment_file
        && unit.WorkingDirectory == case.provider_config.private_home
        && unit.ReadWritePaths == [ case.provider_config.private_home ]
        && unit.StateDirectory == [ ] && unit.Restart == "no"
        && unit.UMask == "0077" && unit.KillMode == "control-group"
        && (!gpu || unit.DevicePolicy == "closed");
    };
in
map evaluate manifest
