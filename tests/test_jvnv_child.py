import hashlib
import io
import json
import os
from pathlib import Path
import struct
import unittest
import uuid
import wave
from tempfile import TemporaryDirectory

from astrumweaver.runtime.providers import jvnv_child as child
from astrumweaver.runtime.providers import style_bert_vits2_manifest as manifest
from astrumweaver.runtime.providers import jvnv_assets as assets


def frame(value):
    payload = json.dumps(value, separators=(",", ":")).encode()
    return struct.pack(">I", len(payload)) + payload


def pcm_wav(seed):
    samples = struct.pack("<5000h", *[(seed + i) % 250 + 1 for i in range(5000)])
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(child.RATE)
        wav.writeframes(samples)
    return output.getvalue()


class RunnerTests(unittest.TestCase):
    def test_frames_reject_bad_lengths_and_duplicate_keys(self):
        for wire in (b"\0\0\0\0", struct.pack(">I", child.LIMIT + 1) + b"x",
                     b"\0\0\0\x0d" + b'{"x":1,"x":2}'):
            with self.subTest(wire=wire), self.assertRaises(ValueError):
                child.read_frame(io.BytesIO(wire))

    def test_text_is_bounded_utf8(self):
        child._text("あ" * 85)
        child._text("valid whitespace inside")
        for text in ("あ" * 86, "", " \t ", "　", "\ud800"):
            with self.subTest(text=repr(text)), self.assertRaises(ValueError):
                child._text(text)

    def test_serve_sequential_jobs_and_canonical_probe(self):
        epoch = str(uuid.uuid4())
        request_id = str(uuid.uuid4())
        ids = [str(uuid.uuid4()), str(uuid.uuid4())]
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        model = Path(tmp.name) / "jvnv-F1-jp"
        model.mkdir()
        model_data = {"model_name": "jvnv-F1-jp", "data": {"sampling_rate": 44100,
                      "spk2id": {"jvnv-F1-jp": 0}, "style2id": {"Neutral": 0}, "use_jp_extra": True}}
        config = model / "config.json"
        config.write_text(json.dumps(model_data))
        provider = manifest.StyleBertManifestConfig(
            enabled=True, model_directory=str(model), interpreter_path="/env/bin/python",
            dependency_lock_path="/inputs/deps.lock", private_home="/private/home",
            dictionary_directory="/inputs/jp-dictionary", dictionary_sha256="sha256:" + "9" * 64,
            rights_review_sha256="sha256:" + "1" * 64, validation_evidence_sha256="sha256:" + "2" * 64,
        )
        dep = provider.serving_declaration().deployment
        req = {"version": 1, "op": "bootstrap", "request_id": request_id,
               "epoch": epoch, "pid": os.getpid(), "worker_id": "worker", "deployment": dep.revision,
               "device": "cpu", "gpu_uuid": None, "model_id": manifest._MODEL,
               "artifact_paths": {"model": str(model / assets.WEIGHT_FILENAME), "config": str(config),
                                  "styles": str(model / "style_vectors.npy"), "dictionary": "/inputs/jp-dictionary"},
               "model_hashes": {"weight": assets.MODEL_SHA256[7:], "styles": assets.STYLES_SHA256[7:],
                                "config": hashlib.sha256(config.read_bytes()).hexdigest()},
               "runtime_revision": manifest._ENGINE, "adapter_revision": dep.adapter_artifact_sha256,
               "voices": [{"alias": "jvnv-f1-neutral", "speaker_id": 0, "style_id": "0", "style": "Neutral"}],
               "probe_format": "pcm16-wav-v1"}
        jobs = [{"version": 1, "op": "infer", "request_id": rid, "epoch": epoch,
                 "text": text, "model_id": manifest._MODEL, "speaker_id": 0, "style": "Neutral"}
                for rid, text in zip(ids, ("first text", "別の文章"))]
        incoming = io.BytesIO(frame(req) + b"".join(map(frame, jobs)))
        outgoing = io.BytesIO()
        seen = []
        audio = {child.PROBE_TEXT: pcm_wav(1),
                 "first text": pcm_wav(2), "別の文章": pcm_wav(3)}

        def synthesize(text):
            seen.append(text)
            return audio[text]

        with self.assertRaises(ValueError):  # EOF after both complete jobs
            child.serve(incoming, outgoing, synthesize=synthesize)

        self.assertEqual(seen, [child.PROBE_TEXT, "first text", "別の文章"])
        stream = io.BytesIO(outgoing.getvalue())
        ready = child.read_frame(stream)
        self.assertEqual(ready["probe_format"], "pcm16-wav-v1")
        self.assertEqual((ready["probe_status"], ready["probe_pcm16"]), ("synthesized", True))
        probe = stream.read(ready["probe_bytes"])
        self.assertEqual(hashlib.sha256(probe).hexdigest(), ready["probe_sha256"])
        self.assertEqual(child._validate_wav(probe), ready["probe_frames"])
        with wave.open(io.BytesIO(probe), "rb") as wav:
            self.assertEqual((wav.getnchannels(), wav.getsampwidth(), wav.getframerate(),
                              wav.getnframes()), (1, 2, child.RATE, ready["probe_frames"]))
        returned = []
        for rid in ids:
            metadata = child.read_frame(stream)
            self.assertEqual((metadata["op"], metadata["request_id"], metadata["epoch"]),
                             ("audio", rid, epoch))
            returned.append(stream.read(metadata["wav_length"]))
        self.assertEqual(returned, [audio["first text"], audio["別の文章"]])
        self.assertNotEqual(returned[0], returned[1])

    def test_downloader_refused_before_python_network_io(self):
        for event in ("urllib.Request", "socket.connect", "socket.getaddrinfo", "socket.sendto"):
            with self.assertRaises(RuntimeError):
                child._offline_io(event, ())


if __name__ == "__main__":
    unittest.main()
