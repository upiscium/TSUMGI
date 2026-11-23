"""Real stdlib-only child process protocol and shutdown regressions."""
import json
import os
from pathlib import Path
import struct
import sys
import threading
import time

import pytest
from tsumgi.runtime.providers import parakeet_process as process

_REV = "44edb27eea9317daf89333e75eb830db4b1cc298"
_SHA = "a2ed2aab9c82cce4e9b699dca669184d7525c7b5e1eb8c3f7b789421bc3637e8"
_GPU = "GPU-12345678-1234-abcd-9876-123456789abc"

def _script(path, *, delay=False, wrong_epoch=False, wrong_request=False):
    source = """
import json, os, struct, sys, time
def read():
    n, = struct.unpack(">I", sys.stdin.buffer.read(4))
    return json.loads(sys.stdin.buffer.read(n))
def send(value):
    raw = json.dumps(value, separators=(",", ":")).encode()
    sys.stdout.buffer.write(struct.pack(">I", len(raw)) + raw); sys.stdout.buffer.flush()
b = read()
send({"kind":"ready", "epoch":b["epoch"], "request_id":b["request_id"],
      "pid":os.getpid(), "model_revision":@REV@,
      "model_sha256":@SHA@, "gpu_uuid":b["gpu_uuid"],
      "python":"3.11.14", "nemo":"2.4.0", "torch":"2.7.1+cu126",
      "torchaudio":"2.7.1+cu126"})
r = read()
"""
    source = source.replace("@REV@", repr(_REV)).replace("@SHA@", repr(_SHA))
    if delay:
        source += "time.sleep(30)\n"
    if wrong_epoch:
        source += 'r["epoch"] = "wrong"\n'
    if wrong_request:
        source += 'r["request_id"] = "00000000000000000000000000000000"\n'
    source += """
send({"kind":"result","epoch":r["epoch"],"request_id":r["request_id"],"text":"fixture"})
time.sleep(30)
"""
    path.write_text(source)

def _process(tmp_path, monkeypatch, *, delay=False, wrong_epoch=False, wrong_request=False):
    child = tmp_path / "synthetic_child.py"
    _script(child, delay=delay, wrong_epoch=wrong_epoch, wrong_request=wrong_request)
    monkeypatch.setattr(process, "_CHILD_PATH", child)
    p = process.ParakeetProcess(sys.executable, "/fixture.nemo", _GPU,
                                private_home=tmp_path, base_python_root=tmp_path / "base",
                                epoch="epoch-fixture")
    monkeypatch.setattr(p, "_env", lambda: {"PATH": os.defpath})
    return p

def test_stdlib_child_handshake_inference_and_reap(tmp_path, monkeypatch):
    p = _process(tmp_path, monkeypatch)
    p.start()
    pid = p._proc.pid
    assert p.transcribe(b"x" * 44) == "fixture"
    p.close()
    assert p._proc.poll() is not None
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)

def test_close_racing_blocked_pipe_read_reaps_without_deadlock(tmp_path, monkeypatch):
    p = _process(tmp_path, monkeypatch, delay=True)
    p.start()
    outcome = []
    t = threading.Thread(target=lambda: outcome.append(pytest.raises(RuntimeError, p.transcribe, b"x" * 44)))
    t.start()
    time.sleep(.15)
    closer = threading.Thread(target=p.close)
    closer.start()
    closer.join(4)
    t.join(4)
    assert not closer.is_alive()
    assert not t.is_alive()
    assert p._proc.poll() is not None

def test_close_during_environment_scan_prevents_spawn(tmp_path, monkeypatch):
    p = _process(tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    spawned = []

    def blocked_env():
        entered.set()
        assert release.wait(2)
        return {"PATH": os.defpath}

    monkeypatch.setattr(p, "_env", blocked_env)
    monkeypatch.setattr(process.subprocess, "Popen", lambda *a, **kw: spawned.append(True))
    starter = threading.Thread(target=lambda: pytest.raises(RuntimeError, p.start))
    starter.start()
    assert entered.wait(2)
    p.close()
    release.set()
    starter.join(2)
    assert not starter.is_alive()
    assert not spawned

def test_close_waits_for_spawn_and_reaps_assigned_child(tmp_path, monkeypatch):
    p = _process(tmp_path, monkeypatch)
    real_popen = process.subprocess.Popen
    entered, release = threading.Event(), threading.Event()

    def blocked_popen(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(process.subprocess, "Popen", blocked_popen)
    starter = threading.Thread(target=lambda: pytest.raises(RuntimeError, p.start))
    starter.start()
    assert entered.wait(2)
    closer = threading.Thread(target=p.close)
    closer.start()
    assert closer.is_alive()
    release.set()
    starter.join(4)
    closer.join(4)
    assert not starter.is_alive() and not closer.is_alive()
    assert p._proc is not None and p._proc.poll() is not None

def test_wrong_epoch_result_rejected_and_child_reaped(tmp_path, monkeypatch):
    p = _process(tmp_path, monkeypatch, wrong_epoch=True)
    p.start()
    with pytest.raises(RuntimeError):
        p.transcribe(b"x" * 44)
    assert p._proc.poll() is not None

def test_wrong_request_id_result_rejected_and_child_reaped(tmp_path, monkeypatch):
    p = _process(tmp_path, monkeypatch, wrong_request=True)
    p.start()
    with pytest.raises(RuntimeError):
        p.transcribe(b"x" * 44)
    assert p._proc.poll() is not None

def test_duplicate_json_keys_rejected():
    from tsumgi.runtime.providers.parakeet_child import _recv
    import io
    raw = b'{"kind":"a","kind":"b"}'
    with pytest.raises(ValueError):
        _recv(io.BytesIO(struct.pack(">I", len(raw)) + raw))
