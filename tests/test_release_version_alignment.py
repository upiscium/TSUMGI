"""Validate v0.1.2 release tier while preserving immutable v0.1.1 history."""
import re
import hashlib
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJECT_VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
# The v0.1.2 RC and GA are the only allowed release tiers for current source.
EXPECTED_NIX_VERSION_BY_PYTHON = {
    "0.1.2rc1": "0.1.2pre1",
    "0.1.2": "0.1.2",
}
assert PROJECT_VERSION in EXPECTED_NIX_VERSION_BY_PYTHON, PROJECT_VERSION
CURRENT_NIX_VERSION = EXPECTED_NIX_VERSION_BY_PYTHON[PROJECT_VERSION]

# Only exact, real, quoted Nix attribute assignments count. A substring such
# as 0.1.2pre2 or an extra comment must never satisfy the exact RC/GA identity.
EXPECTED_NIX_PRODUCT_ATTRIBUTES = {
    "nix/package.nix": [("version", CURRENT_NIX_VERSION)],
    "nix/integration-package.nix": [("version", CURRENT_NIX_VERSION)],
    "nix/control-support.nix": [
        ("version", CURRENT_NIX_VERSION),
        ("name", f"astrumweaver-control-support-{CURRENT_NIX_VERSION}"),
    ],
    "nix/worker-support.nix": [
        ("name", f"astrumweaver-worker-support-{CURRENT_NIX_VERSION}"),
    ],
    "nix/installer-support.nix": [
        ("version", CURRENT_NIX_VERSION),
        ("name", f"astrumweaver-installer-support-base-{CURRENT_NIX_VERSION}"),
        ("name", f"astrumweaver-installer-support-{CURRENT_NIX_VERSION}"),
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
    assert project["version"] in ("0.1.2rc1", "0.1.2")
    assert project["version"] == PROJECT_VERSION
    assert re.fullmatch(r"0\.1\.2(?:rc1)?", project["version"])
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
            invalid += f'\n# version = "{CURRENT_NIX_VERSION}";\n'
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
        f'/* outer\n  /* inner */\n  version = "{CURRENT_NIX_VERSION}";\n*/\n'
        f'  version = "{CURRENT_NIX_VERSION}pre2";\n'
    )
    mask = _nix_code_mask(fake)
    assert not mask[fake.index(f'version = "{CURRENT_NIX_VERSION}"')]
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
        f'  version = "{CURRENT_NIX_VERSION}";\n'
        "'';\n"
        f'  version = "{CURRENT_NIX_VERSION}pre2";\n'
    )
    assert not _nix_code_mask(fake)[fake.index(f'version = "{CURRENT_NIX_VERSION}"')]
    try:
        _assert_current_nix_product_attributes("nix/package.nix", fake)
    except AssertionError:
        pass
    else:
        raise AssertionError("indented string incorrectly satisfied GA identity")


def test_historical_v011_ga_release_notes_remain_intact() -> None:
    """Published v0.1.1 GA notes remain valid historical evidence."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    doc = (ROOT / "docs/releases/v0.1.1.md").read_text()
    # Frozen, public historical GA source notes are independent of current RC.
    assert hashlib.sha256((ROOT / "docs/releases/v0.1.1.md").read_bytes()).hexdigest() == (
        "448f945c655aa3b5d6280d2bd4f8d3b8c4c152ddcaa7e7f191d292656eff175c"
    )
    assert project["version"] == PROJECT_VERSION
    tick = chr(96)
    assert "| Intended GitHub tag | " + tick + "v0.1.1" + tick in doc
    assert "| Python distribution | " + tick + "astrumweaver==0.1.1" + tick + " |" in doc
    assert (
        "| Nix Control, Worker, Installer and integration packages | "
        + tick + "0.1.1" + tick + " |"
    ) in doc
    identity_table = doc.split("## Identity, scope and version compatibility", 1)[1].split("## Opt-in",1)[0]
    assert "0.1.1rc1" not in identity_table
    assert "0.1.1pre1" not in identity_table
    assert "does not establish publication" in doc
    assert "does not include unmerged upstream-Qwen3.8 packaging [#158]" in doc.replace("**", "")
    assert "000–005 are unchanged" in doc
    assert "4 of 5" in doc


def test_v012_rc_and_ga_notes_are_present_and_release_tier_matches_metadata() -> None:
    rc = (ROOT / "docs/releases/v0.1.2-rc.1.md").read_text()
    ga = (ROOT / "docs/releases/v0.1.2.md").read_text()
    assert "v0.1.2-rc.1" in rc
    assert "astrumweaver==0.1.2rc1" in rc
    assert "0.1.2pre1" in rc
    assert "v0.1.2" in ga
    assert ("astrumweaver==0.1.2" + chr(96)) in ga
    assert "Nix" in ga and "0.1.2" in ga
    current_doc = rc if PROJECT_VERSION == "0.1.2rc1" else ga
    assert f"astrumweaver=={PROJECT_VERSION}" in current_doc
    assert (chr(96) + CURRENT_NIX_VERSION + chr(96)) in current_doc
    if PROJECT_VERSION == "0.1.2rc1":
        assert "| Proposed Git tag" in current_doc
    else:
        assert "| Intended GitHub tag" in current_doc
    for note in (rc, ga):
        assert "PR #158" in note and "PR #166" in note and "PR #169" in note
        assert "#168" in note and "#149" in note
        assert "does not establish publication" in note
        assert "Stored Completions" in note
        assert "no" in note.lower() and "Control" in note


def test_published_v011_rc_notes_remain_a_distinct_historical_artifact() -> None:
    rc = (ROOT / "docs/releases/v0.1.1-rc.1.md").read_text()
    assert "TSUMGI v0.1.1-rc.1" in rc
    assert "astrumweaver==0.1.1rc1" in rc
    assert "0.1.1pre1" in rc
    assert "f49c0f9dd2f494b45c0d4a76c24eab80157832c7" in rc
    # Exact historical RC file hash is published in the RC provenance manifest.
    assert hashlib.sha256((ROOT / "docs/releases/v0.1.1-rc.1.md").read_bytes()).hexdigest() == (
        "f4dfd051aad2815a01982b85ddcaa2f381c8a0d4c6790e6c1d747ed0b2d46afc"
    ), "Published v0.1.1-rc.1 historical release note has changed"


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
    # README must reflect the current published stable line and next
    # maintenance release instead of claiming v0.1.0 is still current.
    if PROJECT_VERSION == "0.1.2rc1":
        assert "> **Published stable release:** [v0.1.1]" in readme
        assert "> **v0.1.2 maintenance candidate:**" in readme
    else:
        assert "> **Published stable release:** [v0.1.2]" in readme
    assert "v0.1.2" in readme
    assert "v0.1.0.md" in readme and "ga-qualification-v0.1.0.md" in readme

def test_ga_scope_does_not_imply_release_or_site_authorization() -> None:
    gate = (ROOT / "docs/ga-qualification-v0.1.0.md").read_text()
    notes = (ROOT / "docs/releases/v0.1.0.md").read_text()
    assert "not a new technical PASS" in gate.replace("**", "")
    assert "not newly demonstrated as a full technical PASS" in notes
    assert "site-specific" in gate
    assert "No" in notes
    assert "does not establish publication" in notes
