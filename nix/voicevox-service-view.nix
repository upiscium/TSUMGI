{ lib, stdenvNoCC, python3, libcArchive, libcRoot, engineRoot }:

let
  # These are explicit local inputs, not URLs or instructions to unpack a
  # VOICEVOX bundle. Existence, ownership, containment and byte pins are checked
  # by the builder, not by evaluation (which must not inspect native assets).
  localInput = value:
    (builtins.isPath value || builtins.isString value)
    && (let path = toString value; in
      builtins.match "/[^[:cntrl:]]+" path != null
      && !lib.hasInfix "//" path
      && !lib.hasSuffix "/" path
      && builtins.all (part: part != "." && part != "..") (lib.splitString "/" path));
  view = stdenvNoCC.mkDerivation {
    pname = "astrumweaver-voicevox-service-view";
    version = "glibc-2.35-0ubuntu3.15-candidate";

    inherit libcArchive libcRoot engineRoot;
    dontUnpack = true;
    dontConfigure = true;
    dontBuild = true;
    dontFixup = true;
    dontStrip = true;
    preferLocalBuild = true;
    allowSubstitutes = false;
    nativeBuildInputs = [ python3 ];

    installPhase = ''
      runHook preInstall
      ${python3}/bin/python3 -I -B ${../src/astrumweaver/runtime/providers/voicevox_service_view.py} build \
        --libc-archive "$libcArchive" --libc-root "$libcRoot" \
        --engine-root "$engineRoot" --output "$out"
      ${python3}/bin/python3 -I -B ${../src/astrumweaver/runtime/providers/voicevox_service_view.py} verify \
        --output "$out"
      runHook postInstall
    '';

    passthru = {
      rootSubdirectory = "rootfs";
      receiptSubpath = "share/astrumweaver/voicevox-service-view.json";
      libcArchiveSha256 = "79e35256227e16a607c154cdeb8d76ff12d20e31de286ea7fd9ad3b96fe0452d";
      bootstrapZlibSha256 = "64c206f0146cc58bbddc4f22054436f4ff278f5a554aa3ce6921ddf7e9133370";
      nativeExecutionQualified = false;
      elfLoaderResolutionProven = false;
      firstexecLoadedMapping = "UNKNOWN";
      activationSupported = false;
      servingBindingSupported = false;
      securityStatus = "superseded-abi-investigation-only";

      # Data for review, NOT a NixOS module or a complete/runnable service.
      # In particular, there is no ExecStart, engine substitution, HOME,
      # credentials, networking/NSS setup, writable mount or install target.
      # Nothing imports this into the existing Worker.
      candidateServiceConfig = {
        RootDirectory = "${view}/rootfs";
        RootDirectoryStartOnly = false;
        BindReadOnlyPaths = [ "/nix/store:/nix/store" ];
        MountAPIVFS = true;
        PrivateDevices = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        NoNewPrivileges = true;
      };
    };

    meta = {
      description = "Offline VOICEVOX glibc/zlib content-view candidate; not an activation interface";
      platforms = [ "x86_64-linux" ];
      # No redistribution/voice-rights approval is inferred from local inputs
      # or the included glibc copyright. Keep this candidate opt-in/restricted.
      license = lib.licenses.unfree;
    };
  };
in
assert stdenvNoCC.hostPlatform.system == "x86_64-linux";
assert localInput libcArchive;
assert localInput libcRoot;
assert localInput engineRoot;
view
