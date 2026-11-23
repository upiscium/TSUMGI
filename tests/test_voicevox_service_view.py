"""Synthetic bytes only; no release archive, extractor, loader or engine run."""
import hashlib
import os
from pathlib import Path
import runpy
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest

from tsumgi.runtime.providers import voicevox_service_view as view


def _pin(data):
    return len(data), hashlib.sha256(data).hexdigest()


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    archive = tmp_path / view._ARCHIVE[0]
    libc = tmp_path / "libc"
    engine = tmp_path / "engine"
    files = {}

    def put(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(0o600)
        files[path] = (data, path.stat().st_mode, path.stat().st_mtime_ns)
        return _pin(data)

    archive_pin = put(archive, b"synthetic archive, not a deb")
    monkeypatch.setattr(view, "_ARCHIVE", (archive.name, *archive_pin))
    libc_pins = {}
    for name in view._LIBC:
        data = ("synthetic:" + name).encode()
        libc_pins[name] = put(libc / view._LIBDIR / name, data)
    monkeypatch.setattr(view, "_LIBC", libc_pins)
    monkeypatch.setattr(view, "_COPYRIGHT",
                        put(libc / view._COPYRIGHT_PATH, b"synthetic copyright"))
    monkeypatch.setattr(view, "_RUN", put(engine / "run", b"synthetic run"))
    monkeypatch.setattr(view, "_ZLIB",
                        put(engine / "engine_internal/libz.so.1", b"synthetic zlib"))
    monkeypatch.setattr(view, "_MVEC",
                        put(libc / view._LIBDIR / "libmvec.so.1", b"synthetic mvec"))
    put(engine / "engine_internal/libmvec.so.1", b"synthetic mvec")
    # Unselected content is neither copied nor revalidated as a complete tree.
    put(engine / "model/0.vvm", b"synthetic unrelated model")
    put(engine / "LICENSE", b"synthetic fixture, not a rights grant")
    return archive, libc, engine, tmp_path / "candidate", files


def _build(inputs):
    return view.build_view(*inputs[:4])


def _rewrite(path, data):
    path.chmod(0o600)
    path.write_bytes(data)
    path.chmod(0o444)


def test_native_pins_and_copy_boundary():
    from tsumgi.runtime.providers.voicevox_manifest import EXECUTABLE_SHA256
    assert "sha256:" + view._RUN[1] == EXECUTABLE_SHA256
    assert set(view._LIBC) == {
        "ld-linux-x86-64.so.2", "libc.so.6", "libdl.so.2", "libm.so.6",
        "libpthread.so.0", "librt.so.1", "libutil.so.1",
    }
    assert view._ARCHIVE[1:] == (
        3234380, "79e35256227e16a607c154cdeb8d76ff12d20e31de286ea7fd9ad3b96fe0452d",
    )
    assert view._ZLIB == (
        108936, "64c206f0146cc58bbddc4f22054436f4ff278f5a554aa3ce6921ddf7e9133370",
    )
    assert view._COPYRIGHT == (
        26462, "d3c95b56fa33e28b57860580f0baf4e4f4de2a268a2b80f1d031a5191bade265",
    )


def test_build_and_verify_offline_view(inputs, monkeypatch):
    archive, libc, engine, output, original = inputs
    forbidden = lambda *a, **k: pytest.fail("native execution or extraction")
    monkeypatch.setattr(os, "system", forbidden)
    monkeypatch.setattr(os, "execv", forbidden)
    monkeypatch.setattr(os, "execve", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    built = _build(inputs)
    assert view.verify_view(output) == built
    for field in ("native_execution_qualified", "elf_loader_resolution_proven",
                  "activation_supported", "serving_binding_supported",
                  "full_engine_tree_verified", "signed_archive_chain_verified"):
        assert built[field] is False
    assert built["firstexec_loaded_mapping"] == "UNKNOWN"
    assert not list((output / "rootfs/run").iterdir())
    assert os.readlink(output / view._LINK_PATH) == view._LINK
    assert (output / view._LINK_PATH).resolve() == output / "rootfs" / view._LIBDIR / view._LOADER
    assert (output / "rootfs" / view._LIBDIR / "libz.so.1").read_bytes() == b"synthetic zlib"
    assert not (output / "rootfs" / view._LIBDIR / "libmvec.so.1").exists()
    assert not (output / "rootfs/model").exists()
    assert not (output / "rootfs/engine_internal").exists()
    for path, snapshot in original.items():
        assert (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns) == snapshot
    for path in (output, *(output / relative for relative in view._DIRS)):
        assert stat.S_IMODE(path.stat().st_mode) == 0o555
    assert stat.S_IMODE((output / view._RECEIPT).stat().st_mode) == 0o444
    assert str(output.parent) not in (output / view._RECEIPT).read_text()
    assert "rights_review_sha256" not in built


@pytest.mark.parametrize("which", [
    "archive", "libc", "copyright", "package_mvec", "engine_mvec", "run", "zlib",
])
def test_each_input_pin_is_checked_before_output_exists(inputs, which):
    archive, libc, engine, output, _ = inputs
    path = {
        "archive": archive, "libc": libc / view._LIBDIR / "libc.so.6",
        "copyright": libc / view._COPYRIGHT_PATH,
        "package_mvec": libc / view._LIBDIR / "libmvec.so.1",
        "engine_mvec": engine / "engine_internal/libmvec.so.1",
        "run": engine / "run", "zlib": engine / "engine_internal/libz.so.1",
    }[which]
    # Same-size alteration exercises the hash as well as the size gate.
    path.write_bytes(b"x" * path.stat().st_size)
    with pytest.raises(view.VoicevoxServiceViewError):
        _build(inputs)
    assert not output.exists()


def test_existing_output_is_not_overwritten(inputs):
    output = inputs[3]
    output.mkdir()
    marker = output / "keep"
    marker.write_text("prior")
    with pytest.raises(view.VoicevoxServiceViewError):
        _build(inputs)
    assert marker.read_text() == "prior"


@pytest.mark.parametrize("where", ["libc", "engine", "archive", "ancestor"])
def test_output_overlap_rejected(inputs, where):
    archive, libc, engine, output, _ = inputs
    destination = {"libc": libc / "candidate", "engine": engine / "candidate",
                   "archive": archive, "ancestor": libc.parent}[where]
    with pytest.raises(view.VoicevoxServiceViewError):
        view.build_view(archive, libc, engine, destination)
    assert not output.exists()


def test_input_roots_must_not_overlap(inputs):
    archive, libc, engine, output, _ = inputs
    nested = libc / "engine"
    engine.rename(nested)
    with pytest.raises(view.VoicevoxServiceViewError):
        view.build_view(archive, libc, nested, output)
    assert not output.exists()


@pytest.mark.parametrize("kind", [
    "leaf_symlink", "ancestor_symlink", "hardlink", "fifo", "writable_file",
    "writable_directory", "sticky_directory", "special_bits", "output_symlink",
    "output_parent_symlink", "output_traversal", "input_traversal", "relative",
])
def test_unsafe_paths_fail_closed(inputs, kind):
    archive, libc, engine, output, _ = inputs
    source = engine / "engine_internal/libz.so.1"
    if kind == "leaf_symlink":
        saved = source.with_name("saved")
        source.rename(saved)
        source.symlink_to(saved)
    elif kind == "ancestor_symlink":
        directory = engine / "engine_internal"
        saved = engine / "saved"
        directory.rename(saved)
        directory.symlink_to(saved)
    elif kind == "hardlink":
        os.link(source, engine / "alias")
    elif kind == "fifo":
        source.rename(source.with_name("saved"))
        os.mkfifo(source)
    elif kind == "writable_file":
        source.chmod(0o666)
    elif kind in ("writable_directory", "sticky_directory"):
        engine.chmod(0o777 if kind == "writable_directory" else 0o1777)
    elif kind == "special_bits":
        source.chmod(0o4600)
    elif kind == "output_symlink":
        output.symlink_to(libc.parent / "not-created")
    elif kind == "output_parent_symlink":
        alias = output.with_name("alias")
        alias.symlink_to(libc.parent)
        output = alias / "candidate"
    elif kind == "output_traversal":
        output = libc / ".." / "candidate"
    elif kind == "input_traversal":
        libc = engine / ".." / "libc"
    elif kind == "relative":
        output = Path("relative-candidate")
    with pytest.raises(view.VoicevoxServiceViewError):
        view.build_view(archive, libc, engine, output)
    assert not (inputs[3] / view._RECEIPT).exists()


def test_selected_files_must_remain_on_input_root_filesystem(inputs, monkeypatch):
    read = view._read
    target = inputs[1] / view._LIBDIR / "libc.so.6"

    def different_device(path, *args, **kwargs):
        content, info = read(path, *args, **kwargs)
        if path == target:
            fields = {name: getattr(info, name) for name in (
                "st_dev", "st_ino", "st_uid", "st_gid", "st_mode", "st_nlink",
                "st_size", "st_mtime_ns", "st_ctime_ns",
            )}
            fields["st_dev"] += 1
            info = SimpleNamespace(**fields)
        return content, info

    monkeypatch.setattr(view, "_read", different_device)
    with pytest.raises(view.VoicevoxServiceViewError):
        _build(inputs)
    assert not inputs[3].exists()


def test_store_hardlink_exception_is_narrow():
    root_readonly = SimpleNamespace(st_uid=0, st_mode=0o444)
    assert view._immutable_store_file(Path("/nix/store/example/file"), root_readonly)
    assert not view._immutable_store_file(Path("/tmp/file"), root_readonly)
    for info in (SimpleNamespace(st_uid=1000, st_mode=0o444),
                 SimpleNamespace(st_uid=0, st_mode=0o644)):
        assert not view._immutable_store_file(Path("/nix/store/example/file"), info)


@pytest.mark.parametrize(("mode", "accepted"), [
    (0o1775, True), (0o0555, True), (0o0775, False), (0o1777, False),
])
def test_multiuser_store_parent_without_relaxing_other_paths(monkeypatch, mode, accepted):
    # Synthetic directory metadata: never chmod or write the real Nix store.
    store = Path("/nix/store")
    file = store / "synthetic/file"

    def metadata(path):
        bits = (stat.S_IFREG | 0o444) if path == file else (
            stat.S_IFDIR | (mode if path == store else 0o555)
        )
        return SimpleNamespace(st_mode=bits, st_uid=0)

    monkeypatch.setattr(Path, "lstat", metadata)
    if accepted:
        view._check_path(store, directory=True)  # build's output parent
        view._check_path(file)  # an input's store ancestor
    else:
        for path in (store, file):
            with pytest.raises(view.VoicevoxServiceViewError):
                view._check_path(path)


def test_sticky_store_exception_requires_exact_root_owned_directory():
    info = SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | 0o1775)
    assert view._sticky_nix_store(Path("/nix/store"), info)
    assert not view._sticky_nix_store(Path("/nix/store/untrusted"), info)
    assert not view._sticky_nix_store(Path("/tmp/sticky"), info)
    assert not view._sticky_nix_store(
        Path("/nix/store"), SimpleNamespace(st_uid=1000, st_mode=stat.S_IFDIR | 0o1775),
    )
    assert not view._sticky_nix_store(
        Path("/nix/store"), SimpleNamespace(st_uid=0, st_mode=stat.S_IFLNK | 0o1775),
    )
    assert not view._sticky_nix_store(
        Path("/nix/store"), SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | 0o3775),
    )


@pytest.mark.parametrize("limit", ["_MAX_BYTES", "_MAX_FILE_BYTES", "_MAX_RECEIPT_BYTES", "_DEADLINE_SECONDS"])
def test_build_limits_before_writing(inputs, monkeypatch, limit):
    monkeypatch.setattr(view, limit, 0)
    with pytest.raises(view.VoicevoxServiceViewError):
        _build(inputs)
    assert not inputs[3].exists()


@pytest.mark.parametrize("limit", [
    "_MAX_BYTES", "_MAX_FILE_BYTES", "_MAX_RECEIPT_BYTES",
    "_MAX_ENTRIES", "_MAX_DEPTH", "_DEADLINE_SECONDS",
])
def test_verify_limits_fail_closed(inputs, monkeypatch, limit):
    _build(inputs)
    monkeypatch.setattr(view, limit, 0)
    with pytest.raises(view.VoicevoxServiceViewError):
        view.verify_view(inputs[3])


def test_growth_during_archive_read_is_rejected(inputs, monkeypatch):
    read = os.read
    changed = False

    def grow(fd, size):
        nonlocal changed
        data = read(fd, size)
        if not changed:
            changed = True
            with inputs[0].open("ab") as stream:
                stream.write(b"extra")
        return data

    monkeypatch.setattr(view.os, "read", grow)
    with pytest.raises(view.VoicevoxServiceViewError):
        _build(inputs)
    assert changed and not inputs[3].exists()


def test_source_changed_after_hash_rejected_before_writing(inputs, monkeypatch):
    read = view._read

    def change_earlier(path, *args, **kwargs):
        result = read(path, *args, **kwargs)
        if path == inputs[2] / "run":
            inputs[0].write_bytes(b"x" * inputs[0].stat().st_size)
        return result

    monkeypatch.setattr(view, "_read", change_earlier)
    with pytest.raises(view.VoicevoxServiceViewError):
        _build(inputs)
    assert not inputs[3].exists()


def test_failed_partial_output_is_retained_without_receipt(inputs, monkeypatch):
    write = view._write_new

    def change_source(path, *args):
        write(path, *args)
        if path.name == view._LOADER:
            inputs[0].write_bytes(b"changed")

    monkeypatch.setattr(view, "_write_new", change_source)
    with pytest.raises(view.VoicevoxServiceViewError):
        _build(inputs)
    output = inputs[3]
    assert output.is_dir()
    assert not (output / view._RECEIPT).exists()
    copied = output / "rootfs" / view._LIBDIR / view._LOADER
    before = copied.read_bytes()
    with pytest.raises(view.VoicevoxServiceViewError):
        _build(inputs)
    assert copied.read_bytes() == before


@pytest.mark.parametrize("extra", [
    "rootfs/etc/ld.so.cache", "rootfs/etc/ld.so.preload",
    "rootfs/lib/x86_64-linux-gnu/libmvec.so.1",
    "rootfs/lib/x86_64-linux-gnu/glibc-hwcaps", "rootfs/tmp/escape",
])
def test_unexpected_files_directories_and_links_are_rejected(inputs, extra):
    _build(inputs)
    output = inputs[3]
    path = output / extra
    path.parent.chmod(0o700)
    if path.name == "glibc-hwcaps":
        path.mkdir(mode=0o555)
    elif path.name == "escape":
        path.symlink_to(inputs[1])
    else:
        path.write_bytes(b"unexpected")
        path.chmod(0o444)
    path.parent.chmod(0o555)
    with pytest.raises(view.VoicevoxServiceViewError):
        view.verify_view(output)


@pytest.mark.parametrize("change", [
    "content", "file_mode", "dir_mode", "root_mode", "file_symlink",
    "link_target", "missing_directory", "receipt_symlink", "hardlink",
])
def test_output_mutations_rejected(inputs, change):
    _build(inputs)
    output = inputs[3]
    library = output / "rootfs" / view._LIBDIR / "libz.so.1"
    if change == "content":
        _rewrite(library, b"x" * library.stat().st_size)
    elif change == "file_mode":
        library.chmod(0o555)
    elif change == "dir_mode":
        (output / "rootfs/etc").chmod(0o755)
    elif change == "root_mode":
        output.chmod(0o755)
    elif change in ("file_symlink", "link_target", "receipt_symlink"):
        path = {"file_symlink": library, "link_target": output / view._LINK_PATH,
                "receipt_symlink": output / view._RECEIPT}[change]
        path.parent.chmod(0o700)
        saved = output.parent / "saved"
        path.rename(saved)
        path.symlink_to(saved)
        path.parent.chmod(0o555)
    elif change == "missing_directory":
        path = output / "rootfs/etc"
        path.parent.chmod(0o700)
        # Cross-parent directory rename also needs to update its ".." entry.
        path.chmod(0o700)
        saved = output.parent / "saved-etc"
        path.rename(saved)
        saved.chmod(0o555)
        path.parent.chmod(0o555)
    elif change == "hardlink":
        os.link(library, output.parent / "alias-library")
    with pytest.raises(view.VoicevoxServiceViewError):
        view.verify_view(output)


@pytest.mark.parametrize("change", [
    "native_execution_qualified", "elf_loader_resolution_proven", "activation_supported",
    "serving_binding_supported", "firstexec_loaded_mapping", "duplicate", "oversize",
])
def test_receipt_cannot_promote_candidate_or_hide_duplicate_keys(inputs, change):
    built = _build(inputs)
    path = inputs[3] / view._RECEIPT
    if change == "duplicate":
        raw = path.read_bytes().replace(b"{", b'{"native_execution_qualified":true,', 1)
    elif change == "oversize":
        raw = b" " * (view._MAX_RECEIPT_BYTES + 1)
    else:
        built[change] = "PROVEN" if change == "firstexec_loaded_mapping" else True
        raw = view._canon(built) + b"\n"
    _rewrite(path, raw)
    with pytest.raises(view.VoicevoxServiceViewError):
        view.verify_view(inputs[3])


def test_receipt_verification_never_uses_unbounded_read_bytes(inputs, monkeypatch):
    _build(inputs)
    monkeypatch.setattr(Path, "read_bytes", lambda *_: pytest.fail("unbounded read"))
    view.verify_view(inputs[3])


def test_cli_has_no_activation_or_pin_override(monkeypatch):
    for arguments in (["activate"], ["verify", "--output", "/unused", "--sha256", "fake"]):
        monkeypatch.setattr(sys, "argv", ["voicevox_service_view.py", *arguments])
        with pytest.raises(SystemExit) as error:
            view.main()
        assert error.value.code == 2


def test_standalone_utility_and_sanitized_error(tmp_path, monkeypatch, capsys):
    private = tmp_path / "not-a-view"
    monkeypatch.setattr(sys, "argv", [view.__file__, "verify", "--output", str(private)])
    with pytest.raises(SystemExit) as error:
        runpy.run_path(view.__file__, run_name="__main__")
    assert error.value.code == 1
    captured = capsys.readouterr()
    assert captured.err.strip() == "VOICEVOX service view verification failed"
    assert str(private) not in captured.err
