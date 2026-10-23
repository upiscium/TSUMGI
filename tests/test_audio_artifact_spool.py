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


# Issue #174: process-local accounting, with caller-serialized operations.
# Every case uses real private storage; patches inject individual OS faults only.
@pytest.fixture
def quota_env(tmp_path):
    def make(**limits):
        root = tmp_path / f"quota-root-{len(list(tmp_path.iterdir()))}"
        root.mkdir(mode=0o700)
        root.chmod(0o700)
        clock = Clock()
        spool = ArtifactSpool(root, max_bytes=8, ttl_seconds=10,
                              clock=clock, **limits)
        return spool, root, clock
    return make


class NonBuiltinQuotaInt(int):
    pass


@pytest.mark.parametrize("name", ["max_total_bytes", "max_artifacts"])
@pytest.mark.parametrize("value", [
    True, False, 0, -1, 1.0, "1", b"1", [], {}, object(),
    float("inf"), float("nan"), NonBuiltinQuotaInt(1),
])
def test_quota_limits_require_positive_exact_builtin_ints(tmp_path, name, value):
    root = tmp_path / "private-root"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    with pytest.raises(ValueError):
        ArtifactSpool(root, max_bytes=8, ttl_seconds=10, **{name: value})
    assert list(root.iterdir()) == []


def test_quota_options_are_keyword_only(tmp_path):
    import inspect

    parameters = inspect.signature(ArtifactSpool).parameters
    for name in ("max_total_bytes", "max_artifacts"):
        assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
        assert parameters[name].default is None
    with pytest.raises(TypeError):
        ArtifactSpool(tmp_path, 8, 10, None, None)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("limits", [{}, {"max_total_bytes": None, "max_artifacts": None}])
def test_none_quota_compatibility_keeps_per_artifact_limit(quota_env, limits):
    spool, root, _ = quota_env(**limits)
    receipts = [spool.stage(b"12345678", scope="owner") for _ in range(12)]
    assert len({r["artifact_id"] for r in receipts}) == 12
    before = set(root.rglob("*"))
    with pytest.raises(ValueError):
        spool.stage(b"123456789", scope="owner")
    assert set(root.rglob("*")) == before
    for receipt in receipts:
        assert spool.read(receipt["artifact_id"], scope="owner") == b"12345678"


@pytest.mark.parametrize("limits,payloads,rejected", [
    ({"max_total_bytes": 8}, [b"abc", b"defgh"], b"!"),
    ({"max_artifacts": 2}, [b"", b"abcdefgh"], b""),
    ({"max_total_bytes": 8, "max_artifacts": 2}, [b"abc", b"defgh"], b"!"),
])
def test_quota_exact_boundary_overflow_and_authorized_reuse(quota_env, limits, payloads, rejected):
    spool, root, _ = quota_env(**limits)
    receipts = [spool.stage(data, scope="owner") for data in payloads]
    before = set(root.rglob("*"))
    for _ in range(2):
        with pytest.raises(ValueError):
            spool.stage(rejected, scope="other")
        assert set(root.rglob("*")) == before
    with pytest.raises(ValueError):
        spool.delete(receipts[0]["artifact_id"], scope="other")
    with pytest.raises(ValueError):
        spool.stage(rejected, scope="other")
    for receipt, data in zip(receipts, payloads):
        assert spool.read(receipt["artifact_id"], scope="owner") == data
    old_file = backing_file(root, payloads[0])
    spool.delete(receipts[0]["artifact_id"], scope="owner")
    assert not old_file.exists()
    replacement = spool.stage(payloads[0], scope="other")
    assert spool.read(replacement["artifact_id"], scope="other") == payloads[0]
    with pytest.raises(ValueError):
        spool.delete(receipts[0]["artifact_id"], scope="owner")
    with pytest.raises(ValueError):
        spool.stage(rejected, scope="owner")


def test_overflow_and_invalid_stage_do_not_consume_remaining_capacity(quota_env):
    spool, root, _ = quota_env(max_total_bytes=8, max_artifacts=2)
    first = spool.stage(b"abc", scope="owner")
    before = set(root.rglob("*"))
    for data, scope in [(b"123456", "owner"), (b"123456789", "owner"),
                        (bytearray(b"x"), "owner"), (b"x", "")]:
        with pytest.raises(ValueError):
            spool.stage(data, scope=scope)
        assert set(root.rglob("*")) == before
    second = spool.stage(b"12345", scope="owner")
    assert spool.read(first["artifact_id"], scope="owner") == b"abc"
    assert spool.read(second["artifact_id"], scope="owner") == b"12345"


@pytest.mark.parametrize("fault", ["write", "partial_write", "fchmod", "enospc"])
def test_failed_create_does_not_publish_or_charge_quota(quota_env, fault):
    spool, root, _ = quota_env(max_total_bytes=8, max_artifacts=1)
    real_write = os.write
    real_open = os.open
    created_names = []
    writes = []

    def observe_open(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        if flags & os.O_CREAT:
            created_names.append(os.path.basename(path))
        return fd

    def fail_write(fd, data):
        writes.append(1)
        if fault == "partial_write" and len(writes) == 1:
            return real_write(fd, memoryview(data)[:2])
        raise OSError(errno.ENOSPC if fault == "enospc" else errno.EIO, "injected write fault")

    target = "fchmod" if fault == "fchmod" else "write"
    effect = OSError(errno.EIO, "injected chmod fault") if fault == "fchmod" else fail_write
    before = set(root.rglob("*"))
    with patch("astrumweaver.audio.artifact_spool.os.open", side_effect=observe_open):
        with patch(f"astrumweaver.audio.artifact_spool.os.{target}", side_effect=effect) as injected:
            with pytest.raises(OSError) as failure:
                spool.stage(b"12345678", scope="owner")
            assert injected.called
    assert failure.value.errno == (errno.ENOSPC if fault == "enospc" else errno.EIO)
    assert created_names
    assert set(root.rglob("*")) == before
    for name in created_names:
        for operation in (spool.read, spool.delete):
            with pytest.raises(ValueError):
                operation(name, scope="owner")
    receipt = spool.stage(b"abcdefgh", scope="owner")
    assert spool.read(receipt["artifact_id"], scope="owner") == b"abcdefgh"
    with pytest.raises(ValueError):
        spool.stage(b"x", scope="owner")


def test_failed_create_cleanup_unlink_never_authorizes_or_charges_orphan(quota_env):
    spool, root, _ = quota_env(max_total_bytes=8, max_artifacts=1)
    with patch("astrumweaver.audio.artifact_spool.os.write",
               side_effect=OSError(errno.ENOSPC, "disk full")):
        with patch("astrumweaver.audio.artifact_spool.os.unlink",
                   side_effect=OSError(errno.EACCES, "cleanup denied")) as injected:
            with pytest.raises(OSError):
                spool.stage(b"12345678", scope="owner")
            assert injected.called
    orphans = [p for p in root.rglob("*") if p.is_file()]
    assert orphans
    for orphan in orphans:
        for operation in (spool.read, spool.delete):
            with pytest.raises(ValueError):
                operation(orphan.name, scope="owner")
    receipt = spool.stage(b"abcdefgh", scope="owner")
    assert spool.read(receipt["artifact_id"], scope="owner") == b"abcdefgh"
    assert all(p.exists() for p in orphans)


def test_failed_authorized_unlink_preserves_record_and_both_quotas(quota_env):
    spool, root, _ = quota_env(max_total_bytes=8, max_artifacts=1)
    receipt = spool.stage(b"12345678", scope="owner")
    entry = backing_file(root, b"12345678")
    with patch("astrumweaver.audio.artifact_spool.os.unlink",
               side_effect=OSError(errno.EIO, "unlink failed")) as injected:
        with pytest.raises(OSError):
            spool.delete(receipt["artifact_id"], scope="owner")
        assert injected.called
    assert entry.read_bytes() == b"12345678"
    assert spool.read(receipt["artifact_id"], scope="owner") == b"12345678"
    with pytest.raises(ValueError):
        spool.stage(b"x", scope="owner")
    spool.delete(receipt["artifact_id"], scope="owner")
    assert not entry.exists()
    assert spool.stage(b"abcdefgh", scope="owner")["size"] == 8


@pytest.mark.parametrize("tamper", ["symlink", "replacement", "mode", "hardlink", "missing"])
def test_unsafe_delete_and_reap_do_not_release_capacity(quota_env, tmp_path, tamper):
    spool, root, clock = quota_env(max_total_bytes=8, max_artifacts=1)
    receipt = spool.stage(b"12345678", scope="owner")
    entry = backing_file(root, b"12345678")
    saved = tmp_path / "original-inode"
    outside = tmp_path / "outside-sentinel"
    outside.write_bytes(b"12345678")
    if tamper in ("symlink", "replacement", "missing"):
        entry.rename(saved)  # Keep the original inode alive: no inode-reuse lottery.
        if tamper == "symlink":
            entry.symlink_to(outside)
        elif tamper == "replacement":
            entry.write_bytes(b"12345678")
            entry.chmod(0o600)
    elif tamper == "mode":
        entry.chmod(0o644)
    else:
        os.link(entry, saved)
    with pytest.raises((ValueError, FileNotFoundError)):
        spool.delete(receipt["artifact_id"], scope="owner")
    clock.now = 110
    assert spool.reap_expired() == 0
    assert spool.reap_expired() == 0
    assert outside.read_bytes() == b"12345678"
    if tamper == "symlink":
        assert entry.is_symlink()
    elif tamper != "missing":
        assert entry.read_bytes() == b"12345678"
    with pytest.raises(ValueError):
        spool.stage(b"x", scope="owner")
    # Restore the same recorded inode, then verify precisely one release.
    if tamper in ("symlink", "replacement", "missing"):
        if tamper != "missing":
            entry.unlink()
        saved.rename(entry)
    elif tamper == "mode":
        entry.chmod(0o600)
    else:
        saved.unlink()
    assert spool.reap_expired() == 1
    assert not entry.exists()
    assert spool.reap_expired() == 0
    assert spool.stage(b"abcdefgh", scope="owner")["size"] == 8


def test_reap_inclusive_ttl_exact_counts_live_protection_and_reuse(quota_env):
    spool, root, clock = quota_env(max_total_bytes=8, max_artifacts=3)
    old = [spool.stage(data, scope=scope) for data, scope in
           [(b"abc", "one"), (b"de", "two")]]
    old_files = [backing_file(root, data) for data in (b"abc", b"de")]
    clock.now = 105
    live = spool.stage(b"fgh", scope="three")
    live_file = backing_file(root, b"fgh")
    unrelated = live_file.parent / "operator-sentinel"
    unrelated.write_bytes(b"not a recorded artifact")
    unrelated.chmod(0o600)
    clock.now = 109.999
    assert spool.reap_expired() == 0
    assert all(p.exists() for p in old_files)
    assert spool.read(old[0]["artifact_id"], scope="one") == b"abc"
    clock.now = 110
    count = spool.reap_expired()
    assert type(count) is int and count == 2
    assert all(not p.exists() for p in old_files)
    for receipt, scope in zip(old, ("one", "two")):
        for operation in (spool.read, spool.delete):
            with pytest.raises(ValueError):
                operation(receipt["artifact_id"], scope=scope)
    assert spool.read(live["artifact_id"], scope="three") == b"fgh"
    assert live_file.exists()
    assert spool.reap_expired() == 0
    replacement = spool.stage(b"12345", scope="four")
    with pytest.raises(ValueError):
        spool.stage(b"x", scope="four")
    clock.now = 115
    assert spool.reap_expired() == 1
    assert not live_file.exists()
    assert spool.read(replacement["artifact_id"], scope="four") == b"12345"
    clock.now = 120
    assert spool.reap_expired() == 1
    assert spool.reap_expired() == 0
    assert unrelated.read_bytes() == b"not a recorded artifact"


def test_reap_reclaims_corrupt_same_inode_using_original_accounting(quota_env):
    spool, root, clock = quota_env(max_total_bytes=8, max_artifacts=1)
    spool.stage(b"12345678", scope="owner")
    entry = backing_file(root, b"12345678")
    entry.write_bytes(b"x")
    clock.now = 110
    assert spool.reap_expired() == 1
    assert not entry.exists()
    assert spool.reap_expired() == 0
    assert spool.stage(b"abcdefgh", scope="owner")["size"] == 8


def test_reap_failed_unlink_is_not_counted_or_released(quota_env):
    spool, root, clock = quota_env(max_total_bytes=8, max_artifacts=1)
    spool.stage(b"12345678", scope="owner")
    entry = backing_file(root, b"12345678")
    clock.now = 110
    with patch("astrumweaver.audio.artifact_spool.os.unlink",
               side_effect=OSError(errno.EIO, "reap unlink failed")) as injected:
        # Storage errors may propagate or be left for a later explicit retry;
        # neither policy may report a successful removal.
        try:
            assert spool.reap_expired() == 0
        except OSError as failure:
            assert failure.errno == errno.EIO
        assert injected.called
    assert entry.read_bytes() == b"12345678"
    with pytest.raises(ValueError):
        spool.stage(b"x", scope="owner")
    assert spool.reap_expired() == 1
    assert not entry.exists()
    assert spool.reap_expired() == 0
    assert spool.stage(b"abcdefgh", scope="owner")["size"] == 8


def test_injected_directory_rename_during_failed_write_preserves_outside(quota_env, tmp_path):
    spool, root, _ = quota_env(max_total_bytes=8, max_artifacts=1)
    directory, = root.iterdir()
    moved = root / "moved-original"
    outside = tmp_path / "outside-directory"
    outside.mkdir(mode=0o700)
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"outside must survive")
    real_write = os.write

    def rename_then_fail(fd, data):
        real_write(fd, memoryview(data)[:2])
        directory.rename(moved)
        directory.symlink_to(outside, target_is_directory=True)
        raise OSError(errno.EIO, "injected rename during interrupted write")

    with patch("astrumweaver.audio.artifact_spool.os.write", side_effect=rename_then_fail) as injected:
        with pytest.raises(OSError):
            spool.stage(b"12345678", scope="owner")
        assert injected.called
    assert sentinel.read_bytes() == b"outside must survive"
    assert set(outside.iterdir()) == {sentinel}
    assert list(moved.iterdir()) == []
    directory.unlink()
    moved.rename(directory)
    receipt = spool.stage(b"abcdefgh", scope="owner")
    assert spool.read(receipt["artifact_id"], scope="owner") == b"abcdefgh"


def test_reap_refuses_foreign_owner_without_releasing_quota(quota_env):
    spool, root, clock = quota_env(max_total_bytes=8, max_artifacts=1)
    spool.stage(b"12345678", scope="owner")
    entry = backing_file(root, b"12345678")
    identity = (entry.stat().st_dev, entry.stat().st_ino)
    real_stat, real_fstat = os.stat, os.fstat
    observations = []

    def foreign_owner(info):
        if (info.st_dev, info.st_ino) == identity:
            fields = list(info)
            fields[4] = os.geteuid() + 1
            observations.append(1)
            return os.stat_result(fields)
        return info

    # Simulate ownership metadata only: changing UID would require privilege.
    # The file, directory, identity checks and quota operations remain real.
    clock.now = 110
    with patch("astrumweaver.audio.artifact_spool.os.stat",
               side_effect=lambda *a, **kw: foreign_owner(real_stat(*a, **kw))):
        with patch("astrumweaver.audio.artifact_spool.os.fstat",
                   side_effect=lambda *a, **kw: foreign_owner(real_fstat(*a, **kw))):
            assert spool.reap_expired() == 0
    assert observations
    assert entry.read_bytes() == b"12345678"
    with pytest.raises(ValueError):
        spool.stage(b"x", scope="owner")
    assert spool.reap_expired() == 1
    assert not entry.exists()
    assert spool.reap_expired() == 0
    assert spool.stage(b"abcdefgh", scope="owner")["size"] == 8
