"""Isolated framed-IPC runner for the pinned JVNV JP-Extra F1 voice."""
from __future__ import annotations

import hashlib
from importlib.metadata import version
import json
import os
import re
import struct
import sys
import uuid
from pathlib import Path

LIMIT = 32 * 1024
TEXT_LIMIT = 256
VOICE = "jvnv-F1-jp"
STYLE = "Neutral"
RATE = 44100
PROBE_TEXT = "音声の準備を確認します。"
ALIAS = "jvnv-f1-neutral"
ENGINE_REVISION = "d8148f3090ee5038ca7b4e4b327116c64467f952"
MODEL_NAME = "jvnv-F1-jp_e160_s14000.safetensors"
BOOT_KEYS = {"version", "op", "request_id", "epoch", "pid", "worker_id",
             "deployment", "device", "gpu_uuid", "model_id", "artifact_paths",
             "model_hashes", "runtime_revision", "adapter_revision", "voices",
             "probe_format"}
JOB_KEYS = {"version", "op", "request_id", "epoch", "text", "model_id",
            "speaker_id", "style"}


def read_exact(stream, size):
    data = bytearray()
    while len(data) < size:
        block = stream.read(size - len(data))
        if not block:
            raise ValueError
        data.extend(block)
    return bytes(data)


def read_frame(stream):
    length, = struct.unpack(">I", read_exact(stream, 4))
    if not 1 <= length <= LIMIT:
        raise ValueError

    def unique_pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    value = json.loads(read_exact(stream, length).decode("utf-8"),
                       object_pairs_hook=unique_pairs,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    if type(value) is not dict:
        raise ValueError
    return value


def write_frame(stream, value):
    payload = json.dumps(value, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if not 1 <= len(payload) <= LIMIT:
        raise ValueError
    stream.write(struct.pack(">I", len(payload)) + payload)
    stream.flush()


def _uuid(value):
    if type(value) is not str or str(uuid.UUID(value)) != value or uuid.UUID(value).int == 0:
        raise ValueError


def _text(value):
    if type(value) is not str:
        raise ValueError
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError from exc
    if not value.strip() or len(encoded) > TEXT_LIMIT:
        raise ValueError


def _bootstrap(req):
    if (set(req) != BOOT_KEYS or type(req["version"]) is not int or req["version"] != 1
            or req["op"] != "bootstrap" or type(req["pid"]) is not int
            or req["pid"] != os.getpid() or req["device"] != "cpu"
            or req["gpu_uuid"] is not None or req["probe_format"] != "pcm16-wav-v1"
            or req["model_id"] != VOICE):
        raise ValueError
    _uuid(req["request_id"])
    _uuid(req["epoch"])
    if (type(req["worker_id"]) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", req["worker_id"])
            or type(req["deployment"]) is not str or not re.fullmatch(r"sha256:[0-9a-f]{64}", req["deployment"])
            or req["runtime_revision"] != ENGINE_REVISION):
        raise ValueError
    with Path(__file__).open("rb") as f:
        if req["adapter_revision"] != "sha256:" + hashlib.file_digest(f, "sha256").hexdigest():
            raise ValueError
    paths, hashes = req["artifact_paths"], req["model_hashes"]
    if (type(paths) is not dict or set(paths) != {"model", "config", "styles", "dictionary"}
            or type(hashes) is not dict or set(hashes) != {"weight", "config", "styles"}
            or any(type(p) is not str or not Path(p).is_absolute() for p in paths.values())):
        raise ValueError
    model, config_path, styles_path = (Path(paths[k]) for k in ("model", "config", "styles"))
    if (model.name != MODEL_NAME or model.parent.name != VOICE
            or config_path.name != "config.json" or styles_path.name != "style_vectors.npy"
            or config_path.parent != model.parent or styles_path.parent != model.parent):
        raise ValueError
    voices = req["voices"]
    if (type(voices) is not list or len(voices) != 1 or type(voices[0]) is not dict
            or set(voices[0]) != {"alias", "speaker_id", "style_id", "style"}
            or voices[0]["alias"] != ALIAS or type(voices[0]["speaker_id"]) is not int
            or voices[0]["speaker_id"] != 0 or voices[0]["style_id"] != "0"
            or voices[0]["style"] != STYLE):
        raise ValueError
    for key in ("weight", "config", "styles"):
        if type(hashes[key]) is not str or re.fullmatch(r"[0-9a-f]{64}", hashes[key]) is None:
            raise ValueError
    with config_path.open("rb") as f:
        config_bytes = f.read(1024 * 1024 + 1)
    if len(config_bytes) > 1024 * 1024:
        raise ValueError
    if hashlib.sha256(config_bytes).hexdigest() != hashes["config"]:
        raise ValueError
    config = json.loads(config_bytes.decode("utf-8"))
    data = config["data"]
    if (config.get("model_name") != VOICE or data.get("sampling_rate") != RATE
            or data.get("spk2id") != {VOICE: 0} or data.get("style2id", {}).get(STYLE) != 0
            or data.get("use_jp_extra") is not True):
        raise ValueError
    return model, voices, hashes


def _validate_wav(data):
    if (type(data) is not bytes or not 46 <= len(data) <= 44 + 2 * RATE * 30
            or data[:4] != b"RIFF" or int.from_bytes(data[4:8], "little") != len(data) - 8
            or data[8:12] != b"WAVE"):
        raise ValueError
    if (data[12:16] != b"fmt " or data[16:20] != b"\x10\0\0\0"
            or data[20:24] != b"\x01\0\x01\0"
            or data[24:28] != struct.pack("<I", RATE)
            or data[28:32] != struct.pack("<I", RATE * 2)
            or data[32:36] != b"\x02\0\x10\0" or data[36:40] != b"data"):
        raise ValueError
    size, = struct.unpack("<I", data[40:44])
    if size != len(data) - 44 or size % 2 or not size:
        raise ValueError
    frames = size // 2
    if not RATE // 10 < frames < RATE * 30 or not any(memoryview(data)[44:]):
        raise ValueError
    return frames


def _native_loader(req, model):
    if sys.version_info[:3] != (3, 11, 14) or version("pyopenjtalk-dict") != "0.3.4.dev2":
        raise ValueError
    bert = model.parent.parent / "bert"
    dictionary = Path(req["artifact_paths"]["dictionary"])
    if not bert.is_dir() or not dictionary.is_dir() or not (dictionary / "sys.dic").is_file():
        raise ValueError
    # A missing Open JTalk dictionary can trigger a separate downloader, which
    # HF_HUB_OFFLINE does not cover. Parent has verified the full inventory.
    os.environ.update(OPEN_JTALK_DICT_DIR=str(dictionary), HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                      HF_HUB_DISABLE_TELEMETRY="1", TOKENIZERS_PARALLELISM="false",
                      OMP_NUM_THREADS="1", MKL_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1")
    import torch
    if torch.__version__ != "2.3.1+cpu" or torch.version.cuda is not None:
        raise ValueError
    torch.set_num_threads(1)
    from style_bert_vits2.constants import Languages, VERSION
    from style_bert_vits2.nlp import bert_models
    from style_bert_vits2.tts_model import TTSModel
    if VERSION != "2.7.0":
        raise ValueError
    bert_models.load_tokenizer(Languages.JP, str(bert))
    bert_models.load_model(Languages.JP, str(bert), device_map="cpu")
    paths = req["artifact_paths"]
    native = TTSModel(model_path=model, config_path=Path(paths["config"]),
                      style_vec_path=Path(paths["styles"]), device="cpu")
    native.load()
    import numpy as np

    def synthesize(text):
        with torch.inference_mode():
            rate, samples = native.infer(text=text, language=Languages.JP, speaker_id=0,
                                         style=STYLE, style_weight=1.0, line_split=False)
        if (isinstance(rate, bool) or not isinstance(rate, (int, np.integer))
                or int(rate) != RATE or not isinstance(samples, np.ndarray)
                or samples.dtype != np.int16 or samples.ndim != 1
                or not RATE // 10 < len(samples) < RATE * 30 or not samples.any()):
            raise ValueError
        raw = samples.astype("<i2", copy=False).tobytes()
        return (b"RIFF" + struct.pack("<I", 36 + len(raw)) + b"WAVEfmt "
                + struct.pack("<IHHIIHH", 16, 1, 1, RATE, RATE * 2, 2, 16)
                + b"data" + struct.pack("<I", len(raw)) + raw)
    return synthesize


def serve(source, sink, synthesize=None):
    req = read_frame(source)
    model, voices, hashes = _bootstrap(req)
    if synthesize is None:
        synthesize = _native_loader(req, model)
    probe = synthesize(PROBE_TEXT)
    probe_frames = _validate_wav(probe)
    ready = {key: req[key] for key in ("version", "request_id", "epoch")}
    ready.update(op="ready", pid=os.getpid(), worker_id=req["worker_id"],
                 deployment=req["deployment"], device="cpu", gpu_uuid=None,
                 model_id=req["model_id"], model_hashes=hashes,
                 runtime_revision=req["runtime_revision"], adapter_revision=req["adapter_revision"],
                 voices=voices, probe_status="synthesized", probe_pcm16=True,
                 probe_format="pcm16-wav-v1", probe_sha256=hashlib.sha256(probe).hexdigest(),
                 probe_frames=probe_frames, probe_rate=RATE, probe_bytes=len(probe))
    write_frame(sink, ready)
    sink.write(probe)
    sink.flush()
    del probe
    while True:
        job = read_frame(source)
        if (set(job) != JOB_KEYS or type(job["version"]) is not int or job["version"] != 1
                or job["op"] != "infer" or job["epoch"] != req["epoch"]
                or job["model_id"] != req["model_id"] or type(job["speaker_id"]) is not int
                or job["speaker_id"] != 0 or job["style"] != STYLE):
            raise ValueError
        _uuid(job["request_id"])
        _text(job["text"])
        audio = synthesize(job["text"])
        _validate_wav(audio)
        write_frame(sink, {"version": 1, "op": "audio", "request_id": job["request_id"],
                           "epoch": req["epoch"], "status": "ok", "wav_length": len(audio)})
        sink.write(audio)
        sink.flush()


def _offline_io(event, _args):
    # Installed in this child only. Refuse Python HTTP/DNS/socket fallbacks
    # before any network I/O. This is not an OS sandbox for native libraries.
    if event in {"urllib.Request", "socket.connect", "socket.getaddrinfo", "socket.gethostbyname",
                 "socket.gethostbyaddr", "socket.sendto", "socket.sendmsg", "socket.bind"}:
        raise RuntimeError("JVNV runner requires offline inputs")


def main():
    source, sink = os.fdopen(os.dup(0), "rb"), os.fdopen(os.dup(1), "wb")
    with open(os.devnull, "r+b", buffering=0) as null:
        for fd in (0, 1, 2):
            os.dup2(null.fileno(), fd)
    sys.addaudithook(_offline_io)
    serve(source, sink)


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        os._exit(1)
