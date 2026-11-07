"""#215 B6c Linux /proc acceptance with disposable fake Python TCP child only."""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import sys
import threading
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest
from test_voicevox_managed_runtime import FakeEngine, make_spec

from astrumweaver.runtime.contracts import RuntimeHealthState
from astrumweaver.runtime.providers.voicevox_linux_process import (
    LinuxVoicevoxOwnedProcessControl,
    VoicevoxLinuxLaunchSpec,
    VoicevoxOwnedProcessError,
)
from astrumweaver.runtime.providers.voicevox_managed import (
    VoicevoxOwnedManagedRuntime,
    VoicevoxRuntimeFailure,
)
from astrumweaver.serving import ServingDeploymentDeclaration

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux procfs only")

RUNNER = r"""
import socket
import sys
import time
port=int(sys.argv[1])
sock=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
sock.bind(("127.0.0.1",port))
sock.listen(8)
while True:
    time.sleep(0.03)
"""


def sha(data: bytes) -> str:
    return "sha256:" + sha256(data).hexdigest()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def setup(tmp_path: Path, *, runner_code=RUNNER, port=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    port = port or free_port()
    original = make_spec(tmp_path)
    # The Nix test interpreter can be a small exec-wrapper. Pin the actual
    # kernel executable observed by /proc rather than the bootstrap wrapper,
    # otherwise the child changes ELF inode before its HTTP listener is READY.
    exe = os.path.realpath(os.readlink("/proc/self/exe"))
    exe_sha = sha(Path(exe).read_bytes())
    adapter = replace(original.adapter, engine_artifact_sha256=exe_sha)
    deployment = replace(original.serving.deployment, runtime_artifact_sha256=exe_sha)
    contract = replace(
        original.serving.contracts[0], deployment_revision=deployment.revision
    )
    serving = ServingDeploymentDeclaration(deployment=deployment, contracts=(contract,))
    spec = replace(
        original,
        adapter=adapter,
        serving=serving,
        executable_path=exe,
        owner_uid=os.geteuid(),
        endpoint=f"http://127.0.0.1:{port}",
        startup_timeout_seconds=3.0,
        probe_timeout_seconds=0.7,
        stop_timeout_seconds=1.5,
    )
    runner = tmp_path / "synthetic-listener.py"
    runner.write_text(runner_code)
    pin = VoicevoxLinuxLaunchSpec(
        executable_path=exe,
        fixed_args=("-I", "-u", str(runner), str(port)),
        auxiliary_path=str(runner),
        auxiliary_sha256=sha(runner.read_bytes()),
        stop_timeout_seconds=1.5,
    )
    controller = LinuxVoicevoxOwnedProcessControl(launch_spec=pin, config=spec)
    return spec, pin, runner, controller


async def ready(controller: LinuxVoicevoxOwnedProcessControl, *, seconds=3.0):
    async with asyncio.timeout(seconds):
        while True:
            observation = await controller.inspect()
            if observation is not None:
                return observation
            await asyncio.sleep(0.025)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "nonce",
    [
        pytest.param(None, id="none"),
        pytest.param(0, id="integer"),
        pytest.param(False, id="boolean"),
        pytest.param(b"01234567-89ab-4cde-8123-0123456789ab", id="bytes"),
        pytest.param([], id="list"),
        pytest.param("", id="empty"),
        pytest.param("private-malformed-value", id="malformed"),
        pytest.param("01234567-89AB-4CDE-8123-0123456789AB", id="uppercase"),
        pytest.param("0123456789ab4cde81230123456789ab", id="no-hyphens"),
        pytest.param("01234567-89ab-4cde-8123-0123456789ag", id="non-hex"),
        pytest.param("01234567-89ab-4cde-8123-0123456789ab\n", id="newline"),
        pytest.param("ffffffff-ffff-ffff-ffff-ffffffffffff", id="foreign-valid"),
    ],
)
async def test_unstarted_stop_rejects_unproven_nonce_without_side_effects(
    tmp_path, monkeypatch, nonce
):
    import astrumweaver.runtime.providers.voicevox_linux_process as module

    _cfg, _pin, _runner, controller = setup(tmp_path)
    assert controller._proc is None
    assert controller._nonce is None
    assert controller._verified_nonce is None
    assert controller._aborted_nonce is None
    before = vars(controller).copy()

    def forbidden(*_args, **_kwargs):
        pytest.fail("unstarted stop must not spawn, signal or inspect listeners")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    monkeypatch.setattr(module.os, "killpg", forbidden)
    monkeypatch.setattr(module, "_listener_sockets", forbidden)
    with pytest.raises(VoicevoxOwnedProcessError) as error:
        await controller.stop_owned(nonce=nonce)
    assert str(error.value) == "VOICEVOX owned process unavailable"
    assert vars(controller) == before
    assert not controller._lock.locked()


@pytest.mark.asyncio
async def test_real_child_pid_uid_exec_inode_and_socket_inode_roundtrip(tmp_path):
    cfg, _launch, _runner, controller = setup(tmp_path)
    assert await controller.endpoint_owner() is None
    assert await controller.inspect() is None
    nonce = "01234567-89ab-4cde-8123-0123456789ab"
    await controller.launch(config=cfg, nonce=nonce)
    obs = await ready(controller)
    assert obs.pid > 0 and obs.pid == obs.endpoint_owner_pid
    assert obs.uid == os.geteuid()
    assert obs.executable_sha256 == cfg.adapter.engine_artifact_sha256
    assert obs.model_artifact_sha256 == cfg.adapter.model_artifact_sha256
    assert obs.deployment_revision == cfg.serving.deployment.revision
    assert obs.worker_resource_id == cfg.worker_resource_id
    assert obs.launch_nonce == nonce
    assert await controller.endpoint_owner() == obs.pid
    assert os.getpgid(obs.pid) == obs.pid  # Own isolated process group.
    with pytest.raises(VoicevoxOwnedProcessError):
        await controller.stop_owned(nonce="ffffffff-ffff-ffff-ffff-ffffffffffff")
    assert await controller.inspect() is not None
    proc = controller._proc
    await controller.stop_owned(nonce=nonce)
    assert proc.returncode is not None and controller._proc is None
    assert await controller.inspect() is None
    assert await controller.endpoint_owner() is None
    assert controller._verified_nonce == nonce
    await controller.stop_owned(nonce=nonce)
    with pytest.raises(VoicevoxOwnedProcessError):
        await controller.stop_owned(nonce="ffffffff-ffff-ffff-ffff-ffffffffffff")
    assert controller._verified_nonce == nonce
    assert controller._proc is None


@pytest.mark.asyncio
async def test_existing_foreign_listener_refused_without_killing_foreign_server(
    tmp_path,
):
    with socket.socket() as foreign:
        foreign.bind(("127.0.0.1", 0))
        foreign.listen(2)
        port = foreign.getsockname()[1]
        cfg, _pin, _runner, controller = setup(tmp_path, port=port)
        assert await controller.endpoint_owner() == 0
        nonce = "01234567-89ab-4cde-8123-0123456789ab"
        with pytest.raises(VoicevoxOwnedProcessError):
            await controller.launch(config=cfg, nonce=nonce)
        assert controller._proc is None
        assert await controller.endpoint_owner() == 0
        # Socket is still listening. The process controller did not adopt,
        # signal, stop or free the foreign listener.
        foreign.settimeout(0.5)
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            accepted, _addr = foreign.accept()
            accepted.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "defect", ["bad_model", "bad_engine", "bad_runner", "symlink_runner", "wrong_uid"]
)
async def test_tampered_artifacts_and_wrong_uid_never_spawn_child(tmp_path, defect):
    cfg, pin, runner, ctl = setup(tmp_path)
    if defect == "bad_model":
        Path(cfg.model_artifact_path).write_bytes(b"stale model")
    elif defect == "bad_engine":
        bad = "sha256:" + "a" * 64
        adapter = replace(cfg.adapter, engine_artifact_sha256=bad)
        dep = replace(cfg.serving.deployment, runtime_artifact_sha256=bad)
        contract = replace(cfg.serving.contracts[0], deployment_revision=dep.revision)
        cfg = replace(
            cfg,
            adapter=adapter,
            serving=ServingDeploymentDeclaration(deployment=dep, contracts=(contract,)),
        )
        ctl = LinuxVoicevoxOwnedProcessControl(launch_spec=pin, config=cfg)
    elif defect == "bad_runner":
        runner.write_bytes(b"modified runner")
    elif defect == "symlink_runner":
        target = tmp_path / "runner-backup"
        target.write_bytes(runner.read_bytes())
        runner.unlink()
        runner.symlink_to(target)
    elif defect == "wrong_uid":
        cfg = replace(cfg, owner_uid=os.geteuid() + 1)
        ctl = LinuxVoicevoxOwnedProcessControl(launch_spec=pin, config=cfg)
    nonce = "01234567-89ab-4cde-8123-0123456789ab"
    with pytest.raises(VoicevoxOwnedProcessError):
        await ctl.launch(config=cfg, nonce=nonce)
    assert ctl._proc is None
    assert await ctl.endpoint_owner() is None


@pytest.mark.asyncio
async def test_runtime_can_observe_owned_process_but_fake_voice_probes_only(tmp_path):
    cfg, _pin, _runner, ctl = setup(tmp_path)
    runtime = VoicevoxOwnedManagedRuntime(
        config=cfg,
        process=ctl,
        engine=FakeEngine(),
    )
    assert (await runtime.health()).state is RuntimeHealthState.STOPPED
    await runtime.start()
    assert (await runtime.health()).ready is True
    assert await ctl.endpoint_owner() is not None
    await runtime.release()
    assert ctl._proc is None
    assert (await runtime.health()).state is RuntimeHealthState.STOPPED


@pytest.mark.asyncio
async def test_running_generation_mutation_is_denied_and_never_targets_foreign_pid(
    tmp_path,
):
    cfg, _pin, _runner, ctl = setup(tmp_path)
    nonce = "01234567-89ab-4cde-8123-0123456789ab"
    await ctl.launch(config=cfg, nonce=nonce)
    obs = await ready(ctl)
    original = ctl._start_ticks
    ctl._start_ticks = original + 1
    with pytest.raises(VoicevoxOwnedProcessError):
        await ctl.inspect()
    assert await ctl.endpoint_owner() == 0
    with pytest.raises(VoicevoxOwnedProcessError):
        await ctl.stop_owned(nonce=nonce)
    assert ctl._proc.returncode is None
    assert ctl._proc.pid == obs.pid
    ctl._start_ticks = original  # Test-only restoration of known owned identity.
    await ctl.stop_owned(nonce=nonce)


@pytest.mark.asyncio
async def test_exit_or_model_drift_prevents_new_ready_and_cleans_exact_child(tmp_path):
    cfg, _pin, _runner, ctl = setup(tmp_path)
    nonce = "01234567-89ab-4cde-8123-0123456789ab"
    await ctl.launch(config=cfg, nonce=nonce)
    await ready(ctl)
    Path(cfg.model_artifact_path).write_bytes(b"changed bytes")
    with pytest.raises(VoicevoxOwnedProcessError):
        await ctl.inspect()
    # Model drift does not permit killing an unowned PID. Verify the same child
    # before signaling, via executable inode, proc generation and own group.
    await ctl.stop_owned(nonce=nonce)
    assert ctl._proc is None

    cfg, _pin, _runner, ctl = setup(tmp_path / "second")
    await ctl.launch(config=cfg, nonce=nonce)
    await ready(ctl)
    proc = ctl._proc
    proc.kill()
    await proc.wait()
    with pytest.raises(VoicevoxOwnedProcessError):
        await ctl.inspect()
    await ctl.stop_owned(nonce=nonce)
    assert ctl._proc is None


@pytest.mark.asyncio
async def test_early_exit_is_reaped_without_forge_success(tmp_path):
    cfg, _pin, _runner, ctl = setup(
        tmp_path,
        runner_code="import sys\nsys.exit(7)\n",
    )
    nonce = "01234567-89ab-4cde-8123-0123456789ab"
    try:
        await ctl.launch(config=cfg, nonce=nonce)
    except VoicevoxOwnedProcessError:
        assert ctl._proc is None
    else:
        async with asyncio.timeout(3):
            while ctl._proc.returncode is None:
                await asyncio.sleep(0.02)
        with pytest.raises(VoicevoxOwnedProcessError):
            await ctl.inspect()
        await ctl.stop_owned(nonce=nonce)
    assert ctl._proc is None


@pytest.mark.asyncio
async def test_async_worker_loop_remains_responsive_during_proc_inspections(tmp_path):
    cfg, _pin, _runner, ctl = setup(tmp_path)
    nonce = "01234567-89ab-4cde-8123-0123456789ab"
    await ctl.launch(config=cfg, nonce=nonce)
    await ready(ctl)
    counter = 0

    async def heartbeat():
        nonlocal counter
        for _ in range(15):
            counter += 1
            await asyncio.sleep(0.012)

    tick = asyncio.create_task(heartbeat())
    for _ in range(12):
        assert await ctl.endpoint_owner() == ctl._proc.pid
        assert await ctl.inspect() is not None
    await tick
    assert counter == 15
    await ctl.stop_owned(nonce=nonce)


@pytest.mark.parametrize(
    "mutation",
    [
        {"executable_path": "relative"},
        {"fixed_args": ("ok", "\n")},
        {"fixed_args": ("ok", "x" * 4097)},
        {"fixed_args": tuple("x" for _ in range(25))},
        {"auxiliary_path": "/tmp/x"},
        {"stop_timeout_seconds": float("nan")},
    ],
)
def test_operator_launch_config_denies_unsafe_or_unpinned_command(mutation):
    pin = VoicevoxLinuxLaunchSpec(executable_path="/usr/bin/python", fixed_args=())
    with pytest.raises(ValueError, match="launch configuration is invalid"):
        replace(pin, **mutation)


@pytest.mark.asyncio
async def test_owned_executable_with_public_wildcard_listener_cannot_be_qualified(
    tmp_path,
):
    # Same exact child binary and owned FD are NOT enough: a listener bound
    # to 0.0.0.0 exposes native /audio_query and must not qualify as loopback.
    runner = RUNNER.replace('("127.0.0.1",port)', '("0.0.0.0",port)')
    cfg, _pin, _script, ctl = setup(tmp_path, runner_code=runner)
    nonce = "01234567-89ab-4cde-8123-0123456789ab"
    await ctl.launch(config=cfg, nonce=nonce)
    try:
        async with asyncio.timeout(3):
            while await ctl.endpoint_owner() is None:
                await asyncio.sleep(0.02)
        assert await ctl.endpoint_owner() == 0
        with pytest.raises(VoicevoxOwnedProcessError):
            await ctl.inspect()
    finally:
        await ctl.stop_owned(nonce=nonce)
    assert ctl._proc is None


@pytest.mark.asyncio
async def test_owned_pid_reexec_to_unpinned_script_does_not_count_as_verified_engine(
    tmp_path,
):
    # Linux PID, start ticks, UID, /proc/<pid>/exe and TCP FD can all remain
    # unchanged across exec of a different script under the same interpreter.
    # Reject a re-exec of the pinned auxiliary script even if the port remains
    # owned by the exact same child generation.
    foreign = tmp_path / "foreign.py"
    foreign.write_text("import time\nwhile True: time.sleep(.05)\n")
    script = f"""
import os,socket,sys
port=int(sys.argv[1])
s=socket.socket()
s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
s.set_inheritable(True)
s.bind(("127.0.0.1",port))
s.listen(5)
os.execv(sys.executable,[sys.executable,"-I","-u",{str(foreign)!r},str(port)])
"""
    cfg, _pin, _runner, ctl = setup(tmp_path, runner_code=script)
    nonce = "01234567-89ab-4cde-8123-0123456789ab"
    await ctl.launch(config=cfg, nonce=nonce)
    try:
        proc = ctl._proc
        async with asyncio.timeout(3):
            while (
                foreign.name.encode()
                not in Path(f"/proc/{proc.pid}/cmdline").read_bytes()
            ):
                await asyncio.sleep(0.02)
        assert await ctl.endpoint_owner() == 0
        with pytest.raises(VoicevoxOwnedProcessError):
            await ctl.inspect()
    finally:
        await ctl.stop_owned(nonce=nonce)
    assert ctl._proc is None


@pytest.mark.asyncio
async def test_sigterm_ignoring_owned_child_is_sigkilled_and_reaped(tmp_path):
    runner = RUNNER.replace(
        "while True:",
        "import signal\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nwhile True:",
    )
    cfg, pin, _script, _ctl = setup(tmp_path, runner_code=runner)
    pin = replace(pin, stop_timeout_seconds=0.3)
    ctl = LinuxVoicevoxOwnedProcessControl(launch_spec=pin, config=cfg)
    nonce = "01234567-89ab-4cde-8123-0123456789ab"
    await ctl.launch(config=cfg, nonce=nonce)
    await ready(ctl)
    proc = ctl._proc
    await ctl.stop_owned(nonce=nonce)
    assert ctl._proc is None
    assert proc.returncode == -signal.SIGKILL
    assert await ctl.endpoint_owner() is None


@pytest.mark.asyncio
async def test_simultaneous_lookups_and_stop_do_not_requalify_old_child(tmp_path):
    cfg, _pin, _script, ctl = setup(tmp_path)
    nonce = "01234567-89ab-4cde-8123-0123456789ab"
    await ctl.launch(config=cfg, nonce=nonce)
    await ready(ctl)
    requests = [
        asyncio.create_task(ctl.inspect()),
        asyncio.create_task(ctl.endpoint_owner()),
    ]
    await ctl.stop_owned(nonce=nonce)
    await asyncio.gather(*requests, return_exceptions=True)
    assert await ctl.inspect() is None
    assert await ctl.endpoint_owner() is None
    assert ctl._proc is None


@pytest.mark.parametrize(
    "modification",
    [
        {"auxiliary_path": None, "auxiliary_sha256": None},
        {"fixed_args": ("-I", "-u", "-c", "import os; os.system('true')")},
        {"fixed_args": ("-I", "-m", "unreviewed_module")},
        {"fixed_args": ("-I", "-u", "/tmp/unpinned_module.py")},
    ],
)
def test_unpinned_dynamic_python_argv_is_never_a_qualified_launch(
    tmp_path,
    modification,
):
    """Known source-integrity gap: binary hash != executed Python code hash."""
    _cfg, pin, _runner, _ctl = setup(tmp_path)
    with pytest.raises(ValueError, match="launch configuration is invalid"):
        replace(pin, **modification)


@pytest.mark.asyncio
async def test_cancel_during_delayed_spawn_does_not_hang_indefinitely(
    tmp_path,
    monkeypatch,
):
    """Cancel at subprocess-creation boundary; release fake delay for cleanup."""
    cfg, launch_spec, _runner, controller = setup(tmp_path)
    nonce = "01234567-89ab-4cde-8123-0123456789ab"
    launch_spec = replace(launch_spec, stop_timeout_seconds=0.2)
    controller = LinuxVoicevoxOwnedProcessControl(
        launch_spec=launch_spec,
        config=cfg,
    )
    created = asyncio.Event()
    unblock = asyncio.Event()
    original = asyncio.create_subprocess_exec

    async def delayed_spawn(*args, **kwargs):
        created.set()
        await unblock.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
    task = asyncio.create_task(controller.launch(config=cfg, nonce=nonce))
    await asyncio.wait_for(created.wait(), 2)
    task.cancel()
    _done, pending = await asyncio.wait({task}, timeout=0.3)
    try:
        assert not pending, (
            "launch cancellation remained blocked in subprocess creation"
        )
    finally:
        unblock.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)
        if controller._pending_spawn_cleanup is not None:
            await asyncio.wait_for(controller._pending_spawn_cleanup, 3)
        if controller._proc is not None:
            await controller.stop_owned(nonce=nonce)
    assert controller._proc is None
    assert controller._aborted is True
    with pytest.raises(VoicevoxOwnedProcessError):
        await controller.launch(config=cfg, nonce=nonce)


@pytest.mark.asyncio
async def test_stop_with_pending_spawn_is_bounded_and_never_claims_cleanup(
    tmp_path,
    monkeypatch,
):
    """A delayed spawn keeps the same controller permanently unavailable."""
    cfg, pin, _runner, controller = setup(tmp_path)
    nonce = "01234567-89ab-4cde-8123-0123456789ab"
    pin = replace(pin, stop_timeout_seconds=0.15)
    controller = LinuxVoicevoxOwnedProcessControl(launch_spec=pin, config=cfg)
    created = asyncio.Event()
    unblock = asyncio.Event()
    actual = asyncio.create_subprocess_exec

    async def delayed(*args, **kwargs):
        created.set()
        await unblock.wait()
        return await actual(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed)
    task = asyncio.create_task(controller.launch(config=cfg, nonce=nonce))
    await asyncio.wait_for(created.wait(), 2)
    task.cancel()
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 0.3)
    try:
        with pytest.raises(VoicevoxOwnedProcessError):
            await asyncio.wait_for(
                controller.stop_owned(nonce=nonce),
                timeout=0.4,
            )
        with pytest.raises(VoicevoxOwnedProcessError):
            await controller.launch(config=cfg, nonce=nonce)
        with pytest.raises(VoicevoxOwnedProcessError):
            await controller.endpoint_owner()
    finally:
        unblock.set()
        await asyncio.wait_for(controller._pending_spawn_cleanup, timeout=3)
    await controller.stop_owned(nonce=nonce)
    assert controller._proc is None and controller._aborted


@pytest.mark.asyncio
async def test_cancelled_spawn_with_unknown_creation_failure_never_reports_cleanup(
    tmp_path,
    monkeypatch,
):
    """No handle returned is not proof that subprocess creation had no effects."""
    cfg, pin, _runner, _controller = setup(tmp_path)
    nonce = "01234567-89ab-4cde-8123-0123456789ab"
    controller = LinuxVoicevoxOwnedProcessControl(
        launch_spec=replace(pin, stop_timeout_seconds=0.2),
        config=cfg,
    )
    started = asyncio.Event()
    unblock = asyncio.Event()

    async def unknown_spawn_failure(*_args, **_kwargs):
        started.set()
        await unblock.wait()
        raise OSError("test-only spawn may have begun; ownership uncertain")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", unknown_spawn_failure)
    task = asyncio.create_task(controller.launch(config=cfg, nonce=nonce))
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 1)
    unblock.set()
    try:
        with pytest.raises(VoicevoxOwnedProcessError):
            await asyncio.wait_for(controller._pending_spawn_cleanup, timeout=2)
        with pytest.raises(VoicevoxOwnedProcessError):
            await controller.stop_owned(nonce=nonce)
    finally:
        assert controller._aborted and controller._proc is None


def test_same_port_foreign_interface_listener_is_not_ignored(monkeypatch):
    """One owned loopback listener plus a different IPv4 interface is ambiguous.

    Use synthetic procfs only: never open a real non-loopback test port.
    """
    from astrumweaver.runtime.providers.voicevox_linux_process import (
        _listener_sockets,
    )

    port = 50521
    header = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"

    def row(index, address, inode):
        return (
            f"{index:4d}: {address}:{port:04X} 00000000:0000 0A "
            f"00000000:00000000 00:00000000 00000000 1000 0 {inode} 1 0000000000000000\n"
        )

    original_read_text = Path.read_text

    def fake_proc_net(self, *args, **kwargs):
        if str(self) == "/proc/net/tcp":
            return header + row(0, "0100007F", 123456) + row(1, "010C0A0A", 234567)
        if str(self) == "/proc/net/tcp6":
            return header
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_proc_net)
    entries = _listener_sockets(port)
    assert len(entries) == 2, "foreign interface listener on same port was ignored"
    assert {(kind, address) for kind, address, _inode in entries} == {
        ("tcp", "0100007F"),
        ("tcp", "010C0A0A"),
    }


@pytest.mark.asyncio
async def test_owned_loopback_plus_synthetic_other_interface_denies_ready(
    tmp_path,
    monkeypatch,
):
    """Keep real child on loopback; add only a synthetic non-loopback proc row.

    No actual network-facing socket is opened by this test.
    """
    cfg, _pin, _runner, ctl = setup(tmp_path)
    nonce = "01234567-89ab-4cde-8123-0123456789ab"
    await ctl.launch(config=cfg, nonce=nonce)
    await ready(ctl)
    owned_pid = ctl._proc.pid
    assert await ctl.endpoint_owner() == owned_pid

    port = int(cfg.endpoint.rsplit(":", 1)[1])
    original_read_text = Path.read_text

    def fake_extra_listener(self, *args, **kwargs):
        raw = original_read_text(self, *args, **kwargs)
        if str(self) == "/proc/net/tcp":
            raw += (
                f"999: 010C0A0A:{port:04X} 00000000:0000 0A "
                "00000000:00000000 00:00000000 00000000 "
                "1000 0 123400001 1 0000000000000000\n"
            )
        return raw

    try:
        with monkeypatch.context() as m:
            m.setattr(Path, "read_text", fake_extra_listener)
            assert await ctl.endpoint_owner() == 0
            with pytest.raises(VoicevoxOwnedProcessError):
                await ctl.inspect()
    finally:
        await ctl.stop_owned(nonce=nonce)
    assert ctl._proc is None


@pytest.mark.asyncio
async def test_exec_replacement_denies_ready_but_stops_same_owned_child_generation(
    tmp_path,
):
    """Changed executable is not READY; known spawned child must still be reaped.

    A Nix/Python launcher or a script can exec into a different ELF image with
    the same PID/UID/birth tick/process group. Readiness must remain strict, but
    refusal to reap this direct child would leak an owned process.
    """
    import shutil

    replacement = shutil.which("sleep")
    if replacement is None:
        pytest.skip("no disposable sleep executable")
    script = (
        "import os,time\n"
        "time.sleep(.20)\n"
        f"os.execv({replacement!r},[{replacement!r},'30'])\n"
    )
    cfg, _pin, _runner, ctl = setup(tmp_path, runner_code=script)
    nonce = "01234567-89ab-4cde-8123-0123456789ab"
    await ctl.launch(config=cfg, nonce=nonce)
    proc = ctl._proc
    try:
        async with asyncio.timeout(3):
            while True:
                observed = os.stat(f"/proc/{proc.pid}/exe")
                if (observed.st_dev, observed.st_ino) != ctl._identity[:2]:
                    break
                await asyncio.sleep(0.03)
        # Never infer readiness for an unreviewed replacement image.
        with pytest.raises(VoicevoxOwnedProcessError):
            await ctl.inspect()
        assert await ctl.endpoint_owner() is None
    finally:
        # Still the same direct child handle, UID, start ticks and PGID:
        # safe to terminate even though its executable image has changed.
        await ctl.stop_owned(nonce=nonce)
    assert ctl._proc is None
    assert proc.returncode is not None


@pytest.mark.asyncio
async def test_different_elf_exec_preserving_original_listen_fd_denies_ready_and_reaps(
    tmp_path,
    monkeypatch,
):
    """Changing executable with an inherited listener must revoke READY.

    The same known direct child keeps its PID/UID/starttime/process group and
    bound 127.0.0.1 socket across exec into a different ELF. The listener
    must not become a qualified engine, but stop must still reap that child.
    """
    import shutil

    import astrumweaver.runtime.providers.voicevox_linux_process as process_module
    from astrumweaver.runtime.providers.voicevox_linux_process import (
        _listener_sockets,
        _owned_group_generation,
        _process_socket_inodes,
    )

    replacement = shutil.which("sleep")
    if replacement is None:
        pytest.skip("no disposable alternative executable available")

    gate = tmp_path / "reexec-now"
    script = f"""
import os, socket, sys, time
port = int(sys.argv[1])
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.set_inheritable(True)
s.bind(("127.0.0.1", port))
s.listen(5)
while not os.path.isfile({str(gate)!r}):
    time.sleep(.015)
os.execv({replacement!r}, [{replacement!r}, "30"])
"""
    cfg, _pin, _runner, controller = setup(tmp_path, runner_code=script)
    nonce = "01234567-89ab-4cde-8123-0123456789ab"
    await controller.launch(config=cfg, nonce=nonce)
    proc = controller._proc
    port = int(cfg.endpoint.rsplit(":", 1)[1])
    try:
        observed = await ready(controller)
        assert observed.pid == proc.pid
        assert await controller.endpoint_owner() == proc.pid
        original_image = controller._identity
        original_generation = controller._start_ticks
        before_listeners = await asyncio.to_thread(_listener_sockets, port)
        assert len(before_listeners) == 1
        original_socket_inode = before_listeners[0][2]

        gate.write_text("reexec")
        async with asyncio.timeout(4):
            while True:
                exe = os.stat(f"/proc/{proc.pid}/exe")
                if (exe.st_dev, exe.st_ino) != original_image[:2]:
                    break
                await asyncio.sleep(0.025)

        # The foreign binary retained the exact old socket FD and address.
        listeners = await asyncio.to_thread(_listener_sockets, port)
        assert len(listeners) == 1
        kind, address, inode = listeners[0]
        assert kind == "tcp" and address == "0100007F"
        assert inode == original_socket_inode
        assert inode in await asyncio.to_thread(_process_socket_inodes, proc.pid)
        assert await asyncio.to_thread(
            _owned_group_generation,
            proc.pid,
            cfg.owner_uid,
            original_generation,
        )

        # Image drift revokes READY even if the socket owner and PID match.
        assert await controller.endpoint_owner() == 0
        with pytest.raises(VoicevoxOwnedProcessError):
            await controller.inspect()
        # Prove the executable-image check independently from argv checking:
        # even if a bug mistakenly accepted the changed command line, the
        # swapped ELF alone must still prevent admission.
        with monkeypatch.context() as patch:
            patch.setattr(process_module, "_proc_fixed_command", lambda *_args: True)
            assert await controller.endpoint_owner() == 0
            with pytest.raises(VoicevoxOwnedProcessError):
                await controller.inspect()
    finally:
        # Even though READY was revoked, it remains our known spawned child.
        await controller.stop_owned(nonce=nonce)
    assert controller._proc is None
    assert proc.returncode is not None
    assert await controller.endpoint_owner() is None
    assert await asyncio.to_thread(_listener_sockets, port) == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupt", ["deadline", "cancel"])
async def test_post_reap_verification_is_bounded_tracked_and_reconciled(
    tmp_path, monkeypatch, interrupt
):
    import astrumweaver.runtime.providers.voicevox_linux_process as module

    cfg, pin, _runner, _ctl = setup(tmp_path)
    cfg = replace(cfg, stop_timeout_seconds=0.2)
    ctl = LinuxVoicevoxOwnedProcessControl(
        launch_spec=replace(pin, stop_timeout_seconds=0.2), config=cfg
    )
    runtime = VoicevoxOwnedManagedRuntime(config=cfg, process=ctl, engine=FakeEngine())
    await runtime.start()
    proc, nonce = ctl._proc, runtime._nonce
    entered = threading.Event()
    unblock = threading.Event()
    original = module._listener_sockets
    calls = 0

    def gated_scan(port):
        nonlocal calls
        calls += 1
        assert proc.returncode is not None
        entered.set()
        assert unblock.wait(3), "test must release its own inspection thread"
        return original(port)

    task = None
    try:
        with monkeypatch.context() as patch:
            patch.setattr(module, "_listener_sockets", gated_scan)
            task = asyncio.create_task(runtime.stop())
            async with asyncio.timeout(1):
                while not entered.is_set():
                    await asyncio.sleep(0.002)
            if interrupt == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                with pytest.raises(VoicevoxRuntimeFailure, match="owned_stop_failed"):
                    await asyncio.wait_for(task, 0.6)
            assert proc.returncode is not None
            assert ctl._proc is proc and ctl._nonce == nonce
            assert runtime._nonce == nonce and runtime._cleanup_uncertain
            assert (await runtime.health()).state is RuntimeHealthState.FAILED
            # A second bounded attempt cannot forget an OS thread still running,
            # nor create an unbounded succession of inspections.
            with pytest.raises(VoicevoxRuntimeFailure):
                await runtime.stop()
            assert calls == 1
            with pytest.raises(VoicevoxRuntimeFailure):
                await runtime.start()
        unblock.set()
        await runtime.stop()
        assert ctl._proc is None and runtime._nonce is None
        assert (await runtime.health()).state is RuntimeHealthState.FAILED
        await runtime.release()
    finally:
        unblock.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        if ctl._proc is not None:
            await ctl.stop_owned(nonce=nonce)


@pytest.mark.asyncio
async def test_direct_controller_deadline_includes_post_reap_scan(
    tmp_path, monkeypatch
):
    import astrumweaver.runtime.providers.voicevox_linux_process as module

    cfg, pin, _runner, _ctl = setup(tmp_path)
    ctl = LinuxVoicevoxOwnedProcessControl(
        launch_spec=replace(pin, stop_timeout_seconds=0.15), config=cfg
    )
    nonce = "01234567-89ab-4cde-8123-0123456789ab"
    await ctl.launch(config=cfg, nonce=nonce)
    await ready(ctl)
    proc = ctl._proc
    unblock = threading.Event()
    original = module._listener_sockets

    def delayed_scan(port):
        assert proc.returncode is not None
        unblock.wait(0.4)
        return original(port)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(module, "_listener_sockets", delayed_scan)
            start = asyncio.get_running_loop().time()
            with pytest.raises(VoicevoxOwnedProcessError):
                await ctl.stop_owned(nonce=nonce)
            assert asyncio.get_running_loop().time() - start < 0.35
            assert ctl._proc is proc
    finally:
        unblock.set()
        if ctl._proc is not None:
            await ctl.stop_owned(nonce=nonce)


@pytest.mark.asyncio
async def test_child_exit_during_generation_check_still_reaps_and_proves_port(
    tmp_path, monkeypatch
):
    import astrumweaver.runtime.providers.voicevox_linux_process as module

    cfg, _pin, _runner, ctl = setup(tmp_path)
    nonce = "01234567-89ab-4cde-8123-0123456789ab"
    await ctl.launch(config=cfg, nonce=nonce)
    await ready(ctl)
    proc = ctl._proc
    original_thread = asyncio.to_thread

    async def exit_at_check(func, *args):
        if func is module._owned_group_generation:
            # Exact test-owned handle only, never a discovered/foreign PID.
            proc.terminate()
            await proc.wait()
            return False
        return await original_thread(func, *args)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(asyncio, "to_thread", exit_at_check)
            await ctl.stop_owned(nonce=nonce)
        assert ctl._proc is None
        assert await ctl.endpoint_owner() is None
        await ctl.stop_owned(nonce=nonce)  # Same verified nonce is idempotent.
    finally:
        if ctl._proc is not None:
            await ctl.stop_owned(nonce=nonce)


@pytest.mark.asyncio
async def test_cancel_during_term_grace_retains_live_owned_child_for_explicit_reap(
    tmp_path, monkeypatch
):
    import astrumweaver.runtime.providers.voicevox_linux_process as module

    runner = RUNNER.replace(
        "while True:",
        "import signal\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nwhile True:",
    )
    cfg, pin, _runner, _ctl = setup(tmp_path, runner_code=runner)
    ctl = LinuxVoicevoxOwnedProcessControl(
        launch_spec=replace(pin, stop_timeout_seconds=0.3), config=cfg
    )
    runtime = VoicevoxOwnedManagedRuntime(config=cfg, process=ctl, engine=FakeEngine())
    await runtime.start()
    proc, nonce = ctl._proc, runtime._nonce
    term_sent = asyncio.Event()
    original_signal = module.os.killpg
    sent = []

    def record_owned_signal(pgid, sig):
        assert pgid == proc.pid
        original_signal(pgid, sig)
        sent.append(sig)
        if sig == signal.SIGTERM:
            term_sent.set()

    task = None
    try:
        with monkeypatch.context() as patch:
            patch.setattr(module.os, "killpg", record_owned_signal)
            task = asyncio.create_task(runtime.stop())
            await asyncio.wait_for(term_sent.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert proc.returncode is None
            assert ctl._proc is proc and ctl._nonce == nonce
            assert (await runtime.health()).state is RuntimeHealthState.FAILED
            with pytest.raises(VoicevoxRuntimeFailure):
                await runtime.start()
            await runtime.release()
        assert signal.SIGKILL in sent
        assert proc.returncode == -signal.SIGKILL
        assert ctl._proc is None
        assert (await runtime.health()).state is RuntimeHealthState.FAILED
    finally:
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        if ctl._proc is not None:
            await ctl.stop_owned(nonce=nonce)


@pytest.mark.asyncio
async def test_post_exit_foreign_rebind_retains_handle_without_foreign_signals(
    tmp_path, monkeypatch
):
    import astrumweaver.runtime.providers.voicevox_linux_process as module

    cfg, _pin, _runner, ctl = setup(tmp_path)
    nonce = "01234567-89ab-4cde-8123-0123456789ab"
    await ctl.launch(config=cfg, nonce=nonce)
    await ready(ctl)
    proc = ctl._proc
    proc.terminate()
    await proc.wait()
    port = int(cfg.endpoint.rsplit(":", 1)[1])
    try:
        with socket.socket() as foreign:
            foreign.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            foreign.bind(("127.0.0.1", port))
            foreign.listen(1)

            def forbidden_signal(*_args):
                pytest.fail("no process/group may be signaled after owned child exit")

            with monkeypatch.context() as patch:
                patch.setattr(module.os, "killpg", forbidden_signal)
                with pytest.raises(VoicevoxOwnedProcessError):
                    await ctl.stop_owned(nonce=nonce)
                assert ctl._proc is proc and ctl._nonce == nonce
                with pytest.raises(VoicevoxOwnedProcessError):
                    await ctl.launch(config=cfg, nonce=nonce)
                assert foreign.getsockname()[1] == port
        await ctl.stop_owned(nonce=nonce)
        assert ctl._proc is None
        with pytest.raises(VoicevoxOwnedProcessError):
            await ctl.launch(config=cfg, nonce=nonce)
    finally:
        if ctl._proc is not None:
            await ctl.stop_owned(nonce=nonce)


@pytest.mark.asyncio
async def test_failed_late_spawn_reap_retries_owned_handle_after_listener_clears(
    tmp_path, monkeypatch
):
    """One failed terminal late-reap must not poison same-nonce reconciliation."""
    import astrumweaver.runtime.providers.voicevox_linux_process as module

    cfg, pin, _runner, _controller = setup(tmp_path)
    nonce = "01234567-89ab-4cde-8123-0123456789ab"
    ctl = LinuxVoicevoxOwnedProcessControl(
        launch_spec=replace(pin, stop_timeout_seconds=0.25), config=cfg
    )
    spawn_entered = asyncio.Event()
    unblock_spawn = asyncio.Event()
    foreign = {"listening": False}
    original_spawn = asyncio.create_subprocess_exec
    original_listener = module._listener_sockets

    async def delayed_spawn(*args, **kwargs):
        spawn_entered.set()
        await unblock_spawn.wait()
        proc = await original_spawn(*args, **kwargs)
        foreign["listening"] = True  # Synthetic post-reap foreign port rebind.
        return proc

    def listener_after_reap(port):
        if foreign["listening"]:
            return (("synthetic-foreign-listener",),)
        return original_listener(port)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
    monkeypatch.setattr(module, "_listener_sockets", listener_after_reap)
    launching = asyncio.create_task(ctl.launch(config=cfg, nonce=nonce))
    try:
        await asyncio.wait_for(spawn_entered.wait(), 2)
        launching.cancel()
        await asyncio.wait_for(asyncio.gather(launching, return_exceptions=True), 1)
        unblock_spawn.set()
        with pytest.raises(VoicevoxOwnedProcessError):
            await asyncio.wait_for(ctl._pending_spawn_cleanup, 3)
        proc = ctl._proc
        assert proc is not None and proc.returncode is not None
        assert ctl._aborted and ctl._nonce == nonce
        with pytest.raises(VoicevoxOwnedProcessError):
            await ctl.stop_owned(nonce=nonce)
        assert ctl._proc is proc  # Retain original owned handle while port bound.
        foreign["listening"] = False
        await ctl.stop_owned(nonce=nonce)
        assert ctl._proc is None
        assert ctl._pending_spawn_cleanup is None
        assert ctl._verified_nonce == nonce
        await ctl.stop_owned(nonce=nonce)  # Same verified proof stays idempotent.
        with pytest.raises(VoicevoxOwnedProcessError):
            await ctl.launch(config=cfg, nonce=nonce)
    finally:
        unblock_spawn.set()
        foreign["listening"] = False
        await asyncio.gather(launching, return_exceptions=True)
        if ctl._pending_spawn_cleanup is not None:
            await asyncio.gather(ctl._pending_spawn_cleanup, return_exceptions=True)
        if ctl._proc is not None:
            await ctl._reap_newborn()
