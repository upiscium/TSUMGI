{ pkgs, nixpkgs, system }:

let
  inherit (pkgs) lib;
  workerModule = ../modules/worker.nix;
  gpu = "GPU-01234567-89ab-cdef-0123-456789abcdef";
  # Nix validates the declaration's structural audio binding. These synthetic
  # pins are intentionally not daemon-verified or used to launch a runtime.
  digest = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
  declaration = provider: capability: schema: {
    schema_version = "serving-deployment-v1";
    deployment = {
      schema_version = "deployment-identity-v1";
      provider_id = provider;
      runtime_artifact_sha256 = digest;
      adapter_artifact_sha256 = digest;
      model_artifact_sha256 = digest;
      execution_config_sha256 = digest;
      quantization = "none";
      tokenizer_artifact_sha256 = null;
      template_artifact_sha256 = null;
    };
    contracts = [{
      serving_contract_revision = digest;
      contract = {
        schema_version = "serving-contract-v1";
        deployment_revision = digest;
        inherit capability;
        operation_schema = schema;
        validation_evidence_sha256 = digest;
        features = [ ];
        limits = { };
        semantic_revision = digest;
      };
    }];
  };
  base = {
    imports = [ workerModule ];
    nixpkgs.pkgs = pkgs;
    system.stateVersion = "26.05";
    boot.isContainer = true;
    fileSystems."/" = { device = "none"; fsType = "tmpfs"; };
    services.astrumweaver.worker = {
      enable = lib.mkDefault true;
      package = pkgs.coreutils;
      workerId = "audio-module-test";
      capabilities = lib.mkDefault [ "llm.chat" ];
      executorFactory = lib.mkDefault "test:create_executor";
      controlUrl = lib.mkDefault "http://127.0.0.1:8123";
    };
  };
  evaluate = options: extra: (nixpkgs.lib.nixosSystem {
    inherit system;
    modules = [ base { services.astrumweaver.worker = options; } extra ];
  }).config;
  baseline = evaluate { } { };
  audioWith = provider: overrides: extra:
    let
      stt = provider == "parakeet-ja";
      capability = if stt then "audio.transcribe" else "audio.synthesize";
      schema = if stt then "openai-audio-transcription-metadata-v1" else "openai-audio-speech-v1";
      common = {
        capabilities = [ capability ];
        executorFactory = "";
        user = "audio-test";
        group = "audio-test";
        environmentFile = "/run/secrets/audio.env";
        healthHost = "127.0.0.1";
        runtime = {
          enable = true;
          inherit provider;
          providerConfig = {
            enabled = true;
            private_home = "/var/lib/astrumweaver-audio/${provider}";
          } // lib.optionalAttrs stt { gpu_uuid = gpu; };
          packages = [ ];
          modelRef = if stt then "nvidia/parakeet-tdt_ctc-0.6b-ja"
            else if provider == "voicevox" then "model/0.vvm" else "jvnv-F1-jp";
          modelFormat = if stt then "nemo" else if provider == "voicevox" then "vvm" else "safetensors";
          residencyPolicy = if stt then "vram_only" else "cpu_gpu_hybrid";
          gpuTopology = if stt then "single_gpu" else "none";
          minGpuCount = if stt then 1 else 0;
          minTotalVramMb = if stt then 11264 else 0;
          minSingleGpuVramMb = if stt then 11264 else 0;
        };
        serving.declaration = declaration provider capability schema;
      } // lib.optionalAttrs stt {
        gpuUuids = [ gpu ];
        totalVramMb = 11264;
        maxSingleGpuVramMb = 11264;
        nvidiaSmiPackage = pkgs.coreutils;
        gpuIsolation = { enable = true; deviceMap.${gpu} = "/dev/nvidia0"; };
      };
    in evaluate (lib.recursiveUpdate common overrides) extra;
  audio = provider: overrides: audioWith provider overrides { };
  tts = audio "voicevox" { };
  sbv = audio "style-bert-vits2" { };
  parakeet = audio "parakeet-ja" { };
  serviceOf = cfg: cfg.systemd.services.astrumweaver-worker;
  accepted = cfg: builtins.all (entry: entry.assertion) cfg.assertions;
  rejectedFor = prefix: cfg: builtins.any
    (entry: !entry.assertion && lib.hasPrefix prefix entry.message)
    cfg.assertions;
  rejected = rejectedFor "Worker audio";
  change = overrides: audio "voicevox" overrides;
  badTts = [
    { capabilities = [ "audio.transcribe" ]; }
    { capabilities = [ "audio.synthesize" "llm.chat" ]; }
    { runtime.provider = "ollama"; }
    { runtime.providerConfig.enabled = false; }
    { serving.declaration = null; }
    { runtime.modelRef = "wrong"; }
    { runtime.modelFormat = "wrong"; }
    { runtime.residencyPolicy = "vram_only"; }
    { runtime.packages = [ pkgs.coreutils ]; }
    { settings.worker.id = "override"; }
    { settings = { }; extraArgs = [ "--config" "/other.toml" ]; }
    { executorSettings.extra = "override"; }
    { user = "astrumweaver"; }
    { user = "root"; }
    { user = "0"; }
    { user = "1000"; }
    { group = "root"; }
    { group = "0"; }
    { group = "1000"; }
    { runtime.providerConfig.private_home = "/tmp/audio"; }
    { runtime.providerConfig.private_home = "/var/lib/astrumweaver-audio/../unsafe"; }
    { environmentFile = "/nix/store/secret.env"; }
    { environmentFile = null; }
    { environmentFile = "/run/../etc/audio.env"; }
    { environmentFile = "/run/secrets/${pkgs.coreutils}/audio.env"; }
    { controlUrl = "http://0.0.0.0:8123"; }
    { healthHost = "0.0.0.0"; }
    { maxConcurrency = 2; }
    { totalVramMb = 11264; maxSingleGpuVramMb = 11264; }
  ];
  badServing = [
    (declaration "style-bert-vits2" "audio.synthesize" "openai-audio-speech-v1")
    (declaration "voicevox" "audio.transcribe" "openai-audio-transcription-metadata-v1")
    (declaration "voicevox" "audio.synthesize" "wrong-schema")
  ];
  parakeetBad = [
    { gpuUuids = [ ]; }
    { runtime.providerConfig.gpu_uuid = "GPU-ffffffff-ffff-ffff-ffff-ffffffffffff"; }
    { totalVramMb = 8192; maxSingleGpuVramMb = 8192; runtime.minTotalVramMb = 8192; runtime.minSingleGpuVramMb = 8192; }
    { gpuIsolation.enable = false; }
    { gpuIsolation.auxiliaryDeviceNodes = [ "/dev/nvidia1" ]; }
    { gpuIsolation.auxiliaryDeviceNodes = [ "/dev/nvidia-caps/../nvidia1" ]; }
  ];
  retainedDisabled = evaluate {
    runtime.enable = false;
    runtime.provider = "voicevox";
    runtime.providerConfig = { enabled = false; private_home = "not-a-path"; };
    capabilities = [ "llm.chat" ];
    serving.declaration = { schema_version = "unvalidated-retained-value"; deployment = null; contracts = [ ]; };
  } { };
  lazyDisabled = evaluate {
    runtime = {
      enable = false;
      provider = "parakeet-ja";
      providerConfig = throw "disabled audio provider config was forced";
      packages = throw "disabled audio packages were forced";
    };
    serving.declaration = throw "disabled audio serving declaration was forced";
  } { };
  nonAudio = evaluate {
    executorFactory = "";
    runtime = {
      enable = true;
      provider = "ollama";
      modelRef = "synthetic-non-audio-model";
      modelFormat = "gguf";
      packages = [ pkgs.hello ];
    };
    settings.non_audio.retained = true;
  } { };
in
assert accepted baseline;
assert (serviceOf baseline).serviceConfig.Restart == "on-failure";
assert !(baseline.system.build ? astrumweaverAudioWorkerConfig);
assert accepted tts && accepted sbv && accepted parakeet;
assert (serviceOf tts).serviceConfig.Restart == "no";
assert (serviceOf tts).serviceConfig.User == "audio-test";
assert (serviceOf tts).serviceConfig.Group == "audio-test";
assert (serviceOf tts).serviceConfig.WorkingDirectory == "/var/lib/astrumweaver-audio/voicevox";
assert (serviceOf tts).serviceConfig.ReadWritePaths == [ "/var/lib/astrumweaver-audio/voicevox" ];
assert (serviceOf tts).serviceConfig.StateDirectory == [ ];
assert (serviceOf tts).serviceConfig.RuntimeDirectoryMode == "0700";
assert (serviceOf tts).serviceConfig.UMask == "0077";
assert (serviceOf tts).serviceConfig.KillMode == "control-group";
assert tts.users.users.audio-test.home == "/var/empty" && !tts.users.users.audio-test.createHome;
assert tts.system.build.astrumweaverAudioWorkerConfig.reviewed.serving == declaration "voicevox" "audio.synthesize" "openai-audio-speech-v1";
assert parakeet.system.build.astrumweaverAudioWorkerConfig.reviewed.runtime.demand.min_single_gpu_vram_mb == 11264;
assert accepted retainedDisabled;
assert !(retainedDisabled.system.build ? astrumweaverAudioWorkerConfig);
assert (serviceOf retainedDisabled).serviceConfig
  == (serviceOf baseline).serviceConfig;
assert (serviceOf retainedDisabled).after == (serviceOf baseline).after;
assert (serviceOf retainedDisabled).requires == (serviceOf baseline).requires;
assert (serviceOf retainedDisabled).path == (serviceOf baseline).path;
assert retainedDisabled.systemd.tmpfiles.rules == baseline.systemd.tmpfiles.rules;
assert accepted lazyDisabled;
assert (serviceOf lazyDisabled).serviceConfig == (serviceOf baseline).serviceConfig;
assert (serviceOf lazyDisabled).path == (serviceOf baseline).path;
assert !(lazyDisabled.system.build ? astrumweaverAudioWorkerConfig);
assert accepted nonAudio;
assert builtins.elem pkgs.hello (serviceOf nonAudio).path;
assert (serviceOf nonAudio).serviceConfig.Restart == "on-failure";
assert !(nonAudio.system.build ? astrumweaverAudioWorkerConfig);
assert builtins.all (overrides: rejected (change overrides)) badTts;
assert builtins.all (serving: rejected (change { serving.declaration = serving; })) badServing;
assert builtins.all (overrides: rejected (audio "parakeet-ja" overrides)) parakeetBad;
assert rejectedFor "gpuIsolation.deviceMap values" (audio "parakeet-ja" {
  gpuIsolation.deviceMap.${gpu} = "/dev/not-nvidia";
});
assert accepted (audio "parakeet-ja" { gpuIsolation.deviceMap.${gpu} = "/dev/nvidia1"; });
assert rejected (audioWith "parakeet-ja" { } {
  systemd.services.astrumweaver-worker.serviceConfig.DevicePolicy = lib.mkForce "auto";
});
assert rejected (audioWith "parakeet-ja" { } {
  systemd.services.astrumweaver-worker.serviceConfig.DeviceAllow = lib.mkForce [ "/dev/nvidia1 rw" ];
});
assert rejected (audioWith "voicevox" { } {
  systemd.services.astrumweaver-worker.serviceConfig.User = lib.mkForce "nobody";
});
assert rejected (audioWith "voicevox" { } {
  systemd.services.astrumweaver-worker.serviceConfig.ExecStart = lib.mkForce "/bin/true";
});
assert rejected (audioWith "voicevox" { } {
  systemd.services.astrumweaver-worker.serviceConfig.WorkingDirectory = lib.mkForce "/tmp";
});
assert rejected (audioWith "voicevox" { } {
  systemd.services.astrumweaver-worker.serviceConfig.DynamicUser = lib.mkForce true;
});
assert rejected (audioWith "voicevox" { } {
  systemd.services.astrumweaver-worker.serviceConfig.Restart = lib.mkForce "on-failure";
});
assert rejected (audioWith "voicevox" { } {
  users.users.audio-test.uid = lib.mkForce 0;
});
assert !(evaluate { enable = false; } { }).systemd.services ? astrumweaver-worker;
assert rejected (evaluate {
  runtime.enable = false;
  runtime.provider = "voicevox";
  capabilities = [ "audio.synthesize" ];
  serving.declaration = declaration "voicevox" "audio.synthesize" "openai-audio-speech-v1";
} { });
assert accepted (evaluate {
  runtime.enable = false;
  runtime.provider = "voicevox";
  capabilities = [ "llm.chat" ];
  serving.declaration = declaration "voicevox" "audio.synthesize" "openai-audio-speech-v1";
} { });
pkgs.runCommand "astrumweaver-audio-worker-module-evaluation" { } ''
  mkdir -p "$out"
  printf '%s\n' 'NixOS audio Worker source-module assertions passed at evaluation' > "$out/evidence"
''
