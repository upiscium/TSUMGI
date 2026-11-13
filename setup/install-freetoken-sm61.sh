#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
. "$SCRIPT_DIR/lib/common.sh"
# common.sh sets umask 027, but the Worker runs `ft` as the unprivileged
# astrumweaver user, so the virtualenv must be world-readable (never writable).
umask 022

PIN_DIR="$SCRIPT_DIR/freetoken-sm61"
PREFIX='/opt/astrumweaver/freetoken-sm61'
FORK_REPO='upiscium/FreeToken'
FORK_COMMIT='abfbe04e59c35788953c88507e736bc391d3f389'
CUDA_HOME_DIR='/usr/local/cuda-12.6'
CUDA_KEYRING_URL='https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/cuda-keyring_1.1-1_all.deb'
CUDA_KEYRING_SHA256='d2a6b11c096396d868758b86dab1823b25e14d70333f1dfa74da5ddaf6a06dba'
TORCH_INDEX='https://download.pytorch.org/whl/cu126'
UV_VERSION='0.13.8'
CUDA_PACKAGES=(
  'cuda-nvcc-12-6=12.6.85-1'
  'cuda-cudart-dev-12-6=12.6.77-1'
  'cuda-cccl-12-6=12.6.77-1'
  'cuda-nvrtc-dev-12-6=12.6.85-1'
  'libcublas-dev-12-6=12.6.4.1-1'
)
SKIP_APT=0

usage() {
  cat <<'USAGE'
Usage: install-freetoken-sm61.sh [options]

Installs FreeToken 0.2.2 with the TSUMGI pre-Turing (Pascal sm_61) changes into a
dedicated virtualenv. Package versions come from setup/freetoken-sm61/requirements.lock.
The TSUMGI fork files (upiscium/FreeToken, tsumgi/sm61-v0.2.2) are copied over the wheel
only after their SHA-256 matches setup/freetoken-sm61/overlay.sha256.

Options:
  --prefix DIR     Install root (default: /opt/astrumweaver/freetoken-sm61).
  --skip-apt       Do not install the CUDA 12.6 toolkit/Python headers with apt.
  -h, --help       Show this help.

Requires root, Ubuntu 24.04 x86_64 and an NVIDIA driver >= 580. The script never
changes the NVIDIA driver, GPU passthrough or the Worker service.
USAGE
}

while (($#)); do
  case "$1" in
    --prefix)
      (($# >= 2)) || die "--prefix requires a value"
      PREFIX="$2"; shift 2 ;;
    --skip-apt) SKIP_APT=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

[[ "$PREFIX" == /* && "$PREFIX" != *:* && "$PREFIX" != *..* ]] \
  || die "--prefix must be an absolute path without ':' or '..'"
[[ "$(id -u)" == 0 ]] || die "run as root"
[[ "$(uname -m)" == x86_64 ]] || die "FreeToken packages require x86_64"
. /etc/os-release
[[ "${ID:-}" == ubuntu && "${VERSION_ID:-}" == 24.04 ]] || die "Ubuntu 24.04 is required"
require_cmd nvidia-smi
require_cmd curl
require_cmd sha256sum
require_cmd tar
# sed reads all input, so nvidia-smi cannot die of SIGPIPE under pipefail.
driver="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | sed -n 1p)"
if [[ ! "${driver%%.*}" =~ ^[0-9]+$ ]] || ((${driver%%.*} < 580)); then
  die "NVIDIA driver >= 580 is required (found: ${driver:-none})"
fi

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

if ((SKIP_APT == 0)); then
  require_cmd apt-get
  if ! dpkg-query -W -f='${Status}' cuda-keyring 2>/dev/null | grep -q 'ok installed'; then
    keyring="$work/cuda-keyring.deb"
    curl -fsSL --max-time 120 -o "$keyring" "$CUDA_KEYRING_URL"
    echo "$CUDA_KEYRING_SHA256  $keyring" | sha256sum --check --status \
      || die "cuda-keyring checksum mismatch"
    dpkg -i "$keyring" >/dev/null
  fi
  log "installing CUDA 12.6 toolkit pieces and Python headers"
  DEBIAN_FRONTEND=noninteractive apt-get update -q >/dev/null
  DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends \
    "${CUDA_PACKAGES[@]}" python3.12-venv python3.12-dev >/dev/null
fi
[[ -x "$CUDA_HOME_DIR/bin/nvcc" ]] || die "missing $CUDA_HOME_DIR/bin/nvcc"
[[ -f "$CUDA_HOME_DIR/include/cublas_v2.h" ]] || die "missing cuBLAS headers in $CUDA_HOME_DIR"

VENV="$PREFIX/venv"
log "creating virtualenv at $VENV"
install -d -m 755 "$PREFIX"
[[ -x "$VENV/bin/python" ]] || python3.12 -m venv "$VENV"
"$VENV/bin/python" -m pip install -q "uv==$UV_VERSION"
# The lock is a full freeze of the verified environment. It deliberately keeps the
# cu126 PyTorch build next to FreeToken's CUDA 13 extras, which their declared
# metadata does not allow together, so install it verbatim without resolution.
"$VENV/bin/uv" pip install -q --python "$VENV/bin/python" --no-deps --only-binary :all: \
  --index-url https://pypi.org/simple --extra-index-url "$TORCH_INDEX" \
  --index-strategy unsafe-best-match \
  -r "$PIN_DIR/requirements.lock"

SITE="$("$VENV/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
[[ -d "$SITE/freetoken" ]] || die "freetoken package missing in $SITE"

log "applying $FORK_REPO@$FORK_COMMIT"
curl -fsSL --max-time 300 -o "$work/fork.tar.gz" \
  "https://codeload.github.com/$FORK_REPO/tar.gz/$FORK_COMMIT"
tar -xzf "$work/fork.tar.gz" -C "$work"
src="$work/FreeToken-$FORK_COMMIT/python"
[[ -d "$src/freetoken" ]] || die "unexpected fork archive layout"
while IFS=' ' read -r digest path; do
  [[ -z "$digest" || "$digest" == \#* ]] && continue
  [[ "$path" == freetoken/* && "$path" != *..* ]] || die "unsafe overlay path: $path"
  echo "$digest  $src/$path" | sha256sum --check --status \
    || die "fork file checksum mismatch: $path"
  install -D -m 644 "$src/$path" "$SITE/$path"
  echo "$digest  $SITE/$path" | sha256sum --check --status \
    || die "installed file checksum mismatch: $path"
done < "$PIN_DIR/overlay.sha256"

CU13_LIB="$SITE/nvidia/cu13/lib"
[[ -f "$CU13_LIB/libcudart.so.13" ]] || die "missing CUDA 13 runtime library in $CU13_LIB"

# Repair installs made before the umask fix, which left the venv root-only.
chmod -R a+rX "$PREFIX"

cat > "$PREFIX/INSTALL.json" <<JSON
{
  "freetoken": "0.2.2",
  "fork": "$FORK_REPO@$FORK_COMMIT",
  "requirements_lock_sha256": "$(sha256sum "$PIN_DIR/requirements.lock" | cut -d' ' -f1)",
  "overlay_sha256": "$(sha256sum "$PIN_DIR/overlay.sha256" | cut -d' ' -f1)",
  "executable": "$VENV/bin/ft",
  "cuda_home": "$CUDA_HOME_DIR",
  "library_paths": ["$CU13_LIB"]
}
JSON
chmod 644 "$PREFIX/INSTALL.json"
log "installed; FreeToken provider_config for this host:"
cat >&2 <<EOF
  "executable": "$VENV/bin/ft",
  "attention_backend": "triton",
  "max_prefill_tokens": 2048,
  "cuda_home": "$CUDA_HOME_DIR",
  "library_paths": ["$CU13_LIB"]
EOF
