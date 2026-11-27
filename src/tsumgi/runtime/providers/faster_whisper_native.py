"""Pinned local Faster-Whisper decoder. Imported only inside its isolated child.

No model download, no fallback model and no other language: every request is
decoded with the exact settings that the #178 measurement used.
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

MODEL_REVISION = "4df90f75321148c3a29a9e2351b7ddf8f5b115a8"
# Public identity values: SHA-256 of the files of the pinned third-party
# CTranslate2 conversion (deepdml/faster-whisper-large-v3-turbo-ct2, MIT).
FILE_SHA256 = {
    "config.json": "b0253ea6c0d3bea6b1e19e91a02acfd3b53f4467362efcb5a3e6b16c9b3a9b7e",
    "model.bin": "e76620f83d5f5b69efd3d87e3dc180c1bd21df9fbebacfd4335e5e1efcc018da",
    "preprocessor_config.json": "7ccc62c6f2765af1f3b46c00c9b5894426835a05021c8b9c01eecb6dfb542711",
    "tokenizer.json": "297b13372ac43916285644fb9687add3cc62ee2a1adb60da3dc25cc94c1871fd",
    "vocabulary.json": "c69260f2ab26d659b7c398f9a2b2b48ed0df16c3b47d7326782fd9cba71690c1",
}
MAX_FILE_BYTES = {"model.bin": 4 * 1024**3}
MAX_SMALL_FILE_BYTES = 8 * 1024**2
VERSIONS = {"faster-whisper": "1.2.1", "ctranslate2": "4.9.1",
            "nvidia-cublas-cu12": "12.9.3.13", "nvidia-cudnn-cu12": "9.28.1.4"}
PYTHON = (3, 11, 14)
# Frozen decoding settings; these are identity, never request parameters.
DECODING = {"beam_size": 5, "language": "ja", "vad_filter": False,
            "condition_on_previous_text": False}
COMPUTE_TYPE = "float32"  # CTranslate2 refuses float16 on compute capability 6.1.
_GPU_UUID = re.compile(r"GPU-[A-Fa-f0-9]{8}(?:-[A-Fa-f0-9]{4}){3}-[A-Fa-f0-9]{12}\Z")


def deny_network() -> None:
    """Python-level defence in depth; deployment still has to deny egress."""
    import socket

    def refused(*_args, **_kwargs):
        raise OSError("network access is not permitted")

    class _Denied(socket.socket):
        def __init__(self, *_args, **_kwargs):
            refused()

    socket.socket = _Denied
    socket.create_connection = refused
    socket.getaddrinfo = refused
    socket.gethostbyname = refused


def copy_verified_model(source: Path, destination: Path) -> None:
    """Copy exactly the pinned files into a fresh private directory.

    The directory must contain exactly the pinned file names. Each file is
    opened without following links, size-bounded, hashed while copied and
    compared with the pin; the loaded model is the verified private copy, so a
    later change to the source cannot alter what is executed.
    """
    if set(os.listdir(source)) != set(FILE_SHA256):
        raise ValueError("unexpected model directory content")
    for name, expected in FILE_SHA256.items():
        limit = MAX_FILE_BYTES.get(name, MAX_SMALL_FILE_BYTES)
        digest = hashlib.sha256()
        fd = os.open(source / name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as src, (destination / name).open("xb") as dst:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or not 0 < st.st_size <= limit:
                raise ValueError("invalid local model file")
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
        if digest.hexdigest() != expected:
            raise ValueError("model hash mismatch")


class FasterWhisperNative:
    def __init__(self, model: Any):
        self._model = model

    @classmethod
    def load(cls, model_dir: str, *, gpu_uuid: str) -> "FasterWhisperNative":
        if type(gpu_uuid) is not str or not _GPU_UUID.fullmatch(gpu_uuid):
            raise ValueError("invalid GPU identity")
        path = Path(model_dir)
        if not path.is_absolute():
            raise ValueError("invalid model path")
        if sys.implementation.name != "cpython" or sys.version_info[:3] != PYTHON:
            raise RuntimeError("wrong native runtime")
        if (os.environ.get("CUDA_VISIBLE_DEVICES") != gpu_uuid or
                os.environ.get("HF_HUB_OFFLINE") != "1" or
                os.environ.get("TRANSFORMERS_OFFLINE") != "1"):
            raise RuntimeError("native environment mismatch")
        from importlib.metadata import version
        if any(version(k) != v for k, v in VERSIONS.items()):
            raise RuntimeError("wrong package version")
        import ctranslate2
        if ctranslate2.__version__ != VERSIONS["ctranslate2"] or ctranslate2.get_cuda_device_count() != 1:
            raise RuntimeError("wrong CUDA runtime")
        from faster_whisper import WhisperModel
        with tempfile.TemporaryDirectory(prefix="faster-whisper-native-") as td:
            copy = Path(td)
            copy_verified_model(path, copy)
            # The device and compute type are fixed: no CPU, no float16/int8.
            model = WhisperModel(str(copy), device="cuda", device_index=0,
                                 compute_type=COMPUTE_TYPE, local_files_only=True)
        # After imports and the load: the decode phase then runs with Python-level
        # network creation denied (defence in depth, not a sandbox).
        deny_network()
        return cls(model)

    def transcribe(self, wav: bytes) -> str:
        if type(wav) is not bytes or not wav or len(wav) > 1 << 20:
            raise ValueError("invalid WAV")
        segments, info = self._model.transcribe(io.BytesIO(wav), **DECODING)
        if getattr(info, "language", "ja") != "ja":
            raise RuntimeError("unexpected language")
        return "".join(segment.text for segment in segments)
