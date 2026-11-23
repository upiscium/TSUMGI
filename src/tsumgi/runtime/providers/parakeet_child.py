"""Isolated, fixed-argv Parakeet child with bounded framed private pipes."""
from __future__ import annotations
import importlib.util
import json
import os
from pathlib import Path
import re
import struct
import sys

_MAX_FRAME = (2 << 20) + 4096
_MAX_WAV = 1 << 20
_MAX_TEXT = 16 << 10
_REVISION = "44edb27eea9317daf89333e75eb830db4b1cc298"
_SHA256 = "a2ed2aab9c82cce4e9b699dca669184d7525c7b5e1eb8c3f7b789421bc3637e8"
_GPU_UUID = re.compile(r"GPU-[A-Fa-f0-9]{8}(?:-[A-Fa-f0-9]{4}){3}-[A-Fa-f0-9]{12}\Z")
_REQUEST_ID = re.compile(r"[0-9a-f]{32}\Z")

def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result

def _reject_constant(_value):
    raise ValueError("invalid JSON constant")

def _read_exact(stream, size):
    data = bytearray()
    while len(data) < size:
        part = stream.read(size-len(data))
        if not part:
            raise ValueError("truncated private frame")
        data.extend(part)
    return bytes(data)

def _recv(stream):
    size, = struct.unpack(">I", _read_exact(stream, 4))
    if not 1 <= size <= _MAX_FRAME:
        raise ValueError("invalid frame")
    return json.loads(_read_exact(stream, size), object_pairs_hook=_object,
                      parse_constant=_reject_constant)

def _send(stream, value):
    raw = json.dumps(value, separators=(",", ":"), ensure_ascii=True,
                     allow_nan=False).encode("ascii")
    if not 1 <= len(raw) <= _MAX_FRAME:
        raise ValueError("oversized frame")
    stream.write(struct.pack(">I", len(raw)) + raw)
    stream.flush()

def _load_native(path, gpu_uuid):
    native_path = Path(__file__).resolve().with_name("parakeet_native.py")
    spec = importlib.util.spec_from_file_location("_parakeet_native_child", native_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("native loader unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ParakeetNative.load(path, gpu_uuid=gpu_uuid), module

def main(argv=None):
    if (sys.argv[1:] if argv is None else argv):
        return 2
    source, sink = os.fdopen(os.dup(0), "rb", buffering=0), os.fdopen(os.dup(1), "wb", buffering=0)
    try:
        with open(os.devnull, "r+b", buffering=0) as null:
            for fd in (0, 1, 2):
                os.dup2(null.fileno(), fd)
        init = _recv(source)
        if (type(init) is not dict or set(init) != {"kind", "epoch", "request_id",
                "model_path", "model_revision", "model_sha256", "gpu_uuid"}
                or init["kind"] != "bootstrap" or type(init["epoch"]) is not str
                or not init["epoch"] or len(init["epoch"]) > 128
                or type(init["request_id"]) is not str or not _REQUEST_ID.fullmatch(init["request_id"])
                or type(init["model_path"]) is not str or not Path(init["model_path"]).is_absolute()
                or init["model_revision"] != _REVISION or init["model_sha256"] != _SHA256
                or type(init["gpu_uuid"]) is not str or not _GPU_UUID.fullmatch(init["gpu_uuid"])):
            raise ValueError("invalid bootstrap")
        native, module = _load_native(init["model_path"], init["gpu_uuid"])
        _send(sink, {"kind":"ready", "epoch":init["epoch"], "request_id":init["request_id"], "pid":os.getpid(),
                     "model_revision":module.PARAKEET_REVISION, "model_sha256":module.PARAKEET_SHA256,
                     "gpu_uuid":init["gpu_uuid"], "python":sys.version.split()[0],
                     "nemo":"2.4.0", "torch":"2.7.1+cu126", "torchaudio":"2.7.1+cu126"})
        while True:
            try:
                frame = _recv(source)
            except EOFError:
                return 0
            if (type(frame) is not dict or set(frame) != {"kind","epoch","request_id","wav"}):
                raise ValueError("invalid request")
            if frame["kind"] != "infer" or frame["epoch"] != init["epoch"]:
                raise ValueError("identity mismatch")
            rid, wav = frame["request_id"], frame["wav"]
            if (type(rid) is not str or not _REQUEST_ID.fullmatch(rid) or
                    type(wav) is not str or len(wav) > _MAX_WAV * 2):
                raise ValueError("invalid request")
            raw = bytes.fromhex(wav)
            if not 44 <= len(raw) <= _MAX_WAV:
                raise ValueError("invalid WAV size")
            text = native.transcribe(raw).encode("utf-8")
            if not 1 <= len(text) <= _MAX_TEXT:
                raise ValueError("invalid text")
            _send(sink, {"kind":"result","epoch":init["epoch"],"request_id":rid,"text":text.decode("utf-8")})
    except BaseException:
        return 1
    finally:
        source.close(); sink.close()

if __name__ == "__main__":
    raise SystemExit(main())
