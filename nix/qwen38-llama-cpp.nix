# Pinned upstream ggml-org llama.cpp CUDA backend for Qwen3.8 Q4_K_M; opt-in.
{ lib, stdenv, fetchurl, autoPatchelfHook, cudaPackages_12_9, zlib, openssl }:
let
  cuda = cudaPackages_12_9;
  releaseTag = "b11430";
  archiveName = "llama-" + releaseTag + "-bin-ubuntu-cuda-12.8-x64.tar.gz";
in
stdenv.mkDerivation {
  pname = "tsumgi-qwen38-llama-cpp-cuda";
  version = releaseTag;

  src = fetchurl {
    url = "https://github.com/ggml-org/llama.cpp/releases/download/" + releaseTag + "/" + archiveName;
    hash = "sha256-j3n3UJPo+22cZeWRZ1EelUd3Z5QZEHPkiM6B79Wt5yM=";
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
    install -d "$out/lib/tsumgi-qwen38" "$out/share/tsumgi-qwen38"
    cp -a source/. "$out/lib/tsumgi-qwen38/"
    test -x "$out/lib/tsumgi-qwen38/llama-server"
    printf '%s\n' 'b11430' > "$out/share/tsumgi-qwen38/release-tag"
    printf '%s\n' '8345f333951c661d166b00e6f9362e553768f292' > "$out/share/tsumgi-qwen38/source-revision"
    printf '%s\n' '8f79f75093e8fb6d9c65e59167511e9547776794191073e488ce81efd5ade723' > "$out/share/tsumgi-qwen38/archive-sha256"
    runHook postInstall
  '';

  meta = with lib; {
    description = "Pinned upstream ggml-org llama.cpp b11430, CUDA 12.8, qualified with Qwen3.8 Q4_K_M";
    homepage = "https://github.com/ggml-org/llama.cpp";
    license = licenses.mit;
    platforms = [ "x86_64-linux" ];
  };
}
