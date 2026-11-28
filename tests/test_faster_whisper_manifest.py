"""Manifest/preflight and lifecycle contracts, with no native dependencies."""
import asyncio
from dataclasses import asdict, replace
import hashlib
from pathlib import Path
import sys
import threading

import pytest

from tsumgi.contracts import ResourceShape, WorkerSpec
from tsumgi.runtime import RuntimeDeploymentSpec, RuntimeHostFacts
from tsumgi.runtime.contracts import RuntimeCompatibilityContext, RuntimeHealthState
from tsumgi.runtime.deployment import managed_runtime_from_deployment, provider_from_deployment
from tsumgi.runtime.providers import faster_whisper_manifest as manifest
from tsumgi.runtime.providers import faster_whisper_managed as managed
from tsumgi.runtime.providers import faster_whisper_native as pins
from parakeet_fixtures import staged_environment

GPU = "GPU-01234567-89ab-cdef-0123-456789abcdef"
SHA = "sha256:" + "1" * 64
MESSAGE = "^Faster-Whisper manifest configuration is invalid$"
FILES = ("config.json", "model.bin", "preprocessor_config.json", "tokenizer.json", "vocabulary.json")


def sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def configured(tmp_path, monkeypatch):
    model = tmp_path / "model"
    model.mkdir()
    for name in FILES:
        (model / name).write_bytes(name.encode())
        (model / name).chmod(0o600)
        monkeypatch.setitem(pins.FILE_SHA256, name, hashlib.sha256(name.encode()).hexdigest())
    interpreter = tmp_path / "venv/bin/python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_bytes(b"interpreter")
    interpreter.chmod(0o700)
    lock = tmp_path / "dependencies.lock"
    lock.write_bytes(b"lock")
    lock.chmod(0o600)
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    base = staged_environment(interpreter)
    cfg = manifest.FasterWhisperManifestConfig(
        enabled=True, model_dir=str(model), interpreter_path=str(interpreter),
        base_python_root=str(base), dependency_lock_path=str(lock), private_home=str(home),
        gpu_uuid=GPU, interpreter_sha256=sha(interpreter), dependency_lock_sha256=sha(lock),
        rights_review_sha256=SHA, validation_evidence_sha256=SHA,
    )
    worker = WorkerSpec(
        "fw-test", "single-gpu",
        ResourceShape(gpu_count=1, total_vram_mb=11264, max_single_gpu_vram_mb=11264),
        gpu_uuids=(GPU,), capabilities={"audio.transcribe"},
    )
    host = RuntimeHostFacts(cpu_count=4, host_ram_mb=16384, architecture="x86_64")
    return cfg, worker, host


def test_pins_are_the_selected_identity():
    # Independent literals: the maintainer-selected model, revision and weights.
    assert manifest.PROVIDER_ID == "faster-whisper-ja"
    assert manifest.MODEL_REF == "deepdml/faster-whisper-large-v3-turbo-ct2"
    assert pins.MODEL_REVISION == "4df90f75321148c3a29a9e2351b7ddf8f5b115a8"
    assert pins.FILE_SHA256["model.bin"] == "e76620f83d5f5b69efd3d87e3dc180c1bd21df9fbebacfd4335e5e1efcc018da"
    assert set(pins.FILE_SHA256) == set(FILES)
    assert pins.COMPUTE_TYPE == "float32"
    assert pins.VERSIONS == {"faster-whisper": "1.2.1", "ctranslate2": "4.9.1",
                             "nvidia-cublas-cu12": "12.9.3.13", "nvidia-cudnn-cu12": "9.28.1.4"}
    assert pins.DECODING == {"beam_size": 5, "language": "ja", "vad_filter": False,
                             "condition_on_previous_text": False}


def test_standard_factory_is_lazy_and_requires_exact_serving(configured, monkeypatch):
    cfg, worker, host = configured
    deployment = RuntimeDeploymentSpec(
        provider_id=manifest.PROVIDER_ID, provider_config=asdict(cfg), demand=manifest.DEMAND,
    )
    def forbidden(*_a, **_kw):
        pytest.fail("construction may not start a process")
    monkeypatch.setattr(managed, "FasterWhisperProcess", forbidden)
    runtime = managed_runtime_from_deployment(
        deployment, worker=worker, host=host, serving=cfg.serving_declaration(),
    )
    assert isinstance(runtime, managed.FasterWhisperManagedRuntime)
    assert runtime._state is RuntimeHealthState.STOPPED
    with pytest.raises(ValueError, match="Faster-Whisper manifest"):
        managed_runtime_from_deployment(deployment, worker=worker, host=host)
    assert not {"faster_whisper", "ctranslate2", "torch", "nemo"} & sys.modules.keys()


@pytest.mark.parametrize("enabled", [False, None, 1, "true"])
def test_not_enabled_never_supplies_runtime(configured, enabled):
    cfg, _, _ = configured
    raw = asdict(cfg)
    raw["enabled"] = enabled
    with pytest.raises(ValueError, match=MESSAGE):
        provider_from_deployment(RuntimeDeploymentSpec(
            provider_id=manifest.PROVIDER_ID, provider_config=raw, demand=manifest.DEMAND,
        ))


def test_omitted_enabled_is_not_opt_in(configured):
    cfg, _, _ = configured
    raw = asdict(cfg)
    del raw["enabled"]
    with pytest.raises(ValueError, match=MESSAGE):
        provider_from_deployment(RuntimeDeploymentSpec(
            provider_id=manifest.PROVIDER_ID, provider_config=raw, demand=manifest.DEMAND,
        ))


@pytest.mark.parametrize("key,value", [
    ("compute_type", "float16"), ("device", "cpu"), ("language", "en"), ("beam_size", 1),
    ("model_ref", "Systran/faster-whisper-large-v3"), ("model_revision", "main"),
    ("vad_filter", True), ("fallback_model", "x"), ("hf_token", "secret"), ("model_path", "/x"),
])
def test_unknown_option_or_client_override_is_rejected(configured, key, value):
    cfg, _, _ = configured
    raw = {**asdict(cfg), key: value}
    with pytest.raises(ValueError, match=MESSAGE):
        provider_from_deployment(RuntimeDeploymentSpec(
            provider_id=manifest.PROVIDER_ID, provider_config=raw, demand=manifest.DEMAND,
        ))


@pytest.mark.parametrize("field", [
    "model_dir", "interpreter_path", "base_python_root", "dependency_lock_path", "private_home",
    "gpu_uuid", "interpreter_sha256", "dependency_lock_sha256", "rights_review_sha256",
    "validation_evidence_sha256",
])
def test_every_pin_is_required(configured, field):
    cfg, _, _ = configured
    raw = asdict(cfg)
    del raw[field]
    with pytest.raises(ValueError, match=MESSAGE):
        provider_from_deployment(RuntimeDeploymentSpec(
            provider_id=manifest.PROVIDER_ID, provider_config=raw, demand=manifest.DEMAND,
        ))


@pytest.mark.parametrize("change", [
    {"model_dir": "relative/model"}, {"model_dir": "/a/../b"}, {"model_dir": "//a"},
    {"interpreter_path": "/x/venv/python3"}, {"gpu_uuid": "GPU-1"},
    {"interpreter_sha256": "sha256:" + "A" * 64}, {"dependency_lock_sha256": "abc"},
    {"rights_review_sha256": "sha256:" + "1" * 63}, {"validation_evidence_sha256": None},
    {"private_home": "/x/model"},
])
def test_syntactic_validation(configured, change):
    cfg, _, _ = configured
    if change.get("private_home") == "/x/model":
        change = {"private_home": str(Path(cfg.model_dir) / "inside")}
    with pytest.raises(ValueError, match=MESSAGE):
        replace(cfg, **change)


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
    result = manifest.FasterWhisperManifestProvider(cfg).compatibility(
        RuntimeCompatibilityContext(worker=worker, host=host, demand=demand))
    assert not result.compatible


def test_other_family_demand_is_not_compatible(configured):
    from tsumgi.runtime.providers import parakeet_manifest
    cfg, worker, host = configured
    result = manifest.FasterWhisperManifestProvider(cfg).compatibility(
        RuntimeCompatibilityContext(worker=worker, host=host, demand=parakeet_manifest.DEMAND))
    assert not result.compatible


def test_setup_and_installation_are_not_supported(configured):
    cfg, worker, host = configured
    provider = manifest.FasterWhisperManifestProvider(cfg)
    context = RuntimeCompatibilityContext(worker=worker, host=host, demand=manifest.DEMAND)
    with pytest.raises(ValueError):
        provider.setup_intent(context)
    with pytest.raises(ValueError):
        provider.create_runtime(context, None)


def test_preflight_accepts_exact_pins(configured):
    cfg, _, _ = configured
    cfg.preflight()
    cfg.identities()


@pytest.mark.parametrize("name", FILES)
def test_stale_model_file_hash_is_rejected(configured, name):
    cfg, _, _ = configured
    cfg.preflight()
    (Path(cfg.model_dir) / name).write_bytes(b"family substitution or stale file")
    with pytest.raises(ValueError, match=MESSAGE):
        cfg.preflight()


@pytest.mark.parametrize("case", ["lock", "interpreter", "base_interpreter"])
def test_runtime_and_lock_pins(configured, case):
    cfg, _, _ = configured
    path = {"lock": cfg.dependency_lock_path, "interpreter": cfg.interpreter_path,
            "base_interpreter": str(Path(cfg.base_python_root) / "bin/python3.11")}[case]
    Path(path).write_bytes(b"changed")
    with pytest.raises(ValueError, match=MESSAGE):
        cfg.preflight()


@pytest.mark.parametrize("case", ["extra_file", "missing_file", "symlink_file", "symlink_dir",
                                  "writable_dir", "directory_instead_of_file", "empty_file"])
def test_model_directory_must_be_exactly_the_pinned_regular_files(configured, tmp_path, case):
    cfg, _, _ = configured
    model = Path(cfg.model_dir)
    if case == "extra_file":
        (model / "README.md").write_text("x")
    elif case == "missing_file":
        (model / "vocabulary.json").unlink()
    elif case == "symlink_file":
        target = tmp_path / "elsewhere.json"
        target.write_bytes((model / "config.json").read_bytes())
        (model / "config.json").unlink()
        (model / "config.json").symlink_to(target)
    elif case == "symlink_dir":
        link = tmp_path / "linked-model"
        link.symlink_to(model)
        cfg = replace(cfg, model_dir=str(link))
    elif case == "writable_dir":
        model.chmod(0o777)
    elif case == "directory_instead_of_file":
        (model / "tokenizer.json").unlink()
        (model / "tokenizer.json").mkdir()
    else:
        (model / "config.json").write_bytes(b"")
    with pytest.raises(ValueError, match=MESSAGE):
        cfg.preflight()


@pytest.mark.parametrize("case", ["mode", "foreign_entry", "foreign_dir", "symlink_run", "file_run",
                                  "home_symlink"])
def test_private_home_parent_policy(configured, tmp_path, case):
    cfg, _, _ = configured
    home = Path(cfg.private_home)
    if case == "mode":
        home.chmod(0o755)
    elif case == "foreign_entry":
        (home / "retained").write_bytes(b"state")
    elif case == "foreign_dir":
        (home / "cache").mkdir()
    elif case == "symlink_run":
        (home / ("run-" + "a" * 32)).symlink_to(tmp_path)
    elif case == "file_run":
        (home / ("run-" + "a" * 32)).write_bytes(b"x")
    else:
        link = tmp_path / "home-link"
        link.symlink_to(home)
        cfg = replace(cfg, private_home=str(link))
    with pytest.raises(ValueError, match=MESSAGE):
        cfg.preflight()


def test_stale_run_directories_of_a_previous_instance_do_not_block_a_restart(configured):
    cfg, _, _ = configured
    (Path(cfg.private_home) / ("run-" + "b" * 32)).mkdir(mode=0o700)
    cfg.preflight()


def test_deployment_identity_excludes_local_location_and_gpu(configured):
    cfg, _, _ = configured
    other = replace(cfg, model_dir="/other/model", interpreter_path="/other/venv/bin/python",
                    base_python_root="/other/base-python",
                    dependency_lock_path="/other/deps.lock", private_home="/other/home",
                    gpu_uuid="GPU-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    assert other.serving_declaration() == cfg.serving_declaration()


@pytest.mark.parametrize("field", ["rights_review_sha256", "interpreter_sha256",
                                   "dependency_lock_sha256", "validation_evidence_sha256"])
def test_identity_binds_every_operator_pin(configured, field):
    cfg, _, _ = configured
    assert replace(cfg, **{field: "sha256:" + "2" * 64}).serving_declaration() != cfg.serving_declaration()


def test_identity_binds_model_files_decoding_and_compute_type(configured, monkeypatch):
    cfg, _, _ = configured
    base = cfg.serving_declaration()
    dep = base.deployment
    assert dep.provider_id == "faster-whisper-ja"
    assert dep.quantization == "float32-ctranslate2-beam5"
    assert dep.model_artifact_sha256 == "sha256:" + pins.FILE_SHA256["model.bin"]
    assert dep.tokenizer_artifact_sha256 == "sha256:" + pins.FILE_SHA256["tokenizer.json"]
    assert dep.runtime_artifact_sha256 == cfg.interpreter_sha256
    contract, = base.contracts
    assert contract.capability == "audio.transcribe" and set(contract.features) == {"ja", "json"}
    for name in ("config.json", "preprocessor_config.json", "vocabulary.json"):
        with monkeypatch.context() as m:
            m.setitem(pins.FILE_SHA256, name, "0" * 64)
            assert cfg.serving_declaration() != base
    for attr, value in (("COMPUTE_TYPE", "int8"), ("MODEL_REVISION", "main")):
        with monkeypatch.context() as m:
            m.setattr(pins if attr == "COMPUTE_TYPE" else manifest, attr, value)
            assert cfg.serving_declaration() != base
    with monkeypatch.context() as m:
        m.setitem(pins.DECODING, "beam_size", 1)
        assert cfg.serving_declaration() != base
    with monkeypatch.context() as m:
        m.setitem(pins.VERSIONS, "ctranslate2", "4.9.2")
        assert cfg.serving_declaration() != base


def test_serving_declaration_of_another_provider_is_rejected(configured):
    cfg, worker, host = configured
    context = RuntimeCompatibilityContext(worker=worker, host=host, demand=manifest.DEMAND)
    provider = manifest.FasterWhisperManifestProvider(cfg)
    provider.create_manifest_runtime(context, cfg.serving_declaration())
    other = replace(cfg, rights_review_sha256="sha256:" + "3" * 64).serving_declaration()
    for bad in (other, None, {}):
        with pytest.raises(ValueError, match=MESSAGE):
            provider.create_manifest_runtime(context, bad)


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
    monkeypatch.setattr(managed, "FasterWhisperProcess", lambda *_a, **_kw: child)
    runtime = managed.FasterWhisperManagedRuntime(config=cfg)
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


@pytest.mark.parametrize("target", ["model.bin", "config.json"])
async def test_artifact_drift_revokes_even_cached_executor(configured, monkeypatch, target):
    from tsumgi.execution import JobExecutionError
    cfg, _, _ = configured
    child = SyntheticProcess()
    child.proceed.set()
    monkeypatch.setattr(managed, "FasterWhisperProcess", lambda *_a, **_kw: child)
    runtime = managed.FasterWhisperManagedRuntime(config=cfg)
    try:
        await runtime.start()
        cached = runtime.executor()
        (Path(cfg.model_dir) / target).write_bytes(b"changed")
        with pytest.raises(JobExecutionError, match="runtime unavailable"):
            await cached.execute_audio(None, None)
        assert not (await runtime.health()).ready
    finally:
        await runtime.release()
    assert child.closed


async def test_refused_launch_is_not_cleanup_uncertain_and_restart_works(configured, monkeypatch):
    """D3/D4 of the earlier providers: a refusal before spawn must leave a stoppable runtime."""
    from tsumgi.runtime.providers import faster_whisper_process as process
    cfg, _, _ = configured
    home = Path(cfg.private_home)
    (home / "unrelated-state").write_bytes(b"x")  # refuses launch, with no process ever created
    spawned = []
    monkeypatch.setattr(process.subprocess, "Popen", lambda *a, **kw: spawned.append(1))
    runtime = managed.FasterWhisperManagedRuntime(config=cfg)
    with pytest.raises(RuntimeError, match="startup failed"):
        await runtime.start()
    assert not spawned
    await runtime.stop()  # must not raise "shutdown is unconfirmed"
    assert (await runtime.health()).state is RuntimeHealthState.STOPPED
    await runtime.release()


async def test_managed_runtime_reaches_stopped_after_a_real_process_level_refusal(configured, monkeypatch):
    """D3 end to end: preflight passes, the process refuses before spawn, stop() is clean."""
    from tsumgi.runtime.providers import faster_whisper_process as process
    cfg, _, _ = configured
    spawned = []
    monkeypatch.setattr(process.subprocess, "Popen", lambda *a, **k: spawned.append(1))
    monkeypatch.setattr(process, "validate_environment", lambda *_a: ())
    orig = process.FasterWhisperProcess._env
    def refuse(self):
        raise RuntimeError("invalid private home")
    monkeypatch.setattr(process.FasterWhisperProcess, "_env", refuse)
    runtime = managed.FasterWhisperManagedRuntime(config=cfg)
    with pytest.raises(RuntimeError, match="startup failed"):
        await runtime.start()
    assert not spawned
    await runtime.stop()
    assert (await runtime.health()).state is RuntimeHealthState.STOPPED
    await runtime.release()


def test_preflight_plus_real_sweep_with_many_leftovers_and_a_live_instance(configured):
    """17+ dead (some unremovable-looking) run dirs never block; a flock-held one survives."""
    import fcntl, os
    from tsumgi.runtime.providers import faster_whisper_process as process
    cfg, _, _ = configured
    home = Path(cfg.private_home)
    for i in range(18):
        d = home / f"run-{i:032x}"
        (d / "sub").mkdir(parents=True, mode=0o700)
        (d / "sub" / "f").write_text("x")
        if i % 2:
            (d / "sub").chmod(0o500)
    live = home / ("run-" + "f" * 32)
    live.mkdir(mode=0o700)
    fd = os.open(live, os.O_RDONLY | os.O_DIRECTORY)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        cfg.preflight()  # no count limit here
        p = process.FasterWhisperProcess(
            cfg.interpreter_path, cfg.model_dir, GPU, private_home=cfg.private_home,
            base_python_root=cfg.base_python_root, epoch="e", interpreter_sha256=cfg.interpreter_sha256)
        run = p._fresh_home()
        assert sorted(os.listdir(home)) == sorted([live.name, run.name])
        p._remove_run_dir()
    finally:
        os.close(fd)
