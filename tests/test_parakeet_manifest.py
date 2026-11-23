"""Manifest/preflight and lifecycle contracts, with no native dependencies."""
import asyncio
from dataclasses import asdict, replace
import hashlib
import sys
import threading

import pytest

from tsumgi.contracts import ResourceShape, WorkerSpec
from tsumgi.runtime import RuntimeDeploymentSpec, RuntimeHostFacts
from tsumgi.runtime.contracts import RuntimeCompatibilityContext, RuntimeHealthState
from tsumgi.runtime.deployment import managed_runtime_from_deployment, provider_from_deployment
from tsumgi.runtime.providers import parakeet_manifest as manifest
from tsumgi.runtime.providers import parakeet_managed as managed
from parakeet_fixtures import staged_environment

GPU = "GPU-01234567-89ab-cdef-0123-456789abcdef"
SHA = "sha256:" + "1" * 64


@pytest.fixture
def configured(tmp_path, monkeypatch):
    paths = {
        "model_path": tmp_path / "model.nemo",
        "interpreter_path": tmp_path / "venv/bin/python",
        "dependency_lock_path": tmp_path / "dependencies.lock",
    }
    for key, pin in (("model_path", "MODEL_SHA256"), ("interpreter_path", "INTERPRETER_SHA256"),
                     ("dependency_lock_path", "LOCK_SHA256")):
        path = paths[key]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(key.encode())
        path.chmod(0o700 if key == "interpreter_path" else 0o600)
        monkeypatch.setattr(manifest, pin, "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest())
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    base = staged_environment(paths["interpreter_path"])
    cfg = manifest.ParakeetManifestConfig(
        enabled=True, **{k: str(v) for k, v in paths.items()},
        base_python_root=str(base),
        private_home=str(home), gpu_uuid=GPU, rights_review_sha256=SHA,
        validation_evidence_sha256=SHA,
    )
    worker = WorkerSpec(
        "parakeet-test", "single-gpu",
        ResourceShape(gpu_count=1, total_vram_mb=11264, max_single_gpu_vram_mb=11264),
        gpu_uuids=(GPU,), capabilities={"audio.transcribe"},
    )
    host = RuntimeHostFacts(cpu_count=4, host_ram_mb=16384, architecture="x86_64")
    return cfg, worker, host


def test_standard_factory_is_lazy_and_requires_exact_serving(configured, monkeypatch):
    cfg, worker, host = configured
    deployment = RuntimeDeploymentSpec(
        provider_id=manifest.PROVIDER_ID, provider_config=asdict(cfg), demand=manifest.DEMAND,
    )
    def forbidden(*_a, **_kw):
        pytest.fail("construction may not start a process")
    monkeypatch.setattr(managed, "ParakeetProcess", forbidden)
    runtime = managed_runtime_from_deployment(
        deployment, worker=worker, host=host, serving=cfg.serving_declaration(),
    )
    assert isinstance(runtime, managed.ParakeetManagedRuntime)
    assert runtime._state is RuntimeHealthState.STOPPED
    with pytest.raises(ValueError, match="Parakeet manifest"):
        managed_runtime_from_deployment(deployment, worker=worker, host=host)
    assert not {"torch", "torchaudio", "nemo"} & sys.modules.keys()


@pytest.mark.parametrize("enabled", [False, None, 1, "true"])
def test_not_enabled_never_supplies_runtime(configured, enabled):
    cfg, _, _ = configured
    raw = asdict(cfg)
    raw["enabled"] = enabled
    with pytest.raises(ValueError, match="^Parakeet manifest configuration is invalid$"):
        provider_from_deployment(RuntimeDeploymentSpec(
            provider_id=manifest.PROVIDER_ID, provider_config=raw, demand=manifest.DEMAND,
        ))


@pytest.mark.parametrize("case", ["wrong_gpu", "low_vram", "cpu", "capability", "demand", "architecture"])
def test_compatibility_denies_unreviewed_worker(configured, case):
    cfg, worker, host = configured
    demand = manifest.DEMAND
    if case == "wrong_gpu":
        worker = replace(worker, gpu_uuids=("GPU-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",))
    elif case == "low_vram":
        worker = replace(worker, resources=ResourceShape(gpu_count=1, total_vram_mb=8192, max_single_gpu_vram_mb=8192))
    elif case == "cpu":
        worker = replace(worker, resources=ResourceShape(), gpu_uuids=())
    elif case == "capability":
        worker = replace(worker, capabilities={"audio.synthesize"})
    elif case == "demand":
        demand = replace(demand, min_total_vram_mb=0)
    else:
        host = replace(host, architecture="aarch64")
    result = manifest.ParakeetManifestProvider(cfg).compatibility(
        RuntimeCompatibilityContext(worker=worker, host=host, demand=demand))
    assert not result.compatible


@pytest.mark.parametrize("case", ["model", "lock", "interpreter", "symlink", "home_mode", "home_state"])
def test_preflight_is_byte_pinned_and_redacted(configured, case):
    from pathlib import Path
    cfg, _, _ = configured
    cfg.preflight()
    if case in ("model", "lock", "interpreter"):
        path = {"model": cfg.model_path, "lock": cfg.dependency_lock_path,
                "interpreter": cfg.interpreter_path}[case]
        Path(path).write_bytes(b"changed")
    elif case == "symlink":
        link = Path(cfg.model_path).with_name("link.nemo")
        link.symlink_to(cfg.model_path)
        cfg = replace(cfg, model_path=str(link))
    elif case == "home_mode":
        Path(cfg.private_home).chmod(0o755)
    else:
        (Path(cfg.private_home) / "retained").write_bytes(b"state")
    with pytest.raises(ValueError, match="^Parakeet manifest configuration is invalid$"):
        cfg.preflight()


def test_deployment_identity_excludes_local_location_and_gpu(configured):
    cfg, _, _ = configured
    other = replace(cfg, model_path="/other/model.nemo", interpreter_path="/other/venv/bin/python",
                    base_python_root="/other/base-python",
                    dependency_lock_path="/other/deps.lock", private_home="/other/home",
                    gpu_uuid="GPU-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    assert other.serving_declaration() == cfg.serving_declaration()
    assert replace(cfg, rights_review_sha256="sha256:" + "2" * 64).serving_declaration() != cfg.serving_declaration()


class SyntheticProcess:
    def __init__(self, *_a, **_kw):
        self.alive = False
        self.closed = False
        self.entered = threading.Event()
        self.proceed = threading.Event()

    def start(self):
        self.entered.set()
        if not self.proceed.wait(3) or self.closed:
            raise RuntimeError("synthetic interrupted load")
        self.alive = True

    def close(self):
        self.closed = True
        self.alive = False
        self.proceed.set()


async def test_stop_during_native_start_retains_handle_and_denies_reuse(configured, monkeypatch):
    cfg, _, _ = configured
    child = SyntheticProcess()
    monkeypatch.setattr(managed, "ParakeetProcess", lambda *_a, **_kw: child)
    runtime = managed.ParakeetManagedRuntime(config=cfg)
    start = asyncio.create_task(runtime.start())
    try:
        assert await asyncio.to_thread(child.entered.wait, 2)
        assert (await runtime.health()).state is RuntimeHealthState.STARTING
        await asyncio.wait_for(runtime.stop(), 2)
        with pytest.raises(RuntimeError):
            await start
        assert child.closed and not child.alive
        assert not (await runtime.health()).ready
        with pytest.raises(RuntimeError):
            await runtime.start()
        with pytest.raises(RuntimeError):
            runtime.executor()
    finally:
        child.proceed.set()
        await runtime.release()
        await asyncio.gather(start, return_exceptions=True)


async def test_artifact_drift_revokes_even_cached_executor(configured, monkeypatch):
    from pathlib import Path
    from tsumgi.execution import JobExecutionError
    cfg, _, _ = configured
    child = SyntheticProcess()
    child.proceed.set()
    monkeypatch.setattr(managed, "ParakeetProcess", lambda *_a, **_kw: child)
    runtime = managed.ParakeetManagedRuntime(config=cfg)
    try:
        await runtime.start()
        cached = runtime.executor()
        Path(cfg.model_path).write_bytes(b"changed")
        with pytest.raises(JobExecutionError, match="runtime unavailable"):
            await cached.execute_audio(None, None)
        assert not (await runtime.health()).ready
    finally:
        await runtime.release()
    assert child.closed
