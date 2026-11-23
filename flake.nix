{
  description = "TSUMGI deployment-agnostic heterogeneous compute fabric";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs { inherit system; };
      runtimePkgs = import nixpkgs {
        inherit system;
        config.allowUnfree = true;
      };

      tsumgi = pkgs.callPackage ./nix/package.nix { };
      integration = pkgs.callPackage ./nix/integration-package.nix { };
      runtimeLlamaCpp = runtimePkgs.llama-cpp-cuda;
      qwen38LlamaCore = runtimePkgs.callPackage ./nix/qwen38-llama-cpp.nix { };
      qwen38LlamaBridge = runtimePkgs.callPackage ./nix/qwen38-llama-driver-bridge.nix {
        core = qwen38LlamaCore;
      };
      runtimeQwen38 = runtimePkgs.buildEnv {
        name = "tsumgi-qwen38-llama-cpp-runtime";
        paths = [ qwen38LlamaCore qwen38LlamaBridge ];
      };
      runtimeProfileSupport = pkgs.callPackage ./nix/runtime-profile-support.nix {
        source = self;
      };
      control = pkgs.callPackage ./nix/control-support.nix {
        inherit tsumgi integration;
      };
      worker = pkgs.callPackage ./nix/worker-support.nix {
        inherit tsumgi integration;
      };
      installer = pkgs.callPackage ./nix/installer-support.nix {
        inherit tsumgi integration runtimeProfileSupport;
      };
      generatedNixosCheck = pkgs.writeShellApplication {
        name = "check-generated-nixos";
        runtimeInputs = [
          (pkgs.python312.withPackages (_: [ tsumgi ]))
          pkgs.nix
        ];
        text = ''
          exec python3 ${self}/tools/check_generated_nixos.py --source ${self} "$@"
        '';
      };
      # Experimental opt-in GPU runtime, distinct from the default Worker.
      cudaPkgs = import nixpkgs {
        inherit system;
        config.allowUnfree = true;
      };
      decisionLlamaCuda = cudaArch:
        let
          core = cudaPkgs.callPackage ./nix/decision-llama-cuda.nix { inherit cudaArch; };
          driverBridge = cudaPkgs.callPackage ./nix/decision-llama-driver-bridge.nix {
            inherit core;
          };
        in
        cudaPkgs.buildEnv {
          name = "tsumgi-d1-decision-cuda-sm${cudaArch}-runtime";
          paths = [ core driverBridge ];
        };
      fakeNvidia = pkgs.writeShellScriptBin "nvidia-smi" ''
        if [ "$1" = "--query-gpu=uuid" ]; then
          echo GPU-example-smoke
          exit 0
        fi
        exit 0
      '';
      fakeOllama = pkgs.writeShellScriptBin "ollama" ''
        exit 0
      '';

      moduleSmoke = nixpkgs.lib.nixosSystem {
        inherit system;
        modules = [
          self.nixosModules.default
          ({ ... }: {
            system.stateVersion = "26.05";
            boot.isContainer = true;
            fileSystems."/" = {
              device = "none";
              fsType = "tmpfs";
            };

            services.tsumgi.control = {
              enable = true;
              package = control;
              clientAuth = "none";
              settings.control = {
                host = "127.0.0.1";
                port = 9000;
              };
            };

            services.tsumgi.worker = {
              enable = true;
              package = worker;
              workerId = "smoke-worker";
              workerClass = "modern-single";
              controlUrl = "http://127.0.0.1:9000";
              capabilities = [ "debug.echo" ];
              gpuUuids = [ "GPU-example-smoke" ];
              totalVramMb = 16384;
              maxSingleGpuVramMb = 16384;
              executorFactory = "tsumgi.executors.structured_echo:create_executor";
              nvidiaSmiPackage = fakeNvidia;
              borrowable.enable = true;
            };
          })
        ];
      };
      runtimeModuleSmoke = nixpkgs.lib.nixosSystem {
        inherit system;
        modules = [
          self.nixosModules.default
          ({ ... }: {
            system.stateVersion = "26.05";
            boot.isContainer = true;
            fileSystems."/" = {
              device = "none";
              fsType = "tmpfs";
            };

            services.tsumgi.worker = {
              enable = true;
              package = worker;
              workerId = "runtime-smoke-worker";
              workerClass = "modern-single";
              controlUrl = "http://127.0.0.1:9000";
              capabilities = [ "llm.chat" "text.generate" ];
              gpuUuids = [ "GPU-example-smoke" ];
              accelerators = [
                {
                  uuid = "GPU-example-smoke";
                  memory_mb = 16384;
                  compute_capability = "8.6";
                  device_class = "NVIDIA Test GPU";
                }
              ];
              totalVramMb = 16384;
              maxSingleGpuVramMb = 16384;
              nvidiaSmiPackage = fakeNvidia;
              gpuIsolation = {
                enable = true;
                deviceMap."GPU-example-smoke" = "/dev/nvidia7";
                auxiliaryDeviceNodes = [ "/dev/nvidiactl" ];
              };

              runtime = {
                enable = true;
                provider = "ollama";
                packages = [ fakeOllama ];
                modelRef = "qwen3:8b";
                modelFormat = "ollama";
                modelTopology = "dense";
                residencyPolicy = "prefer_vram";
                providerConfig.keep_alive = "5m";
              };
            };
          })
        ];
      };
      gpuIsolationNoGpuSmoke = nixpkgs.lib.nixosSystem {
        inherit system;
        modules = [
          self.nixosModules.default
          ({ ... }: {
            system.stateVersion = "26.05";
            boot.isContainer = true;
            services.tsumgi.worker = {
              enable = true;
              gpuIsolation.enable = true;
            };
          })
        ];
      };
      gpuIsolationNoGpuRejected = builtins.any
        (assertion:
          !assertion.assertion
          && assertion.message == "gpuIsolation.enable requires at least one selected GPU UUID.")
        gpuIsolationNoGpuSmoke.config.assertions;
      borrowableModePackages = builtins.filter (
        package:
          nixpkgs.lib.getName package == "tsumgi-gpu-mode"
      ) moduleSmoke.config.environment.systemPackages;
      borrowableMode = builtins.head borrowableModePackages;
      runtimeWorkerExec =
        runtimeModuleSmoke.config.systemd.services.tsumgi-worker.serviceConfig.ExecStart;
      runtimeWorkerPath = nixpkgs.lib.makeBinPath
        runtimeModuleSmoke.config.systemd.services.tsumgi-worker.path;
      runtimeDevicePolicy =
        runtimeModuleSmoke.config.systemd.services.tsumgi-worker.serviceConfig.DevicePolicy;
      runtimeDeviceAllow =
        runtimeModuleSmoke.config.systemd.services.tsumgi-worker.serviceConfig.DeviceAllow;
      runtimeEnvironment =
        runtimeModuleSmoke.config.systemd.services.tsumgi-worker.serviceConfig.Environment;
      runtimeWorkerPreflight =
        runtimeModuleSmoke.config.systemd.services.tsumgi-worker.serviceConfig.ExecStartPre;
      runtimeIsolationPreflight =
        runtimeModuleSmoke.config.systemd.services.tsumgi-worker-gpu-isolation-preflight.serviceConfig.ExecStart;
    in
    {
      packages.${system} = {
        inherit tsumgi control worker installer integration;
        runtime-llama-cpp = runtimeLlamaCpp;
        runtime-qwen38-llama-cpp = runtimeQwen38;
        decision-llama-cuda-sm61 = decisionLlamaCuda "61";
        decision-llama-cuda-sm86 = decisionLlamaCuda "86";
        default = tsumgi;
      };

      lib.mkVoicevoxCpuBundle = { pkgs, archive, rightsReviewSha256 }:
        pkgs.callPackage ./nix/voicevox-cpu-bundle.nix {
          inherit archive rightsReviewSha256;
          sevenzip = pkgs._7zz;
        };

      # Explicit, already-acquired inputs only. This factory does not call the
      # bundle extractor, add a package to the default closure or wire a unit.
      lib.mkVoicevoxServiceView = { pkgs, libcArchive, libcRoot, engineRoot }:
        pkgs.callPackage ./nix/voicevox-service-view.nix {
          inherit libcArchive libcRoot engineRoot;
        };

      nixosModules = {
        control = import ./nix/modules/control.nix;
        worker = import ./nix/modules/worker.nix;
        default = import ./nix/modules/default.nix;
      };

      apps.${system}.check-generated-nixos = {
        type = "app";
        program = "${generatedNixosCheck}/bin/check-generated-nixos";
      };

      nixosConfigurations.smoke = moduleSmoke;

      checks.${system} = {
        inherit tsumgi control worker installer integration;

        setup-permissions = import ./nix/tests/setup-permissions.nix {
          inherit pkgs integration;
        };

        control-postgres-cli = import ./nix/tests/control-postgres-cli.nix {
          inherit pkgs control installer;
        };

        audio-control-module = import ./nix/tests/audio-control-module.nix {
          inherit pkgs nixpkgs system;
        };

        audio-worker-module = import ./nix/tests/audio-worker-module.nix {
          inherit pkgs nixpkgs system;
        };

        voicevox-bundle = import ./nix/tests/voicevox-bundle.nix {
          inherit nixpkgs system;
        };

        voicevox-service-view = import ./nix/tests/voicevox-service-view.nix {
          inherit nixpkgs system;
        };

        audio-control-package = import ./nix/tests/audio-control-package.nix {
          inherit pkgs control;
        };

        installation-surface = pkgs.runCommand "tsumgi-installation-surface" { } ''
          test -x ${installer}/bin/tsumgi-setup-tui
          test -x ${installer}/bin/tsumgi-control
          test -x ${installer}/bin/tsumgi-migrate
          test -x ${installer}/bin/tsumgi-worker
          test -x ${installer}/bin/tsumgi-setup-control-plane
          test -x ${installer}/bin/tsumgi-setup-gpu-worker
          test -x ${installer}/bin/tsumgi-gpu-isolation-probe
          test -x ${installer}/bin/tsumgi-runtime-profile
          ${installer}/bin/tsumgi-runtime-profile --help >/dev/null
          ${installer}/bin/tsumgi-runtime-profile status llama-cpp >/dev/null 2>&1 || test "$?" = 1
          ${installer}/bin/tsumgi-runtime-profile status qwen38-llama-cpp >/dev/null 2>&1 || test "$?" = 1
          # The retired Bonsai target must hit the unsupported-package exit 2.
          if ${installer}/bin/tsumgi-runtime-profile status bonsai-llama-cpp >/dev/null 2>&1; then exit 1; else test "$?" = 2; fi
          test -x ${control}/bin/tsumgi-control
          test -x ${control}/bin/tsumgi-migrate
          test -x ${control}/bin/tsumgi-setup-control-plane
          test -x ${worker}/bin/tsumgi-worker
          test -x ${worker}/bin/tsumgi-setup-gpu-worker
          test -x ${worker}/bin/tsumgi-gpu-isolation-probe
          test -x ${worker}/bin/tsumgi-setup-tui
          test -x ${worker}/bin/tsumgi-runtime-deployment-accept
          test -x ${worker}/bin/tsumgi-opencode-chat-accept

          # Execute the actual packaged TUI wrapper through a profile-like
          # symlink.  The wrapper must pass that lexical bin directory into
          # Python; synthetic sys.argv[0] assignment is deliberately not used.
          TEST_ROOT="$PWD/installation-surface-test"
          mkdir -p "$TEST_ROOT/installer-generation-a" "$TEST_ROOT/installer-generation-b"
          cp -a ${installer}/. "$TEST_ROOT/installer-generation-a/"
          cp -a ${installer}/. "$TEST_ROOT/installer-generation-b/"
          ln -s "$TEST_ROOT/installer-generation-a" "$TEST_ROOT/installer-profile"
          PROFILE="$TEST_ROOT/installer-profile"
          MINIMAL_PATH=${pkgs.coreutils}/bin:${pkgs.bash}/bin
          PATH="$MINIMAL_PATH" \
            "$PROFILE/bin/tsumgi-setup-tui" --check-packaging \
            > "$TEST_ROOT/packaging-a"
          grep -Fq "packaged tool authority: $PROFILE/bin" "$TEST_ROOT/packaging-a"
          grep -Fq "tsumgi-worker: $PROFILE/bin/tsumgi-worker" "$TEST_ROOT/packaging-a"
          grep -Fq "tsumgi-control: $PROFILE/bin/tsumgi-control" "$TEST_ROOT/packaging-a"
          (
            cd "$TEST_ROOT"
            PATH="$PROFILE/bin:$MINIMAL_PATH" tsumgi-setup-tui --check-packaging \
              > "$TEST_ROOT/packaging-bare"
          )
          grep -Fq "packaged tool authority: $PROFILE/bin" "$TEST_ROOT/packaging-bare"

          mkdir -p "$TEST_ROOT/input" "$TEST_ROOT/root"
          cat > "$TEST_ROOT/input/control.toml" <<'EOF'
[control]
host = "127.0.0.1"
port = 9000
EOF
          cat > "$TEST_ROOT/input/worker.toml" <<'EOF'
[worker]
id = "profile-convergence-worker"
class = "modern-single"
gpu_uuids = ["GPU-profile-convergence"]
EOF
          "$PROFILE/bin/tsumgi-setup-control-plane" \
            --config "$TEST_ROOT/input/control.toml" \
            --executable "$PROFILE/bin/tsumgi-control" \
            --root "$TEST_ROOT/root"
          "$PROFILE/bin/tsumgi-setup-gpu-worker" \
            --config "$TEST_ROOT/input/worker.toml" \
            --executable "$PROFILE/bin/tsumgi-worker" \
            --root "$TEST_ROOT/root"
          cp "$TEST_ROOT/root/etc/systemd/system/tsumgi-control.service" "$TEST_ROOT/control-a"
          cp "$TEST_ROOT/root/etc/systemd/system/tsumgi-worker.service" "$TEST_ROOT/worker-a"
          cp "$TEST_ROOT/root/usr/local/libexec/tsumgi/gpu-device-map" "$TEST_ROOT/gpu-map-a"
          grep -Fq "ExecStart=$PROFILE/bin/tsumgi-control " "$TEST_ROOT/control-a"
          grep -Fq "ExecStart=$PROFILE/bin/tsumgi-worker " "$TEST_ROOT/worker-a"
          ! grep -Fq "/nix/store/" "$TEST_ROOT/control-a"
          ! grep -Fq "/nix/store/" "$TEST_ROOT/worker-a"
          ! grep -Fq "/nix/store/" "$TEST_ROOT/gpu-map-a"

          rm -f "$PROFILE"
          ln -s "$TEST_ROOT/installer-generation-b" "$PROFILE"
          PATH="$MINIMAL_PATH" \
            "$PROFILE/bin/tsumgi-setup-tui" --check-packaging \
            > "$TEST_ROOT/packaging-b"
          grep -Fq "packaged tool authority: $PROFILE/bin" "$TEST_ROOT/packaging-b"
          "$PROFILE/bin/tsumgi-setup-control-plane" \
            --config "$TEST_ROOT/input/control.toml" \
            --executable "$PROFILE/bin/tsumgi-control" \
            --root "$TEST_ROOT/root"
          "$PROFILE/bin/tsumgi-setup-gpu-worker" \
            --config "$TEST_ROOT/input/worker.toml" \
            --executable "$PROFILE/bin/tsumgi-worker" \
            --root "$TEST_ROOT/root"
          cmp "$TEST_ROOT/control-a" "$TEST_ROOT/root/etc/systemd/system/tsumgi-control.service"
          cmp "$TEST_ROOT/worker-a" "$TEST_ROOT/root/etc/systemd/system/tsumgi-worker.service"
          cmp "$TEST_ROOT/gpu-map-a" "$TEST_ROOT/root/usr/local/libexec/tsumgi/gpu-device-map"

          if PATH="$MINIMAL_PATH" "$PROFILE/bin/tsumgi-runtime-deployment-accept" \
            --gpu-uuid GPU-profile-convergence \
            --revision deadbeef12345678 \
            --deployment-path systemd \
            --worker-config "$TEST_ROOT/missing-worker.toml" \
            --systemctl ${pkgs.coreutils}/bin/false \
            --nvidia-smi ${pkgs.coreutils}/bin/false \
            > "$TEST_ROOT/runtime-acceptance-resolution" 2>&1; then
            exit 1
          fi
          ! grep -Fq "reviewed packaged GPU mapper is unavailable" "$TEST_ROOT/runtime-acceptance-resolution"
          test "$(readlink "$PROFILE")" = "$TEST_ROOT/installer-generation-b"
          touch "$out"
        '';
        hardware-accept-cli = pkgs.runCommand "tsumgi-hardware-accept-cli" { } ''
          test -x ${worker}/bin/tsumgi-hardware-accept
          ${worker}/bin/tsumgi-hardware-accept --help >/dev/null
          touch "$out"
        '';
        runtime-deployment-accept-cli = pkgs.runCommand "tsumgi-runtime-deployment-accept-cli" { } ''
          test -x ${worker}/bin/tsumgi-runtime-deployment-accept
          ${worker}/bin/tsumgi-runtime-deployment-accept --help >/dev/null
          touch "$out"
        '';
        opencode-chat-accept-cli = pkgs.runCommand "tsumgi-opencode-chat-accept-cli" { } ''
          test -x ${worker}/bin/tsumgi-opencode-chat-accept
          ${worker}/bin/tsumgi-opencode-chat-accept --help >/dev/null
          touch "$out"
        '';
        runtime-module-eval = pkgs.runCommand "tsumgi-runtime-module-eval" {
          inherit
            runtimeWorkerExec
            runtimeWorkerPath
            runtimeDevicePolicy
            runtimeWorkerPreflight
            runtimeIsolationPreflight
            ;
          runtimeDeviceAllowText = nixpkgs.lib.concatStringsSep "\n" runtimeDeviceAllow;
          runtimeEnvironmentText = nixpkgs.lib.concatStringsSep "\n" runtimeEnvironment;
        } ''
          test -n "$runtimeWorkerExec"
          printf "%s" "$runtimeWorkerExec" | grep tsumgi-worker >/dev/null
          printf "%s" "$runtimeWorkerPath" | grep ollama >/dev/null
          workerConfig="$(printf "%s" "$runtimeWorkerExec" | sed -n 's/.*--config \([^ ]*\).*/\1/p')"
          test -n "$workerConfig"
          test -f "$workerConfig"
          grep -q '\[runtime\]' "$workerConfig"
          grep -q 'manifest' "$workerConfig"
          test "$runtimeDevicePolicy" = closed
          printf "%s" "$runtimeDeviceAllowText" | grep '/dev/nvidia7 rw' >/dev/null
          printf "%s" "$runtimeDeviceAllowText" | grep '/dev/nvidiactl rw' >/dev/null
          printf "%s" "$runtimeEnvironmentText" | grep 'CUDA_VISIBLE_DEVICES=GPU-example-smoke' >/dev/null
          printf "%s" "$runtimeEnvironmentText" | grep 'TSUMGI_GPU_PREFLIGHT_MODE=isolated-access' >/dev/null
          printf "%s" "$runtimeEnvironmentText" | grep 'TSUMGI_GPU_DEVICE_MAP=' >/dev/null
          printf "%s" "$runtimeEnvironmentText" | grep 'TSUMGI_GPU_WORKER_CONFIG=' >/dev/null
          printf "%s" "$runtimeEnvironmentText" | grep 'TSUMGI_GPU_DEVICE_MAP_COMMAND=' >/dev/null
          printf "%s" "$runtimeEnvironmentText" | grep 'TSUMGI_NVIDIA_SMI=' >/dev/null
          printf "%s" "$runtimeWorkerPreflight" | grep -E '^\+/nix/store/[a-z0-9]{32}-tsumgi-gpu-preflight/bin/tsumgi-gpu-preflight /nix/store/[a-z0-9]{32}-tsumgi-gpu-uuids$' >/dev/null
          printf "%s" "$runtimeIsolationPreflight" | grep 'gpu-device-map verify' >/dev/null
          printf "%s\n%s\n%s\n%s\n%s\n" \
            "$runtimeWorkerExec" "$runtimeWorkerPath" "$workerConfig" \
            "$runtimeDevicePolicy" "$runtimeWorkerPreflight" \
            "$runtimeIsolationPreflight" > "$out"
        '';
        gpu-isolation-requires-gpu =
          assert gpuIsolationNoGpuRejected;
          pkgs.runCommand "tsumgi-gpu-isolation-requires-gpu" { } ''
            touch "$out"
          '';
        module-eval = pkgs.runCommand "tsumgi-module-eval" {
          controlExec = moduleSmoke.config.systemd.services.tsumgi-control.serviceConfig.ExecStart;
          controlClientAuth = moduleSmoke.config.services.tsumgi.control.clientAuth;
          workerExec = moduleSmoke.config.systemd.services.tsumgi-worker.serviceConfig.ExecStart;
          workerPreflight = moduleSmoke.config.systemd.services.tsumgi-worker.serviceConfig.ExecStartPre;
          modeTool = borrowableMode;
        } ''
          test -n "$controlExec"
          test "$controlClientAuth" = none
          controlConfig="$(printf "%s" "$controlExec" | sed -n 's/.*--config \([^ ]*\).*/\1/p')"
          test -n "$controlConfig"
          test -f "$controlConfig"
          grep -q 'client_auth = "none"' "$controlConfig"
          test -n "$workerExec"
          test -n "$workerPreflight"
          test -x "$modeTool/bin/tsumgi-gpu-mode"
          printf "%s" "$controlExec" | grep -q tsumgi-control
          printf "%s" "$workerExec" | grep -q tsumgi-worker
          printf "%s\n%s\n%s\n%s\n" "$controlExec" "$workerExec" "$workerPreflight" "$modeTool" > "$out"
        '';
      };
    };
}
