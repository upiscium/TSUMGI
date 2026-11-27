"""Single-use supervised Faster-Whisper process. No native imports in the Worker.

Mirrors parakeet_process.py (kept untouched) with two deliberate differences
that address defects recorded for earlier providers
(docs/validation/audio-tts-real-engines.md D3/D4):

* ``private_home`` is a dedicated PARENT directory, not the child's HOME. Every
  start creates a fresh ``run-<32 hex>`` 0700 directory inside it as the child's
  HOME/TMPDIR/cwd and removes it after the child is reaped. Stale ``run-*``
  directories of a crashed earlier instance are removed at the next start, so a
  restart policy on a fixed path works and nothing is reused between runs.
* A launch refused before any process exists is a clean, complete failure:
  ``close()`` has nothing to stop, never raises for it, and the owning runtime
  can always reach STOPPED instead of staying cleanup-uncertain.
"""
from __future__ import annotations
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import select
import shutil
import signal
import stat
import struct
import subprocess
import threading
import time
import uuid

from .faster_whisper_environment import validate_environment
from .faster_whisper_native import COMPUTE_TYPE, FILE_SHA256, MODEL_REVISION, PYTHON, VERSIONS

_MODEL_SHA256 = FILE_SHA256["model.bin"]
PYTHON_VERSION = ".".join(map(str, PYTHON))
_MAX_FRAME = (2 << 20) + 4096
_MAX_WAV = 1 << 20
_MAX_TEXT = 16 << 10
_CHILD_PATH = Path(__file__).with_name("faster_whisper_child.py")
RUN_DIR = re.compile(r"run-[0-9a-f]{32}\Z")
MAX_RUN_DIRS = 16


def _wait(fd, deadline, write=False):
    timeout = deadline-time.monotonic()
    if timeout <= 0:
        raise TimeoutError()
    r,w,_ = select.select([] if write else [fd], [fd] if write else [], [], timeout)
    if not (w if write else r):
        raise TimeoutError()

def _read(fd, size, deadline):
    out = bytearray()
    while len(out) < size:
        _wait(fd, deadline)
        block = os.read(fd, size-len(out))
        if not block:
            raise EOFError()
        out.extend(block)
    return bytes(out)

def _send(fd, obj, deadline):
    raw = json.dumps(obj, separators=(",", ":")).encode()
    if not 1 <= len(raw) <= _MAX_FRAME:
        raise ValueError("frame too large")
    data = memoryview(struct.pack(">I",len(raw))+raw)
    while data:
        _wait(fd, deadline, True)
        n = os.write(fd, data[:4096])
        if n <= 0:
            raise EOFError()
        data = data[n:]

def _recv(fd, deadline):
    size, = struct.unpack(">I",_read(fd,4,deadline))
    if not 1 <= size <= _MAX_FRAME:
        raise ValueError("invalid frame")
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate protocol key")
            result[key] = value
        return result
    def reject_constant(_value):
        raise ValueError("invalid protocol constant")
    return json.loads(_read(fd,size,deadline), object_pairs_hook=unique_object,
                      parse_constant=reject_constant)


def check_home_parent(home: Path, *, cap: bool = True) -> list[Path]:
    """Validate the dedicated parent; return its (only allowed) stale run dirs."""
    st = home.lstat()
    if (os.geteuid() == 0 or not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid()
            or stat.S_IMODE(st.st_mode) != 0o700):
        raise RuntimeError("invalid private home")
    stale = []
    for entry in os.scandir(home):
        info = entry.stat(follow_symlinks=False)
        if (not RUN_DIR.fullmatch(entry.name) or not entry.is_dir(follow_symlinks=False)
                or info.st_uid != os.geteuid() or (cap and len(stale) >= MAX_RUN_DIRS)):
            raise RuntimeError("invalid private home")
        stale.append(Path(entry.path))
    return stale


class FasterWhisperProcess:
    def __init__(self, python_path, model_dir, gpu_uuid, *, private_home, base_python_root, epoch,
                 interpreter_sha256):
        self.python = Path(python_path)
        self.model_dir = str(model_dir)
        self.gpu_uuid = gpu_uuid
        self.private_home = Path(private_home)
        self.base_python_root = str(Path(base_python_root))
        self.interpreter_sha256 = str(interpreter_sha256).removeprefix("sha256:")
        self.epoch = epoch
        self._proc = None
        self._run_dir = None
        self._run_lock = None
        self._guard = threading.Lock()
        self._io = threading.Lock()
        self._close_lock = threading.Lock()
        self._closed = False
        self._failed = False
        self._started = False

    @property
    def alive(self):
        with self._guard:
            return (not self._closed and not self._failed and self._proc is not None
                    and self._proc.poll() is None)

    @staticmethod
    def _force_rmtree(path):
        def fix(func, p, _exc):
            try:
                os.chmod(os.path.dirname(p), 0o700)
                os.chmod(p, 0o700)
                func(p)
            except OSError:
                pass
        shutil.rmtree(path, onerror=fix)

    def _fresh_home(self):
        # Every run directory is held under an exclusive flock for its owner's
        # lifetime. Only directories whose lock can be taken (no live owner) are
        # swept; a stubborn leftover never blocks a start, a live one is never touched.
        for stale in check_home_parent(self.private_home, cap=False):
            try:
                fd = os.open(stale, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            except OSError:
                continue
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                continue  # live owner
            try:
                self._force_rmtree(stale)
            finally:
                os.close(fd)
        check_home_parent(self.private_home)  # re-validate, now with the entry cap
        run = self.private_home / ("run-" + uuid.uuid4().hex)
        os.mkdir(run, 0o700)
        self._run_dir = run
        fd = os.open(run, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self._run_lock = fd
        return run

    def _env(self):
        if not self.python.is_absolute() or self.python.parts[-2:] != ("bin","python"):
            raise ValueError("invalid pinned interpreter path")
        root=self.python.parent.parent
        if not (root/"pyvenv.cfg").is_file():
            raise ValueError("invalid pinned environment")
        with self.python.open("rb") as f:
            if (not 0 < os.fstat(f.fileno()).st_size <= 64<<20
                    or hashlib.file_digest(f,"sha256").hexdigest()!=self.interpreter_sha256):
                raise ValueError("unqualified interpreter")
        libraries = validate_environment(self.python, self.base_python_root)
        home = str(self._fresh_home())
        return {"PATH":"/usr/bin:/bin","HOME":home,"TMPDIR":home,
                "CUDA_VISIBLE_DEVICES":self.gpu_uuid,"OMP_NUM_THREADS":"2","HF_HUB_OFFLINE":"1",
                "TRANSFORMERS_OFFLINE":"1","LD_LIBRARY_PATH":":".join(libraries)}

    def start(self):
        with self._guard:
            if self._closed or self._started: raise RuntimeError("process is single-use")
            self._started=True
        try:
            env=self._env()
            with self._guard:
                if self._closed:
                    raise RuntimeError("process closed during environment validation")
                proc=subprocess.Popen([str(self.python),"-I","-B","-u",str(_CHILD_PATH)],cwd=self._run_dir or self.private_home,
                     env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                     bufsize=0,close_fds=True,start_new_session=True)
                self._proc=proc
                self._proc_group=proc.pid
                os.set_blocking(proc.stdin.fileno(),False)
                os.set_blocking(proc.stdout.fileno(),False)
        except BaseException:
            # Refused or failed before a process was assigned: nothing to stop.
            self._failed=True
            try:
                self.close()
            except Exception:
                pass
            raise
        try:
            with self._io:
                deadline=time.monotonic()+155
                request_id = uuid.uuid4().hex
                _send(self._proc.stdin.fileno(),{"kind":"bootstrap","epoch":self.epoch,
                     "request_id":request_id,"model_dir":self.model_dir,
                     "model_revision":MODEL_REVISION,"model_sha256":_MODEL_SHA256,
                     "gpu_uuid":self.gpu_uuid},deadline)
                ready=_recv(self._proc.stdout.fileno(),deadline)
                expected={"kind":"ready","epoch":self.epoch,"request_id":request_id,
                     "pid":self._proc.pid,"model_revision":MODEL_REVISION,
                     "model_sha256":_MODEL_SHA256,"gpu_uuid":self.gpu_uuid,"python":PYTHON_VERSION,
                     "compute_type":COMPUTE_TYPE,"faster_whisper":VERSIONS["faster-whisper"],
                     "ctranslate2":VERSIONS["ctranslate2"]}
                if ready != expected or not self.alive: raise ValueError()
        except Exception:
            self._failed=True
            try: self.close()
            except Exception: pass
            raise RuntimeError("Faster-Whisper child did not confirm readiness") from None

    def transcribe(self,wav):
        if type(wav) is not bytes or not 44<=len(wav)<=_MAX_WAV or not self.alive:
            raise RuntimeError("Faster-Whisper child unavailable")
        try:
            with self._io:
                if not self.alive: raise RuntimeError()
                rid=uuid.uuid4().hex; deadline=time.monotonic()+80
                _send(self._proc.stdin.fileno(),{"kind":"infer","epoch":self.epoch,"request_id":rid,"wav":wav.hex()},deadline)
                reply=_recv(self._proc.stdout.fileno(),deadline)
                if (type(reply) is not dict or set(reply)!={"kind","epoch","request_id","text"} or reply["kind"]!="result" or
                    reply["epoch"]!=self.epoch or reply["request_id"]!=rid or type(reply["text"]) is not str or
                    not reply["text"] or len(reply["text"].encode("utf-8"))>_MAX_TEXT):
                    raise ValueError()
                return reply["text"]
        except Exception:
            self._failed=True
            try: self.close()
            except Exception: pass
            raise RuntimeError("Faster-Whisper child exchange failed") from None

    def _remove_run_dir(self):
        run, self._run_dir = self._run_dir, None
        lock, self._run_lock = self._run_lock, None
        if run is not None:
            # Best effort: the process is already reaped; a leftover is removed
            # at the next start and never makes shutdown uncertain.
            self._force_rmtree(run)
        if lock is not None:
            os.close(lock)

    def close(self):
        # Publish revocation before signalling; never wait on _io while holding
        # _guard because an exchange checks alive while holding _io.
        with self._close_lock:
            with self._guard:
                self._closed = True
                p = self._proc
                pgid = getattr(self, "_proc_group", None)
            if p is None:
                self._remove_run_dir()
                return  # never spawned (e.g. refused launch): complete, not uncertain
            if p.poll() is None:
                try:
                    os.killpg(pgid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    p.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(pgid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    try:
                        p.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        raise RuntimeError("Faster-Whisper child shutdown unconfirmed") from None
            if p.poll() is None:
                raise RuntimeError("Faster-Whisper child shutdown unconfirmed")
            if self._io.acquire(timeout=2):
                try:
                    if p.stdin and not p.stdin.closed:
                        p.stdin.close()
                    if p.stdout and not p.stdout.closed:
                        p.stdout.close()
                finally:
                    self._io.release()
            else:
                raise RuntimeError("Faster-Whisper child pipe cleanup unconfirmed")
            self._remove_run_dir()
