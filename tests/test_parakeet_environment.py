"""Protected-environment admission; never execute the staged fake interpreter."""
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from astrumweaver.runtime.providers import parakeet_environment as environment
from astrumweaver.runtime.providers import parakeet_process as process
from parakeet_fixtures import staged_environment


@pytest.fixture
def installation(tmp_path):
    python = tmp_path / "venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"synthetic interpreter")
    python.chmod(0o700)
    base = staged_environment(python)
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    return SimpleNamespace(
        python=python, base=base, home=home, root=python.parent.parent,
        site=python.parent.parent / "lib/python3.11/site-packages",
    )


def test_protected_installation_is_read_only_and_has_contained_library_paths(installation):
    i = installation
    before = {p: (p.lstat().st_mode, p.lstat().st_mtime_ns)
              for root in (i.root, i.base) for p in root.rglob("*")}
    paths = environment.validate_environment(i.python, i.base)
    assert len(paths) == 4
    assert all(Path(p).is_relative_to(i.root) and Path(p).is_dir() for p in paths)
    assert before == {p: (p.lstat().st_mode, p.lstat().st_mtime_ns) for p in before}


@pytest.mark.parametrize("target", ["site", "package", "cfg", "stdlib", "cuda"])
def test_writable_dependency_rejected_before_any_spawn(installation, monkeypatch, target):
    i = installation
    dependency = i.site / "fixture.py"
    dependency.write_text("# staged package\n")
    paths = {
        "site": i.site, "package": dependency, "cfg": i.root / "pyvenv.cfg",
        "stdlib": i.base / "lib/python3.11/encodings/__init__.py",
        "cuda": i.site / "nvidia/cudnn/lib/synthetic.so",
    }
    path = paths[target]
    path.chmod(path.stat().st_mode | 0o020)
    popen = Mock(side_effect=AssertionError("untrusted environment must not spawn"))
    monkeypatch.setattr(process.subprocess, "Popen", popen)
    monkeypatch.setattr(process, "_PY_SHA256", hashlib.sha256(i.python.read_bytes()).hexdigest())
    child = process.ParakeetProcess(
        str(i.python), "/synthetic/model.nemo", "GPU-01234567-89ab-cdef-0123-456789abcdef",
        private_home=str(i.home), base_python_root=str(i.base), epoch="synthetic",
    )
    with pytest.raises(ValueError, match="^Parakeet installed environment is invalid$"):
        child.start()
    popen.assert_not_called()
    assert not child.alive


@pytest.mark.parametrize("text", [
    "import os\n", "import\tos\n", "/external/site-packages\n",
    "../../../../../external\n", "missing-package-directory\n",
])
def test_executable_or_external_pth_rejected(installation, text):
    i = installation
    (i.site / "injected.pth").write_text(text)
    with pytest.raises(ValueError, match="^Parakeet installed environment is invalid$"):
        environment.validate_environment(i.python, i.base)


def test_only_contained_declarative_pth_and_library_links_are_accepted(installation):
    i = installation
    (i.site / "extra").mkdir()
    (i.site / "extra.pth").write_text("# explicit in-tree import directory\nextra\n")
    library = i.site / "nvidia/cudnn/lib"
    (library / "alias.so").symlink_to("synthetic.so")
    (i.root / "lib64").symlink_to("lib")
    environment.validate_environment(i.python, i.base)


@pytest.mark.parametrize("target", ["package", "library", "pth", "base"])
def test_external_symlink_is_rejected(installation, tmp_path, target):
    i = installation
    outside = tmp_path / "external"
    outside.mkdir()
    (outside / "fixture").write_bytes(b"external")
    paths = {
        "package": i.site / "outside",
        "library": i.site / "nvidia/cudnn/lib/redirect.so",
        "pth": i.site / "redirect.pth",
        "base": i.base / "lib/python3.11/redirect",
    }
    paths[target].symlink_to(outside / "fixture")
    with pytest.raises(ValueError):
        environment.validate_environment(i.python, i.base)


@pytest.mark.parametrize("change", [
    "include-system-site-packages = true",
    "home = /unreviewed/bin",
    "executable = /unreviewed/bin/python3.11",
    "version = 3.12.1",
    "unrecognized = setting",
    "home = duplicate",
    "Home = /unreviewed/bin",
])
def test_pyvenv_cfg_cannot_select_other_python_or_site(installation, change):
    i = installation
    cfg = i.root / "pyvenv.cfg"
    if change == "home = duplicate" or change.startswith(("unrecognized", "Home")):
        cfg.write_text(cfg.read_text() + change + "\n")
    else:
        key = change.split(" = ")[0]
        cfg.write_text("\n".join(
            change if line.startswith(key + " = ") else line
            for line in cfg.read_text().splitlines()
        ) + "\n")
    with pytest.raises(ValueError):
        environment.validate_environment(i.python, i.base)


@pytest.mark.parametrize("relative", [
    "bin/pyvenv.cfg", "bin/python._pth", "lib/python3.11/libpython3.11._pth",
])
def test_alternate_path_configuration_is_rejected(installation, relative):
    (installation.root / relative).write_text("unreviewed")
    with pytest.raises(ValueError):
        environment.validate_environment(installation.python, installation.base)


def test_scan_has_entry_depth_time_and_text_bounds(installation, monkeypatch):
    i = installation
    for name, limit in (("_MAX_ENTRIES", 1), ("_MAX_DEPTH", 1), ("_SCAN_SECONDS", 0)):
        with monkeypatch.context() as patch:
            patch.setattr(environment, name, limit)
            with pytest.raises(ValueError):
                environment.validate_environment(i.python, i.base)
    (i.site / "oversize.pth").write_bytes(b"#" * (environment._MAX_PTH + 1))
    with pytest.raises(ValueError):
        environment.validate_environment(i.python, i.base)


def test_nonregular_dependency_is_rejected_without_opening(installation):
    i = installation
    os.mkfifo(i.site / "not-a-package.pth", mode=0o600)
    with pytest.raises(ValueError):
        environment.validate_environment(i.python, i.base)


def test_foreign_owner_is_rejected_without_permission_mutation(installation, monkeypatch):
    original = environment._protected

    def changed_owner(info, *, link=False):
        if info.st_ino == installation.python.stat().st_ino:
            info = SimpleNamespace(st_uid=os.geteuid() + 1234, st_mode=info.st_mode)
        return original(info, link=link)

    monkeypatch.setattr(environment, "_protected", changed_owner)
    with pytest.raises(ValueError):
        environment.validate_environment(installation.python, installation.base)
