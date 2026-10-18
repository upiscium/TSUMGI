# Minimal host libcuda injection for PrismML Bonsai. Rejects driver stubs
# and strips inherited Worker/Control secrets from the native child.
{ lib, writeShellApplication, coreutils, core, cudaPackages_12_9, stdenv, openssl }:
let
  cuda = cudaPackages_12_9;
  libraries = lib.makeLibraryPath [ stdenv.cc.cc.lib cuda.cuda_cudart cuda.libcublas openssl ];
in
writeShellApplication {
  name = "llama-server";
  runtimeInputs = [ coreutils ];
  text = ''
    set -euo pipefail
    driver="''${ASTRUMWEAVER_LIBCUDA_SO:-}"
    if [[ -z "$driver" ]]; then
      for candidate in /run/opengl-driver/lib/libcuda.so.1 \
                       /usr/lib/x86_64-linux-gnu/libcuda.so.1 \
                       /lib/x86_64-linux-gnu/libcuda.so.1; do
        if [[ -f "$candidate" ]]; then
          driver="$candidate"
          break
        fi
      done
    fi
    if [[ -z "$driver" || ! -f "$driver" ]]; then
      echo "tsumgi-bonsai: cuda_driver_library_unavailable" >&2
      exit 65
    fi
    driver="$(readlink -f -- "$driver")"
    if [[ "$driver" == */stubs/* ]]; then
      echo "tsumgi-bonsai: cuda_stub_is_not_a_driver" >&2
      exit 65
    fi
    driver_mode="$(stat -c %a -- "$driver")"
    if [[ "$(stat -c %u -- "$driver")" != "0" ]] || (( (8#$driver_mode & 18) != 0 )); then
      echo "tsumgi-bonsai: cuda_driver_library_is_not_root_owned_read_only" >&2
      exit 65
    fi
    # Validate all host drivers before creating the private shim; failed
    # preflight cannot leak an untrapped temporary directory.
    driver_version="$(basename -- "$driver" | cut -d. -f3-)"
    if [[ "$driver" != *.so.[0-9]* || -z "$driver_version" ]]; then
      echo "tsumgi-bonsai: cuda_driver_version_unrecognized" >&2
      exit 65
    fi
    ptx_companion=""
    nvvm_companion=""
    # CUDA may dlopen the PTX JIT compiler after llama.cpp initialises.
    # A private libcuda-only bridge passes --version but fails at GPU load
    # with "PTX JIT compiler library not found" on Pascal GPUs.
    # Include the host-matched NVVM companion of the PTX JIT compiler.
    for soname in libnvidia-ptxjitcompiler.so.1 libnvidia-nvvm.so.4; do
      companion=""
      for candidate in "$(dirname -- "$driver")/$soname" \
                       /run/opengl-driver/lib/"$soname" \
                       /usr/lib/x86_64-linux-gnu/"$soname" \
                       /lib/x86_64-linux-gnu/"$soname"; do
        if [[ -f "$candidate" ]]; then
          companion="$(readlink -f -- "$candidate")"
          break
        fi
      done
      if [[ -z "$companion" || ! -f "$companion" || "$companion" == */stubs/* ]]; then
        echo "tsumgi-bonsai: required_nvidia_driver_companion_unavailable: $soname" >&2
        exit 65
      fi
      companion_mode="$(stat -c %a -- "$companion")"
      if [[ "$(stat -c %u -- "$companion")" != "0" ]] || (( (8#$companion_mode & 18) != 0 )) || [[ ! -r "$companion" ]]; then
        echo "tsumgi-bonsai: nvidia_driver_companion_not_root_owned_read_only: $soname" >&2
        exit 65
      fi
      companion_version="$(basename -- "$companion" | cut -d. -f3-)"
      if [[ "$companion_version" != "$driver_version" ]]; then
        echo "tsumgi-bonsai: nvidia_driver_version_mismatch: $soname" >&2
        exit 65
      fi
      if [[ "$soname" == libnvidia-ptxjitcompiler.so.1 ]]; then
        ptx_companion="$companion"
      else
        nvvm_companion="$companion"
      fi
    done
    private="$(mktemp -d -t tsumgi-bonsai-cuda.XXXXXXXX)"
    chmod 700 "$private"
    ln -s -- "$driver" "$private/libcuda.so.1"
    ln -s -- "$ptx_companion" "$private/libnvidia-ptxjitcompiler.so.1"
    ln -s -- "$nvvm_companion" "$private/libnvidia-nvvm.so.4"
    runtime_env=(
      "LD_LIBRARY_PATH=$private:${core}/lib/tsumgi-bonsai:${libraries}"
      "PATH=$PATH"
    )
    for name in CUDA_VISIBLE_DEVICES CUDA_DEVICE_ORDER HOME TMPDIR LANG LC_ALL; do
      if [[ -v $name ]]; then
        runtime_env+=( "$name=''${!name}" )
      fi
    done
    child=""
    cleanup() {
      status=$?
      trap - EXIT TERM INT HUP
      if [[ -n "$child" ]] && kill -0 "$child" 2>/dev/null; then
        kill -TERM "$child" 2>/dev/null || true
        for ((i=0; i<80; i++)); do
          kill -0 "$child" 2>/dev/null || break
          sleep 0.1
        done
        if kill -0 "$child" 2>/dev/null; then
          kill -KILL "$child" 2>/dev/null || true
        fi
        wait "$child" 2>/dev/null || true
      fi
      rm -rf -- "$private"
      exit "$status"
    }
    trap cleanup EXIT
    trap 'exit 143' TERM
    trap 'exit 130' INT
    trap 'exit 129' HUP
    env -i "''${runtime_env[@]}" "${core}/lib/tsumgi-bonsai/llama-server" "$@" &
    child=$!
    wait "$child"
  '';
}
