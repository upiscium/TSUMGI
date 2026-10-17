"""Validate stable v0.1.0 product versions while preserving RC history and legacy ABI."""
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Only exact, real, quoted Nix attribute assignments count. A substring such
# as 0.1.0pre2 or an extra comment must never satisfy the stable release gate.
EXPECTED_NIX_PRODUCT_ATTRIBUTES = {
    "nix/package.nix": [("version", "0.1.0")],
    "nix/integration-package.nix": [("version", "0.1.0")],
    "nix/control-support.nix": [
        ("version", "0.1.0"),
        ("name", "astrumweaver-control-support-0.1.0"),
    ],
    "nix/worker-support.nix": [
        ("name", "astrumweaver-worker-support-0.1.0"),
    ],
    "nix/installer-support.nix": [
        ("version", "0.1.0"),
        ("name", "astrumweaver-installer-support-base-0.1.0"),
        ("name", "astrumweaver-installer-support-0.1.0"),
    ],
}


def _assert_ga_nix_product_attributes(path: str, data: str) -> None:
    # This is a deliberately narrow check of fixed, simple product declarations,
    # not a general Nix parser. Require all occurrences and exact full values.
    actual = re.findall(
        r'^[ \t]*(name|version)[ \t]*=[ \t]*"([^"\n]*)"[ \t]*;',
        data,
        flags=re.MULTILINE,
    )
    expected = EXPECTED_NIX_PRODUCT_ATTRIBUTES[path]
    assert actual == expected, (path, actual, expected)


def test_ga_product_versions_align_across_python_and_nix() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["name"] == "astrumweaver"
    assert project["version"] == "0.1.0"
    assert re.fullmatch(r"\d+\.\d+\.\d+", project["version"])
    for path in EXPECTED_NIX_PRODUCT_ATTRIBUTES:
        _assert_ga_nix_product_attributes(path, (ROOT / path).read_text())
    assert (ROOT / "src/astrumweaver").is_dir()
    assert 'version = "0.6.0-dev-' in (ROOT / "nix/decision-llama-cuda.nix").read_text()


def test_ga_product_versions_reject_incorrect_full_attributes() -> None:
    for path, assignments in EXPECTED_NIX_PRODUCT_ATTRIBUTES.items():
        data = (ROOT / path).read_text()
        for key, value in assignments:
            original = f'{key} = "{value}";'
            assert original in data
            invalid = data.replace(original, f'{key} = "{value}pre2";', 1)
            invalid += '\n# 0.1.0\n'
            try:
                _assert_ga_nix_product_attributes(path, invalid)
            except AssertionError:
                pass
            else:
                raise AssertionError((path, key, value, "wrong attribute was accepted"))


def test_ga_product_versions_ignore_comments_for_missing_declarations() -> None:
    for path, assignments in EXPECTED_NIX_PRODUCT_ATTRIBUTES.items():
        data = (ROOT / path).read_text()
        key, value = assignments[0]
        original = f'{key} = "{value}";'
        assert original in data
        commented = data.replace(original, f'# {original}', 1)
        try:
            _assert_ga_nix_product_attributes(path, commented)
        except AssertionError:
            pass
        else:
            raise AssertionError((path, "commented-out declaration was accepted"))

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
        "v0.1.0", "does not establish publication", "astrumweaver==0.1.0",
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
    assert "does not establish publication" in notes
