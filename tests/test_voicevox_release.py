"""Synthetic bytes only: no release archive, model, extractor or native code."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from astrumweaver.runtime.providers import voicevox_manifest as manifest
from astrumweaver.runtime.providers import voicevox_release as release

RIGHTS = "sha256:" + "a" * 64


def _sha(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def test_build_utility_release_pins_equal_standard_provider():
    assert release.ARCHIVE_SHA256 == manifest.ARCHIVE_SHA256
    assert release.EXECUTABLE_SHA256 == manifest.EXECUTABLE_SHA256
    assert release.MODEL_SHA256 == manifest.MODEL_SHA256
    assert release.VERSION == "0.26.2"


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    root = tmp_path / "package/lib/voicevox/linux-cpu-x64"
    root.mkdir(parents=True)
    (root / "model").mkdir()
    (root / "lib").mkdir()
    executable, model = b"synthetic non-executable fixture", b"synthetic model"
    (root / "run").write_bytes(executable)
    (root / "run").chmod(0o700)
    (root / "model/0.vvm").write_bytes(model)
    (root / "lib/runtime.so").write_bytes(b"synthetic dependency")
    (root / "LICENSE").write_text("synthetic license fixture, not a grant\n")
    monkeypatch.setattr(release, "EXECUTABLE_SHA256", _sha(executable))
    monkeypatch.setattr(release, "MODEL_SHA256", _sha(model))
    receipt = tmp_path / "package/voicevox-bundle.json"
    return root, receipt


def test_receipt_covers_all_content_without_native_execution(bundle, monkeypatch):
    root, receipt = bundle
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("native execution"))
    before = {p: (p.read_bytes(), p.stat().st_mode) for p in root.rglob("*") if p.is_file()}
    recorded = release.record_bundle(root, receipt, rights_review_sha256=RIGHTS)
    assert release.verify_bundle(root, receipt, rights_review_sha256=RIGHTS) == recorded
    assert recorded["native_execution_qualified"] is False
    assert len(recorded["inventory"]) == 6
    assert str(root) not in receipt.read_text()
    assert before == {p: (p.read_bytes(), p.stat().st_mode) for p in before}
    with pytest.raises(release.VoicevoxBundleError):
        release.record_bundle(root, receipt, rights_review_sha256=RIGHTS)


@pytest.mark.parametrize("change", [
    "run", "model", "dependency", "extra", "mode", "hardlink", "fifo",
    "escape", "absolute_link", "rights", "receipt_symlink", "receipt_duplicate",
])
def test_invalid_or_changed_bundle_is_rejected(bundle, tmp_path, change):
    root, receipt = bundle
    release.record_bundle(root, receipt, rights_review_sha256=RIGHTS)
    rights = RIGHTS
    if change in ("run", "model", "dependency"):
        relative = {"run": "run", "model": "model/0.vvm", "dependency": "lib/runtime.so"}[change]
        (root / relative).write_bytes(b"changed")
    elif change == "extra":
        (root / "extra").write_bytes(b"unexpected input")
    elif change == "mode":
        (root / "lib/runtime.so").chmod(0o666)
    elif change == "hardlink":
        os.link(root / "lib/runtime.so", root / "alias")
    elif change == "fifo":
        os.mkfifo(root / "not-a-library")
    elif change == "escape":
        (root / "escape").symlink_to("../../../../")
    elif change == "absolute_link":
        (root / "absolute").symlink_to(root / "lib/runtime.so")
    elif change == "rights":
        rights = "sha256:" + "b" * 64
    elif change == "receipt_symlink":
        linked = tmp_path / "receipt-link"
        linked.symlink_to(receipt)
        receipt = linked
    elif change == "receipt_duplicate":
        receipt.write_text('{"schema_version":"a","schema_version":"b"}')
    with pytest.raises(release.VoicevoxBundleError, match="^VOICEVOX bundle verification failed$"):
        release.verify_bundle(root, receipt, rights_review_sha256=rights)


def test_contained_relative_library_symlink_is_recorded(bundle):
    root, receipt = bundle
    (root / "lib/alias.so").symlink_to("runtime.so")
    recorded = release.record_bundle(root, receipt, rights_review_sha256=RIGHTS)
    assert {"path": "lib/alias.so", "kind": "symlink", "target": "runtime.so"} in recorded["inventory"]
    release.verify_bundle(root, receipt, rights_review_sha256=RIGHTS)


def test_receipt_survives_readonly_store_permission_normalization(bundle):
    root, receipt = bundle
    recorded = release.record_bundle(root, receipt, rights_review_sha256=RIGHTS)
    for path in root.rglob("*"):
        if path.is_file():
            path.chmod(0o555 if path.stat().st_mode & 0o111 else 0o444)
    receipt.chmod(0o444)
    assert release.verify_bundle(root, receipt, rights_review_sha256=RIGHTS) == recorded


def test_hardlink_exception_requires_root_owned_readonly_store_file():
    store_file = Path("/nix/store/synthetic-only/file")
    readonly_root = SimpleNamespace(st_uid=0, st_mode=0o444)
    assert release._immutable_store_file(store_file, readonly_root)
    assert not release._immutable_store_file(Path("/tmp/file"), readonly_root)
    assert not release._immutable_store_file(store_file, SimpleNamespace(st_uid=1000, st_mode=0o444))
    assert not release._immutable_store_file(store_file, SimpleNamespace(st_uid=0, st_mode=0o644))


@pytest.mark.parametrize(("name", "limit"), [
    ("_MAX_ENTRIES", 1), ("_MAX_DEPTH", 0), ("_FILE_BYTES", 1),
    ("_TOTAL_BYTES", 1), ("_MAX_RECEIPT_BYTES", 1), ("_SCAN_SECONDS", 0),
])
def test_bundle_limits_fail_closed(bundle, monkeypatch, name, limit):
    root, receipt = bundle
    monkeypatch.setattr(release, name, limit)
    with pytest.raises(release.VoicevoxBundleError):
        release.record_bundle(root, receipt, rights_review_sha256=RIGHTS)
    assert not receipt.exists()


def test_record_refuses_receipt_inside_engine_tree(bundle):
    root, _ = bundle
    with pytest.raises(release.VoicevoxBundleError):
        release.record_bundle(root, root / "receipt.json", rights_review_sha256=RIGHTS)
    assert not (root / "receipt.json").exists()


def test_archive_pin_checked_without_extractor(tmp_path, monkeypatch):
    archive = tmp_path / "archive.7z"
    data = b"synthetic archive; not 7z bytes"
    archive.write_bytes(data)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("extractor called"))
    with pytest.raises(release.VoicevoxBundleError):
        release.verify_archive(archive)
    monkeypatch.setattr(release, "ARCHIVE_SHA256", _sha(data))
    release.verify_archive(archive)
    archive.write_bytes(data + b"changed")
    with pytest.raises(release.VoicevoxBundleError):
        release.verify_archive(archive)


def test_archive_rejects_growth_during_hash(tmp_path, monkeypatch):
    archive = tmp_path / "archive.7z"
    archive.write_bytes(b"synthetic archive")
    read = os.read
    calls = []

    def grow(fd, size):
        block = read(fd, size)
        calls.append(size)
        if len(calls) == 1:
            with archive.open("ab") as stream:
                stream.write(b"growing source")
        return block

    monkeypatch.setattr(release.os, "read", grow)
    with pytest.raises(release.VoicevoxBundleError):
        release.verify_archive(archive)
    assert calls == [len(b"synthetic archive"), 1]
