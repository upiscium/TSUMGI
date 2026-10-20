{ lib, nix, python312, source, writeShellApplication }:

let
  profile = "/nix/var/nix/profiles/astrumweaver-runtime-llama-cpp";
  qwen38Profile = "/nix/var/nix/profiles/astrumweaver-runtime-qwen38";
  bonsaiProfile = "/nix/var/nix/profiles/astrumweaver-runtime-bonsai";
  candidateRef = "path:${source}";
  registryRef = "astrumweaver-runtime-candidate";
in
writeShellApplication {
  name = "astrumweaver-runtime-profile";
  text = ''
    profile=${lib.escapeShellArg profile}
    qwen38_profile=${lib.escapeShellArg qwen38Profile}
    bonsai_profile=${lib.escapeShellArg bonsaiProfile}
    runtime_attr="runtime-llama-cpp"
    candidate_ref=${lib.escapeShellArg candidateRef}
    registry_ref=${lib.escapeShellArg registryRef}
    nix_bin=${lib.escapeShellArg "${nix}/bin/nix"}
    python_bin=${lib.escapeShellArg "${python312}/bin/python3"}

    usage() {
      cat >&2 <<'EOF'
usage:
  astrumweaver-runtime-profile verify package llama-cpp
  astrumweaver-runtime-profile ensure llama-cpp
  astrumweaver-runtime-profile upgrade llama-cpp
  astrumweaver-runtime-profile rollback llama-cpp
  astrumweaver-runtime-profile status llama-cpp
  astrumweaver-runtime-profile verify package qwen38-llama-cpp
  astrumweaver-runtime-profile ensure qwen38-llama-cpp
  astrumweaver-runtime-profile upgrade qwen38-llama-cpp
  astrumweaver-runtime-profile rollback qwen38-llama-cpp
  astrumweaver-runtime-profile status qwen38-llama-cpp
  astrumweaver-runtime-profile verify package bonsai-llama-cpp
  astrumweaver-runtime-profile ensure bonsai-llama-cpp
  astrumweaver-runtime-profile upgrade bonsai-llama-cpp
  astrumweaver-runtime-profile rollback bonsai-llama-cpp
  astrumweaver-runtime-profile status bonsai-llama-cpp
EOF
      exit "''${1:-2}"
    }

    require_llama_cpp() {
      case "''${1-}" in
        llama-cpp)
          profile=${lib.escapeShellArg profile}
          runtime_attr="runtime-llama-cpp"
          ;;
        qwen38-llama-cpp)
          profile="$qwen38_profile"
          runtime_attr="runtime-qwen38-llama-cpp"
          ;;
        bonsai-llama-cpp)
          profile="$bonsai_profile"
          runtime_attr="runtime-bonsai-llama-cpp"
          ;;
        *)
          echo "astrumweaver-runtime-profile: unsupported runtime package: ''${1-<missing>}" >&2
          exit 2
          ;;
      esac
    }

    managed_llama_cpp_profile() {
      if [ ! -x "$profile/bin/llama-server" ]; then
        return 1
      fi
      "$python_bin" - "$nix_bin" "$profile" "$registry_ref" "$runtime_attr" <<'PY'
import json
import subprocess
import sys

nix_bin, profile, registry_ref, runtime_attr = sys.argv[1:]

completed = subprocess.run(
    [nix_bin, "profile", "list", "--profile", profile, "--json"],
    check=False,
    capture_output=True,
    text=True,
    timeout=30,
)
if completed.returncode != 0:
    raise SystemExit(1)
try:
    profile_data = json.loads(completed.stdout)
except json.JSONDecodeError:
    raise SystemExit(1)

elements = profile_data.get("elements", {})
if isinstance(elements, dict):
    values = list(elements.values())
elif isinstance(elements, list):
    values = elements
else:
    raise SystemExit(1)

expected_original = "flake:" + registry_ref
expected_attr = "packages.x86_64-linux." + runtime_attr
matches = [
    element
    for element in values
    if isinstance(element, dict)
    and element.get("active", True)
    and element.get("originalUrl") == expected_original
    and element.get("attrPath") == expected_attr
]
raise SystemExit(0 if len(matches) == 1 and len(values) == 1 else 1)
PY
    }

    verify_llama_cpp() {
      managed_llama_cpp_profile || return 1
      "$python_bin" - "$nix_bin" "$profile" "$candidate_ref" "$registry_ref" "$runtime_attr" <<'PY'
import json
import subprocess
import sys

nix_bin, profile, candidate_ref, registry_ref, runtime_attr = sys.argv[1:]

def run_json(args):
    completed = subprocess.run(
        args,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise SystemExit(1)
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError:
        raise SystemExit(1)

profile_data = run_json(
    [nix_bin, "profile", "list", "--profile", profile, "--json"]
)
candidate_data = run_json(
    [nix_bin, "flake", "metadata", "--json", candidate_ref]
)
candidate_url = candidate_data.get("url") or candidate_data.get("lockedUrl")
if not isinstance(candidate_url, str) or not candidate_url:
    raise SystemExit(1)

elements = profile_data.get("elements", {})
if isinstance(elements, dict):
    values = list(elements.values())
elif isinstance(elements, list):
    values = elements
else:
    raise SystemExit(1)

expected_original = "flake:" + registry_ref
expected_attr = "packages.x86_64-linux." + runtime_attr
matches = [
    element
    for element in values
    if isinstance(element, dict)
    and element.get("active", True)
    and element.get("originalUrl") == expected_original
    and element.get("attrPath") == expected_attr
    and (element.get("url") or element.get("uri")) == candidate_url
]
raise SystemExit(0 if len(matches) == 1 and len(values) == 1 else 1)
PY
    }

    status_llama_cpp() {
      if verify_llama_cpp; then
        echo "satisfied: $profile is bound to this AstrumWeaver candidate"
        return 0
      fi
      if managed_llama_cpp_profile; then
        echo "candidate-mismatch: $profile is an AstrumWeaver llama.cpp profile from another candidate" >&2
      elif [ -e "$profile" ] || [ -L "$profile" ]; then
        echo "invalid-profile: refusing unmanaged or mixed content at $profile" >&2
      else
        echo "missing: $profile/bin/llama-server" >&2
      fi
      return 1
    }

    install_or_upgrade_llama_cpp() {
      if verify_llama_cpp; then
        echo "runtime profile already matches this AstrumWeaver candidate"
        return 0
      fi

      if [ -e "$profile" ] || [ -L "$profile" ]; then
        if ! managed_llama_cpp_profile; then
          echo "astrumweaver-runtime-profile: refusing to mutate unmanaged or mixed runtime profile: $profile" >&2
          exit 1
        fi
        "$nix_bin" profile upgrade \
          --profile "$profile" \
          --all \
          --override-flake "$registry_ref" "$candidate_ref"
      else
        "$nix_bin" profile add \
          --profile "$profile" \
          --override-flake "$registry_ref" "$candidate_ref" \
          "$registry_ref#$runtime_attr"
      fi

      if ! verify_llama_cpp; then
        echo "astrumweaver-runtime-profile: Nix completed but the reviewed candidate binding is not active" >&2
        exit 1
      fi
      echo "runtime profile now matches this AstrumWeaver candidate"
    }

    case "''${1-}" in
      -h|--help)
        usage 0
        ;;
      verify)
        [ "''${2-}" = "package" ] || usage
        require_llama_cpp "''${3-}"
        verify_llama_cpp
        ;;
      ensure)
        require_llama_cpp "''${2-}"
        install_or_upgrade_llama_cpp
        ;;
      upgrade)
        require_llama_cpp "''${2-}"
        install_or_upgrade_llama_cpp
        ;;
      rollback)
        require_llama_cpp "''${2-}"
        if ! managed_llama_cpp_profile; then
          echo "astrumweaver-runtime-profile: refusing to rollback unmanaged or mixed runtime profile: $profile" >&2
          exit 1
        fi
        "$nix_bin" profile rollback --profile "$profile"
        status_llama_cpp || true
        ;;
      status)
        require_llama_cpp "''${2-}"
        status_llama_cpp
        ;;
      *)
        usage
        ;;
    esac
  '';
  meta = {
    description = "TSUMGI generic-systemd RuntimeBackend Nix profile manager";
    license = lib.licenses.bsd3;
    platforms = lib.platforms.linux;
  };
}
