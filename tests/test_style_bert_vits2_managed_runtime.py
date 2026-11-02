"""#216 B5c: actual disposable stdlib child; no native Style-Bert model/GPU."""

import asyncio
import os
import sys
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest
from test_style_bert_vits2_provider import FakeMedia, job
from test_style_bert_vits2_provider import config as adapter_config

from astrumweaver.contracts import ResourceShape, WorkerSpec
from astrumweaver.execution import JobExecutionError
from astrumweaver.runtime.contracts import RuntimeHealthState, RuntimeHostFacts
from astrumweaver.runtime.lifecycle import RuntimeLifecycleManager
from astrumweaver.runtime.providers.style_bert_vits2 import StyleBertVits2ModelPin
from astrumweaver.runtime.providers.style_bert_vits2_managed import (
    StyleBertOwnedManagedRuntime,
    StyleBertOwnedRuntimeSpec,
    StyleBertRuntimeFailure,
)
from astrumweaver.serving import (
    DeploymentIdentity,
    ServingContract,
    ServingDeploymentDeclaration,
)
from astrumweaver.worker.runtime import require_executor_serving_features
from astrumweaver.worker.supervision import RuntimeHealthSupervisor


def digest(body: bytes) -> str:
    return sha256(body).hexdigest()


# A *synthetic* child protocol server, never an engine and never imported by TSUMGI.
# The only sound inference claim is that sync sleep occurs in a separate process.
STUB = r"""
import json
import struct
import sys
import time
FAULT = __FAULT__
def read():
    inp = sys.stdin.buffer
    length = struct.unpack(">I", inp.read(4))[0]
    return json.loads(inp.read(length))
def send(obj):
    raw = json.dumps(obj, separators=(",",":")).encode()
    sys.stdout.buffer.write(struct.pack(">I",len(raw)) + raw)
    sys.stdout.buffer.flush()
def wav():
    samples = b"\x00\x00" * 32
    rate = 24000
    return (b"RIFF" + struct.pack("<I",len(samples)+36) + b"WAVEfmt "
        + struct.pack("<IHHIIHH",16,1,1,rate,rate*2,2,16)
        + b"data" + struct.pack("<I",len(samples)) + samples)
request = read()
if FAULT == "boot_exit":
    sys.exit(5)
if FAULT == "boot_delay":
    time.sleep(2)
reply = {k:v for k,v in request.items() if k not in ("op", "artifact_paths")}
reply["op"] = "ready"
reply["probe_status"] = "synthesized"
reply["probe_pcm16"] = True
if FAULT == "wrong_epoch": reply["epoch"] = "stale"
if FAULT == "wrong_pid": reply["pid"] = 42
if FAULT == "wrong_model": reply["model_hashes"]["weight"] = "other"
if FAULT == "wrong_voices": reply["voices"] = []
if FAULT == "unqualified_probe": reply["probe_pcm16"] = False
if FAULT == "large_boot_frame":
    sys.stdout.buffer.write(struct.pack(">I",9999999))
    sys.stdout.buffer.flush()
    sys.exit(0)
send(reply)
while True:
    try:
        request = read()
    except (EOFError,struct.error):
        sys.exit(0)
    if FAULT == "infer_delay": time.sleep(2)
    if FAULT == "infer_exit": sys.exit(9)
    if FAULT == "bad_ipc_epoch": request["epoch"] = "wrong-generation"
    if FAULT == "malformed_header":
        sys.stdout.buffer.write(struct.pack(">I",999999))
        sys.stdout.buffer.flush()
        continue
    data = wav()
    if FAULT == "noncanonical_wav": data = b"bad-wav" + data
    if FAULT == "oversize_wav":
        send(dict(version=1,epoch=request["epoch"],request_id=request["request_id"],
                  op="audio",status="ok",wav_length=20000000))
        continue
    send(dict(version=1,epoch=request["epoch"],request_id=request["request_id"],
              op="audio",status="ok",wav_length=len(data)))
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()
"""


def make_spec(tmp_path: Path, *, fault="none", gpu=False):
    model_dir = tmp_path / "synthetic-model"
    model_dir.mkdir()
    model_file = model_dir / "pinned_e160.safetensors"
    model_file.write_bytes(b"not-real-model-weight-contents")
    conf = model_dir / "config.json"
    conf.write_bytes(b"not-real-config")
    styles = model_dir / "style_vectors.npy"
    styles.write_bytes(b"not-real-npy")
    model = StyleBertVits2ModelPin(
        model_id="synthetic-voice",
        model_filename=model_file.name,
        model_sha256=digest(model_file.read_bytes()),
        config_sha256=digest(conf.read_bytes()),
        style_vectors_sha256=digest(styles.read_bytes()),
        runtime_revision="d" * 40,
        adapter_revision="e" * 40,
    )
    adapter = adapter_config(model=model, synthesis_timeout_seconds=0.15)
    runner = tmp_path / "fake-runner.py"
    runner.write_text(STUB.replace("__FAULT__", repr(fault)), encoding="utf-8")
    interpreter = os.path.realpath(sys.executable)
    executable = "sha256:" + digest(Path(interpreter).read_bytes())
    runner_digest = "sha256:" + digest(runner.read_bytes())
    dep = DeploymentIdentity(
        provider_id="style-bert-vits2",
        runtime_artifact_sha256=executable,
        adapter_artifact_sha256=runner_digest,
        model_artifact_sha256="sha256:" + model.model_sha256,
        execution_config_sha256="sha256:" + "1" * 64,
        quantization="synthetic",
    )
    serving = ServingDeploymentDeclaration(
        deployment=dep,
        contracts=(
            ServingContract(
                deployment_revision=dep.revision,
                capability="audio.synthesize",
                operation_schema="openai-audio-speech-v1",
                validation_evidence_sha256="sha256:" + "4" * 64,
                features=frozenset({"ja", "wav"}),
                semantic_revision=adapter.required_semantic_revision,
            ),
        ),
    )
    worker = WorkerSpec(
        worker_id="synthetic-child",
        worker_class="test-only",
        resources=(
            ResourceShape(
                gpu_count=1, total_vram_mb=12000, max_single_gpu_vram_mb=12000
            )
            if gpu
            else ResourceShape()
        ),
        gpu_uuids=("GPU-synthetic-0",) if gpu else (),
        capabilities=frozenset({"audio.synthesize"}),
    )
    spec = StyleBertOwnedRuntimeSpec(
        adapter=adapter,
        serving=serving,
        worker=worker,
        host=RuntimeHostFacts(cpu_count=4, host_ram_mb=8192),
        model_directory=str(model_dir),
        interpreter_path=interpreter,
        runner_path=str(runner),
        runner_sha256=runner_digest,
        interpreter_sha256=executable,
        rights_review_sha256="sha256:" + "f" * 64,
        voice_rights_qualified=True,  # Synthetic operator switch, NOT real license review.
        device="cuda" if gpu else "cpu",
        gpu_uuid="GPU-synthetic-0" if gpu else None,
        required_single_gpu_vram_mb=4096 if gpu else 0,
        boot_timeout_seconds=0.25,
        inference_timeout_seconds=0.7,
        stop_timeout_seconds=0.4,
    )
    return spec


@pytest.mark.asyncio
@pytest.mark.parametrize("gpu", [False, True])
async def test_owned_child_bootstrap_frame_and_one_wav_stage(gpu, tmp_path):
    spec = make_spec(tmp_path, gpu=gpu)
    runtime = StyleBertOwnedManagedRuntime(config=spec)
    assert (await runtime.health()).state is RuntimeHealthState.STOPPED
    with pytest.raises(StyleBertRuntimeFailure, match="not_ready"):
        runtime.executor()
    ready = await RuntimeLifecycleManager(runtime).ensure_ready(timeout_seconds=1)
    assert ready.state is RuntimeHealthState.READY and ready.ready is True
    assert runtime._proc is not None and runtime._proc.returncode is None
    executor = runtime.executor()
    require_executor_serving_features(executor, spec.serving)
    media = FakeMedia()
    result = await executor.execute_audio(job(), media)
    assert len(result.artifacts) == 1 and len(media.calls) == 1
    assert result.text is None and not result.outputs
    assert "散歩" not in str(result) and spec.model_directory not in str(result)
    assert (await runtime.health()).ready is True
    child = runtime._proc
    await runtime.release()
    assert child.returncode is not None
    assert runtime._proc is None
    with pytest.raises(StyleBertRuntimeFailure):
        runtime.executor()
    await runtime.release()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    [
        "wrong_epoch",
        "wrong_pid",
        "wrong_model",
        "wrong_voices",
        "unqualified_probe",
        "large_boot_frame",
        "boot_exit",
        "boot_delay",
    ],
)
async def test_bootstrap_rejects_fake_unverified_or_unresponsive_process_and_reaps(
    mode, tmp_path
):
    cfg = make_spec(tmp_path, fault=mode)
    runtime = StyleBertOwnedManagedRuntime(config=cfg)
    with pytest.raises(StyleBertRuntimeFailure, match="stylebert_bootstrap_failed"):
        await runtime.start()
    assert runtime._proc is None
    assert (await runtime.health()).state is RuntimeHealthState.FAILED
    with pytest.raises(StyleBertRuntimeFailure):
        runtime.executor()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    ["weight", "config", "styles", "runner", "symlink", "duplicate", "missing"],
)
async def test_preflight_denies_bad_files_before_process_start(mutation, tmp_path):
    spec = make_spec(tmp_path)
    root = Path(spec.model_directory)
    if mutation == "weight":
        (root / spec.adapter.model.model_filename).write_bytes(b"modified")
    if mutation == "config":
        (root / "config.json").write_bytes(b"modified")
    if mutation == "styles":
        (root / "style_vectors.npy").write_bytes(b"modified")
    if mutation == "runner":
        Path(spec.runner_path).write_bytes(b"modified")
    if mutation == "symlink":
        target = tmp_path / "target"
        orig = root / "style_vectors.npy"
        target.write_bytes(orig.read_bytes())
        orig.unlink()
        orig.symlink_to(target)
    if mutation == "duplicate":
        (root / "another.safetensors").write_bytes(b"second-weight")
    if mutation == "missing":
        (root / "config.json").unlink()
    runtime = StyleBertOwnedManagedRuntime(config=spec)
    with pytest.raises(StyleBertRuntimeFailure):
        await runtime.start()
    assert runtime._proc is None
    assert (await runtime.health()).state is RuntimeHealthState.FAILED


@pytest.mark.asyncio
async def test_worker_heartbeat_remains_responsive_while_fake_sync_infer_blocks_child(
    tmp_path,
):
    cfg = make_spec(tmp_path, fault="infer_delay")
    runtime = StyleBertOwnedManagedRuntime(config=cfg)
    await runtime.start()
    media = FakeMedia()
    beat_count = 0

    async def heartbeat():
        nonlocal beat_count
        while True:
            beat_count += 1
            await asyncio.sleep(0.01)

    beat = asyncio.create_task(heartbeat())
    task = asyncio.create_task(runtime.executor().execute_audio(job(), media))
    with pytest.raises(JobExecutionError, match="timed out"):
        await task
    beat.cancel()
    await asyncio.gather(beat, return_exceptions=True)
    assert beat_count >= 8
    assert not media.calls
    assert (await runtime.health()).state is RuntimeHealthState.FAILED
    assert runtime._proc is None


@pytest.mark.asyncio
async def test_cancelled_inference_terminates_real_child_before_result_or_restaging(
    tmp_path,
):
    cfg = make_spec(tmp_path, fault="infer_delay")
    runtime = StyleBertOwnedManagedRuntime(config=cfg)
    await runtime.start()
    child = runtime._proc
    media = FakeMedia()
    request = job()
    task = asyncio.create_task(runtime.executor().execute_audio(request, media))
    await asyncio.sleep(0.04)
    await runtime.executor().cancel(request.job_id)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert child.returncode is not None and runtime._proc is None
    assert (await runtime.health()).state is RuntimeHealthState.FAILED
    assert not media.calls
    with pytest.raises(StyleBertRuntimeFailure):
        await runtime.start()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["infer_exit", "malformed_header", "oversize_wav", "bad_ipc_epoch"]
)
async def test_inference_ipc_fault_fails_closed_and_reaps_without_staging(
    fault, tmp_path
):
    spec = make_spec(tmp_path, fault=fault)
    runtime = StyleBertOwnedManagedRuntime(config=spec)
    await runtime.start()
    media = FakeMedia()
    with pytest.raises(JobExecutionError):
        await runtime.executor().execute_audio(job(), media)
    assert not media.calls and runtime._proc is None
    assert (await runtime.health()).state is RuntimeHealthState.FAILED


@pytest.mark.asyncio
async def test_invalid_pcm16_rejected_before_artifact_stage(tmp_path):
    runtime = StyleBertOwnedManagedRuntime(
        config=make_spec(tmp_path, fault="noncanonical_wav")
    )
    await runtime.start()
    media = FakeMedia()
    with pytest.raises(JobExecutionError, match="invalid"):
        await runtime.executor().execute_audio(job(), media)
    assert not media.calls
    await runtime.release()


@pytest.mark.asyncio
async def test_unexpected_child_death_latches_health_supervisor(tmp_path):
    runtime = StyleBertOwnedManagedRuntime(config=make_spec(tmp_path))
    await runtime.start()
    supervisor = RuntimeHealthSupervisor(
        runtime, interval_seconds=0.04, timeout_seconds=0.25
    )
    assert await supervisor.check()
    child = runtime._proc
    child.kill()
    await child.wait()
    assert not await supervisor.check()
    assert not supervisor.available
    assert (await runtime.health()).state is RuntimeHealthState.FAILED
    await supervisor.close()
    await runtime.stop()


@pytest.mark.parametrize(
    "changes",
    [
        {"device": "rocm"},
        {"device": "cuda"},
        {"gpu_uuid": "not-owned"},
        {"required_single_gpu_vram_mb": 10},
        {"interpreter_path": "relative"},
        {"runner_sha256": "private/unpinned"},
        {"rights_review_sha256": "not-approved"},
        {"required_host_ram_mb": 9000},
        {"inference_timeout_seconds": float("nan")},
        {"voice_rights_qualified": False},
        {"voice_rights_qualified": 1},
    ],
)
def test_unqualified_operator_config_fails_before_launch(changes, tmp_path):
    spec = make_spec(tmp_path)
    with pytest.raises(ValueError, match="configuration is invalid"):
        replace(spec, **changes)


@pytest.mark.asyncio
async def test_no_job_override_may_start_nonreviewed_model_or_style(tmp_path):
    runtime = StyleBertOwnedManagedRuntime(config=make_spec(tmp_path))
    await runtime.start()
    with pytest.raises(StyleBertRuntimeFailure, match="not_allowed"):
        await runtime.synthesize(
            model=replace(runtime._config.adapter.model, model_id="foreign"),
            text="private phrase",
            speaker_id=1,
            style="Neutral",
        )
    with pytest.raises(StyleBertRuntimeFailure, match="not_allowed"):
        await runtime.synthesize(
            model=runtime._config.adapter.model,
            text="private phrase",
            speaker_id=999,
            style="Neutral",
        )
    assert (await runtime.health()).ready
    await runtime.release()


@pytest.mark.asyncio
async def test_changed_model_bytes_after_ready_latch_unavailable_before_new_claim(
    tmp_path,
):
    cfg = make_spec(tmp_path)
    runtime = StyleBertOwnedManagedRuntime(config=cfg)
    await runtime.start()
    assert (await runtime.health()).ready
    (Path(cfg.model_directory) / "style_vectors.npy").write_bytes(b"new-style-vectors")
    status = await runtime.health()
    assert status.state is RuntimeHealthState.FAILED and status.ready is False
    assert status.detail == "stylebert_artifact_drift"
    with pytest.raises(StyleBertRuntimeFailure, match="not_ready"):
        runtime.executor()
    await runtime.stop()
