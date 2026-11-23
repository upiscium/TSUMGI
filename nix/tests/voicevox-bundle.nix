{ nixpkgs, system }:

let
  pkgs = import nixpkgs {
    inherit system;
    config.allowUnfreePredicate = pkg:
      (pkg.pname or "") == "tsumgi-voicevox-cpu-bundle";
  };
  archive = builtins.toFile "synthetic-voicevox.7z" "not a real archive";
  rights = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
  bundle = import ../voicevox-cpu-bundle.nix {
    lib = pkgs.lib;
    stdenvNoCC = pkgs.stdenvNoCC;
    python3 = pkgs.python3;
    sevenzip = pkgs._7zz;
    inherit archive;
    rightsReviewSha256 = rights;
  };
  wrongSystem = builtins.tryEval (
    (import ../voicevox-cpu-bundle.nix {
      lib = pkgs.lib;
      stdenvNoCC = pkgs.stdenvNoCC // {
        hostPlatform = pkgs.stdenvNoCC.hostPlatform // { system = "aarch64-linux"; };
      };
      python3 = pkgs.python3;
      sevenzip = pkgs._7zz;
      inherit archive;
      rightsReviewSha256 = rights;
    }).drvPath
  );
  badRights = builtins.tryEval (
    (import ../voicevox-cpu-bundle.nix {
      lib = pkgs.lib;
      stdenvNoCC = pkgs.stdenvNoCC;
      python3 = pkgs.python3;
      sevenzip = pkgs._7zz;
      inherit archive;
      rightsReviewSha256 = "not-a-digest";
    }).drvPath
  );
  contextualRights = builtins.tryEval (
    (import ../voicevox-cpu-bundle.nix {
      lib = pkgs.lib;
      stdenvNoCC = pkgs.stdenvNoCC;
      python3 = pkgs.python3;
      sevenzip = pkgs._7zz;
      inherit archive;
      rightsReviewSha256 = builtins.appendContext rights (builtins.getContext archive);
    }).drvPath
  );
in
assert !wrongSystem.success;
assert !badRights.success;
assert !contextualRights.success;
assert bundle.src == archive;
assert !builtins.hasContext rights;
assert bundle.passthru.nativeExecutionQualified == false;
assert bundle.passthru.engineSubdirectory == "lib/voicevox/linux-cpu-x64";
assert bundle.passthru.archiveSha256 == "483633de02700cd918731538543a22ce895877aeb09450a990ff38af6e4c26c0";
assert bundle.dontFixup && bundle.dontStrip && bundle.dontUnpack;
assert bundle.nativeBuildInputs == [ pkgs.python3 pkgs._7zz ];
assert builtins.isString bundle.drvPath;
assert system == "x86_64-linux";
pkgs.runCommand "voicevox-bundle-evaluation-test" { } ''
  # No bundle/derivation path interpolation here: that would add a build
  # dependency and try to realise the deliberately invalid synthetic archive.
  printf '%s\n' 'offline VOICEVOX bundle recipe evaluation passed; native build not run' > "$out"
''
