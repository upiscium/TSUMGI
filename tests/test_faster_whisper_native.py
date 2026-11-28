"""Native loading policy with injected doubles; never imports ctranslate2/faster_whisper."""
import hashlib
from pathlib import Path
import socket
import sys
from types import ModuleType, SimpleNamespace

import pytest

from tsumgi.runtime.providers import faster_whisper_native as native

GPU = "GPU-01234567-89ab-cdef-0123-456789abcdef"
NAMES = tuple(native.FILE_SHA256)


@pytest.fixture
def model_dir(tmp_path, monkeypatch):
    source = tmp_path / "model"
    source.mkdir()
    for name in NAMES:
        (source / name).write_bytes(name.encode())
        monkeypatch.setitem(native.FILE_SHA256, name, hashlib.sha256(name.encode()).hexdigest())
    return source


def test_gpu_identity_is_canonical(model_dir):
    assert native._GPU_UUID.fullmatch(GPU)
    with pytest.raises(ValueError):
        native.FasterWhisperNative.load(str(model_dir), gpu_uuid="fixture")


def test_copy_hashes_exact_bytes_of_exactly_the_pinned_files(model_dir, tmp_path):
    destination = tmp_path / "private"
    destination.mkdir()
    native.copy_verified_model(model_dir, destination)
    assert {p.name: p.read_bytes() for p in destination.iterdir()} == {n: n.encode() for n in NAMES}


@pytest.mark.parametrize("case", ["wrong_hash", "extra", "missing", "symlink", "oversize", "empty", "dir"])
def test_copy_rejects_unpinned_content(model_dir, tmp_path, monkeypatch, case):
    destination = tmp_path / "private"
    destination.mkdir()
    if case == "wrong_hash":
        (model_dir / "model.bin").write_bytes(b"another model family")
    elif case == "extra":
        (model_dir / "README.md").write_text("x")
    elif case == "missing":
        (model_dir / "vocabulary.json").unlink()
    elif case == "symlink":
        (model_dir / "config.json").unlink()
        (model_dir / "config.json").symlink_to(model_dir / "tokenizer.json")
    elif case == "oversize":
        monkeypatch.setattr(native, "MAX_SMALL_FILE_BYTES", 3)
    elif case == "empty":
        (model_dir / "config.json").write_bytes(b"")
    else:
        (model_dir / "config.json").unlink()
        (model_dir / "config.json").mkdir()
    with pytest.raises((ValueError, OSError)):
        native.copy_verified_model(model_dir, destination)


def test_copy_stops_at_original_size_if_source_grows(model_dir, tmp_path, monkeypatch):
    destination = tmp_path / "private"
    destination.mkdir()
    fdopen = native.os.fdopen
    first = {"done": False}

    class Growing:
        def __init__(self, fd, mode):
            self.stream = fdopen(fd, mode)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.stream.close()
        def read(self, size):
            value = self.stream.read(size)
            if not first["done"]:
                first["done"] = True
                with (model_dir / "config.json").open("ab") as append:
                    append.write(b"unreviewed growth")
            return value

    monkeypatch.setattr(native.os, "fdopen", Growing)
    with pytest.raises(ValueError):
        native.copy_verified_model(model_dir, destination)


def _install(monkeypatch, model_dir, case, calls):
    import importlib.metadata
    versions = dict(native.VERSIONS)
    if case == "wrong_wheel":
        versions["ctranslate2"] = "4.9.0"
    if case == "wrong_cudnn":
        versions["nvidia-cudnn-cu12"] = "8.9"
    monkeypatch.setattr(native.sys, "version_info", (3, 12, 0, "final", 0) if case == "wrong_python"
                        else (3, 11, 14, "final", 0))
    monkeypatch.setattr(importlib.metadata, "version", lambda package: versions[package])
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "GPU-other" if case == "wrong_gpu_env" else GPU)
    monkeypatch.setenv("HF_HUB_OFFLINE", "0" if case == "online" else "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")

    class FakeModel:
        def __init__(self, path, **kwargs):
            calls.append((Path(path), kwargs))
            assert {p.name for p in Path(path).iterdir()} == set(NAMES)
            # Replacing the source after the verified copy must not matter.
            (model_dir / "model.bin").write_bytes(b"replacement after verified copy")
        def transcribe(self, audio, **kwargs):
            calls.append(("transcribe", kwargs))
            lang = "en" if case == "wrong_language" else "ja"
            return iter([SimpleNamespace(text="こんにちは"), SimpleNamespace(text="。")]), SimpleNamespace(language=lang)

    ct2 = ModuleType("ctranslate2")
    ct2.__version__ = "4.9.1"
    ct2.get_cuda_device_count = lambda: {"no_gpu": 0, "two_gpus": 2}.get(case, 1)
    fw = ModuleType("faster_whisper")
    fw.WhisperModel = FakeModel
    monkeypatch.setitem(sys.modules, "ctranslate2", ct2)
    monkeypatch.setitem(sys.modules, "faster_whisper", fw)
    for name in ("socket", "create_connection", "getaddrinfo", "gethostbyname"):
        monkeypatch.setattr(socket, name, getattr(socket, name))  # restore the real ones afterwards


@pytest.mark.parametrize("case", ["wrong_python", "wrong_gpu_env", "online", "wrong_wheel", "wrong_cudnn",
                                  "no_gpu", "two_gpus", "wrong_digest"])
def test_load_refuses_before_constructing_the_model(model_dir, monkeypatch, case):
    calls = []
    if case == "wrong_digest":
        (model_dir / "model.bin").write_bytes(b"other")
    _install(monkeypatch, model_dir, case, calls)
    with pytest.raises((ValueError, RuntimeError)):
        native.FasterWhisperNative.load(str(model_dir), gpu_uuid=GPU)
    assert calls == []


def test_load_uses_the_fixed_engine_arguments_on_a_verified_copy_and_then_denies_network(model_dir, monkeypatch):
    calls = []
    _install(monkeypatch, model_dir, "success", calls)
    engine = native.FasterWhisperNative.load(str(model_dir), gpu_uuid=GPU)
    path, kwargs = calls[0]
    assert kwargs == {"device": "cuda", "device_index": 0, "compute_type": "float32", "local_files_only": True}
    assert path != model_dir and not path.exists()  # private copy, removed after loading
    assert engine.transcribe(b"RIFF" + bytes(40)) == "こんにちは。"
    _, decode = calls[1]
    assert decode == {"beam_size": 5, "language": "ja", "vad_filter": False,
                      "condition_on_previous_text": False}
    with pytest.raises(OSError):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", 9))


def test_defensive_language_assertion_and_input_bounds(model_dir, monkeypatch):
    # The language is forced to "ja" in the call, so a mismatch cannot occur with the real
    # engine; this only exercises the defensive assertion against a double that misreports it.
    calls = []
    _install(monkeypatch, model_dir, "wrong_language", calls)
    engine = native.FasterWhisperNative.load(str(model_dir), gpu_uuid=GPU)
    with pytest.raises(RuntimeError, match="unexpected language"):
        engine.transcribe(b"RIFF" + bytes(40))
    for bad in (b"", "text", b"x" * ((1 << 20) + 1)):
        with pytest.raises(ValueError):
            engine.transcribe(bad)
