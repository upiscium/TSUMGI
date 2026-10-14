"""Validate RC package version alignment while keeping legacy runtime names."""
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SURFACES = {
    "nix/package.nix": 1,
    "nix/integration-package.nix": 1,
    "nix/control-support.nix": 2,
    "nix/worker-support.nix": 1,
    "nix/installer-support.nix": 3,
}

def test_product_rc_versions_align_across_python_and_nix() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["name"] == "astrumweaver"
    match = re.fullmatch(r"(\d+\.\d+\.\d+)rc([1-9]\d*)", project["version"])
    assert match
    nix_version = f"{match[1]}-rc.{match[2]}"
    for path, count in SURFACES.items():
        data = (ROOT / path).read_text()
        assert data.count(nix_version) == count, path
        assert "0.1.0-dev" not in data, path
    assert (ROOT / "src/astrumweaver").is_dir()
    assert 'version = "0.6.0-dev-' in (ROOT / "nix/decision-llama-cuda.nix").read_text()

def test_rc_notes_have_risk_and_approval_boundaries() -> None:
    notes = (ROOT / "docs/releases/v0.1.0-rc.1.md").read_text()
    for phrase in ("UNPUBLISHED", "v0.1.0-rc.1", "0.1.0rc1", "UNKNOWN_COMMIT",
                   "PostgreSQL 17", "UTF8", "OBSERVED_ONLY", "astrumweaver", "#79", "#124"):
        assert phrase in notes
    checklist = (ROOT / "docs/rc-qualification-v0.1.0-rc.1.md").read_text()
    assert "NO TAG" in checklist
    assert "NO PRODUCTION" in checklist
