"""Tiny stdlib-only installed-environment fixtures; not executable runtimes."""
from pathlib import Path


def staged_environment(interpreter: Path) -> Path:
    root = interpreter.parent.parent
    base = root.with_name("base-python")
    (base / "bin").mkdir(parents=True)
    binary = base / "bin/python3.11"
    binary.write_bytes(interpreter.read_bytes())
    binary.chmod(0o700)
    encodings = base / "lib/python3.11/encodings"
    encodings.mkdir(parents=True)
    (encodings / "__init__.py").write_text("# synthetic stdlib\n")
    site = root / "lib/python3.11/site-packages"
    for name in ("cublas", "cudnn", "cuda_runtime", "cuda_nvrtc"):
        library = site / "nvidia" / name / "lib"
        library.mkdir(parents=True)
        (library / "synthetic.so").write_bytes(b"not a native library")
    (root / "pyvenv.cfg").write_text(
        f"home = {base}/bin\ninclude-system-site-packages = false\n"
        f"version = 3.11.14\nexecutable = {base}/bin/python3.11\n"
    )
    return base
