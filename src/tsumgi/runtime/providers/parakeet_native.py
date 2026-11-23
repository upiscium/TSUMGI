"""Pinned local Parakeet decoder. Imported only inside its isolated child."""
from __future__ import annotations

import hashlib
import os
import re
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

PARAKEET_REVISION = "44edb27eea9317daf89333e75eb830db4b1cc298"
PARAKEET_SHA256 = "a2ed2aab9c82cce4e9b699dca669184d7525c7b5e1eb8c3f7b789421bc3637e8"
_GPU_UUID = re.compile(r"GPU-[A-Fa-f0-9]{8}(?:-[A-Fa-f0-9]{4}){3}-[A-Fa-f0-9]{12}\Z")
_VERSIONS = {"nemo_toolkit": "2.4.0", "torch": "2.7.1+cu126", "torchaudio": "2.7.1+cu126"}
_MAX_MODEL_BYTES = 4 * 1024**3


def _copy_verified_model(source: Path, destination: Path) -> str:
    digest = hashlib.sha256()
    fd = os.open(source, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as src, destination.open("xb") as dst:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or not 0 < st.st_size <= _MAX_MODEL_BYTES:
            raise ValueError("invalid local model")
        remaining = st.st_size
        while remaining:
            block = src.read(min(1024 * 1024, remaining))
            if not block:
                raise ValueError("local model changed")
            remaining -= len(block)
            digest.update(block)
            dst.write(block)
        after = os.fstat(fd)
        if (src.read(1) or after.st_size != st.st_size
                or after.st_mtime_ns != st.st_mtime_ns or after.st_ctime_ns != st.st_ctime_ns):
            raise ValueError("local model changed")
    return digest.hexdigest()


class ParakeetNative:
    def __init__(self, model: Any, torch: Any):
        self._model, self._torch = model, torch

    @classmethod
    def load(cls, local_path: str, *, gpu_uuid: str) -> "ParakeetNative":
        if type(gpu_uuid) is not str or not _GPU_UUID.fullmatch(gpu_uuid):
            raise ValueError("invalid GPU identity")
        path = Path(local_path)
        if not path.is_absolute() or path.suffix != ".nemo":
            raise ValueError("invalid model path")
        if sys.implementation.name != "cpython" or sys.version_info[:3] != (3, 11, 14):
            raise RuntimeError("wrong native runtime")
        if (os.environ.get("CUDA_VISIBLE_DEVICES") != gpu_uuid or
                os.environ.get("HF_HUB_OFFLINE") != "1" or
                os.environ.get("TRANSFORMERS_OFFLINE") != "1"):
            raise RuntimeError("native environment mismatch")
        from importlib.metadata import version
        if any(version(k) not in (v, v.split("+")[0]) for k, v in _VERSIONS.items()):
            raise RuntimeError("wrong package version")
        import torch
        import torchaudio
        if (torch.__version__ != _VERSIONS["torch"] or
                torchaudio.__version__ != _VERSIONS["torchaudio"] or
                torch.version.cuda != "12.6" or not torch.cuda.is_available() or
                torch.cuda.device_count() != 1):
            raise RuntimeError("wrong CUDA runtime")
        from nemo.collections.asr.models import ASRModel
        from omegaconf import OmegaConf
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.backends.cudnn.allow_tf32 = False
        with tempfile.TemporaryDirectory(prefix="parakeet-native-") as td:
            model_path = Path(td) / "model.nemo"
            if _copy_verified_model(path, model_path) != PARAKEET_SHA256:
                raise ValueError("model hash mismatch")
            model = ASRModel.restore_from(str(model_path), map_location="cuda:0").float().eval()
            model.change_decoding_strategy(decoder_type="rnnt", verbose=False)
            cfg = OmegaConf.to_container(model.cfg.decoding, resolve=True)
            if cfg.get("strategy") != "greedy_batch" or cfg.get("model_type") != "tdt":
                raise RuntimeError("unsupported decoding strategy")
        return cls(model, torch)

    def transcribe(self, wav: bytes) -> str:
        if type(wav) is not bytes or not wav or len(wav) > 1 << 20:
            raise ValueError("invalid WAV")
        name = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                name = f.name
                f.write(wav)
            with self._torch.inference_mode():
                result = self._model.transcribe([name], batch_size=1, num_workers=0,
                                                return_hypotheses=True, verbose=False)
                self._torch.cuda.synchronize()
            if len(result) != 1 or type(result[0].text) is not str:
                raise RuntimeError("invalid hypothesis")
            return result[0].text
        finally:
            if name is not None:
                Path(name).unlink(missing_ok=True)
