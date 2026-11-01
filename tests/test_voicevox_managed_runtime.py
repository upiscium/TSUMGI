"""#215 B5b synthetic owned-VOICEVOX lifecycle: no real network, package, or engine."""

import asyncio
import json
from dataclasses import replace
from hashlib import sha256

import pytest
from test_voicevox_provider import FakeMedia, FakeVoicevox, job
from test_voicevox_provider import config as basic_config

from astrumweaver.runtime.contracts import RuntimeHealthState
from astrumweaver.runtime.lifecycle import RuntimeLifecycleManager
from astrumweaver.runtime.providers.voicevox_managed import (
    VoicevoxOwnedManagedRuntime,
    VoicevoxOwnedRuntimeSpec,
    VoicevoxProcessObservation,
    VoicevoxRuntimeFailure,
)
from astrumweaver.serving import (
    DeploymentIdentity,
    ServingContract,
    ServingDeploymentDeclaration,
)
from astrumweaver.worker.supervision import RuntimeHealthSupervisor


def hash_bytes(value):
    return "sha256:" + sha256(value).hexdigest()


class FakeEngine(FakeVoicevox):
    def __init__(self):
        super().__init__()
        self.invalid = None
        self.delay = False
        self.closed = 0

    async def bounded_json(self, *, path, max_bytes):
        assert path in ("/version", "/speakers")
        self.invocations.append(("probe", path, max_bytes))
        if self.delay:
            await asyncio.sleep(1)
        if self.fail == "probe":
            raise RuntimeError("private token, raw Japanese text, endpoint path")
        if self.invalid is not None:
            return self.invalid
        obj = self.return_version if path == "/version" else self.return_speakers
        return json.dumps(obj, ensure_ascii=False).encode("utf-8")

    async def close(self):
        self.closed += 1


class FakeProcess:
    def __init__(self):
        self.obs = None
        self.foreign_pid = None
        self.launch_calls = 0
        self.stop_nonces = []
        self.launch_delay = 0

    async def endpoint_owner(self):
        return (
            self.foreign_pid
            if self.foreign_pid is not None
            else (self.obs.endpoint_owner_pid if self.obs else None)
        )

    async def inspect(self):
        return self.obs

    async def launch(self, *, config, nonce):
        self.launch_calls += 1
        if self.launch_delay:
            await asyncio.sleep(self.launch_delay)
        self.obs = VoicevoxProcessObservation(
            pid=4421,
            uid=config.owner_uid,
            endpoint_owner_pid=4421,
            executable_sha256=config.adapter.engine_artifact_sha256,
            model_artifact_sha256=config.adapter.model_artifact_sha256,
            endpoint=config.endpoint,
            worker_resource_id=config.worker_resource_id,
            deployment_revision=config.serving.deployment.revision,
            launch_nonce=nonce,
        )

    async def stop_owned(self, *, nonce):
        self.stop_nonces.append(nonce)
        if self.obs is not None and self.obs.launch_nonce == nonce:
            self.obs = None


def make_spec(tmp_path, **changes):
    exe = tmp_path / "voicevox-engine"
    model = tmp_path / "voicevox-model"
    exe.write_bytes(b"synthetic-executable-allowlist")
    model.write_bytes(b"synthetic-model-artifact")
    adapter = basic_config(
        engine_artifact_sha256=hash_bytes(exe.read_bytes()),
        model_artifact_sha256=hash_bytes(model.read_bytes()),
    )
    dep = DeploymentIdentity(
        provider_id="voicevox",
        runtime_artifact_sha256=adapter.engine_artifact_sha256,
        adapter_artifact_sha256=adapter.adapter_artifact_sha256,
        model_artifact_sha256=adapter.model_artifact_sha256,
        execution_config_sha256=hash_bytes(b"synthetic-owned-config"),
        quantization="synthetic",
    )
    contract = ServingContract(
        deployment_revision=dep.revision,
        capability="audio.synthesize",
        operation_schema="openai-audio-speech-v1",
        validation_evidence_sha256=hash_bytes(b"synthetic-validation"),
        semantic_revision=adapter.semantic_revision,
        features=frozenset({"ja", "wav"}),
    )
    values = {
        "adapter": adapter,
        "serving": ServingDeploymentDeclaration(deployment=dep, contracts=(contract,)),
        "executable_path": str(exe),
        "model_artifact_path": str(model),
        "endpoint": "http://127.0.0.1:50521",
        "worker_resource_id": "synthetic-worker-gpu0",
        "owner_uid": 1000,
        "approved_voice_aliases": frozenset({"reviewed-a"}),
        "voice_rights_review_sha256": hash_bytes(
            b"synthetic-operator-review-not-license-proof"
        ),
        "startup_timeout_seconds": 0.25,
        "probe_timeout_seconds": 0.07,
        "stop_timeout_seconds": 0.2,
    }
    return VoicevoxOwnedRuntimeSpec(**(values | changes))


def make_runtime(tmp_path):
    cfg = make_spec(tmp_path)
    engine = FakeEngine()
    process = FakeProcess()
    return (
        VoicevoxOwnedManagedRuntime(config=cfg, engine=engine, process=process),
        engine,
        process,
        cfg,
    )


@pytest.mark.asyncio
async def test_owned_pinned_fake_process_is_ready_only_after_observed_voice_and_exact_deployment(
    tmp_path,
):
    runtime, engine, process, cfg = make_runtime(tmp_path)
    assert (await runtime.health()).state is RuntimeHealthState.STOPPED
    with pytest.raises(VoicevoxRuntimeFailure, match="not_ready"):
        runtime.executor()
    ready = await RuntimeLifecycleManager(
        runtime, poll_interval_seconds=0.01
    ).ensure_ready(
        timeout_seconds=1,
    )
    assert ready.state is RuntimeHealthState.READY and ready.ready is True
    assert ready.metadata == {"runtime_provider": "voicevox"}
    assert process.launch_calls == 1
    ex = runtime.executor()
    assert ex.serving_features == {"audio.synthesize": frozenset({"ja", "wav"})}
    from astrumweaver.worker.runtime import require_executor_serving_features

    require_executor_serving_features(ex, cfg.serving)
    result = await ex.execute_audio(job(), FakeMedia())
    assert len(result.artifacts) == 1
    assert await runtime.health() == ready
    await runtime.stop()
    assert len(process.stop_nonces) == 1
    assert (await runtime.health()).state is RuntimeHealthState.STOPPED
    with pytest.raises(VoicevoxRuntimeFailure):
        runtime.executor()
    await runtime.release()
    await runtime.release()
    assert engine.closed == 1 and len(process.stop_nonces) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "defect", ["bad_exe", "bad_model", "missing_file", "symlink", "foreign_endpoint"]
)
async def test_untrusted_preflight_fails_before_any_engine_or_process_launch(
    tmp_path, defect
):
    runtime, engine, process, cfg = make_runtime(tmp_path)
    if defect == "bad_exe":
        from pathlib import Path

        Path(cfg.executable_path).write_bytes(b"modified-executable")
    if defect == "bad_model":
        from pathlib import Path

        Path(cfg.model_artifact_path).write_bytes(b"modified-model")
    if defect == "missing_file":
        from pathlib import Path

        Path(cfg.model_artifact_path).unlink()
    if defect == "symlink":
        from pathlib import Path

        path = Path(cfg.executable_path)
        target = tmp_path / "elsewhere"
        target.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(target)
    if defect == "foreign_endpoint":
        process.foreign_pid = 12345
    with pytest.raises(VoicevoxRuntimeFailure, match="voicevox_start_failed"):
        await runtime.start()
    assert process.launch_calls == 0
    assert engine.invocations == []
    assert (await runtime.health()).state is RuntimeHealthState.FAILED
    assert process.stop_nonces == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "defect",
    [
        "version_drift",
        "speaker_deleted",
        "speaker_reassigned",
        "duplicate_global_style",
        "foreign_listener",
        "owner_uid_changed",
        "model_changed",
        "process_replaced",
        "process_exited",
        "probe_exception",
        "bad_json",
        "oversize_json",
        "stale_generation",
    ],
)
async def test_after_ready_identity_or_voice_drift_latches_failure(tmp_path, defect):
    runtime, engine, process, _ = make_runtime(tmp_path)
    await runtime.start()
    if defect == "version_drift":
        engine.return_version = "unreviewed-version"
    elif defect == "speaker_deleted":
        engine.return_speakers = [
            {
                "speaker_uuid": engine.return_speakers[0]["speaker_uuid"],
                "styles": [{"id": 12}],
            }
        ]
    elif defect == "speaker_reassigned":
        engine.return_speakers = [
            {
                "speaker_uuid": "c3b5b742-0000-4000-a000-000000000002",
                "styles": [{"id": 3}],
            }
        ]
    elif defect == "duplicate_global_style":
        engine.return_speakers.append(
            {
                "speaker_uuid": "c3b5b742-0000-4000-a000-000000000002",
                "styles": [{"id": 3}],
            }
        )
    elif defect == "foreign_listener":
        process.foreign_pid = 8888
    elif defect == "owner_uid_changed":
        process.obs = replace(process.obs, uid=33)
    elif defect == "model_changed":
        process.obs = replace(
            process.obs, model_artifact_sha256=hash_bytes(b"other-model")
        )
    elif defect == "process_replaced":
        process.obs = replace(process.obs, pid=885)
    elif defect == "process_exited":
        process.obs = None
    elif defect == "probe_exception":
        engine.fail = "probe"
    elif defect == "bad_json":
        engine.invalid = b'{"broken":NaN}'
    elif defect == "oversize_json":
        engine.invalid = b"x" * 280000
    elif defect == "stale_generation":
        process.obs = replace(process.obs, launch_nonce="old-generation")
    health = await runtime.health()
    assert health.state is RuntimeHealthState.FAILED
    assert health.ready is False
    assert health.detail.startswith("voicevox_")
    with pytest.raises(VoicevoxRuntimeFailure):
        runtime.executor()
    with pytest.raises(VoicevoxRuntimeFailure):
        await runtime.start()
    for forbidden in (
        "unreviewed-version",
        "other-model",
        "private",
        "token",
        "http://",
    ):
        assert forbidden not in str(health)
    await runtime.stop()
    assert len(process.stop_nonces) == 1


@pytest.mark.asyncio
async def test_supervisor_latches_real_runtime_probe_failure(tmp_path):
    runtime, engine, _process, _ = make_runtime(tmp_path)
    await runtime.start()
    supervisor = RuntimeHealthSupervisor(
        runtime, interval_seconds=0.02, timeout_seconds=0.1
    )
    assert await supervisor.check()
    assert supervisor.available
    engine.return_version = "drifted"
    assert not await supervisor.check()
    assert not supervisor.available and supervisor.failure_reason == "health_not_ready"
    engine.return_version = "test-version.1"
    assert not await supervisor.check()  # No spontaneous recovery.
    await supervisor.close()
    await runtime.stop()


@pytest.mark.asyncio
async def test_ongoing_health_probe_is_bounded_and_does_not_leak_native_errors(
    tmp_path,
):
    runtime, engine, _process, _ = make_runtime(tmp_path)
    await runtime.start()
    engine.delay = True
    health = await asyncio.wait_for(runtime.health(), 0.4)
    assert health.state is RuntimeHealthState.FAILED
    assert health.detail == "voicevox_probe_timeout"
    await runtime.stop()


@pytest.mark.asyncio
async def test_cancelled_startup_cleans_only_its_own_nonce_and_never_ready(tmp_path):
    runtime, _engine, process, _ = make_runtime(tmp_path)
    process.launch_delay = 1.0
    task = asyncio.create_task(runtime.start())
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.launch_calls == 1
    assert len(process.stop_nonces) == 1
    assert (await runtime.health()).state is RuntimeHealthState.FAILED


@pytest.mark.asyncio
async def test_unresponsive_start_is_latched_and_only_owned_process_stop_is_requested(
    tmp_path,
):
    runtime, _engine, process, _ = make_runtime(tmp_path)
    process.launch_delay = 1.0
    with pytest.raises(VoicevoxRuntimeFailure, match="voicevox_start_failed"):
        await asyncio.wait_for(runtime.start(), 1)
    assert len(process.stop_nonces) == 1
    assert (await runtime.health()).state is RuntimeHealthState.FAILED


@pytest.mark.parametrize(
    "changes",
    [
        {"endpoint": "http://0.0.0.0:50521"},
        {"endpoint": "http://127.0.0.1:secret-private-port"},
        {"endpoint": "http://[invalid:host]:50521"},
        {"endpoint": "http://localhost:50521"},
        {"endpoint": "http://127.0.0.1:50521/public"},
        {"endpoint": "http://127.0.0.1:50521?text=secret"},
        {"executable_path": "../escape"},
        {"worker_resource_id": "unsafe resource"},
        {"approved_voice_aliases": frozenset()},
        {"voice_rights_review_sha256": "not-reviewed"},
        {"owner_uid": True},
        {"startup_timeout_seconds": float("nan")},
    ],
)
def test_static_config_denies_bypass_without_any_side_effect(tmp_path, changes):
    with pytest.raises(ValueError, match="configuration is invalid"):
        make_spec(tmp_path, **changes)


def test_serving_identity_and_voice_license_reference_are_exact_operator_inputs(
    tmp_path,
):
    spec = make_spec(tmp_path)
    bad = replace(spec.adapter, engine_version="unreviewed")
    with pytest.raises(ValueError, match="configuration is invalid"):
        replace(spec, adapter=replace(bad, engine_artifact_sha256=hash_bytes(b"wrong")))
    c = spec.serving.contracts[0]
    with pytest.raises(ValueError, match="configuration is invalid"):
        replace(
            spec,
            serving=ServingDeploymentDeclaration(
                deployment=spec.serving.deployment,
                contracts=(
                    replace(c, semantic_revision=hash_bytes(b"wrong-semantic")),
                ),
            ),
        )
