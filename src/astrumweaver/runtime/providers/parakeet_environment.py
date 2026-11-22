"""Read-only protection checks for an explicitly staged CPython installation.

This is not a package-byte attestation or an OS sandbox. Root and the dedicated
Worker identity are trusted; they must not change the installation during use.
No interpreter, .pth code, package, or native library is executed by this check.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat
import time

_ERROR = "Parakeet installed environment is invalid"
_MAX_ENTRIES = 200_000
_MAX_DEPTH = 32
_SCAN_SECONDS = 30.0
_MAX_CONFIG = 8192
_MAX_PTH = 64 * 1024
_NVIDIA_LIBRARIES = ("cublas", "cudnn", "cuda_runtime", "cuda_nvrtc")


def _invalid() -> ValueError:
    return ValueError(_ERROR)


def _identity(info: os.stat_result) -> tuple:
    return (info.st_dev, info.st_ino, info.st_uid, info.st_gid, info.st_mode,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _protected(info: os.stat_result, *, link: bool = False) -> None:
    if (info.st_uid not in (0, os.geteuid())
            or (not link and info.st_mode & 0o022)
            or info.st_mode & (stat.S_ISUID | stat.S_ISGID)):
        raise _invalid()


def _canonical(path: Path) -> None:
    value = str(path)
    if (not path.is_absolute() or value == "/" or value.startswith("//")
            or os.path.normpath(value) != value or ":" in value
            or len(value.encode("utf-8")) > 4096
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
            or path.resolve(strict=True) != path):
        raise _invalid()
    _protected(path.lstat())
    for parent in path.parents:
        info = parent.lstat()
        sticky_tmp = (str(parent) in ("/tmp", "/var/tmp") and info.st_uid == 0
                      and info.st_mode & stat.S_ISVTX)
        if not stat.S_ISDIR(info.st_mode):
            raise _invalid()
        if not sticky_tmp:
            _protected(info)


def _read_text(path: Path, maximum: int) -> str:
    before = path.lstat()
    _protected(before)
    if not stat.S_ISREG(before.st_mode) or not 0 <= before.st_size <= maximum:
        raise _invalid()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as stream:
        if _identity(os.fstat(fd)) != _identity(before):
            raise _invalid()
        raw = stream.read(maximum + 1)
        if (len(raw) != before.st_size or _identity(os.fstat(fd)) != _identity(before)
                or _identity(path.lstat()) != _identity(before)):
            raise _invalid()
    return raw.decode("utf-8")


def _venv_config(root: Path, base: Path) -> None:
    values = {}
    # CPython venv's declarative fields only; no alternate virtualenv launchers.
    for line in _read_text(root / "pyvenv.cfg", _MAX_CONFIG).splitlines():
        if not line.strip():
            continue
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or key in values or key not in {
            "home", "include-system-site-packages", "version", "executable", "command", "prompt",
        }:
            raise _invalid()
        values[key] = value
    if (values.get("home") != str(base / "bin")
            or values.get("include-system-site-packages") != "false"
            or values.get("version") != "3.11.14"
            or values.get("executable", str(base / "bin/python3.11")) != str(base / "bin/python3.11")):
        raise _invalid()
    # Reject alternate interpreter path configuration before the interpreter runs.
    for parent in (root, root / "bin", base, base / "bin"):
        for name in ("pyvenv.cfg", "python._pth", "python3._pth", "python3.11._pth"):
            candidate = parent / name
            if candidate != root / "pyvenv.cfg" and os.path.lexists(candidate):
                raise _invalid()


def _pth(path: Path, root: Path) -> None:
    for line in _read_text(path, _MAX_PTH).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Executable startup hooks can import arbitrary paths before our child
        # validates anything. This lane requires an explicitly staged environment
        # without them; it never silently rewrites/removes installed .pth files.
        if (line.startswith(("import ", "import\t")) or Path(line).is_absolute()
                or ":" in line or any(ord(c) < 32 for c in line)):
            raise _invalid()
        target = (path.parent / line).resolve(strict=True)
        if not target.is_relative_to(root) or not target.is_dir():
            raise _invalid()


def _scan(roots: tuple[Path, ...]) -> None:
    remaining = _MAX_ENTRIES
    deadline = time.monotonic() + _SCAN_SECONDS
    for root in roots:
        root_info = root.lstat()
        pending = [(root, 0)]
        while pending:
            directory, depth = pending.pop()
            if depth > _MAX_DEPTH or time.monotonic() >= deadline:
                raise _invalid()
            before = directory.lstat()
            _protected(before)
            if not stat.S_ISDIR(before.st_mode) or before.st_dev != root_info.st_dev:
                raise _invalid()
            with os.scandir(directory) as entries:
                for entry in entries:
                    remaining -= 1
                    if remaining < 0 or time.monotonic() >= deadline:
                        raise _invalid()
                    path = Path(entry.path)
                    if path.name.endswith("._pth"):
                        raise _invalid()
                    info = entry.stat(follow_symlinks=False)
                    link = stat.S_ISLNK(info.st_mode)
                    _protected(info, link=link)
                    if info.st_dev != root_info.st_dev:
                        raise _invalid()
                    if link:
                        # Only relative links inside this same protected tree.
                        # Do not follow directory links while walking.
                        raw = os.readlink(path)
                        lexical = Path(os.path.normpath(path.parent / raw))
                        target = path.resolve(strict=True)
                        if (Path(raw).is_absolute() or not lexical.is_relative_to(root)
                                or not target.is_relative_to(root)):
                            raise _invalid()
                        # .pth is read separately and must never be a symlink.
                        if path.suffix == ".pth":
                            raise _invalid()
                    elif stat.S_ISDIR(info.st_mode):
                        pending.append((path, depth + 1))
                    elif stat.S_ISREG(info.st_mode):
                        if path.suffix == ".pth":
                            _pth(path, root)
                    else:
                        raise _invalid()  # sockets, devices, FIFOs are not code
            if _identity(directory.lstat()) != _identity(before):
                raise _invalid()


def validate_environment(interpreter: Path, base_python_root: Path) -> tuple[str, ...]:
    """Check protected installation trees and return validated CUDA search dirs."""
    try:
        interpreter, base = Path(interpreter), Path(base_python_root)
        root = interpreter.parent.parent
        if interpreter.parts[-2:] != ("bin", "python"):
            raise _invalid()
        for path in (interpreter, root, base):
            _canonical(path)
        if (root.is_relative_to(base) or base.is_relative_to(root)
                or not stat.S_ISREG(interpreter.lstat().st_mode)):
            raise _invalid()
        site = root / "lib/python3.11/site-packages"
        libraries = tuple(site / "nvidia" / name / "lib" for name in _NVIDIA_LIBRARIES)
        for directory in (root / "bin", base / "bin", base / "lib/python3.11", site, *libraries):
            _canonical(directory)
            if not directory.is_dir():
                raise _invalid()
        for file in (base / "bin/python3.11", base / "lib/python3.11/encodings/__init__.py"):
            _canonical(file)
            if not file.is_file():
                raise _invalid()
        _venv_config(root, base)
        _scan((root, base))
        return tuple(str(path) for path in libraries)
    except (OSError, ValueError, RuntimeError):
        raise _invalid() from None
