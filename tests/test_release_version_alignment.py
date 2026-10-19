"""Validate v0.1.1 GA candidate versions while preserving published release history."""
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Only exact, real, quoted Nix attribute assignments count. A substring such
# as 0.1.1pre2 or an extra comment must never satisfy the exact GA identity.
EXPECTED_NIX_PRODUCT_ATTRIBUTES = {
    "nix/package.nix": [("version", "0.1.1")],
    "nix/integration-package.nix": [("version", "0.1.1")],
    "nix/control-support.nix": [
        ("version", "0.1.1"),
        ("name", "astrumweaver-control-support-0.1.1"),
    ],
    "nix/worker-support.nix": [
        ("name", "astrumweaver-worker-support-0.1.1"),
    ],
    "nix/installer-support.nix": [
        ("version", "0.1.1"),
        ("name", "astrumweaver-installer-support-base-0.1.1"),
        ("name", "astrumweaver-installer-support-0.1.1"),
    ],
}


def _nix_code_mask(source: str) -> bytearray:
    """Mark code positions, ignoring comments and both Nix string syntaxes."""
    mask = bytearray(b"\x01") * len(source)
    i = 0
    while i < len(source):
        start = i
        if source.startswith("''", i):
            i += 2
            while i < len(source):
                if source.startswith("''$" + "{", i):
                    i += 4
                elif source.startswith("''" + "'", i):
                    i += 3
                elif source.startswith("''", i):
                    i += 2
                    break
                else:
                    i += 1
            else:
                raise AssertionError("unterminated Nix indented string")
        elif source[i] == '"':
            i += 1
            while i < len(source):
                if source[i] == "\\":
                    i += 2
                elif source[i] == '"':
                    i += 1
                    break
                else:
                    i += 1
            else:
                raise AssertionError("unterminated Nix quoted string")
        elif source.startswith("/*", i):
            depth = 1
            i += 2
            while depth:
                if i >= len(source):
                    raise AssertionError("unterminated Nix block comment")
                if source.startswith("/*", i):
                    depth += 1
                    i += 2
                elif source.startswith("*/", i):
                    depth -= 1
                    i += 2
                else:
                    i += 1
        elif source[i] == "#":
            newline = source.find("\n", i)
            i = len(source) if newline < 0 else newline
        else:
            i += 1
            continue
        mask[start:i] = b"\x00" * (i-start)
    return mask


def _assert_current_nix_product_attributes(path: str, data: str) -> None:
    # Exact assignments must occur in active Nix code. A block comment, a
    # quoted literal or an indented shell string cannot supply a fake version.
    mask = _nix_code_mask(data)
    actual = [
        (match.group(1), match.group(2))
        for match in re.finditer(
            r'^[ \t]*(name|version)[ \t]*=[ \t]*"([^"\n]*)"[ \t]*;',
            data, flags=re.MULTILINE,
        )
        if mask[match.start(1)]
    ]
    expected = EXPECTED_NIX_PRODUCT_ATTRIBUTES[path]
    assert actual == expected, (path, actual, expected)


def test_ga_candidate_product_versions_align_across_python_and_nix() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["name"] == "astrumweaver"
    assert project["version"] == "0.1.1"
    assert re.fullmatch(r"\d+\.\d+\.\d+", project["version"])
    for path in EXPECTED_NIX_PRODUCT_ATTRIBUTES:
        _assert_current_nix_product_attributes(path, (ROOT / path).read_text())
    assert (ROOT / "src/astrumweaver").is_dir()
    assert 'version = "0.6.0-dev-' in (ROOT / "nix/decision-llama-cuda.nix").read_text()


def test_ga_candidate_product_versions_reject_incorrect_full_attributes() -> None:
    for path, assignments in EXPECTED_NIX_PRODUCT_ATTRIBUTES.items():
        data = (ROOT / path).read_text()
        for key, value in assignments:
            original = f'{key} = "{value}";'
            assert original in data
            invalid = data.replace(original, f'{key} = "{value}pre2";', 1)
            invalid += '\n# version = "0.1.1";\n'
            try:
                _assert_current_nix_product_attributes(path, invalid)
            except AssertionError:
                pass
            else:
                raise AssertionError((path, key, value, "wrong attribute was accepted"))


def test_ga_candidate_product_versions_ignore_comments_for_missing_declarations() -> None:
    for path, assignments in EXPECTED_NIX_PRODUCT_ATTRIBUTES.items():
        data = (ROOT / path).read_text()
        key, value = assignments[0]
        original = f'{key} = "{value}";'
        assert original in data
        commented = data.replace(original, f'# {original}', 1)
        try:
            _assert_current_nix_product_attributes(path, commented)
        except AssertionError:
            pass
        else:
            raise AssertionError((path, "commented-out declaration was accepted"))


def test_ga_candidate_product_versions_ignore_nested_block_comment_decoys() -> None:
    fake = (
        '/* outer\n  /* inner */\n  version = "0.1.1";\n*/\n'
        '  version = "0.1.1pre2";\n'
    )
    mask = _nix_code_mask(fake)
    assert not mask[fake.index('version = "0.1.1"')]
    try:
        _assert_current_nix_product_attributes("nix/package.nix", fake)
    except AssertionError:
        pass
    else:
        raise AssertionError("block comment incorrectly satisfied GA identity")


def test_ga_candidate_product_versions_ignore_indented_shell_decoys() -> None:
    fake = (
        "installPhase = ''\n"
        '  cp migrations/*.sql /tmp/example\n'
        '  version = "0.1.1";\n'
        "'';\n"
        '  version = "0.1.1pre2";\n'
    )
    assert not _nix_code_mask(fake)[fake.index('version = "0.1.1"')]
    try:
        _assert_current_nix_product_attributes("nix/package.nix", fake)
    except AssertionError:
        pass
    else:
        raise AssertionError("indented string incorrectly satisfied GA identity")


def test_v011_ga_release_note_version_identity_matches_current_packages() -> None:
    """Release notes must not describe a different Python/Nix release tier."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    doc = (ROOT / "docs/releases/v0.1.1.md").read_text()
    assert project["version"] == "0.1.1"
    assert "v0.1.1" in doc
    assert "astrumweaver==0.1.1" in doc
    assert "0.1.1" in doc
    assert "does not establish publication" in doc
    assert "#158" in doc
    assert "000–005 are unchanged" in doc


def test_published_v011_rc_notes_remain_a_distinct_historical_artifact() -> None:
    rc = (ROOT / "docs/releases/v0.1.1-rc.1.md").read_text()
    assert "TSUMGI v0.1.1-rc.1" in rc
    assert "astrumweaver==0.1.1rc1" in rc
    assert "0.1.1pre1" in rc
    assert "f49c0f9dd2f494b45c0d4a76c24eab80157832c7" in rc


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
