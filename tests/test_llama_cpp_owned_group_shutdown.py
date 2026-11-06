"""Real disposable process-tree tests: no GPU, no systemd or production process."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import signal
import sys

import pytest

from astrumweaver.runtime.providers.llama_cpp import (
    LlamaCppLaunchPolicy,
    LlamaCppSplitMode,
    LlamaCppSubprocessController,
)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX session only")


def fixture_script(tmp_path: Path) -> Path:
    script = tmp_path / "synthetic-llama-server"
    script.write_text(
        f"#!{sys.executable}\n"
        "import os, signal, subprocess, sys, time\n"
        "from pathlib import Path\n"
        "if 'child' in sys.argv:\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "    while True: time.sleep(.05)\n"
        "mode = os.environ.get('TSUMGI_CHILD_MODE', 'plain')\n"
        "if mode in ('fork', 'fork_exit'):\n"
        "    child = subprocess.Popen([sys.executable, __file__, 'child'], "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, "
        "stderr=subprocess.DEVNULL)\n"
        "    Path(os.environ['TSUMGI_CHILD_PIDFILE']).write_text(str(child.pid))\n"
        "    if mode == 'fork_exit': sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n"
        "while True: time.sleep(.05)\n"
    )
    script.chmod(0o700)
    return script


def controller(script: Path) -> LlamaCppSubprocessController:
    return LlamaCppSubprocessController(
        executable=str(script),
        base_url="http://127.0.0.1:29999",
        model_ref="/synthetic/model.gguf",
        model_alias="synthetic",
        gpu_uuids=(),
        context_size=4096,
        launch_policy=LlamaCppLaunchPolicy(
            gpu_layers="auto",
            split_mode=LlamaCppSplitMode.NONE,
            fit=False,
            tensor_split=None,
            fit_target_mb=None,
            main_gpu=0,
            cpu_moe=False,
            n_cpu_moe=None,
            n_cpu_ffn=None,
        ),
        offline=True,
        no_webui=True,
    )


async def until(predicate, *, timeout: float = 5.0) -> None:
    end = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > end:
            raise AssertionError("synthetic subprocess did not reach expected state")
        await asyncio.sleep(0.05)


def is_live(pid: int) -> bool:
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1][0] != "Z"
    except FileNotFoundError:
        return False


@pytest.mark.asyncio
async def test_clean_owned_single_process_stops_and_is_idempotent(tmp_path):
    proc = controller(fixture_script(tmp_path))
    await proc.start()
    leader = proc._process
    assert leader is not None
    assert os.getpgid(leader.pid) == leader.pid
    await proc.stop()
    assert not is_live(leader.pid)
    assert proc._process is None
    await proc.stop()


@pytest.mark.asyncio
async def test_forked_native_ignoring_term_gets_killed_in_its_owned_group(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("TSUMGI_CHILD_MODE", "fork")
    pidfile = tmp_path / "child.pid"
    monkeypatch.setenv("TSUMGI_CHILD_PIDFILE", str(pidfile))
    proc = controller(fixture_script(tmp_path))
    await proc.start()
    leader = proc._process
    assert leader is not None
    child_pid = None
    try:
        await until(pidfile.exists)
        child_pid = int(pidfile.read_text())
        assert is_live(child_pid)
        assert os.getpgid(child_pid) == leader.pid
        original_wait = proc._wait_group_quiet

        async def accelerated_wait(process, pgid, timeout):
            return await original_wait(process, pgid, min(timeout, 0.4))

        monkeypatch.setattr(proc, "_wait_group_quiet", accelerated_wait)
        await proc.stop()
        assert not is_live(child_pid)
        assert proc._process is None
    finally:
        # Synthetic fixture only, even if an assertion fails. Scope to the
        # newly created session; never send a signal to unrelated processes.
        if proc._process is not None and leader.pid > 1:
            try:
                os.killpg(leader.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await leader.wait()


@pytest.mark.asyncio
async def test_wrapper_exit_does_not_hide_orphaned_native_descendant(tmp_path, monkeypatch):
    monkeypatch.setenv("TSUMGI_CHILD_MODE", "fork_exit")
    child_file = tmp_path / "child.pid"
    monkeypatch.setenv("TSUMGI_CHILD_PIDFILE", str(child_file))
    proc = controller(fixture_script(tmp_path))
    await proc.start()
    leader = proc._process
    assert leader is not None
    try:
        await until(child_file.exists)
        child_pid = int(child_file.read_text())
        await leader.wait()  # The wrapper is gone; the native child remains.
        assert leader.returncode is not None
        assert is_live(child_pid)
        assert os.getpgid(child_pid) == leader.pid
        original_wait = proc._wait_group_quiet

        async def quick_wait(process, pgid, timeout):
            return await original_wait(process, pgid, min(timeout, 0.4))

        monkeypatch.setattr(proc, "_wait_group_quiet", quick_wait)
        await proc.stop()
        assert proc._process is None
        assert not is_live(child_pid)
    finally:
        if proc._process is not None:
            try:
                os.killpg(leader.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.asyncio
async def test_failed_group_cleanup_preserves_ownership_and_reports_failure(tmp_path, monkeypatch):
    proc = controller(fixture_script(tmp_path))

    class Unstoppable:
        pid = 567891
        returncode = None

    owner = Unstoppable()
    proc._process = owner
    sent = []
    monkeypatch.setattr(proc, "_group_exists", lambda _pgid: True)
    monkeypatch.setattr(proc, "_signal_group", lambda pgid, sig: sent.append((pgid, sig)))

    async def never_quiet(_process, _pgid, _timeout):
        return False

    monkeypatch.setattr(proc, "_wait_group_quiet", never_quiet)
    with pytest.raises(RuntimeError, match="process group did not stop"):
        await proc.stop()
    assert sent == [
        (owner.pid, signal.SIGTERM),
        (owner.pid, signal.SIGKILL),
    ]
    assert proc._process is owner  # Fail closed; never release an unverified owner.


@pytest.mark.asyncio
async def test_failed_shutdown_fences_restart_even_if_wrapper_still_running(
    tmp_path, monkeypatch,
):
    proc = controller(fixture_script(tmp_path))

    class Previous:
        pid = 567891
        returncode = None

        async def wait(self):
            return 0

    owner = Previous()
    proc._process = owner
    state = {"exists": True}
    monkeypatch.setattr(proc, "_group_exists", lambda _pgid: state["exists"])
    monkeypatch.setattr(proc, "_signal_group", lambda *_args: None)

    async def failed_wait(_process, _pgid, _timeout):
        return False

    monkeypatch.setattr(proc, "_wait_group_quiet", failed_wait)
    with pytest.raises(RuntimeError, match="process group did not stop"):
        await proc.stop()
    assert proc._stop_incomplete is True

    async def no_new_spawn(*args, **kwargs):
        pytest.fail("cannot spawn or re-admit while stop is incomplete")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", no_new_spawn)
    with pytest.raises(RuntimeError, match="shutdown has not completed"):
        await proc.start()
    assert proc._process is owner

    # An explicit second stop may clear ownership once the old group is
    # independently observed absent and its immediate wrapper has exited.
    state["exists"] = False
    owner.returncode = 0

    async def quiet_wait(_process, _pgid, _timeout):
        return True

    monkeypatch.setattr(proc, "_wait_group_quiet", quiet_wait)
    await proc.stop()
    assert proc._process is None
    assert proc._stop_incomplete is False


@pytest.mark.asyncio
async def test_broken_group_identity_denies_unscoped_termination(tmp_path):
    proc = controller(fixture_script(tmp_path))
    class Broken:
        pid = None
        returncode = None
    proc._process = Broken()
    with pytest.raises(RuntimeError, match="group identity unavailable"):
        await proc.stop()
    assert proc._process is not None


@pytest.mark.asyncio
async def test_exited_wrapper_requires_explicit_stop_before_new_start(tmp_path, monkeypatch):
    proc = controller(fixture_script(tmp_path))

    class Exited:
        pid = 432111
        returncode = 0

        async def wait(self):
            return 0

    previous = Exited()
    proc._process = previous

    async def unexpected_spawn(*args, **kwargs):
        pytest.fail("must not spawn another native process with old owner bound")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", unexpected_spawn)
    with pytest.raises(RuntimeError, match="must be stopped before restart"):
        await proc.start()
    assert proc._process is previous

    monkeypatch.setattr(proc, "_group_exists", lambda pgid: False)
    await proc.stop()
    assert proc._process is None


@pytest.mark.asyncio
async def test_start_and_stop_are_serialized_while_spawn_in_progress(
    tmp_path, monkeypatch,
):
    proc = controller(fixture_script(tmp_path))
    entered = asyncio.Event()
    release_spawn = asyncio.Event()

    class Fake:
        pid = 432111
        returncode = 0

        async def wait(self):
            return 0

    async def delayed_spawn(*args, **kwargs):
        entered.set()
        await release_spawn.wait()
        return Fake()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
    monkeypatch.setattr(proc, "_group_exists", lambda pgid: False)
    starter = asyncio.create_task(proc.start())
    await asyncio.wait_for(entered.wait(), timeout=2)
    stopper = asyncio.create_task(proc.stop())
    await asyncio.sleep(0)
    assert not stopper.done(), "stop must wait for the in-progress spawn"
    release_spawn.set()
    await asyncio.wait_for(asyncio.gather(starter, stopper), timeout=3)
    assert proc._process is None


@pytest.mark.asyncio
async def test_stop_cancellation_sends_owned_kill_without_claiming_release(
    tmp_path, monkeypatch,
):
    proc = controller(fixture_script(tmp_path))

    class Fake:
        pid = 567891
        returncode = None

    owner = Fake()
    proc._process = owner
    entered = asyncio.Event()
    sent = []

    async def blocked_wait(_process, _pgid, _timeout):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(proc, "_group_exists", lambda pgid: True)
    monkeypatch.setattr(proc, "_wait_group_quiet", blocked_wait)
    monkeypatch.setattr(
        proc, "_signal_group", lambda pgid, sig: sent.append((pgid, sig)),
    )
    task = asyncio.create_task(proc.stop())
    await asyncio.wait_for(entered.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sent == [
        (owner.pid, signal.SIGTERM),
        (owner.pid, signal.SIGKILL),
    ]
    assert proc._process is owner
    assert proc._stop_incomplete is True
    with pytest.raises(RuntimeError, match="shutdown has not completed"):
        await proc.start()


@pytest.mark.asyncio
async def test_two_simultaneous_starts_spawn_only_one_owned_process(tmp_path, monkeypatch):
    proc = controller(fixture_script(tmp_path))
    count = 0

    class Fake:
        pid = 432111
        returncode = None

    async def spawn(*args, **kwargs):
        nonlocal count
        count += 1
        await asyncio.sleep(0.01)
        return Fake()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await asyncio.gather(proc.start(), proc.start())
    assert count == 1
    assert proc._process is not None
