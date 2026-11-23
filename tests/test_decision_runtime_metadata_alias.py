"""The decision runtime republishes the CUDA build's metadata under share/tsumgi.

The heavy llama.cpp build keeps its pre-rename name and text (so a host that
holds it does not recompile CUDA llama.cpp), which puts its metadata under
share/astrumweaver. The small alias in flake.nix copies from exactly that
directory. A blanket rename once changed the copy source and broke the build
of the runtime package, which nothing else notices (the Nix checks only
evaluate it), so this guards the two files against each other.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _core_metadata_dir() -> str:
    text = (ROOT / "nix/decision-llama-cuda.nix").read_text()
    dirs = set(re.findall(r'mkdir -p "\$out/share/([A-Za-z0-9_-]+)"', text))
    assert len(dirs) == 1, dirs
    return dirs.pop()


def test_the_alias_copies_from_the_directory_the_core_build_writes():
    flake = (ROOT / "flake.nix").read_text()
    sources = re.findall(r"cp \$\{core\}/share/([A-Za-z0-9_-]+)/\* \"\$out/share/tsumgi/\"", flake)
    assert sources == [_core_metadata_dir()], (sources, _core_metadata_dir())


def test_the_alias_publishes_where_the_code_reads_it():
    flake = (ROOT / "flake.nix").read_text()
    assert 'mkdir -p "$out/share/tsumgi"' in flake
    smoke = (ROOT / "src/tsumgi/validation/d1_gpu_smoke.py").read_text()
    assert '"llama-cpp-revision"' in smoke  # read from the share/tsumgi alias


def test_the_core_build_keeps_its_pre_rename_store_identity():
    text = (ROOT / "nix/decision-llama-cuda.nix").read_text()
    assert 'pname = "astrumweaver-llama-decision-cuda-sm${cudaArch}";' in text
