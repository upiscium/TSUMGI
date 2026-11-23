"""Single-use, isolated process for the pinned local Parakeet model."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
from .parakeet_environment import validate_environment
import select
import signal
import struct
import subprocess
import threading
import time
import uuid

_PY_SHA256 = "e3cc9523fbfbd83a0fbc51fa568868e77e06388480297a7ece177ce491bbe1b9"
_MAX_FRAME = (2 << 20) + 4096
_MAX_WAV = 1 << 20
_MAX_TEXT = 16 << 10
_MODEL_REVISION = "44edb27eea9317daf89333e75eb830db4b1cc298"
_MODEL_SHA256 = "a2ed2aab9c82cce4e9b699dca669184d7525c7b5e1eb8c3f7b789421bc3637e8"
_CHILD_PATH = Path(__file__).with_name("parakeet_child.py")

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

class ParakeetProcess:
    def __init__(self, python_path, model_path, gpu_uuid, *, private_home, base_python_root, epoch):
        self.python = Path(python_path)
        self.model_path = str(model_path)
        self.gpu_uuid = gpu_uuid
        self.private_home = Path(private_home)
        self.base_python_root = str(Path(base_python_root))
        self.epoch = epoch
        self._proc = None
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

    def _env(self):
        st=self.private_home.stat()
        if (os.geteuid()==0 or st.st_uid!=os.geteuid() or st.st_mode & 0o077 or
                not self.private_home.is_dir()):
            raise RuntimeError("invalid private home")
        if not self.python.is_absolute() or self.python.parts[-2:] != ("bin","python"):
            raise ValueError("invalid pinned interpreter path")
        root=self.python.parent.parent
        if not (root/"pyvenv.cfg").is_file():
            raise ValueError("invalid pinned environment")
        with self.python.open("rb") as f:
            if not 0 < os.fstat(f.fileno()).st_size <= 64<<20 or hashlib.file_digest(f,"sha256").hexdigest()!=_PY_SHA256:
                raise ValueError("unqualified interpreter")
        libraries = validate_environment(self.python, self.base_python_root)
        return {"PATH":"/usr/bin:/bin","HOME":str(self.private_home),"TMPDIR":str(self.private_home),
                "CUDA_VISIBLE_DEVICES":self.gpu_uuid,"OMP_NUM_THREADS":"2","HF_HUB_OFFLINE":"1",
                "TRANSFORMERS_OFFLINE":"1","NVIDIA_TF32_OVERRIDE":"0",
                 "LD_LIBRARY_PATH":":".join(libraries)}

    def start(self):
        with self._guard:
            if self._closed or self._started: raise RuntimeError("process is single-use")
            self._started=True
        try:
            env=self._env()
            with self._guard:
                if self._closed:
                    raise RuntimeError("process closed during environment validation")
                child = _CHILD_PATH
                proc=subprocess.Popen([str(self.python),"-I","-B","-u",str(child)],cwd=self.private_home,
                     env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                     bufsize=0,close_fds=True,start_new_session=True)
                self._proc=proc
                self._proc_group=proc.pid
                os.set_blocking(proc.stdin.fileno(),False)
                os.set_blocking(proc.stdout.fileno(),False)
        except BaseException:
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
                     "request_id":request_id,"model_path":self.model_path,
                     "model_revision":_MODEL_REVISION,
                     "model_sha256":_MODEL_SHA256,
                     "gpu_uuid":self.gpu_uuid},deadline)
                ready=_recv(self._proc.stdout.fileno(),deadline)
                expected={"kind":"ready","epoch":self.epoch,"request_id":request_id,
                     "pid":self._proc.pid,"model_revision":_MODEL_REVISION,
                     "model_sha256":_MODEL_SHA256,
                     "gpu_uuid":self.gpu_uuid,"python":"3.11.14","nemo":"2.4.0",
                     "torch":"2.7.1+cu126","torchaudio":"2.7.1+cu126"}
                if ready != expected or not self.alive: raise ValueError()
        except Exception:
            self._failed=True
            try: self.close()
            except Exception: pass
            raise RuntimeError("Parakeet child did not confirm readiness") from None

    def transcribe(self,wav):
        if type(wav) is not bytes or not 44<=len(wav)<=_MAX_WAV or not self.alive:
            raise RuntimeError("Parakeet child unavailable")
        try:
            with self._io:
                if not self.alive: raise RuntimeError()
                rid=uuid.uuid4().hex; deadline=time.monotonic()+80
                _send(self._proc.stdin.fileno(),{"kind":"infer","epoch":self.epoch,"request_id":rid,"wav":wav.hex()},deadline)
                reply=_recv(self._proc.stdout.fileno(),deadline)
                if (set(reply)!={"kind","epoch","request_id","text"} or reply["kind"]!="result" or
                    reply["epoch"]!=self.epoch or reply["request_id"]!=rid or type(reply["text"]) is not str or
                    not reply["text"] or len(reply["text"].encode("utf-8"))>_MAX_TEXT):
                    raise ValueError()
                return reply["text"]
        except Exception:
            self._failed=True
            try: self.close()
            except Exception: pass
            raise RuntimeError("Parakeet child exchange failed") from None

    def close(self):
        # Publish revocation before signalling; never wait on _io while holding
        # _guard because an exchange checks alive while holding _io.
        with self._close_lock:
            with self._guard:
                self._closed = True
                p = self._proc
                pgid = getattr(self, "_proc_group", None)
            if p is None:
                return
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
                        raise RuntimeError("Parakeet child shutdown unconfirmed") from None
            if p.poll() is None:
                raise RuntimeError("Parakeet child shutdown unconfirmed")
            # Child death makes any blocked exchange finish. Closing descriptors
            # is serialized, but bounded process reap never depends on this lock.
            if self._io.acquire(timeout=2):
                try:
                    if p.stdin and not p.stdin.closed:
                        p.stdin.close()
                    if p.stdout and not p.stdout.closed:
                        p.stdout.close()
                finally:
                    self._io.release()
            else:
                raise RuntimeError("Parakeet child pipe cleanup unconfirmed")
