"""Validate stable v0.1.0 product versions while preserving RC history and legacy ABI."""
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

def test_ga_product_versions_align_across_python_and_nix() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["name"] == "astrumweaver"
    assert project["version"] == "0.1.0"
    assert re.fullmatch(r"\d+\.\d+\.\d+", project["version"])
    for path, count in SURFACES.items():
        data = (ROOT / path).read_text()
        assert data.count("0.1.0") == count, path
        assert "0.1.0pre1" not in data, path
        assert "0.1.0-dev" not in data, path
    assert (ROOT / "src/astrumweaver").is_dir()
    assert 'version = "0.6.0-dev-' in (ROOT / "nix/decision-llama-cuda.nix").read_text()

def test_historical_rc_evidence_is_preserved_and_not_live_status() -> None:
    notes = (ROOT / "docs/releases/v0.1.0-rc.1.md").read_text()
    checklist = (ROOT / "docs/rc-qualification-v0.1.0-rc.1.md").read_text()
    for phrase in ("Historical", "v0.1.0-rc.1", "0.1.0rc1", "0.1.0pre1",
                   "UNKNOWN_COMMIT", "PostgreSQL 17", "UTF8", "OBSERVED_ONLY",
                   "astrumweaver", "#79", "#124", "UNPUBLISHED"):
        assert phrase in notes, phrase
    assert "was published" in notes
    assert "was publicly released" in checklist
    assert "NO TAG" in checklist and "NO PRODUCTION" in checklist

def test_ga_release_notes_constrain_claims_and_publication() -> None:
    notes = (ROOT / "docs/releases/v0.1.0.md").read_text()
    gate = (ROOT / "docs/ga-qualification-v0.1.0.md").read_text()
    readme = (ROOT / "README.md").read_text()
    readiness = (ROOT / "docs/release-readiness.md").read_text()
    for phrase in (
        "v0.1.0", "NOT PUBLISHED", "astrumweaver==0.1.0",
        "PostgreSQL 17", "UTF8", "UNKNOWN_COMMIT", "OBSERVED_ONLY",
        "#120", "#124", "#149", "#150", "#151", "trusted",
        "feature", "SHA-256",
    ):
        assert phrase.lower() in notes.lower(), phrase
    for phrase in (
        "FEATURE FROZEN", "NOT CREATED", "main", "PostgreSQL 17",
        "SHA256SUMS", "v0.1.0", "#80", "#124", "Accept",
    ):
        # The human approval is explicit in prose, not a magic GitHub action.
        if phrase == "Accept":
            assert "explicit human approval" in gate
        else:
            assert phrase in gate, phrase
    assert "Feature Freeze" in readiness
    assert "v0.1.0-rc.1 was" not in readme  # README links to published RC, avoids stale preparation.
    assert "feature-frozen" in readme
    assert "v0.1.0.md" in readme and "ga-qualification-v0.1.0.md" in readme

def test_ga_scope_does_not_imply_release_or_site_authorization() -> None:
    gate = (ROOT / "docs/ga-qualification-v0.1.0.md").read_text()
    notes = (ROOT / "docs/releases/v0.1.0.md").read_text()
    assert "not a new technical PASS" in gate.replace("**", "")
    assert "not newly demonstrated as a full technical PASS" in notes
    assert "site-specific" in gate
    assert "No" in notes
    assert "NOT PUBLISHED" in notes
