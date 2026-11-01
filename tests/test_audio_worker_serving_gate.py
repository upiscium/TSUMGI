"""Synthetic entrypoint checks; no provider, GPU, server or Control invocation."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from astrumweaver.runtime import RuntimeHealth, RuntimeHealthState
from astrumweaver.gateway.audio import (
    SPEECH_OPERATION_SCHEMA,
    TRANSCRIPTION_OPERATION_SCHEMA,
)
from astrumweaver.serving import (
    DeploymentIdentity,
    ServingContract,
    ServingDeploymentDeclaration,
)
from astrumweaver.worker import daemon


SPEECH = ("audio.synthesize", SPEECH_OPERATION_SCHEMA)
TRANSCRIPTION = ("audio.transcribe", TRANSCRIPTION_OPERATION_SCHEMA)
ECHO = ("debug.echo", "echo-v1")
MISSING_MANIFEST = "audio capabilities require a reviewed serving manifest"
INVALID_CONTRACT = (
    "audio capability requires exactly one matching serving operation contract"
)


def digest(character: str) -> str:
    return "sha256:" + character * 64


def declaration(*operations, provider_id="synthetic-provider"):
    deployment = DeploymentIdentity(
        provider_id=provider_id,
        runtime_artifact_sha256=digest("1"),
        adapter_artifact_sha256=digest("2"),
        model_artifact_sha256=digest("3"),
        execution_config_sha256=digest("4"),
        quantization="synthetic",
    )
    contracts = tuple(
        ServingContract(
            deployment_revision=deployment.revision,
            capability=capability,
            operation_schema=operation_schema,
            validation_evidence_sha256=digest("7"),
            semantic_revision=digest("8"),
            features=(
                frozenset({"ja", "wav"}) if capability == SPEECH[0]
                else frozenset({"ja", "json"}) if capability == TRANSCRIPTION[0]
                else frozenset()
            ),
            limits={"request_bytes": 4096},
        )
        for capability, operation_schema in operations
    )
    return ServingDeploymentDeclaration(deployment=deployment, contracts=contracts)


class RegistrationReached(Exception):
    """Stop at the real WorkerRuntime's registration boundary, before any I/O."""


@pytest.fixture
async def worker_harness(monkeypatch, tmp_path):
    config = {
        "worker": {
            "id": "synthetic-audio-gate-worker",
            "class": "cpu-test",
            "control_url": "http://unused-control",
            "capabilities": [],
            "labels": {"synthetic": "unchanged"},
        },
        "executor": {"factory": "synthetic:never-imported"},
    }
    events = []
    registrations = []

    async def execute_audio(_job, _media):
        pytest.fail("registration fixture must not execute a job")

    executor = SimpleNamespace(
        execute_audio=execute_audio,
        capabilities=frozenset({SPEECH[0], TRANSCRIPTION[0], ECHO[0], "other.work"}),
        serving_features={
            SPEECH[0]: frozenset({"ja", "wav"}),
            TRANSCRIPTION[0]: frozenset({"ja", "json"}),
        },
    )

    def forbidden(*_args, **_kwargs):
        pytest.fail("no provider, host discovery or real runtime may be invoked")

    class FakeClient:
        def __init__(self, *_args, **_kwargs):
            events.append("client")

        async def register(self, **kwargs):
            events.append("register")
            registrations.append(kwargs)
            raise RegistrationReached

        async def aclose(self):
            events.append("close")

    class FakeHealthServer:
        def __init__(self, _config):
            self.should_exit = False

        async def serve(self):
            while not self.should_exit:
                await asyncio.sleep(0)

    def load_executor(*_args, **_kwargs):
        events.append("executor")
        return executor

    def configure(capabilities, serving=None, *, backend="executor"):
        config["worker"]["capabilities"] = list(capabilities)
        if backend == "runtime":
            config.pop("executor", None)
            config["runtime"] = {"manifest": "unused-runtime.json"}
        if serving is not None:
            manifest = tmp_path / "synthetic-serving.json"
            manifest.write_text(json.dumps(serving.to_dict()), encoding="utf-8")
            config["serving"] = {"manifest": str(manifest)}

    monkeypatch.setenv("ASTRUMWEAVER_WORKER_TOKEN", "synthetic-secret-token")
    monkeypatch.setattr(daemon, "_load_toml", lambda _path: config)
    monkeypatch.setattr(daemon, "_run_gpu_preflight", lambda *_args: events.append("gpu"))
    monkeypatch.setattr(daemon, "ControlClient", FakeClient)
    monkeypatch.setattr(daemon, "load_executor", load_executor)
    for name in (
        "_load_runtime_deployment", "discover_runtime_host_facts",
        "managed_runtime_from_deployment", "RuntimeLifecycleManager",
        "RuntimeHealthSupervisor",
    ):
        monkeypatch.setattr(daemon, name, forbidden)
    monkeypatch.setattr(daemon, "create_health_app", lambda _runtime: object())
    monkeypatch.setattr(daemon.uvicorn, "Config", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(daemon.uvicorn, "Server", FakeHealthServer)
    monkeypatch.setattr(
        asyncio.get_running_loop(), "add_signal_handler", lambda *_args: None,
    )
    return SimpleNamespace(
        config=config, events=events, registrations=registrations,
        executor=executor, configure=configure, forbidden=forbidden,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["executor", "runtime"])
@pytest.mark.parametrize("capabilities", [
    (SPEECH[0],), (TRANSCRIPTION[0],), (SPEECH[0], TRANSCRIPTION[0]),
    (ECHO[0], SPEECH[0], "other.work"),
])
async def test_missing_manifest_rejected_before_gpu_executor_runtime_or_registration(
    worker_harness, backend, capabilities,
):
    harness = worker_harness
    harness.configure(capabilities, backend=backend)

    with pytest.raises(RuntimeError) as caught:
        await daemon.run_worker("unused-worker.toml")

    assert str(caught.value) == MISSING_MANIFEST
    assert harness.events == []
    assert harness.registrations == []


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["executor", "runtime"])
@pytest.mark.parametrize(("capabilities", "operations"), [
    ((SPEECH[0],), (ECHO,)),
    ((TRANSCRIPTION[0],), (ECHO,)),
    ((SPEECH[0],), ((SPEECH[0], TRANSCRIPTION_OPERATION_SCHEMA),)),
    ((TRANSCRIPTION[0],), ((TRANSCRIPTION[0], SPEECH_OPERATION_SCHEMA),)),
    # The right schema under the wrong capability is not a match.
    ((SPEECH[0],), ((TRANSCRIPTION[0], SPEECH_OPERATION_SCHEMA),)),
    ((SPEECH[0], TRANSCRIPTION[0]), (SPEECH,)),
    ((SPEECH[0], TRANSCRIPTION[0]), (TRANSCRIPTION,)),
    # Count all contracts for the capability, not just ones with the right schema.
    ((SPEECH[0],), (SPEECH, (SPEECH[0], "future-speech-v2"))),
    ((TRANSCRIPTION[0],), (TRANSCRIPTION, (TRANSCRIPTION[0], "future-stt-v2"))),
])
async def test_missing_wrong_or_ambiguous_contract_fails_closed(
    worker_harness, backend, capabilities, operations,
):
    harness = worker_harness
    harness.configure(capabilities, declaration(*operations), backend=backend)

    with pytest.raises(RuntimeError) as caught:
        await daemon.run_worker("unused-worker.toml")

    assert str(caught.value) == INVALID_CONTRACT
    assert harness.events == []
    assert harness.registrations == []


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_id", ["synthetic-provider-a", "synthetic-provider-b"])
@pytest.mark.parametrize("operations", [
    (SPEECH,), (TRANSCRIPTION,), (SPEECH, TRANSCRIPTION),
    (SPEECH, TRANSCRIPTION, ECHO),
])
async def test_pinned_contracts_never_authorize_unsupervised_audio_factory(
    worker_harness, provider_id, operations,
):
    harness = worker_harness
    serving = declaration(*operations, provider_id=provider_id)
    capabilities = frozenset(capability for capability, _schema in operations)
    harness.configure(capabilities, serving)

    # A reviewed static ServingContract and feature-complete mock executor
    # are not proof of a running/owned TTS or STT engine. No GPU, client,
    # provider import, Control I/O, or registration is permitted.
    with pytest.raises(RuntimeError) as caught:
        await daemon.run_worker("unused-worker.toml")

    assert str(caught.value) == daemon._AUDIO_MANAGED_ONLY_ERROR
    assert harness.events == [] and harness.registrations == []


@pytest.mark.asyncio
@pytest.mark.parametrize("with_serving", [False, True])
async def test_non_audio_registration_is_unchanged(worker_harness, with_serving):
    harness = worker_harness
    serving = declaration(ECHO) if with_serving else None
    capabilities = frozenset({ECHO[0], "other.work"})
    harness.configure(capabilities, serving)

    with pytest.raises(RegistrationReached):
        await asyncio.wait_for(daemon.run_worker("unused-worker.toml"), timeout=1)

    assert harness.events == ["gpu", "client", "executor", "register", "close"]
    registration, = harness.registrations
    assert registration["spec"] == daemon._build_spec(harness.config["worker"])
    assert registration["spec"].capabilities == capabilities
    if serving is None:
        assert registration["serving"] is None
    else:
        assert registration["serving"].contracts == serving.contracts


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["capability", "features"])
async def test_valid_audio_declaration_does_not_bypass_executor_gates(
    worker_harness, monkeypatch, missing,
):
    harness = worker_harness
    harness.configure((SPEECH[0],), declaration(SPEECH), backend="runtime")

    # The executor-capability/serving gates still run AFTER managed readiness.
    # Fake managed readiness here is source-only, not engine qualification.
    class ReadyLifecycle:
        def __init__(self, _managed):
            pass

        async def ensure_ready(self, **_kwargs):
            harness.events.append("ensure-ready")
            return RuntimeHealth(state=RuntimeHealthState.READY, ready=True)

        async def shutdown_owned(self, **_kwargs):
            harness.events.append("shutdown")

    monkeypatch.setattr(
        daemon, "_load_runtime_deployment",
        lambda _: SimpleNamespace(provider_id="synthetic-provider"),
    )
    monkeypatch.setattr(daemon, "discover_runtime_host_facts", lambda: object())
    monkeypatch.setattr(
        daemon, "managed_runtime_from_deployment",
        lambda *_args, **_kwargs: SimpleNamespace(executor=lambda: harness.executor),
    )
    monkeypatch.setattr(daemon, "RuntimeLifecycleManager", ReadyLifecycle)
    monkeypatch.setattr(daemon, "RuntimeHealthSupervisor", lambda *_args, **_kwargs: object())

    if missing == "capability":
        harness.executor.capabilities = frozenset({ECHO[0]})
        error = "executor does not support advertised capabilities"
    else:
        harness.executor.serving_features = {}
        error = "executor does not support advertised serving features"

    with pytest.raises(RuntimeError, match=error):
        await daemon.run_worker("unused-worker.toml")

    assert harness.events == ["gpu", "client", "ensure-ready", "close", "shutdown"]
    assert harness.registrations == []


@pytest.mark.asyncio
async def test_valid_audio_declaration_still_requires_runtime_readiness(
    worker_harness, monkeypatch,
):
    harness = worker_harness
    harness.configure((SPEECH[0],), declaration(SPEECH), backend="runtime")

    class NotReadyLifecycle:
        def __init__(self, _runtime):
            pass

        async def ensure_ready(self, **_kwargs):
            harness.events.append("ensure-ready")
            raise RuntimeError("synthetic runtime not ready")

        async def shutdown_owned(self, **_kwargs):
            harness.events.append("shutdown")

    monkeypatch.setattr(
        daemon, "_load_runtime_deployment",
        lambda _path: SimpleNamespace(provider_id="synthetic-provider"),
    )
    monkeypatch.setattr(daemon, "discover_runtime_host_facts", lambda: object())
    monkeypatch.setattr(
        daemon, "managed_runtime_from_deployment",
        lambda *_args, **_kwargs: SimpleNamespace(executor=harness.forbidden),
    )
    monkeypatch.setattr(daemon, "RuntimeLifecycleManager", NotReadyLifecycle)

    with pytest.raises(RuntimeError, match="^synthetic runtime not ready$"):
        await daemon.run_worker("unused-worker.toml")

    assert harness.events == ["gpu", "client", "ensure-ready", "close", "shutdown"]
    assert harness.registrations == []


@pytest.mark.asyncio
@pytest.mark.parametrize("capabilities", [
    (SPEECH[0],),
    (TRANSCRIPTION[0],),
    (SPEECH[0], TRANSCRIPTION[0]),
    ("other.work", SPEECH[0]),
])
async def test_audio_bare_factory_rejected_before_host_and_control_even_with_proven_features(
    worker_harness, capabilities,
):
    harness = worker_harness
    operations = tuple(
        (SPEECH if cap == SPEECH[0] else TRANSCRIPTION)
        for cap in capabilities if cap in (SPEECH[0], TRANSCRIPTION[0])
    )
    harness.configure(capabilities, declaration(*operations))
    harness.config["worker"].update({
        "gpu_preflight": True, "control_url": "http://private-secret-host/token",
    })
    harness.config["executor"]["factory"] = "dangerous:side_effect_if_loaded"
    with pytest.raises(RuntimeError) as caught:
        await daemon.run_worker("unused-worker.toml")
    assert str(caught.value) == daemon._AUDIO_MANAGED_ONLY_ERROR
    assert harness.events == [] and harness.registrations == []
    assert "private-secret" not in str(caught.value)
    assert "dangerous:side_effect" not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("health", [
    RuntimeHealth(state=RuntimeHealthState.STOPPED, ready=True),
    RuntimeHealth(state=RuntimeHealthState.STARTING, ready=True),
    RuntimeHealth(state=RuntimeHealthState.DEGRADED, ready=True),
    RuntimeHealth(state=RuntimeHealthState.FAILED, ready=True),
    RuntimeHealth(state=RuntimeHealthState.READY, ready=False),
    RuntimeHealth(state=RuntimeHealthState.READY, ready=1),
    None,
    {"state": "ready", "ready": True},
])
async def test_audio_managed_runtime_rejects_inconsistent_or_fabricated_ready_before_executor(
    worker_harness, monkeypatch, health,
):
    harness = worker_harness
    harness.configure((SPEECH[0],), declaration(SPEECH), backend="runtime")

    class UnreliableLifecycle:
        def __init__(self, runtime):
            self.runtime = runtime

        async def ensure_ready(self, **_kwargs):
            harness.events.append("ensure-ready")
            return health

        async def shutdown_owned(self, **_kwargs):
            harness.events.append("shutdown")

    class MockManaged:
        def executor(self):
            pytest.fail("inconsistent READY must not expose the audio executor")

    monkeypatch.setattr(
        daemon, "_load_runtime_deployment",
        lambda _path: SimpleNamespace(provider_id="synthetic-provider"),
    )
    monkeypatch.setattr(daemon, "discover_runtime_host_facts", lambda: object())
    monkeypatch.setattr(
        daemon, "managed_runtime_from_deployment", lambda *_args, **_kwargs: MockManaged(),
    )
    monkeypatch.setattr(daemon, "RuntimeLifecycleManager", UnreliableLifecycle)

    with pytest.raises(RuntimeError) as caught:
        await daemon.run_worker("unused-worker.toml")
    assert str(caught.value) == daemon._AUDIO_READY_ERROR
    assert harness.events == ["gpu", "client", "ensure-ready", "close", "shutdown"]
    assert harness.registrations == []


@pytest.mark.asyncio
async def test_valid_managed_audio_runtime_passes_existing_supervisor_before_registration(
    worker_harness, monkeypatch,
):
    """Real lifecycle/supervisor + fake owned runtime, no process/network/GPU."""
    from astrumweaver.runtime import RuntimeLifecycleManager
    from astrumweaver.execution import ResidencyReport
    from astrumweaver.worker.supervision import RuntimeHealthSupervisor

    harness = worker_harness
    harness.configure((SPEECH[0],), declaration(SPEECH), backend="runtime")

    class MockManaged:
        provider_id = "synthetic-provider"

        def __init__(self):
            self.stops = 0
            self.releases = 0
            self.health_probes = 0

        async def start(self):
            pytest.fail("already READY synthetic runtime should not be started")

        async def health(self):
            self.health_probes += 1
            return RuntimeHealth(state=RuntimeHealthState.READY, ready=True)

        def executor(self):
            return harness.executor

        async def stop(self):
            self.stops += 1

        async def release(self):
            self.releases += 1

        async def residency(self):
            return ResidencyReport()

    mock = MockManaged()
    monkeypatch.setattr(
        daemon, "_load_runtime_deployment",
        lambda _path: SimpleNamespace(provider_id=mock.provider_id),
    )
    monkeypatch.setattr(daemon, "discover_runtime_host_facts", lambda: object())
    monkeypatch.setattr(daemon, "managed_runtime_from_deployment", lambda *_a, **_kw: mock)
    monkeypatch.setattr(daemon, "RuntimeLifecycleManager", RuntimeLifecycleManager)
    monkeypatch.setattr(daemon, "RuntimeHealthSupervisor", RuntimeHealthSupervisor)

    with pytest.raises(RegistrationReached):
        await asyncio.wait_for(daemon.run_worker("unused-worker.toml"), timeout=1)
    assert harness.events[:3] == ["gpu", "client", "register"]
    assert harness.events[-1] == "close"
    assert mock.health_probes >= 2  # lifecycle + supervisor before registration
    assert mock.stops == mock.releases == 1
    assert len(harness.registrations) == 1
    assert harness.registrations[0]["serving"].contracts == declaration(SPEECH).contracts
