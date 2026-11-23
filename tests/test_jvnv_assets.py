import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from astrumweaver.runtime.providers import jvnv_assets as assets


def _write(path, data):
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def _fixture(tmp_path, monkeypatch):
    root = tmp_path / "assets"
    model = root / "jvnv-F1-jp"
    bert = root / "bert"
    model.mkdir(parents=True)
    bert.mkdir()
    monkeypatch.setattr(
        assets, "_BERT_SIZES", {name: len(name.encode()) for name in assets._BERT_BLOBS}
    )
    for name in assets._MODEL_FILES:
        _write(model / name, name.encode())
    for name, (sha256, sha1) in assets._BERT_BLOBS.items():
        data = name.encode()
        if sha256:
            digest = _write(bert / name, data)
            monkeypatch.setitem(assets._BERT_BLOBS, name, (digest, None))
        else:
            (bert / name).write_bytes(data)
            oid = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
            monkeypatch.setitem(assets._BERT_BLOBS, name, (None, oid))
    lock = tmp_path / "deps.lock"
    lock_digest = _write(lock, b"lock")
    monkeypatch.setattr(assets, "LOCK_SHA256", "sha256:" + lock_digest)
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    executable = venv / "bin" / "python"
    interpreter_digest = _write(executable, b"interpreter")
    monkeypatch.setattr(assets, "INTERPRETER_SHA256", "sha256:" + interpreter_digest)
    home = tmp_path / "private"
    home.mkdir(mode=0o700)
    home.chmod(0o700)
    dictionary = tmp_path / "dictionary"
    dictionary.mkdir()
    for name in assets._DICT_CORE:
        _write(dictionary / name, name.encode())
    return assets.JvnvInputs(str(home), str(lock), str(dictionary),
                            assets.dictionary_inventory_sha256(str(dictionary))), model, executable


def test_preflight_and_watched_paths(tmp_path, monkeypatch):
    inputs, model, executable = _fixture(tmp_path, monkeypatch)
    inputs.preflight(str(model), str(executable))
    watched = inputs.watched_paths(str(model))
    assert watched == (
        str(model.parent / "bert"),
        *(str(model.parent / "bert" / name) for name in assets._BERT_BLOBS),
        str(tmp_path / "deps.lock"),
        inputs.dictionary_directory,
        *(str(Path(inputs.dictionary_directory) / n) for n in sorted(assets._DICT_CORE)),
    )


@pytest.mark.parametrize("extra", ["unexpected.bin", "__pycache__"])
def test_rejects_extra_files(tmp_path, monkeypatch, extra):
    inputs, model, executable = _fixture(tmp_path, monkeypatch)
    (model / extra).write_bytes(b"x")
    with pytest.raises(ValueError, match="JVNV staged assets are invalid"):
        inputs.preflight(str(model), str(executable))


def test_rejects_wrong_encoder_digest(tmp_path, monkeypatch):
    inputs, model, executable = _fixture(tmp_path, monkeypatch)
    (model.parent / "bert" / "config.json").write_bytes(b"x" * len("config.json"))
    with pytest.raises(ValueError):
        inputs.preflight(str(model), str(executable))


def test_rejects_wrong_lock_bytes(tmp_path, monkeypatch):
    inputs, model, executable = _fixture(tmp_path, monkeypatch)
    Path(inputs.dependency_lock_path).write_bytes(b"wrong")
    with pytest.raises(ValueError):
        inputs.preflight(str(model), str(executable))


def test_rejects_populated_home(tmp_path, monkeypatch):
    inputs, model, executable = _fixture(tmp_path, monkeypatch)
    Path(inputs.private_home, "inherited").write_text("x")
    with pytest.raises(ValueError):
        inputs.preflight(str(model), str(executable))


def test_rejects_symlink_unsafe_parent(tmp_path, monkeypatch):
    inputs, model, executable = _fixture(tmp_path, monkeypatch)
    real_parent = tmp_path / "owned"
    real_parent.mkdir()
    unsafe = tmp_path / "unsafe"
    unsafe.symlink_to(real_parent, target_is_directory=True)
    home = real_parent / "home"
    home.mkdir(mode=0o700)
    home.chmod(0o700)
    with pytest.raises(ValueError):
        replace(inputs, private_home=str(unsafe / "home")).preflight(
            str(model), str(executable)
        )


def test_rejects_home_venv_overlap(tmp_path, monkeypatch):
    inputs, model, executable = _fixture(tmp_path, monkeypatch)
    overlap = executable.parent.parent / "private"
    overlap.mkdir(mode=0o700)
    overlap.chmod(0o700)
    with pytest.raises(ValueError):
        replace(inputs, private_home=str(overlap)).preflight(
            str(model), str(executable)
        )


@pytest.mark.parametrize("mutation", ["changed", "extra", "incomplete", "writable"])
def test_no_missing_or_unreviewed_dictionary(tmp_path, monkeypatch, mutation):
    inputs, model, exe = _fixture(tmp_path, monkeypatch)
    lex = Path(inputs.dictionary_directory)
    if mutation == "changed":
        (lex / "sys.dic").write_bytes(b"changes")
    elif mutation == "extra":
        (lex / "other.dic").write_bytes(b"other")
    elif mutation == "incomplete":
        (lex / "sys.dic").rename(lex / "wrong-name.dic")
    else:
        (lex / "sys.dic").chmod(0o666)
    with pytest.raises(ValueError, match="^JVNV staged assets are invalid$"):
        inputs.preflight(str(model), str(exe))
