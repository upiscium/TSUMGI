"""Native loading policy tests are synthetic and do not import model libraries."""
import hashlib
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest
from tsumgi.runtime.providers import parakeet_native as native

def test_gpu_identity_is_canonical():
    assert native._GPU_UUID.fullmatch("GPU-12345678-1234-abcd-9876-123456789abc")
    assert not native._GPU_UUID.fullmatch("fixture")


def test_model_copy_hashes_exact_bytes(tmp_path):
    data = b"small synthetic model"
    source, destination = tmp_path / "source.nemo", tmp_path / "private.nemo"
    source.write_bytes(data)
    assert native._copy_verified_model(source, destination) == hashlib.sha256(data).hexdigest()
    assert destination.read_bytes() == data


def test_model_copy_rejects_symlink_and_oversize(tmp_path, monkeypatch):
    source = tmp_path / "source.nemo"
    source.write_bytes(b"synthetic model")
    link = tmp_path / "link.nemo"
    link.symlink_to(source)
    with pytest.raises(OSError):
        native._copy_verified_model(link, tmp_path / "symlink-copy.nemo")
    monkeypatch.setattr(native, "_MAX_MODEL_BYTES", 4)
    with pytest.raises(ValueError, match="invalid local model"):
        native._copy_verified_model(source, tmp_path / "large-copy.nemo")


def test_model_copy_stops_at_original_size_if_source_grows(tmp_path, monkeypatch):
    source = tmp_path / "source.nemo"
    original = b"synthetic model"
    source.write_bytes(original)
    fdopen = native.os.fdopen
    reads = []

    class GrowingSource:
        def __init__(self, fd, mode):
            self.stream = fdopen(fd, mode)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.stream.close()
        def read(self, size):
            reads.append(size)
            value = self.stream.read(size)
            if len(reads) == 1:
                with source.open("ab") as append:
                    append.write(b"unreviewed growth")
            return value

    monkeypatch.setattr(native.os, "fdopen", GrowingSource)
    destination = tmp_path / "copy.nemo"
    with pytest.raises(ValueError, match="local model changed"):
        native._copy_verified_model(source, destination)
    assert reads == [len(original), 1]
    assert destination.read_bytes() == original


@pytest.mark.parametrize("case", [
    "success", "wrong_digest", "wrong_decoder", "cpu_only", "wrong_runtime", "wrong_wheel",
])
def test_load_pins_before_restore_using_fake_native_modules(tmp_path, monkeypatch, case):
    """All imports below resolve to injected doubles, never torch/NeMo binaries."""
    import importlib.metadata
    data = b"small pinned model"
    monkeypatch.setattr(native, "PARAKEET_SHA256", hashlib.sha256(data).hexdigest())
    monkeypatch.setattr(native.sys, "version_info", (3, 11, 14, "final", 0))
    monkeypatch.setattr(importlib.metadata, "version", lambda package: {
        "nemo_toolkit": "2.4.0",
        "torch": "2.7.1+cu128" if case == "wrong_runtime" else "2.7.1",
        "torchaudio": "2.7.1",
    }[package])
    gpu = "GPU-01234567-89ab-cdef-0123-456789abcdef"
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", gpu)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    source = tmp_path / "model.nemo"
    source.write_bytes(b"wrong model" if case == "wrong_digest" else data)
    captured = []

    class FakeModel:
        cfg = SimpleNamespace(decoding={
            "strategy": "greedy" if case == "wrong_decoder" else "greedy_batch",
            "model_type": "tdt",
        })
        def float(self):
            return self
        def eval(self):
            return self
        def change_decoding_strategy(self, **kwargs):
            assert kwargs == {"decoder_type": "rnnt", "verbose": False}

    class FakeASR:
        @staticmethod
        def restore_from(path, *, map_location):
            assert map_location == "cuda:0"
            copied = Path(path)
            captured.append(copied)
            assert copied.read_bytes() == data
            source.write_bytes(b"replacement after verified copy")
            return FakeModel()

    torch = ModuleType("torch")
    torch.__version__ = "2.7.1+cpu" if case == "wrong_wheel" else "2.7.1+cu126"
    torch.version = SimpleNamespace(cuda="12.6")
    torch.cuda = SimpleNamespace(is_available=lambda: case != "cpu_only", device_count=lambda: 1)
    torch.backends = SimpleNamespace(
        cuda=SimpleNamespace(matmul=SimpleNamespace(allow_tf32=True)),
        cudnn=SimpleNamespace(allow_tf32=True),
    )
    modules = {name: ModuleType(name) for name in (
        "nemo", "nemo.collections", "nemo.collections.asr", "nemo.collections.asr.models",
        "omegaconf", "torchaudio",
    )}
    modules["torch"] = torch
    modules["torchaudio"].__version__ = "2.7.1+cu126"
    modules["nemo.collections.asr.models"].ASRModel = FakeASR
    modules["omegaconf"].OmegaConf = SimpleNamespace(to_container=lambda cfg, resolve: cfg)
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    if case != "success":
        with pytest.raises(ValueError if case == "wrong_digest" else RuntimeError):
            native.ParakeetNative.load(str(source), gpu_uuid=gpu)
        if case != "wrong_decoder":
            assert not captured
    else:
        assert isinstance(native.ParakeetNative.load(str(source), gpu_uuid=gpu), native.ParakeetNative)
        assert not captured[0].exists()
        assert not torch.backends.cuda.matmul.allow_tf32
        assert not torch.backends.cudnn.allow_tf32
