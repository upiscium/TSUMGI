from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
from unittest.mock import patch

import pytest

from astrumweaver.audio import ArtifactSpool
from astrumweaver.audio.artifact_spool import ArtifactSpool as ImplementedArtifactSpool


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


@pytest.fixture
def spool_env(tmp_path):
    root = tmp_path / "spool-root"
    root.mkdir(mode=0o700)
    root.chmod(0o700)  # Enforce the actual fixture mode regardless of umask.
    clock = Clock()
    spool = ArtifactSpool(root, max_bytes=256, ttl_seconds=10, clock=clock)
    return spool, root, clock


def backing_file(root, payload):
    # Discover storage only through our own directory, never receipt metadata.
    matches = [
        entry for entry in root.rglob("*")
        if entry.is_file() and not entry.is_symlink() and entry.read_bytes() == payload
    ]
    assert len(matches) == 1
    return matches[0]


def test_export_and_default_clock(tmp_path):
    assert ArtifactSpool is ImplementedArtifactSpool
    spool = ArtifactSpool(tmp_path, max_bytes=64, ttl_seconds=60)
    payload = b"default monotonic clock"
    receipt = spool.stage(payload, scope="session-a")
    assert spool.read(receipt["artifact_id"], scope="session-a") == payload


def test_receipt_is_minimal_json_and_ids_are_opaque_and_distinct(spool_env):
    spool, root, _ = spool_env
    payload = b"private audio sentinel\x00\xff\x80"
    scope = "secret-session-marker"
    receipts = [spool.stage(payload, scope=scope) for _ in range(3)]
    ids = []
    for receipt in receipts:
        assert type(receipt) is dict
        assert set(receipt) == {"artifact_id", "sha256", "size"}
        assert receipt["sha256"] == hashlib.sha256(payload).hexdigest()
        assert type(receipt["size"]) is int
        assert receipt["size"] == len(payload)
        artifact_id = receipt["artifact_id"]
        assert isinstance(artifact_id, str) and artifact_id
        assert not any(part in artifact_id for part in ("/", "\\", "..", scope))
        assert artifact_id != receipt["sha256"]
        serialized = json.dumps(receipt)
        assert json.loads(serialized) == receipt
        assert str(root) not in serialized
        assert "private audio sentinel" not in serialized
        assert scope not in serialized
        assert spool.read(artifact_id, scope=scope) == payload
        ids.append(artifact_id)
    assert len(set(ids)) == len(ids)


@pytest.mark.parametrize("wrong_scope", ["other", "Session-a", "session-a ", " session-a"])
def test_scope_matching_is_exact_and_unauthorized_delete_is_non_destructive(spool_env, wrong_scope):
    spool, _, _ = spool_env
    payload = bytes(range(32))
    artifact_id = spool.stage(payload, scope="session-a")["artifact_id"]
    for operation in (spool.read, spool.delete):
        with pytest.raises(ValueError):
            operation(artifact_id, scope=wrong_scope)
        assert spool.read(artifact_id, scope="session-a") == payload


@pytest.mark.parametrize("scope", [None, "", 0, False, b"session-a", [], {}])
def test_scope_must_be_an_explicit_nonempty_string(spool_env, scope):
    spool, _, _ = spool_env
    with pytest.raises(ValueError):
        spool.stage(b"invalid scope", scope=scope)
    artifact_id = spool.stage(b"keep", scope="owner")["artifact_id"]
    for operation in (spool.read, spool.delete):
        with pytest.raises(ValueError):
            operation(artifact_id, scope=scope)
    assert spool.read(artifact_id, scope="owner") == b"keep"


def test_authorized_deletion_removes_backing_content(spool_env):
    spool, root, _ = spool_env
    payload = b"delete this audio"
    artifact_id = spool.stage(payload, scope="owner")["artifact_id"]
    entry = backing_file(root, payload)
    spool.delete(artifact_id, scope="owner")
    assert not entry.exists()
    for operation in (spool.read, spool.delete):
        with pytest.raises(ValueError):
            operation(artifact_id, scope="owner")


@pytest.mark.parametrize("age", [10.0, 10.001, 1000.0])
def test_ttl_expires_at_or_after_boundary_without_refresh_on_read(spool_env, age):
    spool, _, clock = spool_env
    payload = b"clock controlled audio"
    artifact_id = spool.stage(payload, scope="owner")["artifact_id"]
    clock.now = 109.999
    assert spool.read(artifact_id, scope="owner") == payload
    clock.now = 100.0 + age
    with pytest.raises(ValueError):
        spool.read(artifact_id, scope="owner")


def test_ttl_is_measured_from_each_stage(spool_env):
    spool, _, clock = spool_env
    first = spool.stage(b"first", scope="owner")["artifact_id"]
    clock.now += 5
    second = spool.stage(b"second", scope="owner")["artifact_id"]
    clock.now += 5
    with pytest.raises(ValueError):
        spool.read(first, scope="owner")
    assert spool.read(second, scope="owner") == b"second"


def test_size_boundary_and_oversize_rejection(spool_env):
    spool, root, _ = spool_env
    payload = bytes(range(256))
    receipt = spool.stage(payload, scope="owner")
    assert receipt["size"] == len(payload)
    assert receipt["sha256"] == hashlib.sha256(payload).hexdigest()
    before = set(root.rglob("*"))
    with pytest.raises(ValueError):
        spool.stage(payload + b"!", scope="owner")
    assert set(root.rglob("*")) == before
    assert spool.read(receipt["artifact_id"], scope="owner") == payload


@pytest.mark.parametrize("payload", [None, "audio", 1, bytearray(b"audio"), memoryview(b"audio"), []])
def test_only_bytes_can_be_staged(spool_env, payload):
    spool, _, _ = spool_env
    with pytest.raises(ValueError):
        spool.stage(payload, scope="owner")


@pytest.mark.parametrize("kwargs", [
    {"max_bytes": 0}, {"max_bytes": -1}, {"max_bytes": "64"},
    {"ttl_seconds": 0}, {"ttl_seconds": -1}, {"ttl_seconds": "10"},
])
def test_invalid_constructor_limits_raise_value_error(tmp_path, kwargs):
    options = {"max_bytes": 64, "ttl_seconds": 10}
    options.update(kwargs)
    with pytest.raises(ValueError):
        ArtifactSpool(tmp_path, **options)


@pytest.mark.parametrize("artifact_id", [
    None, "", 0, b"id", [], {}, "not-an-artifact", "0" * 32,
    ".", "..", "../outside", "nested/../../outside", "nested/id",
    "..\\outside", "\x00", "/etc/passwd",
])
def test_missing_malformed_and_path_ids_are_rejected(spool_env, artifact_id):
    spool, _, _ = spool_env
    valid = spool.stage(b"untouched", scope="owner")["artifact_id"]
    for operation in (spool.read, spool.delete):
        with pytest.raises(ValueError):
            operation(artifact_id, scope="owner")
    assert spool.read(valid, scope="owner") == b"untouched"


def test_absolute_and_traversal_ids_do_not_touch_attack_target(spool_env, tmp_path):
    spool, _, _ = spool_env
    target = tmp_path / "outside"
    target.write_bytes(b"outside sentinel")
    for artifact_id in (str(target), "../outside", "../../outside"):
        for operation in (spool.read, spool.delete):
            with pytest.raises(ValueError):
                operation(artifact_id, scope="owner")
            assert target.read_bytes() == b"outside sentinel"


@pytest.mark.parametrize("scope_kind", ["absolute", "traversal", "nested", "backslash"])
def test_malicious_scope_is_not_used_as_a_path(spool_env, tmp_path, scope_kind):
    spool, root, _ = spool_env
    target = tmp_path / "outside"
    target.write_bytes(b"scope attack sentinel")
    scopes = {
        "absolute": str(target), "traversal": "../outside",
        "nested": "scope-marker/child", "backslash": "..\\outside",
    }
    scope = scopes[scope_kind]
    payload = b"scope is an authorization label"
    receipt = spool.stage(payload, scope=scope)
    assert spool.read(receipt["artifact_id"], scope=scope) == payload
    entry = backing_file(root, payload)
    assert root in entry.parents
    assert not (root / "scope-marker").exists()
    assert not any("outside" in path.name or "scope-marker" in path.name for path in root.rglob("*"))
    assert target.read_bytes() == b"scope attack sentinel"
    spool.delete(receipt["artifact_id"], scope=scope)
    assert target.read_bytes() == b"scope attack sentinel"
    assert set(tmp_path.iterdir()) == {root, target}


@pytest.mark.parametrize("tamper", ["modify", "truncate"])
def test_modified_or_truncated_content_is_rejected(spool_env, tamper):
    spool, root, _ = spool_env
    payload = bytes(range(80))
    artifact_id = spool.stage(payload, scope="owner")["artifact_id"]
    entry = backing_file(root, payload)
    replacement = bytes([payload[0] ^ 255]) + payload[1:] if tamper == "modify" else payload[:-1]
    entry.write_bytes(replacement)
    with pytest.raises(ValueError):
        spool.read(artifact_id, scope="owner")


def test_symlink_root_is_rejected_without_changing_target(tmp_path):
    target = tmp_path / "real-root"
    target.mkdir()
    sentinel = target / "sentinel"
    sentinel.write_bytes(b"root attack sentinel")
    link = tmp_path / "linked-root"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError):
        ArtifactSpool(link, max_bytes=256, ttl_seconds=10)
    assert link.is_symlink()
    assert set(target.iterdir()) == {sentinel}
    assert sentinel.read_bytes() == b"root attack sentinel"


@pytest.mark.parametrize("operation", ["read", "delete"])
def test_substituted_symlink_artifact_is_rejected(spool_env, tmp_path, operation):
    spool, root, _ = spool_env
    payload = b"same digest must not authorize a symlink"
    artifact_id = spool.stage(payload, scope="owner")["artifact_id"]
    entry = backing_file(root, payload)
    target = tmp_path / "outside"
    target.write_bytes(payload)
    entry.unlink()
    entry.symlink_to(target)
    with pytest.raises(ValueError):
        getattr(spool, operation)(artifact_id, scope="owner")
    assert target.read_bytes() == payload


def test_private_spool_directory_and_file_permissions(spool_env):
    spool, root, _ = spool_env
    payload = b"permission test audio"
    spool.stage(payload, scope="owner")
    entry = backing_file(root, payload)
    assert entry.parent != root  # The existing root contains a private spool.
    assert stat.S_IMODE(entry.stat().st_mode) == 0o600
    directories = [path for path in root.rglob("*") if path.is_dir()]
    assert directories
    for directory in directories:
        assert not directory.is_symlink()
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700


# Independent corrections against explicit CHG-001..004 failure cases.
# These tests derive expected outcomes from the approved private-root policy,
# not from the implementation's helper predicates.


@pytest.mark.parametrize("level", ["root", "ancestor"])
def test_chg001_refuses_nonsticky_writable_root_or_parent(tmp_path, level):
    parent = tmp_path / "untrusted"
    parent.mkdir(mode=0o700)
    parent.chmod(0o700)
    private_root = parent / "spool"
    private_root.mkdir(mode=0o700)
    private_root.chmod(0o700)
    if level == "root":
        private_root.chmod(0o777)
    else:
        parent.chmod(0o777)
    try:
        with pytest.raises(ValueError):
            ArtifactSpool(private_root, max_bytes=32, ttl_seconds=5)
        assert list(private_root.iterdir()) == []
    finally:
        private_root.chmod(0o700)
        parent.chmod(0o700)


def test_chg001_refuses_nonprivate_root_even_without_other_write(tmp_path):
    root = tmp_path / "too-visible"
    root.mkdir(mode=0o700)
    root.chmod(0o755)  # Ignore process umask; test the actual 0755 mode.
    with pytest.raises(ValueError):
        ArtifactSpool(root, max_bytes=32, ttl_seconds=5)


def test_chg001_allows_private_root_under_platform_temp_ancestor(tmp_path):
    root = tmp_path / "valid-private-root"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    spool = ArtifactSpool(root, max_bytes=32, ttl_seconds=5)
    receipt = spool.stage(b"good", scope="job/a/attempt/1")
    assert spool.read(receipt["artifact_id"], scope="job/a/attempt/1") == b"good"


@pytest.mark.parametrize("failure", ["open", "fchmod"])
def test_chg002_failed_initialization_removes_only_new_directory(tmp_path, failure):
    root = tmp_path / "private-spool-root"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    sentinel = root / "operator-owned-sentinel"
    sentinel.write_bytes(b"unrelated")
    real_open = os.open

    def failed_open(path, flags, *args, **kwargs):
        if isinstance(path, str) and path.startswith("artifact-spool-") and flags & os.O_DIRECTORY:
            raise OSError("injected failure opening freshly created directory")
        return real_open(path, flags, *args, **kwargs)

    if failure == "open":
        mock = patch("astrumweaver.audio.artifact_spool.os.open", side_effect=failed_open)
    else:
        mock = patch("astrumweaver.audio.artifact_spool.os.fchmod", side_effect=OSError("fchmod failed"))

    with mock:
        with pytest.raises((OSError, ValueError)):
            ArtifactSpool(root, max_bytes=32, ttl_seconds=5)
    assert sorted(root.iterdir()) == [sentinel]
    assert sentinel.read_bytes() == b"unrelated"


def test_chg003_write_failure_is_storage_error_and_cleans_interrupted_file(tmp_path):
    root = tmp_path / "private-spool-root"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    spool = ArtifactSpool(root, max_bytes=16, ttl_seconds=5)
    with patch("astrumweaver.audio.artifact_spool.os.write",
               side_effect=OSError("injected disk-write failure")):
        with pytest.raises(OSError):
            spool.stage(b"private bytes", scope="job/a/attempt/1")
    assert not any(p.is_file() for p in root.rglob("*"))
    receipt = spool.stage(b"new", scope="job/a/attempt/1")
    assert spool.read(receipt["artifact_id"], scope="job/a/attempt/1") == b"new"


def test_chg004_authenticated_cleanup_reclaims_truncated_same_inode(tmp_path):
    root = tmp_path / "private-spool-root"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    spool = ArtifactSpool(root, max_bytes=64, ttl_seconds=5)
    payload = b"intact private binary payload"
    receipt = spool.stage(payload, scope="job/a/attempt/1")
    stored = backing_file(root, payload)
    external = tmp_path / "external-file"
    external.write_bytes(b"do-not-touch")
    stored.write_bytes(b"broken")
    with pytest.raises(ValueError):
        spool.read(receipt["artifact_id"], scope="job/a/attempt/1")
    spool.delete(receipt["artifact_id"], scope="job/a/attempt/1")
    assert not stored.exists()
    assert external.read_bytes() == b"do-not-touch"


def test_chg004_wrong_scope_never_recovers_corrupt_data(tmp_path):
    root = tmp_path / "private-spool-root"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    spool = ArtifactSpool(root, max_bytes=64, ttl_seconds=5)
    payload = b"private"
    receipt = spool.stage(payload, scope="owner")
    stored = backing_file(root, payload)
    stored.write_bytes(b"broken")
    with pytest.raises(ValueError):
        spool.delete(receipt["artifact_id"], scope="attacker")
    assert stored.exists()
    spool.delete(receipt["artifact_id"], scope="owner")
    assert not stored.exists()


def test_chg001_sticky_exception_only_system_temp_root():
    # Directly exercise the explicit policy lookup to rule out accidentally
    # allowing arbitrary root-owned sticky directories elsewhere in the tree.
    trusted = os.stat("/tmp")
    if trusted.st_uid != 0 or not trusted.st_mode & stat.S_ISVTX:
        pytest.skip("system /tmp is not root-owned sticky on this platform")
    ArtifactSpool._check_trusted_ancestor(trusted, "/tmp")
    with pytest.raises(ValueError):
        ArtifactSpool._check_trusted_ancestor(trusted, "/unrelated-sticky")


def test_chg003_constructor_enospc_is_storage_error(tmp_path):
    root = tmp_path / "private-spool-root"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    real_mkdir = os.mkdir

    def disk_full(path, mode=0o777, *, dir_fd=None):
        if isinstance(path, str) and path.startswith("artifact-spool-"):
            raise OSError(errno.ENOSPC, "simulated disk full")
        return real_mkdir(path, mode, dir_fd=dir_fd)

    with patch("astrumweaver.audio.artifact_spool.os.mkdir", side_effect=disk_full):
        with pytest.raises(OSError) as failure:
            ArtifactSpool(root, max_bytes=64, ttl_seconds=5)
    assert failure.value.errno == errno.ENOSPC
    assert list(root.iterdir()) == []


def test_chg003_zero_progress_write_is_storage_error(tmp_path):
    root = tmp_path / "private-spool-root"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    spool = ArtifactSpool(root, max_bytes=64, ttl_seconds=5)
    with patch("astrumweaver.audio.artifact_spool.os.write", return_value=0):
        with pytest.raises(OSError):
            spool.stage(b"not-written", scope="owner")
    assert not any(p.is_file() for p in root.rglob("*"))


def test_chg003_partial_write_then_eio_leaves_no_artifact(tmp_path):
    root = tmp_path / "private-spool-root"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    spool = ArtifactSpool(root, max_bytes=64, ttl_seconds=5)
    real_write = os.write
    calls = []

    def partial_then_error(fd, data):
        calls.append(1)
        if len(calls) == 1:
            return real_write(fd, memoryview(data)[:3])
        raise OSError(errno.EIO, "injected interrupted file write")

    with patch("astrumweaver.audio.artifact_spool.os.write", side_effect=partial_then_error):
        with pytest.raises(OSError) as failure:
            spool.stage(b"private-audio-content", scope="owner")
    assert failure.value.errno == errno.EIO
    assert len(calls) == 2
    assert not any(p.is_file() for p in root.rglob("*"))


def test_chg010_bytes_subclass_cannot_disguise_real_buffer_size(tmp_path):
    class ForgedLengthBytes(bytes):
        def __len__(self):
            return 1

    private_root = tmp_path / "private-spool-root"
    private_root.mkdir(mode=0o700)
    private_root.chmod(0o700)
    spool = ArtifactSpool(private_root, max_bytes=4, ttl_seconds=5)
    oversized = ForgedLengthBytes(b"actual-bytes-exceed-limit")
    assert len(oversized) == 1 and memoryview(oversized).nbytes > 4
    with pytest.raises(ValueError):
        spool.stage(oversized, scope="owner")
    assert not any(f.is_file() for f in private_root.rglob("*"))
