{ lib, stdenvNoCC, python3, sevenzip, archive, rightsReviewSha256 }:

let
  validSha256 = value:
    builtins.isString value && !builtins.hasContext value
    && builtins.match "sha256:[0-9a-f]{64}" value != null;
in
assert stdenvNoCC.hostPlatform.system == "x86_64-linux";
assert validSha256 rightsReviewSha256;
assert builtins.isPath archive || builtins.isString archive;
stdenvNoCC.mkDerivation {
  pname = "tsumgi-voicevox-cpu-bundle";
  version = "0.26.2";

  # Explicit caller-provided, locally acquired archive; deliberately no fetcher.
  src = archive;
  dontUnpack = true;
  dontConfigure = true;
  dontBuild = true;
  dontFixup = true;
  dontStrip = true;
  nativeBuildInputs = [ python3 sevenzip ];

  installPhase = ''
    runHook preInstall
    ${python3}/bin/python3 ${../src/tsumgi/runtime/providers/voicevox_release.py} verify-archive "$src"
    tmp="$TMPDIR/voicevox-unpack"
    mkdir -m 0700 "$tmp"
    ${lib.getExe sevenzip} x -bd -y "-o$tmp" "$src"
    test -d "$tmp/linux-cpu-x64"
    test ! -L "$tmp/linux-cpu-x64"
    test "$(find "$tmp" -mindepth 1 -maxdepth 1 | wc -l)" -eq 1
    install -d "$out/lib/voicevox"
    cp -R "$tmp/linux-cpu-x64" "$out/lib/voicevox/linux-cpu-x64"
    install -d "$out/share/tsumgi"
    ${python3}/bin/python3 ${../src/tsumgi/runtime/providers/voicevox_release.py} record \
      "$out/lib/voicevox/linux-cpu-x64" \
      "$out/share/tsumgi/voicevox-bundle.json" \
      --rights-review-sha256 ${lib.escapeShellArg rightsReviewSha256}
    runHook postInstall
  '';

  passthru = {
    engineSubdirectory = "lib/voicevox/linux-cpu-x64";
    receiptSubpath = "share/tsumgi/voicevox-bundle.json";
    nativeExecutionQualified = false;
    archiveSha256 = "483633de02700cd918731538543a22ce895877aeb09450a990ff38af6e4c26c0";
    runSha256 = "acf74848282c0ff6f0aaf8514427c5f44b0992c26b48448d76b35a97dbe81a25";
    vvmSha256 = "ecd35374d4182cd883cba5040376f7f888cc6ba248b1c2f4cea07cdb34bb1318";
  };

  meta = {
    description = "Offline raw native-content VOICEVOX CPU bundle candidate; execution ABI not qualified";
    platforms = [ "x86_64-linux" ];
    license = lib.licenses.unfree;
  };
}
