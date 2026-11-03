"""#215 B6c Linux /proc acceptance with disposable fake Python TCP child only."""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import sys
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
    exe = os.path.realpath(sys.executable)
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
