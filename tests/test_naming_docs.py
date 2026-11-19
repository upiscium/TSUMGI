"""Guard the TSUMGI names across the docs, package metadata and Nix interface."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_readme_and_docs_use_tsumgi_brand() -> None:
    readme = (ROOT / "README.md").read_text()
    naming = (ROOT / "docs" / "naming.md").read_text()
    assert readme.startswith("# TSUMGI\n")
    assert "[Naming and compatibility](docs/naming.md)" in readme
    assert "TSUMGI（紡ぎ）" in naming


def test_runtime_interfaces_use_the_tsumgi_names() -> None:
    project = (ROOT / "pyproject.toml").read_text()
    flake = (ROOT / "flake.nix").read_text()
    assert 'name = "tsumgi"' in project
    assert 'tsumgi-control = "tsumgi.control.daemon:main"' in project
    assert "services.tsumgi.control" in flake
    assert "services.tsumgi.worker" in flake
