{ nixpkgs, system }:

let
  pkgs = import nixpkgs {
    inherit system;
    config.allowUnfreePredicate = pkg:
      (pkg.pname or "") == "tsumgi-voicevox-service-view";
  };
  # Intentionally invalid as native inputs. Never build the candidate from
  # this test, nor turn its store path into a dependency of the check.
  synthetic = builtins.toFile "synthetic-voicevox-service-view-input" "not native content";
  args = {
    inherit (pkgs) lib stdenvNoCC python3;
    libcArchive = synthetic;
    libcRoot = synthetic;
    engineRoot = synthetic;
  };
  make = overrides: import ../voicevox-service-view.nix (args // overrides);
  candidate = make { };
  rejected = overrides: !(builtins.tryEval (make overrides).drvPath).success;
  fragment = candidate.passthru.candidateServiceConfig;
in
assert system == "x86_64-linux";
assert rejected {
  stdenvNoCC = pkgs.stdenvNoCC // {
    hostPlatform = pkgs.stdenvNoCC.hostPlatform // { system = "aarch64-linux"; };
  };
};
assert rejected { libcArchive = "https://example.invalid/candidate.deb"; };
assert rejected { libcRoot = "relative/unpacked"; };
assert rejected { engineRoot = "/candidate/../engine"; };
assert rejected { engineRoot = "/candidate//engine"; };
assert rejected { libcArchive = "/candidate\ninput"; };
assert rejected { libcRoot = null; };
assert rejected { engineRoot = true; };
assert candidate.libcArchive == synthetic && candidate.libcRoot == synthetic;
assert candidate.engineRoot == synthetic;
assert candidate.dontUnpack && candidate.dontFixup && candidate.dontStrip;
assert candidate.preferLocalBuild && !candidate.allowSubstitutes;
assert candidate.nativeBuildInputs == [ pkgs.python3 ];
assert candidate.passthru.rootSubdirectory == "rootfs";
assert candidate.passthru.receiptSubpath == "share/tsumgi/voicevox-service-view.json";
assert candidate.passthru.libcArchiveSha256 == "79e35256227e16a607c154cdeb8d76ff12d20e31de286ea7fd9ad3b96fe0452d";
assert candidate.passthru.bootstrapZlibSha256 == "64c206f0146cc58bbddc4f22054436f4ff278f5a554aa3ce6921ddf7e9133370";
assert !candidate.passthru.nativeExecutionQualified;
assert !candidate.passthru.elfLoaderResolutionProven;
assert candidate.passthru.firstexecLoadedMapping == "UNKNOWN";
assert !candidate.passthru.activationSupported && !candidate.passthru.servingBindingSupported;
assert candidate.passthru.securityStatus == "superseded-abi-investigation-only";
assert fragment.RootDirectory == "${candidate}/rootfs";
assert fragment.BindReadOnlyPaths == [ "/nix/store:/nix/store" ];
assert fragment.MountAPIVFS && fragment.PrivateDevices && fragment.NoNewPrivileges;
assert fragment.ProtectSystem == "strict" && fragment.ProtectHome;
assert !fragment.RootDirectoryStartOnly;
assert !(fragment ? ExecStart) && !(fragment ? Environment) && !(fragment ? EnvironmentFile);
assert !(fragment ? BindPaths) && !(fragment ? ReadWritePaths);
assert builtins.isString candidate.drvPath;
pkgs.runCommand "voicevox-service-view-evaluation-test" { } ''
  # No interpolation of candidate, its inputs or fragment: assertions above
  # evaluate only. Even building THIS check must not realise native content.
  printf '%s\n' 'offline service-view evaluation passed; native build/activation not run' > "$out"
''
