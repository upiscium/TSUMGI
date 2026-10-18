# Pinned PrismML Bonsai 2 PQ2_0 native CUDA backend. No auto upgrades.
{ lib, stdenv, fetchurl, autoPatchelfHook, cudaPackages_12_9, zlib, openssl }:
let
  cuda = cudaPackages_12_9;
  releaseTag = "prism-b10772-e8fd209";
  archiveName = "llama-" + releaseTag + "-bin-linux-cuda-12.4-x64.tar.gz";
in
stdenv.mkDerivation {
  pname = "tsumgi-bonsai-llama-cpp-cuda";
  version = releaseTag;

  src = fetchurl {
    url = "https://github.com/PrismML-Eng/llama.cpp/releases/download/" + releaseTag + "/" + archiveName;
    hash = "sha256-9aRsTtu8dTPiysls6RkSBMegNlXq9015Y5Rp9QyNmYw=";
  };
  nativeBuildInputs = [ autoPatchelfHook ];
  buildInputs = [ stdenv.cc.cc.lib cuda.cuda_cudart cuda.libcublas zlib openssl ];
  autoPatchelfIgnoreMissingDeps = [ "libcuda.so.1" ];
  dontConfigure = true;
  dontBuild = true;

  unpackPhase = ''
    runHook preUnpack
    mkdir source
    tar -xzf "$src" -C source --strip-components=1 --no-same-owner
    runHook postUnpack
  '';
  installPhase = ''
    runHook preInstall
    install -d "$out/lib/tsumgi-bonsai" "$out/share/tsumgi-bonsai"
    cp -a source/. "$out/lib/tsumgi-bonsai/"
    test -x "$out/lib/tsumgi-bonsai/llama-server"
    printf '%s\n' 'prism-b10772-e8fd209' > "$out/share/tsumgi-bonsai/release-tag"
    printf '%s\n' 'e8fd209c975c80aa7fb36a8423571ff3b1b7e955' > "$out/share/tsumgi-bonsai/source-revision"
    printf '%s\n' 'f5a46c4edbbc7533e2cac96ce9191204c7a03655eaf74d79639469f50c8d998c' > "$out/share/tsumgi-bonsai/archive-sha256"
    runHook postInstall
  '';

  meta = with lib; {
    description = "Pinned PrismML Bonsai 2 PQ2_0 fork of llama.cpp, CUDA 12.4 prebuilt native runtime";
    homepage = "https://github.com/PrismML-Eng/llama.cpp";
    license = licenses.mit;
    platforms = [ "x86_64-linux" ];
  };
}
