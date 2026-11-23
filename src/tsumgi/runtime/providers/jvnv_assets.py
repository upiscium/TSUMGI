"""Fail-closed preflight for the locally staged JVNV CPU assets."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

INTERPRETER_SHA256 = "sha256:e3cc9523fbfbd83a0fbc51fa568868e77e06388480297a7ece177ce491bbe1b9"
LOCK_SHA256 = "sha256:e640656757996b2b944b89e48b7bd8a7d0cb0111481603488fb3e082fddc73a0"
MODEL_SHA256 = "sha256:a90fa6c9444d9235c9ec4db99daf7c5c6a21cc26ca141b4c48455d66a3257d01"
CONFIG_SHA256 = "sha256:d310da5c1e9b095a833591b4bc07209631d6edece263da88e597aaf7f9984d3d"
STYLES_SHA256 = "sha256:1f959bb45ed0922efc31ff24e9147253814f42cb1d2d1e2bb10391a9df368489"
WEIGHT_FILENAME = "jvnv-F1-jp_e160_s14000.safetensors"

_ERROR = "JVNV staged assets are invalid"
_MAX_FILE = 1_318_453_704
_MAX_LOCK = 8 * 1024 * 1024
_DICT_CORE = {"sys.dic", "unk.dic", "matrix.bin", "char.bin"}
_DICT_FILE_LIMIT = 160 * 1024**2
_DICT_TOTAL_LIMIT = 256 * 1024**2
_BERT_SIZES = {
    "model.safetensors": 1_318_453_704,
    "config.json": 895,
    "tokenizer_config.json": 520,
    "vocab.txt": 88_151,
    "special_tokens_map.json": 125,
}
_MODEL_FILES = (WEIGHT_FILENAME, "config.json", "style_vectors.npy")
_BERT_BLOBS = {
    "model.safetensors": ("2630f547d018524a7b03506a42c700cbac49e29bdc441845b0615bfb3b5d74d2", None),
    "config.json": (None, "9fb6b0ac2ec49b6556e58b5ed9492eb33166714d"),
    "tokenizer_config.json": (None, "8ab2175580e45760875557201e5543019ca3039b"),
    "vocab.txt": (None, "4e0fdefcccde8187db648276f62a3e79bb3350de"),
    "special_tokens_map.json": (None, "a8b3208c2884c4efb86e49300fdd3dc877220cdf"),
}


def _fail() -> None:
    raise ValueError(_ERROR)


def _canonical(path: str) -> Path:
    if type(path) is not str or not path or "\x00" in path or not os.path.isabs(path):
        _fail()
    p = Path(path)
    if os.path.normpath(path) != path or str(p.resolve(strict=True)) != path:
        _fail()
    return p


def _safe_ancestors(path: Path) -> None:
    for item in path.parents:
        try:
            st = item.lstat()
        except OSError:
            _fail()
        if stat.S_ISLNK(st.st_mode):
            _fail()
        if not stat.S_ISDIR(st.st_mode):
            _fail()
        sticky_tmp = (
            str(item) in ("/tmp", "/var/tmp")
            and st.st_uid == 0
            and bool(st.st_mode & stat.S_ISVTX)
        )
        if st.st_uid not in (0, os.geteuid()) or (
            st.st_mode & 0o022 and not sticky_tmp
        ):
            _fail()


def _safe_directory(path: Path) -> None:
    _safe_ancestors(path / ".directory-check")


def _identity(st: os.stat_result) -> tuple[int, ...]:
    return (
        st.st_dev, st.st_ino, st.st_mode, st.st_nlink, st.st_uid, st.st_gid,
        st.st_size, st.st_mtime_ns, st.st_ctime_ns,
    )


def _regular(path: Path, maximum: int, exact: int | None = None) -> int:
    try:
        _safe_ancestors(path)
        st = path.lstat()
        if (not stat.S_ISREG(st.st_mode) or not 0 < st.st_size <= maximum
                or st.st_uid not in (0, os.geteuid()) or st.st_mode & 0o022
                or (exact is not None and st.st_size != exact)):
            _fail()
        return st.st_size
    except OSError:
        _fail()


def _digest(path: Path, algorithm: str, maximum: int, exact: int | None = None) -> tuple[str, int]:
    size = _regular(path, maximum, exact)
    h = hashlib.new(algorithm)
    if algorithm == "sha1":
        h.update(f"blob {size}\0".encode("ascii"))
    count = 0
    try:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size != size:
                _fail()
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                count += len(chunk)
                if count > size:
                    _fail()
                h.update(chunk)
            after = os.fstat(stream.fileno())
            if _identity(before) != _identity(after):
                _fail()
    except OSError:
        _fail()
    if count != size:
        _fail()
    return h.hexdigest(), size


def _dictionary_files(directory: Path) -> tuple[Path, ...]:
    _safe_directory(directory)
    files = []
    total = 0
    with os.scandir(directory) as entries:
        for ent in entries:
            if len(files) >= 32 or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", ent.name) is None:
                _fail()
            f = directory / ent.name
            total += _regular(f, _DICT_FILE_LIMIT)
            if total > _DICT_TOTAL_LIMIT:
                _fail()
            files.append(f)
    if not _DICT_CORE <= {p.name for p in files}:
        _fail()
    return tuple(sorted(files))


def dictionary_inventory_sha256(directory: str) -> str:
    """Identity of all filenames and bytes, not an implicit approval of those bytes."""
    try:
        entries = {p.name: _digest(p, "sha256", _DICT_FILE_LIMIT)[0]
                   for p in _dictionary_files(_canonical(directory))}
        raw = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("ascii")
        return "sha256:" + hashlib.sha256(raw).hexdigest()
    except (OSError, RuntimeError, ValueError):
        raise ValueError(_ERROR) from None


@dataclass(frozen=True, slots=True, repr=False)
class JvnvInputs:
    private_home: str
    dependency_lock_path: str
    dictionary_directory: str
    dictionary_sha256: str

    def preflight(self, model_directory: str, interpreter_path: str) -> None:
        try:
            model = _canonical(model_directory)
            home = _canonical(self.private_home)
            lock = _canonical(self.dependency_lock_path)
            dictionary = _canonical(self.dictionary_directory)
            interpreter = _canonical(interpreter_path)
            if model.name != "jvnv-F1-jp":
                _fail()
            bert = model.parent / "bert"
            for d in (model, bert):
                _safe_directory(d)
                if not d.is_dir() or d.is_symlink():
                    _fail()
            for directory, expected in ((model, set(_MODEL_FILES)), (bert, set(_BERT_BLOBS))):
                with os.scandir(directory) as entries:
                    actual = set()
                    for ent in entries:
                        if ent.name not in expected:
                            _fail()
                        actual.add(ent.name)
                if actual != expected:
                    _fail()
            for name in _MODEL_FILES:
                _regular(model / name, _MAX_FILE)
            for name, (sha256, sha1) in _BERT_BLOBS.items():
                got, _ = _digest(
                    bert / name, "sha256" if sha256 else "sha1",
                    _MAX_FILE, _BERT_SIZES[name],
                )
                if got != (sha256 or sha1):
                    _fail()
            got, _ = _digest(lock, "sha256", _MAX_LOCK)
            if got != LOCK_SHA256.removeprefix("sha256:"):
                _fail()
            if interpreter.name != "python" or interpreter.parent.name != "bin":
                _fail()
            venv = interpreter.parent.parent
            _safe_ancestors(interpreter)
            _regular(interpreter, _MAX_FILE)
            if home == venv or home in venv.parents or venv in home.parents:
                _fail()
            st = home.lstat()
            _safe_directory(home)
            if not stat.S_ISDIR(st.st_mode) or stat.S_IMODE(st.st_mode) != 0o700 or st.st_uid != os.geteuid():
                _fail()
            if any(home.iterdir()):
                _fail()
            paths = (model, bert, lock, interpreter, dictionary)
            if any(p == home or p in home.parents or home in p.parents for p in paths):
                _fail()
            if lock in (model, bert) or model in lock.parents or bert in lock.parents:
                _fail()
            if (type(self.dictionary_sha256) is not str or not re.fullmatch(r"sha256:[0-9a-f]{64}", self.dictionary_sha256)
                    or dictionary_inventory_sha256(str(dictionary)) != self.dictionary_sha256):
                _fail()
        except (OSError, RuntimeError, ValueError):
            raise ValueError(_ERROR) from None

    def watched_paths(self, model_directory: str) -> tuple[str, ...]:
        try:
            model = _canonical(model_directory)
            bert = model.parent / "bert"
            _safe_ancestors(bert)
            dictionary = _canonical(self.dictionary_directory)
            return (str(bert), *(str(bert / name) for name in _BERT_BLOBS),
                    str(_canonical(self.dependency_lock_path)), str(dictionary),
                    *(str(f) for f in _dictionary_files(dictionary)))
        except (OSError, RuntimeError, ValueError):
            raise ValueError(_ERROR) from None
