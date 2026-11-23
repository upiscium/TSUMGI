"""Parakeet manifest admission through the real Worker daemon with a synthetic backend."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import httpx
import pytest
from parakeet_fixtures import staged_environment

from astrumweaver.audio import ArtifactSpool
from astrumweaver.audio.input_channel import AudioInputChannel
from astrumweaver.control import InMemoryControlRepository
from astrumweaver.control.api import create_app
from astrumweaver.gateway.audio import (
    AudioGatewayProfile, AudioProfileCatalog, LogicalServingProfile, resolve_profile,
)
from astrumweaver.gateway.audio_ingress import AudioIngressLimits
from astrumweaver.gateway.audio_transcriptions_api import (
    AudioTranscriptionsLimits, create_audio_transcriptions_router,
)
from astrumweaver.runtime import RuntimeHostFacts
from astrumweaver.runtime.deployment import RuntimeDeploymentSpec
from astrumweaver.runtime.providers import parakeet_manifest
from astrumweaver.runtime.providers import parakeet_managed
from astrumweaver.worker import daemon
from astrumweaver.worker.client import ControlClient
from test_audio_transcriptions_api import WAV, multipart


RIGHTS = "sha256:" + "1" * 64
EVIDENCE = "sha256:" + "2" * 64
TEXT = "これは合成された日本語です。"


class SyntheticProcess:
    """Native boundary double; manifest, daemon and registration stay real."""

    def __init__(self, _python, _model, _gpu, *, private_home, epoch, base_python_root):
        self.private_home = Path(private_home)
        self.epoch = epoch
        self.started = Event()
        self.release = Event()
        self.closed = Event()
        self.alive = False
        self.fail_start = False
        self.decode_entered = Event()
        self.decode_release = Event()
        self.decode_finished = Event()
        self.block_decode = False

    def start(self):
        self.started.set()
        if not self.release.wait(3) or self.closed.is_set() or self.fail_start:
            return
        self.alive = True

    def transcribe(self, _audio):
        self.decode_entered.set()
        try:
            if self.block_decode:
                self.decode_release.wait(3)
            if self.closed.is_set():
                raise RuntimeError("synthetic child closed")
            return TEXT
        finally:
            self.decode_finished.set()

    def close(self):
        self.alive = False
        self.closed.set()
        self.release.set()
        self.decode_release.set()


def _write(path: Path, value: dict) -> str:
    path.write_text(json.dumps(value), encoding="utf-8")
    return str(path)


class _HealthServer:
    should_exit = False

    async def serve(self):
        while not self.should_exit:
            await asyncio.sleep(0.01)


def _setup(tmp_path, monkeypatch):
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    model_file = model_dir / "model.nemo"
    model_file.write_bytes(b"synthetic model")
    interpreter = tmp_path / "venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_bytes(b"synthetic interpreter")
    interpreter.chmod(0o700)
    base = staged_environment(interpreter)
    lock = tmp_path / "requirements.lock"
    lock.write_bytes(b"synthetic lock")
    home = tmp_path / "private"
    home.mkdir(mode=0o700)
    for name, path in (
        ("MODEL_SHA256", model_file),
        ("INTERPRETER_SHA256", interpreter),
        ("LOCK_SHA256", lock),
    ):
        monkeypatch.setattr(
            parakeet_manifest, name,
            "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
        )
    cfg = parakeet_manifest.ParakeetManifestConfig(
        enabled=True, model_path=str(model_file), interpreter_path=str(interpreter),
        base_python_root=str(base),
        dependency_lock_path=str(lock), private_home=str(home),
        gpu_uuid="GPU-01234567-89ab-cdef-0123-456789abcdef",
        rights_review_sha256=RIGHTS, validation_evidence_sha256=EVIDENCE,
    )
    declaration = cfg.serving_declaration()
    deployment = RuntimeDeploymentSpec(
        provider_id=parakeet_manifest.PROVIDER_ID,
        provider_config=asdict(cfg),
        demand=parakeet_manifest.DEMAND,
    )
    runtime_path = _write(tmp_path / "runtime.json", deployment.to_dict())
    serving_path = _write(tmp_path / "serving.json", declaration.to_dict())
    control = tmp_path / "worker.toml"
    control.write_text(
        '[worker]\nid = "parakeet-test"\nclass = "gpu"\n'
        'control_url = "http://127.0.0.1:18080"\ncapabilities = ["audio.transcribe"]\n'
        'gpu_count = 1\ngpu_uuids = ["GPU-01234567-89ab-cdef-0123-456789abcdef"]\n'
        'total_vram_mb = 11264\nmax_single_gpu_vram_mb = 11264\n'
        'max_concurrency = 1\ngpu_preflight = true\n'
        'telemetry_interval_seconds = 0\npoll_interval_seconds = 0.01\n'
        'heartbeat_interval_seconds = 0.05\n'
        f'[runtime]\nmanifest = "{runtime_path}"\nstartup_timeout_seconds = 4\n'
        f'[serving]\nmanifest = "{serving_path}"\n', encoding="utf-8",
    )
    processes = []
    monkeypatch.setattr(parakeet_managed, "ParakeetProcess", lambda *a, **kw: (
        processes.append(SyntheticProcess(*a, **kw)) or processes[-1]
    ))
    monkeypatch.setattr(daemon, "discover_runtime_host_facts", lambda: RuntimeHostFacts(
        cpu_count=4, host_ram_mb=16384, architecture="x86_64",
    ))
    monkeypatch.setattr(daemon, "require_exact_gpu_set", lambda *_a, **_kw: None)
    monkeypatch.setattr(daemon, "require_isolated_gpu_access", lambda *_a, **_kw: None)
    monkeypatch.setenv("ASTRUMWEAVER_WORKER_TOKEN", "synthetic-token")
    repo = InMemoryControlRepository(lease_seconds=5)
    contract = declaration.contracts[0]
    logical = LogicalServingProfile(
        "parakeet-test", declaration.deployment.revision, contract.revision,
        contract.capability, contract.operation_schema,
    )
    resolved = resolve_profile(logical, contract, declaration.deployment)
    profile = AudioGatewayProfile(resolved, "model-stt", ())
    catalog = AudioProfileCatalog((profile,))
    input_root = tmp_path / "audio-input"
    input_root.mkdir(mode=0o700)
    input_channel = AudioInputChannel(
        repo, ArtifactSpool(input_root, max_bytes=1 << 20, max_total_bytes=2 << 20,
                            max_artifacts=2, ttl_seconds=60),
        max_bytes=1 << 20, max_decoded_bytes=960000, max_duration_seconds=30,
    )
    app = create_app(
        repo, client_token="synthetic-client", worker_token="synthetic-token",
        audio_input_channel=input_channel,
    )
    app.include_router(create_audio_transcriptions_router(
        catalog, repo, input_channel, client_auth="bearer", client_token="audio-token",
        ingress_limits=AudioIngressLimits(
            request_bytes=2 << 20, input_bytes=1 << 20, file_bytes=1 << 20,
            metadata_bytes=4096, part_header_bytes=4096, total_header_bytes=8192,
            receive_events=32, idle_timeout_seconds=1, total_timeout_seconds=3,
        ),
        limits=AudioTranscriptionsLimits(
            text_bytes=16384, request_timeout_seconds=8,
            max_pending_admissions=1, poll_interval_seconds=0.005,
        ),
    ))
    statuses = []

    async def observe(scope, receive, send):
        async def capture(message):
            if scope.get("path") == "/v1/workers/register" and message["type"] == "http.response.start":
                statuses.append(message["status"])
            await send(message)
        await app(scope, receive, capture)

    monkeypatch.setattr(daemon, "ControlClient", lambda *a, **kw: ControlClient(
        *a, **kw, transport=httpx.ASGITransport(app=observe),
    ))
    monkeypatch.setattr(daemon.uvicorn, "Config", lambda *_a, **_kw: object())
    monkeypatch.setattr(daemon.uvicorn, "Server", lambda _c: _HealthServer())
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda *_a: None)
    return SimpleNamespace(config=control, declaration=declaration, repo=repo,
                           app=app, statuses=statuses, processes=processes, home=home,
                           profile=profile, input_channel=input_channel)


async def _cancel(task):
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_registration_waits_for_real_managed_runtime_readiness(tmp_path, monkeypatch):
    h = _setup(tmp_path, monkeypatch)
    task = asyncio.create_task(daemon.run_worker(str(h.config)))
    try:
        async with asyncio.timeout(4):
            while not h.processes or not h.processes[0].started.is_set():
                if task.done():
                    await task
                await asyncio.sleep(0.005)
        assert not h.statuses
        assert not h.repo.list_workers()
        h.processes[0].release.set()
        async with asyncio.timeout(4):
            while not h.statuses:
                if task.done():
                    await task
                await asyncio.sleep(0.005)
        assert h.statuses == [201]
        worker, = h.repo.list_workers()
        assert worker.serving.deployment_revision == h.declaration.deployment.revision
    finally:
        for process in h.processes:
            process.release.set()
        await _cancel(task)


@pytest.mark.asyncio
async def test_transcription_waits_for_ready_worker_then_completes_once(tmp_path, monkeypatch):
    h = _setup(tmp_path, monkeypatch)
    task = asyncio.create_task(daemon.run_worker(str(h.config)))
    headers = {"Authorization": "Bearer audio-token",
               "Content-Type": "multipart/form-data; boundary=test"}
    body = multipart(model=h.profile.profile_id, data=WAV)
    try:
        async with httpx.AsyncClient(
            base_url="http://synthetic", transport=httpx.ASGITransport(app=h.app),
        ) as client, asyncio.timeout(6):
            before = await client.post("/v1/audio/transcriptions", headers=headers, content=body)
            assert before.status_code == 503
            assert not h.repo.list_jobs() and not h.statuses
            while not h.processes or not h.processes[0].started.is_set():
                if task.done():
                    await task
                await asyncio.sleep(0.005)
            assert not h.statuses and not h.repo.list_workers()
            h.processes[0].release.set()
            while not h.statuses:
                if task.done():
                    await task
                await asyncio.sleep(0.005)
            response = await client.post("/v1/audio/transcriptions", headers=headers, content=body)
            assert response.status_code == 200, response.text
            assert response.json() == {"text": TEXT}
            record, = h.repo.list_jobs()
            assert record.attempts == 1
            assert record.status.value == "succeeded"
            assert record.claimed_runtime_instance_epoch == (
                h.repo.list_workers()[0].serving.runtime_instance.epoch
            )
    finally:
        for process in h.processes:
            process.close()
        await _cancel(task)


@pytest.mark.asyncio
async def test_failed_process_start_never_registers(tmp_path, monkeypatch):
    h = _setup(tmp_path, monkeypatch)
    factory = parakeet_managed.ParakeetProcess

    def failed(*args, **kwargs):
        process = factory(*args, **kwargs)
        process.fail_start = True
        return process

    monkeypatch.setattr(parakeet_managed, "ParakeetProcess", failed)
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(daemon.run_worker(str(h.config)), 4)
    assert h.processes and h.processes[0].closed.is_set()
    assert not h.statuses and not h.repo.list_workers()


@pytest.mark.asyncio
async def test_cancelled_decode_closes_backend_and_cannot_reuse_it(tmp_path, monkeypatch):
    h = _setup(tmp_path, monkeypatch)
    task = asyncio.create_task(daemon.run_worker(str(h.config)))
    request = None
    headers = {"Authorization": "Bearer audio-token",
               "Content-Type": "multipart/form-data; boundary=test"}
    body = multipart(model=h.profile.profile_id, data=WAV)
    try:
        async with httpx.AsyncClient(
            base_url="http://synthetic", transport=httpx.ASGITransport(app=h.app),
        ) as client, asyncio.timeout(6):
            while not h.processes:
                if task.done():
                    await task
                await asyncio.sleep(0.005)
            h.processes[0].release.set()
            while not h.statuses:
                if task.done():
                    await task
                await asyncio.sleep(0.005)
            process = h.processes[0]
            process.block_decode = True
            request = asyncio.create_task(client.post(
                "/v1/audio/transcriptions", headers=headers, content=body,
            ))
            while not process.decode_entered.is_set():
                if request.done():
                    pytest.fail(f"request returned before decode: {request.result().status_code}")
                await asyncio.sleep(0.005)
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)
            async with asyncio.timeout(3):
                while not process.closed.is_set() or not process.decode_finished.is_set():
                    await asyncio.sleep(0.005)
            assert all(record.status.value != "succeeded" for record in h.repo.list_jobs())
            with pytest.raises(RuntimeError):
                # A cancelled native decode revokes this runtime; it is not reusable.
                process.transcribe(WAV)
    finally:
        if request is not None and not request.done():
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)
        for process in h.processes:
            process.close()
        await _cancel(task)


@pytest.mark.parametrize("case", [
    "disabled", "omitted", "unknown", "trial", "gpu_count", "gpu_uuid",
    "serving", "slots", "model_pin", "lock_pin", "low_vram", "preflight",
    "writable_dependency", "executable_pth", "base_python_pin",
])
@pytest.mark.asyncio
async def test_invalid_parakeet_admission_never_registers(tmp_path, monkeypatch, case):
    h = _setup(tmp_path, monkeypatch)
    runtime_path = tmp_path / "runtime.json"
    raw = json.loads(runtime_path.read_text())
    if case in ("disabled", "omitted", "unknown"):
        config = raw["provider_config"]
        if case == "disabled":
            config["enabled"] = False
        elif case == "omitted":
            del config["enabled"]
        elif case == "unknown":
            config["unreviewed_option"] = True
        _write(runtime_path, raw)
    elif case == "trial":
        control = h.config.read_text()
        h.config.write_text(
            control.replace(f'manifest = "{runtime_path}"',
                            f'manifest = "{runtime_path}"\nparakeet_trial = true'),
        )
    elif case in ("gpu_count", "gpu_uuid", "slots", "low_vram", "preflight"):
        control = h.config.read_text()
        if case == "gpu_count":
            control = control.replace("gpu_count = 1", "gpu_count = 2")
        elif case == "gpu_uuid":
            control = control.replace(
                "GPU-01234567-89ab-cdef-0123-456789abcdef",
                "GPU-ffffffff-ffff-ffff-ffff-ffffffffffff",
            )
        elif case == "slots":
            control = control.replace("max_concurrency = 1", "max_concurrency = 2")
        elif case == "low_vram":
            control = control.replace("total_vram_mb = 11264", "total_vram_mb = 11263")
        else:
            control = control.replace("gpu_preflight = true", "gpu_preflight = false")
        h.config.write_text(control)
    elif case == "serving":
        (tmp_path / "serving.json").write_text("{}", encoding="utf-8")
    elif case == "model_pin":
        (tmp_path / "model/model.nemo").write_bytes(b"changed")
    elif case == "lock_pin":
        (tmp_path / "requirements.lock").write_bytes(b"changed")
    elif case == "writable_dependency":
        (tmp_path / "venv/lib/python3.11/site-packages").chmod(0o777)
    elif case == "executable_pth":
        (tmp_path / "venv/lib/python3.11/site-packages/hook.pth").write_text("import os\n")
    elif case == "base_python_pin":
        (tmp_path / "base-python/bin/python3.11").write_bytes(b"changed")
    with pytest.raises((RuntimeError, ValueError)):
        await daemon.run_worker(str(h.config))
    assert not h.processes and not h.statuses and not h.repo.list_workers()
