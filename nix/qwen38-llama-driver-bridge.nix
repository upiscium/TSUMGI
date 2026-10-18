# Minimal host libcuda injection for upstream Qwen3.8. Rejects driver stubs
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
      echo "tsumgi-qwen38: cuda_driver_library_unavailable" >&2
      exit 65
    fi
    driver="$(readlink -f -- "$driver")"
    if [[ "$driver" == */stubs/* ]]; then
      echo "tsumgi-qwen38: cuda_stub_is_not_a_driver" >&2
      exit 65
    fi
    private="$(mktemp -d -t tsumgi-qwen38-cuda.XXXXXXXX)"
    chmod 700 "$private"
    ln -s -- "$driver" "$private/libcuda.so.1"
    runtime_env=(
      "LD_LIBRARY_PATH=$private:${core}/lib/tsumgi-qwen38:${libraries}"
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
    env -i "''${runtime_env[@]}" "${core}/lib/tsumgi-qwen38/llama-server" "$@" &
    child=$!
    wait "$child"
  '';
}
