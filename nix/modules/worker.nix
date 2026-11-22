{ config, lib, pkgs, ... }:

let
  cfg = config.services.astrumweaver.worker;
  astrumweaverPackage = pkgs.callPackage ../package.nix { };
  integrationPackage = pkgs.callPackage ../integration-package.nix { };
  defaultPackage = pkgs.callPackage ../worker-support.nix {
    astrumweaver = astrumweaverPackage;
    integration = integrationPackage;
  };
  toml = pkgs.formats.toml { };
  audioProfiles = {
    voicevox = { model = "model/0.vvm"; format = "vvm"; gpu = false; };
    style-bert-vits2 = { model = "jvnv-F1-jp"; format = "safetensors"; gpu = false; };
    parakeet-ja = { model = "nvidia/parakeet-tdt_ctc-0.6b-ja"; format = "nemo"; gpu = true; };
  };
  audioSelected = builtins.hasAttr cfg.runtime.provider audioProfiles;
  audioActive = cfg.runtime.enable && audioSelected;
  audioProfile = audioProfiles.${cfg.runtime.provider};
  audioCapability = if audioProfile.gpu then "audio.transcribe" else "audio.synthesize";
  audioSchema = if audioProfile.gpu
    then "openai-audio-transcription-metadata-v1" else "openai-audio-speech-v1";
  privateHome = cfg.runtime.providerConfig.private_home or null;
  service = config.systemd.services.astrumweaver-worker.serviceConfig;
  # References only. No live private input is opened or copied into the store.
  canonicalReference = prefix: value:
    builtins.isString value && !builtins.hasContext value
    && builtins.match prefix value != null
    && builtins.all (part: part != "" && part != "." && part != "..")
      (lib.drop 1 (lib.splitString "/" value));
  protectedEnvironment = canonicalReference "/(etc|run)/[A-Za-z0-9_./-]+" cfg.environmentFile;
  protectedHome = canonicalReference "/var/lib/astrumweaver-audio/[A-Za-z0-9_./-]+" privateHome;
  # systemd interprets numeric User=/Group= strings as IDs, independently of
  # NixOS's named-account uid/gid options. Require names, not numeric aliases.
  staticAccountName = value: builtins.match "[a-z_][a-z0-9_-]{0,31}" value != null;
  loopbackControl = builtins.match "http://127\\.0\\.0\\.1:([1-9][0-9]{0,4})" cfg.controlUrl;
  # A retained declaration must not activate an explicitly disabled audio lane.
  servingActive = (!audioSelected || cfg.runtime.enable) && cfg.serving.declaration != null;
  declaration = cfg.serving.declaration;
  declarationProvider =
    if builtins.isAttrs declaration && builtins.isAttrs (declaration.deployment or null)
    then declaration.deployment.provider_id or null else null;
  audioDeclarationMatches =
    builtins.isAttrs declaration && (declaration.schema_version or null) == "serving-deployment-v1"
    && declarationProvider == cfg.runtime.provider
    && builtins.isList (declaration.contracts or null)
    && builtins.length declaration.contracts == 1
    && builtins.isAttrs (builtins.head declaration.contracts)
    && builtins.isAttrs ((builtins.head declaration.contracts).contract or null)
    && ((builtins.head declaration.contracts).contract.capability or null) == audioCapability
    && ((builtins.head declaration.contracts).contract.operation_schema or null) == audioSchema;
  effectiveRuntimeGpuTopology =
    if cfg.runtime.gpuTopology != null
    then cfg.runtime.gpuTopology
    else if cfg.gpuUuids == [ ]
    then "none"
    else if builtins.length cfg.gpuUuids == 1
    then "single_gpu"
    else "multi_gpu";
  effectiveRuntimeMinGpuCount =
    if effectiveRuntimeGpuTopology == "multi_gpu"
    then builtins.max 2 cfg.runtime.minGpuCount
    else cfg.runtime.minGpuCount;
  runtimeManifestData = {
    schema_version = "v1";
    provider_id = cfg.runtime.provider;
    provider_config = cfg.runtime.providerConfig;
    demand = {
      model = {
        model_ref = cfg.runtime.modelRef;
        model_format = cfg.runtime.modelFormat;
        topology = cfg.runtime.modelTopology;
        estimated_size_mb = cfg.runtime.estimatedModelSizeMb;
        metadata = cfg.runtime.modelMetadata;
      };
      residency_policy = cfg.runtime.residencyPolicy;
      gpu_topology = effectiveRuntimeGpuTopology;
      min_gpu_count = effectiveRuntimeMinGpuCount;
      min_total_vram_mb = cfg.runtime.minTotalVramMb;
      min_single_gpu_vram_mb = cfg.runtime.minSingleGpuVramMb;
      min_host_ram_mb = cfg.runtime.minHostRamMb;
      preferred_host_ram_mb = cfg.runtime.preferredHostRamMb;
      metadata = cfg.runtime.demandMetadata;
    };
    setup_intent = null;
  };
  runtimeManifest = pkgs.writeText "astrumweaver-runtime-deployment.json" (
    builtins.toJSON runtimeManifestData
  );
  servingManifest = pkgs.writeText "astrumweaver-serving-deployment.json" (
    builtins.toJSON declaration
  );
  audioDemandMatches = runtimeManifestData.demand == {
    model = {
      model_ref = audioProfile.model;
      model_format = audioProfile.format;
      topology = "dense";
      estimated_size_mb = null;
      metadata = { };
    };
    residency_policy = if audioProfile.gpu then "vram_only" else "cpu_gpu_hybrid";
    gpu_topology = if audioProfile.gpu then "single_gpu" else "none";
    min_gpu_count = if audioProfile.gpu then 1 else 0;
    min_total_vram_mb = if audioProfile.gpu then 11264 else 0;
    min_single_gpu_vram_mb = if audioProfile.gpu then 11264 else 0;
    min_host_ram_mb = 0;
    preferred_host_ram_mb = 0;
    metadata = { };
  };
  coreSettings = {
    worker = {
      id = cfg.workerId;
      class = cfg.workerClass;
      control_url = cfg.controlUrl;
      gpu_uuids = cfg.gpuUuids;
      gpu_count = builtins.length cfg.gpuUuids;
      total_vram_mb = cfg.totalVramMb;
      max_single_gpu_vram_mb = cfg.maxSingleGpuVramMb;
      capabilities = cfg.capabilities;
      labels = cfg.labels;
      accelerators = cfg.accelerators;
      max_concurrency = cfg.maxConcurrency;
      gpu_preflight = cfg.gpuUuids != [ ];
      health_host = cfg.healthHost;
      health_port = cfg.healthPort;
    };
    executor = {
      factory = cfg.executorFactory;
      settings = cfg.executorSettings;
    };
  } // lib.optionalAttrs cfg.runtime.enable {
    runtime = {
      manifest = runtimeManifest;
      startup_timeout_seconds = cfg.runtime.startupTimeoutSeconds;
      shutdown_timeout_seconds = cfg.runtime.shutdownTimeoutSeconds;
    };
  } // lib.optionalAttrs servingActive {
    serving.manifest = servingManifest;
  };
  effectiveSettings = lib.recursiveUpdate cfg.settings coreSettings;
  generatedConfig = toml.generate "astrumweaver-worker.toml" effectiveSettings;
  # Reviewable configuration artifacts, not an engine/model installation. The
  # passthru exposes the exact serialization inputs for evaluation-only checks.
  audioConfigArtifacts = {
    "worker.toml" = generatedConfig;
    "runtime.json" = runtimeManifest;
    "serving.json" = servingManifest;
  };
  audioConfigBundle = (pkgs.linkFarm "astrumweaver-audio-worker-config"
    (lib.mapAttrsToList (name: path: { inherit name path; }) audioConfigArtifacts)
  ).overrideAttrs (_: {
    passthru = {
      artifacts = audioConfigArtifacts;
      reviewed = {
        settings = effectiveSettings;
        runtime = runtimeManifestData;
        serving = declaration;
      };
    };
  });
  sharedNvidiaDeviceNodes = [
    "/dev/nvidiactl"
    "/dev/nvidia-modeset"
    "/dev/nvidia-uvm"
    "/dev/nvidia-uvm-tools"
  ];
  expectedGpuUuids = pkgs.writeText "astrumweaver-gpu-uuids" (
    lib.concatStringsSep "\n" (lib.sort builtins.lessThan cfg.gpuUuids) + "\n"
  );
  gpuIsolationDevicePaths = map (
    uuid: cfg.gpuIsolation.deviceMap.${uuid} or ""
  ) cfg.gpuUuids;
  gpuDeviceAllows = map (path: "${path} rw")
    (gpuIsolationDevicePaths ++ cfg.gpuIsolation.auxiliaryDeviceNodes);
  gpuIsolationMap = pkgs.writeText "astrumweaver-gpu-device-map" (
    lib.concatStringsSep "\n" (
      map (
        uuid: "${uuid}=${cfg.gpuIsolation.deviceMap.${uuid} or ""}"
      ) (lib.sort builtins.lessThan cfg.gpuUuids)
    ) + "\n"
  );
  gpuMappingPython = pkgs.writeText "astrumweaver-gpu-mapping.py" (
    builtins.readFile ../../src/astrumweaver/gpu_mapping.py
  );
  preflight = pkgs.writeShellApplication {
    name = "astrumweaver-gpu-preflight";
    runtimeInputs = [ pkgs.coreutils pkgs.gawk ]
      ++ lib.optional (cfg.nvidiaSmiPackage != null) cfg.nvidiaSmiPackage;
    text = builtins.readFile ../../libexec/gpu-preflight;
  };
  gpuDeviceMapVerifier = pkgs.writeShellApplication {
    name = "astrumweaver-gpu-device-map";
    runtimeInputs = [ pkgs.coreutils pkgs.gawk pkgs.python312 ]
      ++ lib.optional (cfg.nvidiaSmiPackage != null) cfg.nvidiaSmiPackage;
    text = ''
      exec ${pkgs.python312}/bin/python3 ${gpuMappingPython} "$@"
    '';
  };
  effectiveCommand =
    if cfg.command == null
    then "${cfg.package}/bin/astrumweaver-worker"
    else cfg.command;
  execStart = lib.escapeShellArgs ([ effectiveCommand "--config" generatedConfig ] ++ cfg.extraArgs);
  gpuUuidArgs = lib.concatMapStringsSep " " (uuid:
    "--gpu-uuid ${lib.escapeShellArg uuid}"
  ) cfg.gpuUuids;
  nvidiaSmiCommand =
    if cfg.nvidiaSmiPackage == null
    then "nvidia-smi"
    else "${cfg.nvidiaSmiPackage}/bin/nvidia-smi";
  borrowableMode = pkgs.writeShellApplication {
    name = "astrumweaver-gpu-mode";
    text = ''
      if [ "$#" -ne 1 ]; then
        echo "usage: astrumweaver-gpu-mode {development|astrumweaver|status}" >&2
        exit 2
      fi
      exec ${cfg.package}/bin/astrumweaver-worker-mode "$1" \
        --service astrumweaver-worker.service \
        ${gpuUuidArgs} \
        --health-url ${lib.escapeShellArg "http://${cfg.healthHost}:${toString cfg.healthPort}"} \
        --systemctl ${pkgs.systemd}/bin/systemctl \
        --nvidia-smi ${nvidiaSmiCommand} \
        --drain-timeout-seconds ${toString cfg.borrowable.drainTimeoutSeconds} \
        --start-timeout-seconds ${toString cfg.borrowable.startTimeoutSeconds}
    '';
  };
in
{
  options.services.astrumweaver.worker = {
    enable = lib.mkEnableOption "AstrumWeaver Worker service";

    package = lib.mkOption {
      type = lib.types.package;
      default = defaultPackage;
      defaultText = lib.literalExpression "AstrumWeaver worker package from this module";
      description = "Worker runtime package.";
    };

    command = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "/custom/bin/astrumweaver-worker";
      description = "Optional daemon executable override. Defaults to the packaged astrumweaver-worker entrypoint.";
    };

    workerId = lib.mkOption {
      type = lib.types.str;
      default = "";
      example = "worker-1";
      description = "Stable scheduler-visible Worker identity.";
    };

    workerClass = lib.mkOption {
      type = lib.types.str;
      default = "generic";
      example = "modern-single";
      description = "Hardware/runtime topology class; not an application role.";
    };

    controlUrl = lib.mkOption {
      type = lib.types.str;
      default = "";
      example = "https://control.example.invalid";
      description = "AstrumWeaver Control API base URL.";
    };

    capabilities = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      example = [ "llm.chat" "image.generate" ];
      description = "Capabilities advertised by this Worker.";
    };

    labels = lib.mkOption {
      type = lib.types.attrsOf lib.types.str;
      default = { };
      example = { runtime_family = "modern"; };
    };

    gpuUuids = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      example = [ "GPU-example-a" "GPU-example-b" ];
      description = "Exact guest-visible NVIDIA GPU UUID set. Leave empty for a non-GPU Worker.";
    };

    gpuIsolation = {
      enable = lib.mkEnableOption "systemd device-cgroup isolation for the selected GPU UUID set";

      deviceMap = lib.mkOption {
        type = lib.types.attrsOf lib.types.str;
        default = { };
        example = {
          "GPU-example-a" = "/dev/nvidia0";
        };
        description = "Explicit UUID to physical /dev/nvidiaN mapping. Keys must exactly match gpuUuids when isolation is enabled.";
      };

      auxiliaryDeviceNodes = lib.mkOption {
        type = lib.types.listOf lib.types.str;
        default = sharedNvidiaDeviceNodes;
        description = "Shared NVIDIA control/UVM device nodes required by the runtime service in addition to selected per-GPU nodes.";
      };
    };

    accelerators = lib.mkOption {
      type = lib.types.listOf (lib.types.submodule {
        options = {
          uuid = lib.mkOption {
            type = lib.types.str;
            description = "Exact GPU UUID for this accelerator fact.";
          };
          memory_mb = lib.mkOption {
            type = lib.types.ints.positive;
            description = "Per-device VRAM in MiB.";
          };
          compute_capability = lib.mkOption {
            type = lib.types.nullOr lib.types.str;
            default = null;
            description = "Validated NVIDIA compute capability, e.g. 8.6.";
          };
          device_class = lib.mkOption {
            type = lib.types.nullOr lib.types.str;
            default = null;
            description = "Auditable accelerator device class/model when known.";
          };
        };
      });
      default = [ ];
      description = "Optional ordered per-device accelerator facts. UUID order must match gpuUuids.";
    };

    totalVramMb = lib.mkOption {
      type = lib.types.ints.unsigned;
      default = 0;
    };

    maxSingleGpuVramMb = lib.mkOption {
      type = lib.types.ints.unsigned;
      default = 0;
    };

    maxConcurrency = lib.mkOption {
      type = lib.types.ints.positive;
      default = 1;
      description = "Maximum concurrent jobs. The v1 daemon currently supports 1.";
    };

    executorFactory = lib.mkOption {
      type = lib.types.str;
      default = "";
      example = "my_executor:create_executor";
      description = "Configured JobExecutor factory in module:attribute form.";
    };

    executorSettings = lib.mkOption {
      type = lib.types.attrs;
      default = { };
      description = "Executor-specific non-secret configuration.";
    };

    settings = lib.mkOption {
      type = lib.types.attrs;
      default = { };
      description = "Additional non-secret Worker TOML settings. Core module-owned fields override conflicting values.";
    };

    environmentFile = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "/run/secrets/astrumweaver-worker.env";
      description = "Protected EnvironmentFile containing ASTRUMWEAVER_WORKER_TOKEN and other secret overrides.";
    };

    nvidiaSmiPackage = lib.mkOption {
      type = lib.types.nullOr lib.types.package;
      default = null;
      example = lib.literalExpression "config.hardware.nvidia.package";
      description = "Package providing nvidia-smi. Required when gpuUuids is non-empty.";
    };

    supplementaryGroups = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      example = [ "video" "render" ];
      description = "Host-specific groups needed for accelerator device access.";
    };

    user = lib.mkOption {
      type = lib.types.str;
      default = "astrumweaver";
    };

    group = lib.mkOption {
      type = lib.types.str;
      default = "astrumweaver";
    };

    healthHost = lib.mkOption {
      type = lib.types.str;
      default = "127.0.0.1";
    };

    healthPort = lib.mkOption {
      type = lib.types.port;
      default = 9100;
    };

    runtime = {
      enable = lib.mkEnableOption "RuntimeProvider-managed Worker execution";

      provider = lib.mkOption {
        type = lib.types.enum [
          "ollama"
          "llama-cpp"
          "vllm"
          "freetoken"
          "exllamav3"
          "voicevox"
          "style-bert-vits2"
          "parakeet-ja"
        ];
        default = "ollama";
        description = "Explicit first-class RuntimeProvider selected by the operator.";
      };

      packages = lib.mkOption {
        type = lib.types.listOf lib.types.package;
        default = [ ];
        description = "Packages that provide a non-audio runtime executable/backend. Owned audio uses separately reviewed pre-provisioned assets and requires this list to be empty.";
      };

      providerConfig = lib.mkOption {
        type = lib.types.attrs;
        default = { };
        description = "Provider-specific non-secret configuration persisted in the runtime deployment manifest.";
      };

      modelRef = lib.mkOption {
        type = lib.types.str;
        default = "";
        description = "Explicit model reference used by the RuntimeProvider.";
      };

      modelFormat = lib.mkOption {
        type = lib.types.str;
        default = "";
        description = "Provider-neutral model/package format.";
      };

      modelTopology = lib.mkOption {
        type = lib.types.enum [ "dense" "moe" ];
        default = "dense";
      };

      estimatedModelSizeMb = lib.mkOption {
        type = lib.types.nullOr lib.types.ints.positive;
        default = null;
      };

      residencyPolicy = lib.mkOption {
        type = lib.types.enum [
          "vram_only"
          "prefer_vram"
          "cpu_gpu_hybrid"
        ];
        default = "prefer_vram";
      };

      gpuTopology = lib.mkOption {
        type = lib.types.nullOr (lib.types.enum [
          "none"
          "single_gpu"
          "multi_gpu"
        ]);
        default = null;
        description = "GPU topology demand. null derives it from the Worker's exact gpuUuids.";
      };

      minGpuCount = lib.mkOption {
        type = lib.types.ints.unsigned;
        default = 0;
      };

      minTotalVramMb = lib.mkOption {
        type = lib.types.ints.unsigned;
        default = 0;
      };

      minSingleGpuVramMb = lib.mkOption {
        type = lib.types.ints.unsigned;
        default = 0;
      };

      minHostRamMb = lib.mkOption {
        type = lib.types.ints.unsigned;
        default = 0;
      };

      preferredHostRamMb = lib.mkOption {
        type = lib.types.ints.unsigned;
        default = 0;
      };

      modelMetadata = lib.mkOption {
        type = lib.types.attrs;
        default = { };
      };

      demandMetadata = lib.mkOption {
        type = lib.types.attrs;
        default = { };
      };

      startupTimeoutSeconds = lib.mkOption {
        type = lib.types.ints.positive;
        default = 600;
      };

      shutdownTimeoutSeconds = lib.mkOption {
        type = lib.types.ints.positive;
        default = 60;
      };
    };

    serving.declaration = lib.mkOption {
      type = lib.types.nullOr lib.types.attrs;
      default = null;
      description = ''
        Explicit reviewed, non-secret ServingDeploymentDeclaration JSON value.
        It is serialized into the standard serving.manifest; no catalog, pins,
        rights receipt, or validation evidence is invented. Required for an
        enabled owned audio provider. Retained values do not activate a disabled
        audio runtime. Full binding is rechecked by the daemon before startup.
      '';
    };

    borrowable = {
      enable = lib.mkEnableOption "borrowable GPU ownership mode for a development node";

      drainTimeoutSeconds = lib.mkOption {
        type = lib.types.ints.unsigned;
        default = 0;
        description = "Seconds to wait for the current job to drain. 0 waits indefinitely and never forces the job.";
      };

      startTimeoutSeconds = lib.mkOption {
        type = lib.types.ints.positive;
        default = 60;
        description = "Seconds to wait for the Worker service and local readiness when returning GPU ownership to AstrumWeaver.";
      };
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
    assertions = [
      {
        assertion = cfg.workerId != "";
        message = "services.astrumweaver.worker.workerId must be set.";
      }
      {
        assertion = cfg.controlUrl != "";
        message = "services.astrumweaver.worker.controlUrl must be set.";
      }
      {
        assertion = cfg.capabilities != [ ];
        message = "services.astrumweaver.worker.capabilities must not be empty.";
      }
      {
        assertion =
          if cfg.runtime.enable
          then cfg.executorFactory == ""
          else cfg.executorFactory != "";
        message = "Configure exactly one Worker execution path: runtime.enable=true with no executorFactory, or a non-empty executorFactory.";
      }
      {
        assertion = !cfg.runtime.enable || cfg.runtime.modelRef != "";
        message = "services.astrumweaver.worker.runtime.modelRef must be set when RuntimeProvider execution is enabled.";
      }
      {
        assertion = !cfg.runtime.enable || cfg.runtime.modelFormat != "";
        message = "services.astrumweaver.worker.runtime.modelFormat must be set when RuntimeProvider execution is enabled.";
      }
      {
        assertion = !cfg.runtime.enable || audioActive || cfg.runtime.packages != [ ];
        message = "services.astrumweaver.worker.runtime.packages must provide the selected runtime closure.";
      }
      {
        assertion = !cfg.runtime.enable || cfg.runtime.preferredHostRamMb >= cfg.runtime.minHostRamMb;
        message = "runtime.preferredHostRamMb must be >= runtime.minHostRamMb.";
      }
      {
        assertion =
          cfg.accelerators == [ ]
          || (
            builtins.length cfg.accelerators == builtins.length cfg.gpuUuids
            && map (item: item.uuid) cfg.accelerators == cfg.gpuUuids
          );
        message = "services.astrumweaver.worker.accelerators must be empty or exactly match gpuUuids order.";
      }
      {
        assertion = cfg.maxConcurrency == 1;
        message = "AstrumWeaver v1 Worker daemon currently requires maxConcurrency = 1.";
      }
      {
        assertion = builtins.length cfg.gpuUuids == builtins.length (lib.unique cfg.gpuUuids);
        message = "services.astrumweaver.worker.gpuUuids must not contain duplicates.";
      }
      {
        assertion = !cfg.gpuIsolation.enable || cfg.gpuUuids != [ ];
        message = "gpuIsolation.enable requires at least one selected GPU UUID.";
      }
      {
        assertion =
          !cfg.gpuIsolation.enable
          || lib.sort builtins.lessThan (builtins.attrNames cfg.gpuIsolation.deviceMap)
             == lib.sort builtins.lessThan cfg.gpuUuids;
        message = "gpuIsolation.deviceMap keys must exactly match services.astrumweaver.worker.gpuUuids.";
      }
      {
        assertion =
          !cfg.gpuIsolation.enable
          || builtins.all (
            path: builtins.match "^/dev/nvidia[0-9]+$" path != null
          ) gpuIsolationDevicePaths;
        message = "gpuIsolation.deviceMap values must be physical /dev/nvidiaN device paths.";
      }
      {
        assertion =
          !cfg.gpuIsolation.enable
          || builtins.all (
            path: builtins.match "^/dev/nvidia[-a-zA-Z0-9_/]+$" path != null
          ) cfg.gpuIsolation.auxiliaryDeviceNodes;
        message = "gpuIsolation.auxiliaryDeviceNodes must contain NVIDIA /dev paths only.";
      }
      {
        assertion = cfg.gpuUuids == [ ] || cfg.nvidiaSmiPackage != null;
        message = "services.astrumweaver.worker.nvidiaSmiPackage is required for GPU Workers.";
      }
      {
        assertion = !cfg.borrowable.enable || cfg.gpuUuids != [ ];
        message = "services.astrumweaver.worker.borrowable requires at least one GPU UUID.";
      }
      {
        assertion = !cfg.borrowable.enable || cfg.nvidiaSmiPackage != null;
        message = "services.astrumweaver.worker.borrowable requires nvidiaSmiPackage.";
      }
      {
        assertion = !cfg.borrowable.enable || builtins.elem cfg.healthHost [ "127.0.0.1" "localhost" ];
        message = "borrowable Worker healthHost must remain local (127.0.0.1 or localhost).";
      }
      {
        assertion =
          if cfg.gpuUuids == [ ]
          then cfg.totalVramMb == 0 && cfg.maxSingleGpuVramMb == 0
          else cfg.totalVramMb > 0
            && cfg.maxSingleGpuVramMb > 0
            && cfg.maxSingleGpuVramMb <= cfg.totalVramMb;
        message = "Worker VRAM shape must be zero for non-GPU Workers and positive/consistent for GPU Workers.";
      }
      {
        assertion = !(builtins.any (cap: builtins.elem cap [ "audio.synthesize" "audio.transcribe" ]) cfg.capabilities)
          || audioActive;
        message = "Worker audio capabilities require an explicitly enabled owned audio RuntimeProvider.";
      }
      {
        assertion = !servingActive || !(builtins.elem declarationProvider (builtins.attrNames audioProfiles))
          || audioActive;
        message = "An audio serving declaration requires the matching enabled owned audio RuntimeProvider.";
      }
    ] ++ lib.optionals audioActive [
      {
        assertion = (cfg.runtime.providerConfig.enabled or false) == true
          && audioDeclarationMatches && audioDemandMatches;
        message = "Worker audio requires literal providerConfig.enabled=true, the exact qualified demand, and one matching reviewed serving declaration.";
      }
      {
        assertion = pkgs.stdenv.hostPlatform.system == "x86_64-linux"
          && cfg.capabilities == [ audioCapability ]
          && cfg.maxConcurrency == 1 && cfg.healthHost == "127.0.0.1"
          && loopbackControl != null && lib.toInt (builtins.head loopbackControl) <= 65535;
        message = "Worker audio requires a dedicated single-slot x86_64-linux capability and loopback Control/health endpoints.";
      }
      {
        assertion = if audioProfile.gpu then
          builtins.length cfg.gpuUuids == 1
          && builtins.match "GPU-[A-Fa-f0-9]{8}(-[A-Fa-f0-9]{4}){3}-[A-Fa-f0-9]{12}" (builtins.head cfg.gpuUuids) != null
          && (cfg.runtime.providerConfig.gpu_uuid or null) == builtins.head cfg.gpuUuids
          && cfg.totalVramMb >= 11264 && cfg.maxSingleGpuVramMb >= 11264
          && cfg.gpuIsolation.enable
          && builtins.all (path: builtins.elem path sharedNvidiaDeviceNodes)
            cfg.gpuIsolation.auxiliaryDeviceNodes
          && (service.DevicePolicy or null) == "closed"
          && (service.DeviceAllow or [ ]) == gpuDeviceAllows
        else cfg.gpuUuids == [ ] && cfg.accelerators == [ ]
          && cfg.totalVramMb == 0 && cfg.maxSingleGpuVramMb == 0
          && !cfg.gpuIsolation.enable;
        message = "Worker audio requires CPU-only TTS or the exact isolated single Parakeet GPU with the declared 11264 MiB floor.";
      }
      {
        assertion = cfg.settings == { } && cfg.extraArgs == [ ]
          && cfg.executorSettings == { } && cfg.runtime.packages == [ ]
          && (service.ExecStart or null) == execStart;
        message = "Worker audio requires typed configuration only, no settings/extraArgs/executorSettings/runtime.packages or ExecStart override.";
      }
      {
        assertion = protectedEnvironment && (service.EnvironmentFile or null) == cfg.environmentFile;
        message = "Worker audio requires a protected canonical /etc or /run environmentFile without store context or service override.";
      }
      {
        assertion = protectedHome && (service.WorkingDirectory or null) == privateHome
          && (service.ReadWritePaths or [ ]) == [ privateHome ]
          && (service.StateDirectory or [ ]) == [ ]
          && (service.UMask or null) == "0077"
          && (service.ProtectSystem or null) == "strict"
          && (service.ProtectHome or false) == true;
        message = "Worker audio requires its pre-provisioned private_home beneath /var/lib/astrumweaver-audio with matching protected unit paths; no shared StateDirectory or permission repair.";
      }
      {
        assertion = staticAccountName cfg.user && !(builtins.elem cfg.user [ "root" "astrumweaver" ])
          && staticAccountName cfg.group && cfg.group != "root"
          && (service.User or null) == cfg.user && (service.Group or null) == cfg.group
          && !(service.DynamicUser or false)
          && (config.users.users.${cfg.user}.uid or null) != 0
          && (config.users.groups.${cfg.group}.gid or null) != 0
          && config.users.users.${cfg.user}.home == "/var/empty"
          && !config.users.users.${cfg.user}.createHome
          && (service.NoNewPrivileges or false) == true
          && (service.Restart or null) == "no"
          && (service.KillMode or null) == "control-group";
        message = "Worker audio requires a dedicated non-root static user, no automatic home provisioning/restart, and owned control-group shutdown.";
      }
    ];

    system.build = lib.optionalAttrs audioActive {
      astrumweaverAudioWorkerConfig = audioConfigBundle;
    };

    users.groups.${cfg.group} = { };
    users.users.${cfg.user} = {
      isSystemUser = lib.mkDefault true;
      group = lib.mkDefault cfg.group;
      extraGroups = cfg.supplementaryGroups;
      home = lib.mkDefault (if audioActive then "/var/empty" else "/var/lib/astrumweaver");
      createHome = lib.mkDefault (!audioActive);
    };

    environment.systemPackages =
      [ cfg.package ]
      ++ lib.optional cfg.borrowable.enable borrowableMode;

    systemd.services.astrumweaver-worker-gpu-isolation-preflight =
      lib.mkIf (cfg.gpuIsolation.enable && cfg.gpuUuids != [ ]) {
        description = "Verify AstrumWeaver Worker GPU UUID/device mapping";
        before = [ "astrumweaver-worker.service" ];
        path = [ gpuDeviceMapVerifier ]
          ++ lib.optional (cfg.nvidiaSmiPackage != null) cfg.nvidiaSmiPackage;
        serviceConfig = {
          Type = "oneshot";
          ExecStart = "${gpuDeviceMapVerifier}/bin/astrumweaver-gpu-device-map verify ${expectedGpuUuids} ${gpuIsolationMap}";
        };
      };

    systemd.services.astrumweaver-worker = {
      description = "AstrumWeaver Worker";
      wantedBy = [ "multi-user.target" ];
      wants = [ "network-online.target" ];
      after = [ "network-online.target" ]
        ++ lib.optional cfg.gpuIsolation.enable "astrumweaver-worker-gpu-isolation-preflight.service";
      requires = lib.optional cfg.gpuIsolation.enable "astrumweaver-worker-gpu-isolation-preflight.service";
      path = [ cfg.package ]
        ++ lib.optional (cfg.nvidiaSmiPackage != null) cfg.nvidiaSmiPackage
        ++ (if audioSelected && !cfg.runtime.enable then [ ] else cfg.runtime.packages)
        ++ cfg.extraPackages;

      serviceConfig = {
        Type = "simple";
        User = cfg.user;
        Group = cfg.group;
        ExecStart = execStart;
        Restart = if audioActive then "no" else "on-failure";
        RestartSec = "5s";
        RuntimeDirectory = "astrumweaver-worker";
        RuntimeDirectoryMode = if audioActive then "0700" else "0750";
        StateDirectory = if audioActive then [ ] else "astrumweaver";
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
      // lib.optionalAttrs audioActive {
        WorkingDirectory = privateHome;
        ReadWritePaths = [ privateHome ];
        UMask = "0077";
        KillMode = "control-group";
      }
      // lib.optionalAttrs cfg.gpuIsolation.enable {
        DevicePolicy = "closed";
        DeviceAllow = gpuDeviceAllows;
        Environment = [
          "CUDA_VISIBLE_DEVICES=${lib.concatStringsSep "," cfg.gpuUuids}"
          "ASTRUMWEAVER_GPU_PREFLIGHT_MODE=isolated-access"
          "ASTRUMWEAVER_GPU_DEVICE_MAP=${gpuIsolationMap}"
          "ASTRUMWEAVER_GPU_WORKER_CONFIG=${generatedConfig}"
          "ASTRUMWEAVER_GPU_DEVICE_MAP_COMMAND=${gpuDeviceMapVerifier}/bin/astrumweaver-gpu-device-map"
          "ASTRUMWEAVER_NVIDIA_SMI=${nvidiaSmiCommand}"
        ];
      }
      // lib.optionalAttrs (cfg.gpuUuids != [ ]) {
        ExecStartPre = "+${preflight}/bin/astrumweaver-gpu-preflight ${expectedGpuUuids}";
      }
      // lib.optionalAttrs (cfg.environmentFile != null) {
        EnvironmentFile = cfg.environmentFile;
      };
    };
  };
}
