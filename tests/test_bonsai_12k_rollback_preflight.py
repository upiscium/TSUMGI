"""Pure synthetic host snapshot and GCroot rollback verification (#232)."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from tools.bonsai_12k_rollback_preflight import (
    CONTROL_FILES,
    CONTROL_ROOT,
    LEGACY_DENEB_UUID,
    MODEL_SHA256,
    SCHEMA,
    SNAPSHOT,
    SPICA_FILES,
    SPICA_PROFILES,
    SPICA_UUID,
    PreflightBlock,
    check_host,
    mapped,
)


def _write(path: Path, payload: bytes, mode: int) -> None:
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    path.write_bytes(payload)
    path.chmod(mode)


def _symlink(root: Path, source: str, store: str) -> None:
    store_path = mapped(root, store)
    store_path.mkdir(mode=0o755, parents=True, exist_ok=False)
    p = mapped(root, source)
    p.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    p.symlink_to(store_path, target_is_directory=True)


def _spica_bytes(name: str) -> bytes:
    if name == "bonsai-worker.toml":
        return (
            b'[worker]\nid = "bonsai-spica"\ngpu_uuids = ["'
            + SPICA_UUID.encode()
            + b'"]\n'
        )
    if name == "bonsai-runtime.json":
        return json.dumps({
            "provider_config": {"context_size": 8192},
            "setup_intent": {"configuration": {
                "context_size": 8192, "gpu_uuids": [LEGACY_DENEB_UUID],
            }},
        }).encode()
    if name == "bonsai-model.sha256":
        return (MODEL_SHA256 + "  /synthetic/pinned-model.gguf\n").encode()
    return ("synthetic-only/" + name + "\n").encode()


def _control_bytes(name: str) -> bytes:
    if name == "chat-catalog.json":
        return json.dumps({
            "schema_version": "chat-gateway-catalog-v1",
            "profiles": [
                {"profile": {"profile_id": "qwen3.8-27b"},
                 "contract": {"limits": {"total_tokens": 12288}}},
                {"profile": {"profile_id": "bonsai2-27b"},
                 "contract": {"limits": {"total_tokens": 8192}}},
            ],
        }).encode()
    return ("synthetic-only/" + name + "\n").encode()


def make_fixture(root: Path, site: str) -> tuple[Path, dict]:
    snap = mapped(root, SNAPSHOT)
    snap.mkdir(mode=0o700, parents=True)
    snap.chmod(0o700)
    spica = site == "spica"
    files = SPICA_FILES if spica else CONTROL_FILES
    source = _spica_bytes if spica else _control_bytes
    field = "source" if spica else "path"
    records = {}
    for name, path in files.items():
        mode = 0o644 if name.endswith((".service", ".conf")) else 0o640
        data = source(name)
        _write(mapped(root, path), data, mode)
        _write(snap / name, data, 0o400)
        records[name] = {
            field: path,
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "mode": mode,
            "uid": os.getuid(),
            "gid": os.getgid(),
        }
    common = {
        "schema": SCHEMA,
        "host": "spica" if spica else "tsumgi-control",
        "mode": (
            "CURRENT_8K_HOST_LOCAL_BACKUP_ONLY" if spica
            else "CURRENT_QWEN12K_BONSAI8K_HOST_LOCAL_BACKUP_ONLY"
        ),
        "read_only_restore_rehearsed": True,
        "services_changed": False,
        "cross_host_atomic": False,
        "live_rollback_verified": False,
    }
    if spica:
        roots = {}
        for label, (link, profile) in SPICA_PROFILES.items():
            store = f"/nix/store/{'a' * 32}-{label}-synthetic-profile"
            _symlink(root, link, store)
            p = mapped(root, profile)
            p.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            p.symlink_to(mapped(root, store), target_is_directory=True)
            roots[label] = {"root": link, "profile": profile, "store": store}
        receipt = {
            **common,
            "items": records,
            "nix_roots": roots,
            "model_artifact_sha256": "sha256:" + MODEL_SHA256,
            "model_bytes_sha256_verified_prior": True,
            "model_weights_copied": False,
            "external_offhost_backup": False,
            "spica_setup_uuid_matches_worker": False,
        }
    else:
        store = "/nix/store/" + "b" * 32 + "-control-synthetic-profile"
        _symlink(root, CONTROL_ROOT, store)
        p = mapped(root, "/nix/var/nix/profiles/tsumgi-v012-ga-control")
        p.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        p.symlink_to(mapped(root, store), target_is_directory=True)
        receipt = {
            **common,
            "source": records,
            "nix_control_profile": store,
            "source_catalog_limits": {
                "qwen3.8-27b": 12288,
                "bonsai2-27b": 8192,
            },
            "offhost_backup": False,
        }
    _write(snap / "snapshot-receipt.json",
           (json.dumps(receipt) + "\n").encode(), 0o600)
    return snap, receipt


@pytest.mark.parametrize("site,file_count,root_count", [
    ("spica", 9, 2), ("control", 4, 1),
])
def test_synthetic_backup_receipt_matches_exact_sources(
    tmp_path: Path, site: str, file_count: int, root_count: int,
):
    root = tmp_path / site
    root.mkdir()
    make_fixture(root, site)
    before = {str(p) for p in root.rglob("*")}
    result = check_host(site, root=root)
    assert result == {
        "status": "HOST_LOCAL_BACKUP_BYTES_MATCH_NO_LIVE_ADMISSION",
        "site": site,
        "files_checked": file_count,
        "nix_roots_checked": root_count,
        "synthetic_root": True,
        "live_rollback_verified": False,
        "cross_host_atomic": False,
        "offhost_backup": False,
        "model_full_hash_verified_this_run": False,
        "canary_activation_authorized": False,
    }
    assert before == {str(p) for p in root.rglob("*")}


@pytest.mark.parametrize("site", ["spica", "control"])
def test_source_drift_and_backup_tampering_are_rejected(tmp_path, site):
    root = tmp_path / site
    root.mkdir()
    snap, _ = make_fixture(root, site)
    name = "bonsai-worker.toml" if site == "spica" else "chat-catalog.json"
    source = SPICA_FILES[name] if site == "spica" else CONTROL_FILES[name]
    live = mapped(root, source)
    live.write_bytes(live.read_bytes() + b"corrupted")
    with pytest.raises(PreflightBlock, match="source_or_snapshot_drift|snapshot_size"):
        check_host(site, root=root)
    live.write_bytes((snap / name).read_bytes())
    (snap / name).chmod(0o600)
    with pytest.raises(PreflightBlock, match="file_mode_mismatch"):
        check_host(site, root=root)
    (snap / name).chmod(0o400)
    (snap / name).unlink()
    (snap / name).symlink_to(live)
    with pytest.raises(PreflightBlock, match="nonregular_or_symlink"):
        check_host(site, root=root)


@pytest.mark.parametrize("site", ["spica", "control"])
def test_receipt_not_private_or_false_evidence_rejected(tmp_path, site):
    root = tmp_path / site
    root.mkdir()
    snap, receipt = make_fixture(root, site)
    path = snap / "snapshot-receipt.json"
    path.chmod(0o644)
    with pytest.raises(PreflightBlock, match="file_mode_mismatch"):
        check_host(site, root=root)
    path.chmod(0o600)
    receipt["live_rollback_verified"] = True
    path.write_text(json.dumps(receipt))
    with pytest.raises(PreflightBlock, match="receipt_invalid_lifecycle_claim"):
        check_host(site, root=root)


@pytest.mark.parametrize("site", ["spica", "control"])
def test_changed_gcroot_or_active_profile_is_rejected(tmp_path, site):
    root = tmp_path / site
    root.mkdir()
    _, receipt = make_fixture(root, site)
    if site == "spica":
        label = "bonsai_runtime"
        root_link = mapped(root, receipt["nix_roots"][label]["root"])
        profile = mapped(root, receipt["nix_roots"][label]["profile"])
    else:
        root_link = mapped(root, CONTROL_ROOT)
        profile = mapped(root, "/nix/var/nix/profiles/tsumgi-v012-ga-control")
    root_link.unlink()
    root_link.symlink_to(root / "nix/store/not-the-pinned-profile")
    with pytest.raises(PreflightBlock, match="nix_link_target_unavailable"):
        check_host(site, root=root)
    root_link.unlink()
    if site == "spica":
        expected = mapped(root, receipt["nix_roots"]["bonsai_runtime"]["store"])
    else:
        expected = mapped(root, receipt["nix_control_profile"])
    root_link.symlink_to(expected)
    profile.unlink()
    wrong = root / "nix/store/not-the-pinned-profile"
    wrong.mkdir(parents=True)
    profile.symlink_to(wrong)
    with pytest.raises(PreflightBlock, match="active_nix_profile_drift"):
        check_host(site, root=root)


def test_spica_worker_and_runtime_8k_identity_must_be_exact(tmp_path):
    root = tmp_path / "spica"
    root.mkdir()
    snap, receipt = make_fixture(root, "spica")
    path = snap / "bonsai-runtime.json"
    source = mapped(root, SPICA_FILES["bonsai-runtime.json"])
    modified = json.loads(path.read_text())
    modified["setup_intent"]["configuration"]["context_size"] = 12288
    raw = json.dumps(modified).encode()
    source.write_bytes(raw)
    path.chmod(0o600)
    path.write_bytes(raw)
    path.chmod(0o400)
    receipt["items"]["bonsai-runtime.json"]["size"] = len(raw)
    receipt["items"]["bonsai-runtime.json"]["sha256"] = hashlib.sha256(raw).hexdigest()
    (snap / "snapshot-receipt.json").write_text(json.dumps(receipt))
    with pytest.raises(PreflightBlock, match="spica_old_8k_runtime_changed"):
        check_host("spica", root=root)


def test_control_profile_set_must_not_silently_include_12k_bonsai(tmp_path):
    root = tmp_path / "control"
    root.mkdir()
    snap, receipt = make_fixture(root, "control")
    source = mapped(root, CONTROL_FILES["chat-catalog.json"])
    data = json.loads(source.read_text())
    data["profiles"].append({
        "profile": {"profile_id": "bonsai2-27b-12k"},
        "contract": {"limits": {"total_tokens": 12288}},
    })
    raw = json.dumps(data).encode()
    source.write_bytes(raw)
    target = snap / "chat-catalog.json"
    target.chmod(0o600)
    target.write_bytes(raw)
    target.chmod(0o400)
    receipt["source"]["chat-catalog.json"]["size"] = len(raw)
    receipt["source"]["chat-catalog.json"]["sha256"] = hashlib.sha256(raw).hexdigest()
    (snap / "snapshot-receipt.json").write_text(json.dumps(receipt))
    with pytest.raises(PreflightBlock, match="baseline_catalog_changed"):
        check_host("control", root=root)


def test_unsafe_source_path_in_receipt_refused(tmp_path):
    root = tmp_path / "spica"
    root.mkdir()
    snap, receipt = make_fixture(root, "spica")
    receipt["items"]["worker.env"]["source"] = "/etc/../private-key"
    (snap / "snapshot-receipt.json").write_text(json.dumps(receipt))
    with pytest.raises(PreflightBlock, match="receipt_source_mismatch"):
        check_host("spica", root=root)


def test_missing_gcroot_and_missing_receipt_are_blocking(tmp_path):
    root = tmp_path / "spica"
    root.mkdir()
    snap, receipt = make_fixture(root, "spica")
    missing = mapped(root, receipt["nix_roots"]["worker_installer"]["root"])
    missing.unlink()
    with pytest.raises(PreflightBlock, match="missing_nix_symlink"):
        check_host("spica", root=root)
    (snap / "snapshot-receipt.json").unlink()
    with pytest.raises(PreflightBlock, match="file_unreadable"):
        check_host("spica", root=root)


def test_writable_snapshot_directory_is_refused(tmp_path):
    root = tmp_path / "control"
    root.mkdir()
    snap, _ = make_fixture(root, "control")
    snap.chmod(0o755)
    with pytest.raises(PreflightBlock, match="snapshot_directory_not_private"):
        check_host("control", root=root)


def test_unknown_site_is_denied(tmp_path):
    with pytest.raises(PreflightBlock, match="unknown_site"):
        check_host("deneb", root=tmp_path)


def test_rollback_receipt_source_owner_change_is_refused(tmp_path):
    root = tmp_path / "spica"
    root.mkdir()
    snap, receipt = make_fixture(root, "spica")
    receipt["items"]["worker.env"]["uid"] = os.getuid() + 1
    (snap / "snapshot-receipt.json").write_text(json.dumps(receipt))
    with pytest.raises(PreflightBlock, match="source_owner_drift"):
        check_host("spica", root=root)


@pytest.mark.parametrize("site", ["spica", "control"])
def test_gcroot_receipt_does_not_allow_store_path_traversal(tmp_path, site):
    root = tmp_path / site
    root.mkdir()
    snap, receipt = make_fixture(root, site)
    if site == "spica":
        receipt["nix_roots"]["bonsai_runtime"]["store"] = "/nix/store/../etc"
    else:
        receipt["nix_control_profile"] = "/nix/store/../etc"
    (snap / "snapshot-receipt.json").write_text(json.dumps(receipt))
    with pytest.raises(PreflightBlock, match="invalid_store_reference"):
        check_host(site, root=root)


@pytest.mark.parametrize("site", ["spica", "control"])
def test_live_configuration_symlink_source_not_followed(tmp_path, site):
    root = tmp_path / site
    root.mkdir()
    _, _ = make_fixture(root, site)
    source = (
        SPICA_FILES["worker.env"] if site == "spica"
        else CONTROL_FILES["control.env"]
    )
    path = mapped(root, source)
    original = path.read_bytes()
    path.unlink()
    target = root / "outside-protected-sources"
    target.write_bytes(original)
    target.chmod(0o640)
    path.symlink_to(target)
    with pytest.raises(PreflightBlock, match="nonregular_or_symlink"):
        check_host(site, root=root)



def test_synthetic_root_path_traversal_is_rejected(tmp_path):
    root = tmp_path / "spica"
    root.mkdir()
    make_fixture(root, "spica")
    (root / "sub").mkdir()
    escaped = root / "sub" / ".."
    assert escaped.is_dir()
    with pytest.raises(PreflightBlock, match="noncanonical_root"):
        check_host("spica", root=escaped)


def test_synthetic_root_with_symlinked_ancestor_is_rejected(tmp_path):
    root = tmp_path / "spica"
    root.mkdir()
    make_fixture(root, "spica")
    alias = tmp_path / "link"
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(PreflightBlock, match="invalid_root"):
        check_host("spica", root=alias)
    (root / "nested").mkdir()
    with pytest.raises(PreflightBlock, match="noncanonical_root"):
        check_host("spica", root=alias / "nested")
