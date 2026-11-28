"""Owned child process: real fixed-argv child module, fake native engine, no model/GPU."""
import io
import os
from pathlib import Path
import shutil
import struct
import sys
import threading
import time

import pytest

from tsumgi.runtime.providers import faster_whisper_child as child_module
from tsumgi.runtime.providers import faster_whisper_native as pins
from tsumgi.runtime.providers import faster_whisper_process as process

GPU = "GPU-12345678-1234-abcd-9876-123456789abc"
WAV = b"x" * 44

FAKE_NATIVE = f'''
import os, sys, time
MODEL_REVISION = {pins.MODEL_REVISION!r}
FILE_SHA256 = {dict(pins.FILE_SHA256)!r}
COMPUTE_TYPE = {pins.COMPUTE_TYPE!r}
VERSIONS = {dict(pins.VERSIONS)!r}
class FasterWhisperNative:
    @classmethod
    def load(cls, model_dir, *, gpu_uuid):
        if os.environ.get("FAKE_MODE") == "load_fail":
            raise RuntimeError("no model")
        return cls()
    def transcribe(self, wav):
        mode = os.environ.get("FAKE_MODE", "ok")
        if mode == "ok": return "テスト"
        if mode == "home": return os.environ["HOME"] + "|" + os.getcwd() + "|" + os.environ["TMPDIR"]
        if mode == "empty": return ""
        if mode == "huge": return "あ" * 6000
        if mode == "crash": os._exit(3)
        if mode == "hang": time.sleep(60)
'''


@pytest.fixture
def make(tmp_path, monkeypatch):
    children = tmp_path / "child"
    children.mkdir()
    shutil.copy(Path(child_module.__file__), children / "faster_whisper_child.py")
    (children / "faster_whisper_native.py").write_text(FAKE_NATIVE)
    monkeypatch.setattr(process, "_CHILD_PATH", children / "faster_whisper_child.py")
    monkeypatch.setattr(process, "PYTHON_VERSION", sys.version.split()[0])  # the fake child runs on the test interpreter
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    made = []

    def build(mode="ok", *, private_home=None):
        p = process.FasterWhisperProcess(
            sys.executable, "/fixture/model", GPU, private_home=private_home or home,
            base_python_root=tmp_path / "base", epoch="epoch-fixture", interpreter_sha256="0" * 64,
        )
        def env():
            run = p._fresh_home()
            return {"PATH": os.defpath, "HOME": str(run), "TMPDIR": str(run), "FAKE_MODE": mode}
        monkeypatch.setattr(p, "_env", env)
        made.append(p)
        return p

    yield build, home
    for p in made:
        try:
            p.close()
        except Exception:
            pass


def runs(home):
    return sorted(os.listdir(home))


def test_handshake_inference_and_reap_remove_the_fresh_home(make):
    build, home = make
    p = build()
    p.start()
    assert len(runs(home)) == 1 and process.RUN_DIR.fullmatch(runs(home)[0])
    assert p.transcribe(WAV) == "テスト"
    pid = p._proc.pid
    p.close()
    assert p._proc.poll() is not None
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert runs(home) == []


def test_child_home_tmp_and_cwd_are_the_fresh_run_directory(make):
    build, home = make
    p = build("home")
    p.start()
    run = str(home / runs(home)[0])
    assert p.transcribe(WAV) == f"{run}|{run}|{run}"
    assert oct(os.stat(run).st_mode & 0o777) == "0o700"


def test_restart_sweeps_dead_leftovers_even_unremovable_ones_but_never_a_live_instance(make):
    build, home = make
    live = build()
    live.start()
    live_name = runs(home)[0]
    stale = home / ("run-" + "d" * 32)
    (stale / "sub").mkdir(parents=True, mode=0o700)
    (stale / "sub" / "f").write_text("x")
    (stale / "sub").chmod(0o500)  # a crashed child left an unremovable-looking tree
    second = build()
    second.start()
    assert live_name in runs(home) and stale.name not in runs(home) and len(runs(home)) == 2
    assert live.transcribe(WAV) == "テスト" and second.transcribe(WAV) == "テスト"
    live.close()
    second.close()
    assert runs(home) == []


@pytest.mark.parametrize("case", ["foreign_entry", "foreign_dir", "mode", "symlink_run"])
def test_refused_launch_spawns_nothing_and_close_is_clean(make, monkeypatch, tmp_path, case):
    """D3: a pre-spawn refusal must not leave close()/stop() failing."""
    build, home = make
    if case == "foreign_entry":
        (home / "state").write_text("x")
    elif case == "foreign_dir":
        (home / "cache").mkdir()
    elif case == "mode":
        home.chmod(0o755)
    else:
        (home / ("run-" + "c" * 32)).symlink_to(tmp_path)
    spawned = []
    monkeypatch.setattr(process.subprocess, "Popen", lambda *a, **k: spawned.append(1))
    p = build()
    with pytest.raises(RuntimeError):
        p.start()
    assert not spawned and p._proc is None and not p.alive
    p.close()  # complete, does not raise
    p.close()  # idempotent
    with pytest.raises(RuntimeError):
        p.start()  # single use


def test_failed_launch_after_home_creation_removes_the_fresh_home(make, monkeypatch):
    build, home = make
    def refuse(*_a, **_k):
        raise OSError("exec refused")
    monkeypatch.setattr(process.subprocess, "Popen", refuse)
    p = build()
    with pytest.raises(OSError):
        p.start()
    p.close()
    assert runs(home) == []


def test_load_failure_is_reported_closed_and_reaped(make):
    build, home = make
    p = build("load_fail")
    with pytest.raises(RuntimeError, match="did not confirm readiness"):
        p.start()
    assert p._proc.poll() is not None and not p.alive
    assert runs(home) == []


def test_child_crash_during_request_revokes_and_reaps(make):
    build, home = make
    p = build("crash")
    p.start()
    with pytest.raises(RuntimeError):
        p.transcribe(WAV)
    assert not p.alive and p._proc.poll() is not None
    with pytest.raises(RuntimeError):
        p.transcribe(WAV)  # never reused after a failure


@pytest.mark.parametrize("mode", ["huge"])
def test_invalid_child_text_revokes_the_instance(make, mode):
    build, _ = make
    p = build(mode)
    p.start()
    with pytest.raises(RuntimeError):
        p.transcribe(WAV)
    assert not p.alive


def test_close_racing_blocked_request_reaps_without_deadlock(make):
    build, home = make
    p = build("hang")
    p.start()
    outcome = []
    t = threading.Thread(target=lambda: outcome.append(pytest.raises(RuntimeError, p.transcribe, WAV)))
    t.start()
    time.sleep(.3)
    closer = threading.Thread(target=p.close)
    closer.start()
    closer.join(8)
    t.join(8)
    assert not closer.is_alive() and not t.is_alive()
    assert p._proc.poll() is not None
    assert runs(home) == []


def test_oversized_or_wrong_type_input_is_refused_before_ipc(make):
    build, _ = make
    p = build()
    p.start()
    for bad in (b"x" * 43, b"x" * ((1 << 20) + 1), "text", bytearray(WAV)):
        with pytest.raises(RuntimeError):
            p.transcribe(bad)
    assert p.alive  # refusal of bad input is not a child failure
    assert p.transcribe(WAV) == "テスト"


def test_close_during_environment_validation_prevents_spawn(make, monkeypatch):
    build, home = make
    p = build()
    entered, release, spawned = threading.Event(), threading.Event(), []
    inner = p._env
    def blocked():
        entered.set()
        assert release.wait(2)
        return inner()
    monkeypatch.setattr(p, "_env", blocked)
    monkeypatch.setattr(process.subprocess, "Popen", lambda *a, **k: spawned.append(1))
    starter = threading.Thread(target=lambda: pytest.raises(RuntimeError, p.start))
    starter.start()
    assert entered.wait(2)
    p.close()
    release.set()
    starter.join(3)
    assert not starter.is_alive() and not spawned


def test_wrong_epoch_or_request_id_result_is_rejected(tmp_path, monkeypatch):
    for bad in ('r["epoch"] = "wrong"', 'r["request_id"] = "0" * 32'):
        script = tmp_path / "synthetic.py"
        script.write_text(f"""
import json, os, struct, sys, time
def read():
    n, = struct.unpack(">I", sys.stdin.buffer.read(4))
    return json.loads(sys.stdin.buffer.read(n))
def send(v):
    raw = json.dumps(v, separators=(",", ":")).encode()
    sys.stdout.buffer.write(struct.pack(">I", len(raw)) + raw); sys.stdout.buffer.flush()
b = read()
send({{"kind":"ready","epoch":b["epoch"],"request_id":b["request_id"],"pid":os.getpid(),
      "model_revision":{pins.MODEL_REVISION!r},"model_sha256":{pins.FILE_SHA256["model.bin"]!r},
      "gpu_uuid":b["gpu_uuid"],"python":"3.11.14","compute_type":"float32",
      "faster_whisper":"1.2.1","ctranslate2":"4.9.1"}})
r = read()
{bad}
send({{"kind":"result","epoch":r["epoch"],"request_id":r["request_id"],"text":"x"}})
time.sleep(30)
""")
        monkeypatch.setattr(process, "_CHILD_PATH", script)
        home = tmp_path / ("h" + str(len(bad)))
        home.mkdir(mode=0o700)
        p = process.FasterWhisperProcess(sys.executable, "/m", GPU, private_home=home,
                                         base_python_root=tmp_path / "b", epoch="e", interpreter_sha256="0" * 64)
        monkeypatch.setattr(p, "_env", lambda: {"PATH": os.defpath})
        p.start()
        with pytest.raises(RuntimeError):
            p.transcribe(WAV)
        assert p._proc.poll() is not None


@pytest.mark.parametrize("field", ["model_revision", "model_sha256", "compute_type", "faster_whisper", "ctranslate2"])
def test_ready_message_must_confirm_every_pin(tmp_path, monkeypatch, field):
    reply = {"kind": "ready", "pid": 0, "model_revision": pins.MODEL_REVISION,
             "model_sha256": pins.FILE_SHA256["model.bin"], "python": "3.11.14",
             "compute_type": "float32", "faster_whisper": "1.2.1", "ctranslate2": "4.9.1"}
    reply[field] = "other"
    script = tmp_path / "synthetic.py"
    script.write_text(f"""
import json, os, struct, sys, time
n, = struct.unpack(">I", sys.stdin.buffer.read(4)); b = json.loads(sys.stdin.buffer.read(n))
v = {reply!r}; v.update(epoch=b["epoch"], request_id=b["request_id"], pid=os.getpid(), gpu_uuid=b["gpu_uuid"])
raw = json.dumps(v).encode(); sys.stdout.buffer.write(struct.pack(">I", len(raw)) + raw); sys.stdout.buffer.flush()
time.sleep(30)
""")
    monkeypatch.setattr(process, "_CHILD_PATH", script)
    home = tmp_path / "h"
    home.mkdir(mode=0o700)
    p = process.FasterWhisperProcess(sys.executable, "/m", GPU, private_home=home,
                                     base_python_root=tmp_path / "b", epoch="e", interpreter_sha256="0" * 64)
    monkeypatch.setattr(p, "_env", lambda: {"PATH": os.defpath})
    with pytest.raises(RuntimeError, match="did not confirm readiness"):
        p.start()
    assert p._proc.poll() is not None


def test_env_rejects_unpinned_interpreter_before_creating_any_home(tmp_path):
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin/python").write_bytes(b"interpreter")
    (venv / "pyvenv.cfg").write_text("home = x\n")
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    p = process.FasterWhisperProcess(venv / "bin/python", "/m", GPU, private_home=home,
                                     base_python_root=tmp_path / "b", epoch="e", interpreter_sha256="0" * 64)
    with pytest.raises(ValueError, match="unqualified interpreter"):
        p._env()
    assert os.listdir(home) == []


def test_child_environment_has_no_network_or_credential_variables(tmp_path, monkeypatch):
    env = {}
    monkeypatch.setattr(process, "validate_environment", lambda *_a: ("/lib/cublas", "/lib/cudnn"))
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin/python").write_bytes(b"interpreter")
    (venv / "pyvenv.cfg").write_text("home = x\n")
    import hashlib
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HF_TOKEN", "secret")
    monkeypatch.setenv("TSUMGI_WORKER_TOKEN", "secret")
    p = process.FasterWhisperProcess(venv / "bin/python", "/m", GPU, private_home=home,
                                     base_python_root=tmp_path / "b", epoch="e",
                                     interpreter_sha256=hashlib.sha256(b"interpreter").hexdigest())
    env = p._env()
    assert set(env) == {"PATH", "HOME", "TMPDIR", "CUDA_VISIBLE_DEVICES", "OMP_NUM_THREADS",
                        "HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "LD_LIBRARY_PATH"}
    assert env["HF_HUB_OFFLINE"] == "1" and env["CUDA_VISIBLE_DEVICES"] == GPU
    assert env["HOME"] == env["TMPDIR"] == str(p._run_dir) and Path(env["HOME"]).parent == home


def test_duplicate_json_keys_and_truncated_frames_rejected():
    raw = b'{"kind":"a","kind":"b"}'
    with pytest.raises(ValueError):
        child_module._recv(io.BytesIO(struct.pack(">I", len(raw)) + raw))
    with pytest.raises(ValueError):
        child_module._read_exact(io.BytesIO(b"x"), 2)
    raw = b'{"a": NaN}'
    with pytest.raises(ValueError):
        child_module._recv(io.BytesIO(struct.pack(">I", len(raw)) + raw))
    stream = io.BytesIO()
    child_module._send(stream, {"kind": "fixture"})
    stream.seek(0)
    assert child_module._recv(stream) == {"kind": "fixture"}


def test_empty_transcript_is_a_valid_result_and_keeps_the_child_alive(make):
    build, _ = make
    p = build("empty")
    p.start()
    assert p.transcribe(WAV) == ""
    assert p.alive and p.transcribe(WAV) == ""


def test_clean_eof_at_a_frame_boundary_exits_zero_but_truncation_is_an_error(tmp_path):
    import subprocess
    child = Path(process._CHILD_PATH)
    def run(tail):
        boot = {"kind": "bootstrap", "epoch": "e", "request_id": "a" * 32, "model_dir": "/m",
                "model_revision": pins.MODEL_REVISION, "model_sha256": pins.FILE_SHA256["model.bin"],
                "gpu_uuid": GPU}
        import json
        raw = json.dumps(boot).encode()
        # The real child module with the real native module cannot load without the engine,
        # so run it with a fake native next to a copy.
        d = tmp_path / ("c" + str(len(tail)))
        d.mkdir()
        shutil.copy(child, d / "faster_whisper_child.py")
        (d / "faster_whisper_native.py").write_text(FAKE_NATIVE)
        p = subprocess.run([sys.executable, "-I", "-B", str(d / "faster_whisper_child.py")],
                           input=struct.pack(">I", len(raw)) + raw + tail, capture_output=True, timeout=20,
                           env={"PATH": os.defpath})
        return p.returncode
    assert run(b"") == 0                      # parent closed the pipe between frames
    assert run(b"\x00\x00") == 1              # truncated header
    assert run(struct.pack(">I", 50) + b"{") == 1  # truncated frame body


def test_symlink_planted_in_a_run_directory_is_never_chmodded_through(make, tmp_path):
    build, home = make
    outside = tmp_path / "outside"
    outside.write_text("keep")
    outside.chmod(0o644)
    stale = home / ("run-" + "e" * 32)
    stale.mkdir(mode=0o700)
    (stale / "link").symlink_to(outside)
    (stale / "locked").mkdir(mode=0o700)
    (stale / "locked" / "f").write_text("x")
    (stale / "locked").chmod(0o500)
    p = build()
    p.start()
    assert stale.name not in runs(home)
    assert outside.read_text() == "keep" and oct(outside.stat().st_mode & 0o777) == "0o644"
    p.close()


def test_many_dead_leftovers_never_block_but_live_ones_count(make):
    build, home = make
    for i in range(20):
        d = home / f"run-{i:032x}"
        (d / "sub").mkdir(parents=True, mode=0o700)
        (d / "sub" / "f").write_text("x")
        (d / "sub").chmod(0o500)
    live = build()
    live.start()
    assert runs(home) == [live._run_dir.name]
    live.close()
