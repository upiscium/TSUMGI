"""The decision runtime's CUDA build carries the TSUMGI name end to end.

Before v0.3.0 GA the heavy llama.cpp build kept its pre-rename store name and
an alias in flake.nix republished its metadata under share/tsumgi. GA renamed
the build itself, so the alias is gone; this keeps the pieces consistent:
the build writes where the code reads, and no pre-rename name comes back.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = (ROOT / "nix/decision-llama-cuda.nix").read_text()


def test_the_core_build_has_the_tsumgi_store_name():
    assert 'pname = "tsumgi-llama-decision-cuda-sm${cudaArch}";' in CORE


def test_the_core_build_writes_the_metadata_where_the_code_reads_it():
    assert 'mkdir -p "$out/share/tsumgi"' in CORE
    for name in ("llama-cpp-revision", "cuda-sm", "cuda-toolkit"):
        assert f'"$out/share/tsumgi/{name}"' in CORE
    smoke = (ROOT / "src/tsumgi/validation/d1_gpu_smoke.py").read_text()
    assert '"llama-cpp-revision"' in smoke


def test_no_pre_rename_name_or_alias_remains_in_the_nix_files():
    flake = (ROOT / "flake.nix").read_text()
    for text in (CORE, flake):
        assert "astrumweaver" not in text.lower()
    assert "decision-metadata" not in flake
