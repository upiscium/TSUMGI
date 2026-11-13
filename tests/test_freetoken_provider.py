from __future__ import annotations

import asyncio
import json
import signal
from collections.abc import Mapping
from dataclasses import replace

import httpx
import pytest

from astrumweaver import ResourceShape, WorkerSpec
from astrumweaver.execution import JobRequest
from astrumweaver.runtime import (
    ExecutionDemand,
    GPUTopology,
    ModelDemand,
    ModelTopology,
    ResidencyPolicy,
    RuntimeCatalog,
    RuntimeCompatibilityContext,
    RuntimeHealthState,
    RuntimeHostFacts,
    RuntimeSelection,
    RuntimeSelectionError,
    RuntimeSelectionMode,
    resolve_runtime,
)
from astrumweaver.execution import JobExecutionError
from astrumweaver.gateway.chat import CHAT_JOB_SCHEMA, FREETOKEN_CHAT_ADAPTER
from astrumweaver.runtime.providers import freetoken as freetoken_module
from astrumweaver.runtime.providers.freetoken import (
    FreeTokenExecutor,
    FreeTokenLaunchPolicy,
    FreeTokenManagedRuntime,
    FreeTokenMoeStrategy,
    FreeTokenProvider,
    FreeTokenProviderConfig,
    FreeTokenSubprocessController,
    HttpFreeTokenApi,
)
from astrumweaver.setup import (
    DeploymentPath,
    PrivilegeMode,
    SetupActionKind,
    SetupHostSnapshot,
    build_runtime_setup_plan,
)


def host(
    ram_mb: int = 131072,
    architecture: str = "x86_64",
) -> RuntimeHostFacts:
    return RuntimeHostFacts(
        cpu_count=32,
        host_ram_mb=ram_mb,
        architecture=architecture,
    )


class ProcessGroup:
    """Fake process group: alive until SIGTERM/SIGKILL, unless children linger."""

    def __init__(self, *, lingering_probes: int = 0) -> None:
        self.signals: list[int] = []
        self.alive = True
        self.lingering_probes = lingering_probes

    def killpg(self, pgid: int, signum: int) -> None:
        self.signals.append(signum)
        if signum == 0:
            if self.lingering_probes > 0:
                self.lingering_probes -= 1
                return
            if not self.alive:
                raise ProcessLookupError(pgid)
            return
        if signum in (signal.SIGTERM, signal.SIGKILL):
            self.alive = False


def single_worker(vram_mb: int = 12288) -> WorkerSpec:
    return WorkerSpec(
        worker_id="freetoken-single",
        worker_class="legacy-single",
        resources=ResourceShape(
            gpu_count=1,
            total_vram_mb=vram_mb,
            max_single_gpu_vram_mb=vram_mb,
        ),
        gpu_uuids=("GPU-example-one",),
        capabilities=frozenset({"llm.chat", "text.generate"}),
    )


def multi_worker() -> WorkerSpec:
    return WorkerSpec(
        worker_id="freetoken-multi",
        worker_class="multi-gpu",
        resources=ResourceShape(
            gpu_count=2,
            total_vram_mb=24576,
            max_single_gpu_vram_mb=12288,
        ),
        gpu_uuids=("GPU-example-a", "GPU-example-b"),
        capabilities=frozenset({"llm.chat", "text.generate"}),
    )


def demand(
    *,
    model_ref: str = "Qwen/Qwen3-30B-A3B",
    model_format: str = "huggingface",
    model_topology: ModelTopology = ModelTopology.MOE,
    gpu_topology: GPUTopology = GPUTopology.SINGLE_GPU,
    residency: ResidencyPolicy = ResidencyPolicy.CPU_GPU_HYBRID,
    estimated_size_mb: int | None = 60000,
    min_host_ram_mb: int = 0,
    preferred_host_ram_mb: int = 0,
) -> ExecutionDemand:
    return ExecutionDemand(
        model=ModelDemand(
            model_ref=model_ref,
            model_format=model_format,
            topology=model_topology,
            estimated_size_mb=estimated_size_mb,
        ),
        residency_policy=residency,
        gpu_topology=gpu_topology,
        min_gpu_count=2 if gpu_topology is GPUTopology.MULTI_GPU else 0,
        min_host_ram_mb=min_host_ram_mb,
        preferred_host_ram_mb=max(
            preferred_host_ram_mb,
            min_host_ram_mb,
        ),
    )


def context(
    *,
    worker: WorkerSpec | None = None,
    execution: ExecutionDemand | None = None,
    host_facts: RuntimeHostFacts | None = None,
) -> RuntimeCompatibilityContext:
    return RuntimeCompatibilityContext(
        worker=worker or single_worker(),
        host=host_facts or host(),
        demand=execution or demand(),
    )


def snapshot(ctx: RuntimeCompatibilityContext) -> SetupHostSnapshot:
    return SetupHostSnapshot(
        runtime_host=ctx.host,
        deployment_path=DeploymentPath.NIXOS,
        os_id="nixos",
        os_version="26.11",
        service_manager="systemd",
        package_manager="nix",
        available_commands=frozenset(
            {"nix", "systemctl", "nvidia-smi"}
        ),
        privilege_mode=PrivilegeMode.SUDO,
    )


def test_moe_single_gpu_hybrid_is_compatible() -> None:
    report = FreeTokenProvider().compatibility(context())

    assert report.compatible
    assert "cpu-gpu-hybrid-supported" in {
        reason.code for reason in report.reasons
    }
    assert "host-ram-interconnect-sensitive" in {
        reason.code for reason in report.reasons
    }


def test_dense_is_rejected_as_astrumweaver_scope_not_upstream_limit() -> None:
    report = FreeTokenProvider().compatibility(
        context(
            execution=demand(model_topology=ModelTopology.DENSE)
        )
    )

    assert not report.compatible
    reason = next(
        reason
        for reason in report.reasons
        if reason.code == "model-topology-outside-provider-scope"
    )
    assert "AstrumWeaver v0.x" in reason.message
    assert "upstream FreeToken" in reason.message


def test_local_gguf_is_compatible() -> None:
    report = FreeTokenProvider().compatibility(
        context(
            execution=demand(
                model_ref="/var/lib/astrumweaver/models/qwen.gguf",
                model_format="gguf",
            )
        )
    )

    assert report.compatible


def test_gguf_requires_local_reference() -> None:
    report = FreeTokenProvider().compatibility(
        context(execution=demand(model_ref="org/model.gguf", model_format="gguf"))
    )

    assert not report.compatible
    assert "gguf-reference-must-be-local" in {
        reason.code for reason in report.reasons
    }


def test_ftw_requires_local_reference() -> None:
    report = FreeTokenProvider().compatibility(
        context(
            execution=demand(
                model_ref="org/checkpoint.ftw",
                model_format="ftw",
            )
        )
    )

    assert not report.compatible
    assert "ftw-reference-must-be-local" in {
        reason.code for reason in report.reasons
    }


def test_multi_gpu_worker_is_rejected_instead_of_partial_use() -> None:
    report = FreeTokenProvider().compatibility(
        context(
            worker=multi_worker(),
            execution=demand(gpu_topology=GPUTopology.MULTI_GPU),
        )
    )

    assert not report.compatible
    assert "single-gpu-required" in {
        reason.code for reason in report.reasons
    }


def test_vram_only_is_explicitly_outside_v0_provider_scope() -> None:
    report = FreeTokenProvider().compatibility(
        context(execution=demand(residency=ResidencyPolicy.VRAM_ONLY))
    )

    assert not report.compatible
    assert "vram-only-unsupported" in {
        reason.code for reason in report.reasons
    }


def test_current_provider_rejects_non_x86_64_host() -> None:
    report = FreeTokenProvider().compatibility(
        context(host_facts=host(architecture="aarch64"))
    )

    assert not report.compatible
    assert "architecture-unsupported" in {
        reason.code for reason in report.reasons
    }


def test_explicit_selection_is_preserved_and_setup_plan_is_deterministic() -> None:
    ctx = context()
    provider = FreeTokenProvider()
    selection = RuntimeSelection(
        mode=RuntimeSelectionMode.EXPLICIT,
        provider_id="freetoken",
    )

    resolution = resolve_runtime(
        catalog=RuntimeCatalog([provider]),
        context=ctx,
        selection=selection,
    )
    assert resolution.selected_provider_id == "freetoken"

    intent = provider.setup_intent(ctx)
    assert intent.package_references == ("freetoken[accel]",)
    assert intent.configuration["gpu_uuid"] == "GPU-example-one"
    assert intent.configuration["moe_strategy"] == "auto"
    assert intent.configuration["memory_ratio"] == 0.9
    assert intent.model_preparation.value == "download"

    plan = build_runtime_setup_plan(
        catalog=RuntimeCatalog([provider]),
        context=ctx,
        selection=selection,
        snapshot=snapshot(ctx),
    )

    assert plan.provider_id == "freetoken"
    assert any(
        action.kind is SetupActionKind.ENSURE_PACKAGE
        and action.payload["package_reference"] == "freetoken[accel]"
        for action in plan.actions
    )
    assert any(
        action.kind is SetupActionKind.DOWNLOAD_MODEL
        and action.payload["model_ref"] == "Qwen/Qwen3-30B-A3B"
        for action in plan.actions
    )


def test_local_model_reference_uses_reference_verification() -> None:
    ctx = context(
        execution=demand(
            model_ref="/models/qwen-ftw",
            model_format="ftw",
        )
    )
    provider = FreeTokenProvider()
    intent = provider.setup_intent(ctx)

    assert intent.model_preparation.value == "reference_only"

    plan = build_runtime_setup_plan(
        catalog=RuntimeCatalog([provider]),
        context=ctx,
        selection=RuntimeSelection(
            mode=RuntimeSelectionMode.EXPLICIT,
            provider_id="freetoken",
        ),
        snapshot=snapshot(ctx),
    )
    assert any(
        action.kind is SetupActionKind.VERIFY_MODEL_REFERENCE
        and action.payload["model_ref"] == "/models/qwen-ftw"
        for action in plan.actions
    )


def test_generic_host_ram_requirement_remains_blocking() -> None:
    ctx = context(
        host_facts=host(ram_mb=32768),
        execution=demand(
            min_host_ram_mb=65536,
            preferred_host_ram_mb=98304,
        ),
    )

    with pytest.raises(RuntimeSelectionError) as exc_info:
        resolve_runtime(
            catalog=RuntimeCatalog([FreeTokenProvider()]),
            context=ctx,
            selection=RuntimeSelection(
                mode=RuntimeSelectionMode.EXPLICIT,
                provider_id="freetoken",
            ),
        )

    assert "insufficient-host-ram" in {
        reason.code for reason in exc_info.value.reasons
    }


def test_provider_does_not_invent_model_size_to_ram_formula() -> None:
    report = FreeTokenProvider().compatibility(
        context(
            host_facts=host(ram_mb=16384),
            execution=demand(
                estimated_size_mb=120000,
                min_host_ram_mb=0,
            ),
        )
    )

    assert report.compatible
    assert not any(
        reason.code == "model-exceeds-host-ram"
        for reason in report.reasons
    )


def test_incompatible_context_cannot_create_setup_or_runtime() -> None:
    provider = FreeTokenProvider()
    ctx = context(
        execution=demand(model_topology=ModelTopology.DENSE)
    )

    with pytest.raises(RuntimeError, match="incompatible"):
        provider.setup_intent(ctx)

    compatible_intent = FreeTokenProvider().setup_intent(context())
    with pytest.raises(RuntimeError, match="incompatible"):
        provider.create_runtime(ctx, compatible_intent)


def test_create_runtime_rejects_tampered_gpu_uuid() -> None:
    provider = FreeTokenProvider()
    ctx = context()
    intent = provider.setup_intent(ctx)
    tampered = replace(
        intent,
        configuration={
            **dict(intent.configuration),
            "gpu_uuid": "GPU-not-owned-by-worker",
        },
    )

    with pytest.raises(ValueError, match="GPU UUID"):
        provider.create_runtime(ctx, tampered)


def test_single_gpu_command_uses_exact_worker_uuid_and_free_token_flags() -> None:
    controller = FreeTokenSubprocessController(
        executable="ft",
        base_url="http://127.0.0.1:1919",
        model_ref="Qwen/Qwen3-30B-A3B",
        served_model_name="astrumweaver",
        launch_policy=FreeTokenLaunchPolicy(
            gpu_uuid="GPU-exact-uuid",
            moe_strategy=FreeTokenMoeStrategy.HYBRID,
            memory_ratio=0.85,
            moe_cache_size=96,
            moe_cache_rate=None,
            moe_cpu_threads=16,
            moe_cpu_layers="0.5",
            moe_hybrid_max_fetch=4,
            max_running_requests=1,
        ),
    )

    command = controller.command()

    assert command[:3] == ("ft", "serve", "--model")
    assert command[command.index("--gpu") + 1] == "GPU-exact-uuid"
    assert command[command.index("--moe-strategy") + 1] == "hybrid"
    assert command[command.index("--memory-ratio") + 1] == "0.85"
    assert command[command.index("--moe-cache-size") + 1] == "96"
    assert command[command.index("--moe-cpu-threads") + 1] == "16"
    assert command[command.index("--moe-cpu-layers") + 1] == "0.5"
    assert command[command.index("--moe-hybrid-max-fetch") + 1] == "4"


@pytest.mark.asyncio
async def test_subprocess_does_not_rewrite_gpu_identity(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Process:
        pid = 4321
        returncode = None

        async def wait(self) -> int:
            self.returncode = 0
            return 0

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = dict(kwargs)
        return Process()

    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    monkeypatch.setattr(freetoken_module.os, "killpg", ProcessGroup().killpg)

    controller = FreeTokenSubprocessController(
        executable="ft",
        base_url="http://127.0.0.1:1919",
        model_ref="/models/qwen",
        served_model_name="astrumweaver",
        launch_policy=FreeTokenLaunchPolicy(
            gpu_uuid="GPU-exact-uuid",
            moe_strategy=FreeTokenMoeStrategy.AUTO,
            memory_ratio=0.9,
            moe_cache_size=None,
            moe_cache_rate=None,
            moe_cpu_threads=None,
            moe_cpu_layers=None,
            moe_hybrid_max_fetch=None,
            max_running_requests=1,
        ),
    )

    await controller.start()

    assert captured["args"][
        captured["args"].index("--gpu") + 1
    ] == "GPU-exact-uuid"
    assert "env" not in captured["kwargs"]
    assert captured["kwargs"]["start_new_session"] is True
    assert "stdout" not in captured["kwargs"]
    assert "stderr" not in captured["kwargs"]

    await controller.stop()


@pytest.mark.asyncio
async def test_http_api_uses_current_freetoken_control_and_openai_endpoints() -> None:
    requests: list[tuple[str, str, Mapping | None]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = None
        if request.content:
            body = json.loads(request.content)
        requests.append((request.method, request.url.path, body))
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/v1/models":
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [{"id": "astrumweaver", "object": "model"}],
                },
            )
        if request.url.path == "/v1/chat/completions":
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "hello",
                            }
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 3,
                        "completion_tokens": 2,
                        "total_tokens": 5,
                    },
                },
            )
        if request.url.path == "/v1/completions":
            return httpx.Response(
                200,
                json={
                    "choices": [{"text": "generated"}],
                    "usage": {"total_tokens": 4},
                },
            )
        if request.url.path == "/v1/stats":
            return httpx.Response(
                200,
                json={
                    "model": {"id": "astrumweaver", "moe": True},
                    "vram_bytes": 123456,
                },
            )
        return httpx.Response(404)

    api = HttpFreeTokenApi(
        "http://127.0.0.1:1919",
        transport=httpx.MockTransport(handler),
    )
    try:
        assert await api.health()
        assert await api.models() == ("astrumweaver",)
        await api.chat(
            {
                "model": "astrumweaver",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
            }
        )
        await api.completion(
            {
                "model": "astrumweaver",
                "prompt": "hi",
                "stream": False,
            }
        )
        assert (await api.stats())["vram_bytes"] == 123456
    finally:
        await api.close()

    assert [path for _, path, _ in requests] == [
        "/health",
        "/v1/models",
        "/v1/chat/completions",
        "/v1/completions",
        "/v1/stats",
    ]


@pytest.mark.asyncio
async def test_http_health_requires_ready_status_not_loading() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"status": "loading", "model": "astrumweaver"},
        )

    api = HttpFreeTokenApi(
        "http://127.0.0.1:1919",
        transport=httpx.MockTransport(handler),
    )
    try:
        assert not await api.health()
    finally:
        await api.close()


class FakeApi:
    def __init__(
        self,
        *,
        reachable: bool = True,
        models: tuple[str, ...] = ("astrumweaver",),
        stats: Mapping[str, object] | None = None,
    ) -> None:
        self.reachable = reachable
        self.model_ids = models
        self.stats_doc = dict(stats or {})
        self.closed = False
        self.chat_started = asyncio.Event()
        self.block_chat = False
        self.chat_payloads: list[dict] = []
        self.completion_payloads: list[dict] = []

    async def health(self) -> bool:
        return self.reachable

    async def models(self) -> tuple[str, ...]:
        if not self.reachable:
            raise httpx.ConnectError("offline")
        return self.model_ids

    async def chat(self, payload: Mapping[str, object]):
        self.chat_payloads.append(dict(payload))
        self.chat_started.set()
        if self.block_chat:
            await asyncio.Event().wait()
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "hello",
                    }
                }
            ],
            "usage": {
                "prompt_tokens": 3,
                "completion_tokens": 2,
                "total_tokens": 5,
            },
        }

    async def completion(self, payload: Mapping[str, object]):
        self.completion_payloads.append(dict(payload))
        return {
            "choices": [{"text": "generated"}],
            "usage": {"total_tokens": 4},
        }

    async def stats(self):
        return dict(self.stats_doc)

    async def close(self) -> None:
        self.closed = True


class FakeProcess:
    def __init__(
        self,
        api: FakeApi,
        *,
        running: bool = False,
        exit_on_start: bool = False,
    ) -> None:
        self.api = api
        self._running = running
        self.exit_on_start = exit_on_start
        self.starts = 0
        self.stops = 0

    @property
    def running(self) -> bool:
        return self._running

    async def start(self) -> None:
        self.starts += 1
        if self.exit_on_start:
            self._running = False
            self.api.reachable = False
            return
        self._running = True
        self.api.reachable = True

    async def stop(self) -> None:
        self.stops += 1
        self._running = False
        self.api.reachable = False


def launch_policy() -> FreeTokenLaunchPolicy:
    return FreeTokenLaunchPolicy(
        gpu_uuid="GPU-example-one",
        moe_strategy=FreeTokenMoeStrategy.AUTO,
        memory_ratio=0.9,
        moe_cache_size=None,
        moe_cache_rate=None,
        moe_cpu_threads=None,
        moe_cpu_layers=None,
        moe_hybrid_max_fetch=None,
        max_running_requests=1,
    )


@pytest.mark.asyncio
async def test_executor_chat_completion_metrics_and_model_binding() -> None:
    api = FakeApi()
    executor = FreeTokenExecutor(
        api=api,
        model_ref="Qwen/Qwen3-30B-A3B",
        served_model_name="astrumweaver",
        residency_metadata={},
    )

    chat = await executor.execute(
        JobRequest(
            job_id="chat",
            capability="llm.chat",
            payload={
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    )
    assert chat.text == "hello"
    assert chat.metrics["total_tokens"] == 5
    assert api.chat_payloads[0]["model"] == "astrumweaver"
    assert api.chat_payloads[0]["stream"] is False

    completion = await executor.execute(
        JobRequest(
            job_id="completion",
            capability="text.generate",
            payload={"prompt": "hi"},
        )
    )
    assert completion.text == "generated"
    assert api.completion_payloads[0]["stream"] is False

    with pytest.raises(ValueError, match="does not match"):
        await executor.execute(
            JobRequest(
                job_id="other-model",
                capability="text.generate",
                payload={"model": "other", "prompt": "hi"},
            )
        )


@pytest.mark.asyncio
async def test_executor_cancellation_aborts_inflight_request() -> None:
    api = FakeApi()
    api.block_chat = True
    executor = FreeTokenExecutor(
        api=api,
        model_ref="Qwen/Qwen3-30B-A3B",
        served_model_name="astrumweaver",
        residency_metadata={},
    )

    execution = asyncio.create_task(
        executor.execute(
            JobRequest(
                job_id="cancel",
                capability="llm.chat",
                payload={"messages": []},
            )
        )
    )
    await api.chat_started.wait()

    await executor.cancel("cancel")

    with pytest.raises(asyncio.CancelledError):
        await execution


@pytest.mark.asyncio
async def test_residency_does_not_invent_memory_bytes() -> None:
    executor = FreeTokenExecutor(
        api=FakeApi(stats={"model": {"id": "astrumweaver", "moe": True}}),
        model_ref="Qwen/Qwen3-30B-A3B",
        served_model_name="astrumweaver",
        residency_metadata={
            "moe_strategy": "auto",
            "host_ram_mb": 131072,
        },
    )

    report = await executor.residency()

    assert len(report.items) == 1
    assert report.items[0].accelerator_memory_bytes is None
    assert report.items[0].metadata["observed_moe"] is True
    assert report.items[0].metadata["host_ram_mb"] == 131072


@pytest.mark.asyncio
async def test_residency_treats_zero_vram_as_unobserved() -> None:
    executor = FreeTokenExecutor(
        api=FakeApi(stats={"vram_bytes": 0}),
        model_ref="Qwen/Qwen3-30B-A3B",
        served_model_name="astrumweaver",
        residency_metadata={},
    )

    report = await executor.residency()

    assert report.items[0].accelerator_memory_bytes is None


@pytest.mark.asyncio
async def test_residency_uses_measured_vram_when_stats_reports_it() -> None:
    executor = FreeTokenExecutor(
        api=FakeApi(
            stats={
                "model": {"id": "astrumweaver", "moe": True},
                "vram_bytes": 123456,
            }
        ),
        model_ref="Qwen/Qwen3-30B-A3B",
        served_model_name="astrumweaver",
        residency_metadata={},
    )

    report = await executor.residency()

    assert report.items[0].accelerator_memory_bytes == 123456


@pytest.mark.asyncio
async def test_managed_runtime_starts_checks_alias_and_stops() -> None:
    api = FakeApi(reachable=False)
    process = FakeProcess(api)
    runtime = FreeTokenManagedRuntime(
        api=api,
        process=process,
        context=context(),
        model_ref="Qwen/Qwen3-30B-A3B",
        served_model_name="astrumweaver",
        startup_timeout_seconds=1.0,
        launch_policy=launch_policy(),
    )

    await runtime.start()

    assert process.starts == 1
    assert (await runtime.health()).ready
    await runtime.stop()
    assert process.stops == 1


@pytest.mark.asyncio
async def test_managed_runtime_fails_fast_when_owned_process_exits_before_readiness() -> None:
    api = FakeApi(reachable=False)
    process = FakeProcess(api, exit_on_start=True)
    runtime = FreeTokenManagedRuntime(
        api=api,
        process=process,
        context=context(),
        model_ref="Qwen/Qwen3-30B-A3B",
        served_model_name="astrumweaver",
        startup_timeout_seconds=60.0,
        launch_policy=launch_policy(),
    )

    with pytest.raises(RuntimeError, match="owned process exited before readiness"):
        await runtime.start()

    assert process.starts == 1
    assert process.stops == 1


@pytest.mark.asyncio
async def test_managed_runtime_rejects_external_server() -> None:
    api = FakeApi(reachable=True)
    process = FakeProcess(api, running=False)
    runtime = FreeTokenManagedRuntime(
        api=api,
        process=process,
        context=context(),
        model_ref="Qwen/Qwen3-30B-A3B",
        served_model_name="astrumweaver",
        startup_timeout_seconds=1.0,
        launch_policy=launch_policy(),
    )

    health = await runtime.health()
    assert health.state is RuntimeHealthState.FAILED

    with pytest.raises(RuntimeError, match="external FreeToken"):
        await runtime.start()


@pytest.mark.asyncio
async def test_managed_runtime_cleans_up_failed_start() -> None:
    api = FakeApi(reachable=False, models=("wrong-alias",))
    process = FakeProcess(api)
    runtime = FreeTokenManagedRuntime(
        api=api,
        process=process,
        context=context(),
        model_ref="Qwen/Qwen3-30B-A3B",
        served_model_name="astrumweaver",
        startup_timeout_seconds=1.0,
        launch_policy=launch_policy(),
    )

    with pytest.raises(RuntimeError, match="model alias"):
        await runtime.start()

    assert process.starts == 1
    assert process.stops == 1


@pytest.mark.asyncio
async def test_managed_runtime_release_closes_api_once() -> None:
    api = FakeApi()
    process = FakeProcess(api, running=True)
    runtime = FreeTokenManagedRuntime(
        api=api,
        process=process,
        context=context(),
        model_ref="Qwen/Qwen3-30B-A3B",
        served_model_name="astrumweaver",
        startup_timeout_seconds=1.0,
        launch_policy=launch_policy(),
    )

    await runtime.release()
    await runtime.release()

    assert api.closed


def test_pre_turing_launch_settings_are_validated() -> None:
    config = FreeTokenProviderConfig(
        attention_backend="triton",
        max_seq_len=8192,
        cuda_home="/usr/local/cuda-12.6",
        library_paths=("/opt/freetoken/lib",),
    )
    assert config.library_paths == ("/opt/freetoken/lib",)

    for bad in (
        {"attention_backend": "Triton; rm"},
        {"max_seq_len": 0},
        {"max_seq_len": True},
        {"cuda_home": "relative/cuda"},
        {"library_paths": ("/ok", "/a:/b")},
    ):
        with pytest.raises(ValueError):
            FreeTokenProviderConfig(**bad)


def test_command_adds_attention_backend_and_context_override() -> None:
    controller = FreeTokenSubprocessController(
        executable="ft",
        base_url="http://127.0.0.1:1919",
        model_ref="/models/qwen.gguf",
        served_model_name="astrumweaver",
        launch_policy=replace(
            launch_policy(), attention_backend="triton", max_seq_len=8192
        ),
    )

    command = controller.command()

    assert command[command.index("--attention-backend") + 1] == "triton"
    assert command[command.index("--max-seq-len-override") + 1] == "8192"
    assert command[command.index("--kv-reserve-tokens") + 1] == "8192"


def test_environment_adds_toolkit_paths_without_touching_gpu_selection() -> None:
    controller = FreeTokenSubprocessController(
        executable="/opt/freetoken/venv/bin/ft",
        base_url="http://127.0.0.1:1919",
        model_ref="/models/qwen.gguf",
        served_model_name="astrumweaver",
        launch_policy=launch_policy(),
        cuda_home="/usr/local/cuda-12.6",
        library_paths=("/opt/freetoken/lib",),
    )

    env = controller.environment(
        {
            "PATH": "/usr/bin",
            "LD_LIBRARY_PATH": "/old",
            "CUDA_VISIBLE_DEVICES": "GPU-example-one",
        }
    )

    assert env is not None
    assert env["CUDA_HOME"] == "/usr/local/cuda-12.6"
    assert env["PATH"] == "/usr/local/cuda-12.6/bin:/opt/freetoken/venv/bin:/usr/bin"
    assert env["LD_LIBRARY_PATH"] == "/opt/freetoken/lib:/old"
    assert env["CUDA_VISIBLE_DEVICES"] == "GPU-example-one"


@pytest.mark.asyncio
async def test_stop_waits_for_lingering_children_in_the_process_group(
    monkeypatch,
) -> None:
    group = ProcessGroup(lingering_probes=2)
    captured: dict[str, object] = {}

    class Process:
        pid = 4321
        returncode = None

        async def wait(self) -> int:
            self.returncode = 0
            return 0

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["kwargs"] = dict(kwargs)
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(freetoken_module.os, "killpg", group.killpg)
    controller = FreeTokenSubprocessController(
        executable="/opt/freetoken/venv/bin/ft",
        base_url="http://127.0.0.1:1919",
        model_ref="/models/qwen.gguf",
        served_model_name="astrumweaver",
        launch_policy=launch_policy(),
        cuda_home="/usr/local/cuda-12.6",
    )

    await controller.start()
    assert captured["kwargs"]["env"]["CUDA_HOME"] == "/usr/local/cuda-12.6"
    await controller.stop()

    assert group.signals == [signal.SIGTERM, 0, 0, 0]
    assert not controller.running


def test_setup_intent_and_runtime_carry_pre_turing_settings() -> None:
    created: dict[str, object] = {}
    provider = FreeTokenProvider(
        FreeTokenProviderConfig(
            attention_backend="triton",
            max_seq_len=8192,
            cuda_home="/usr/local/cuda-12.6",
            library_paths=("/opt/freetoken/lib",),
        ),
        api_factory=lambda base_url: FakeApi(),
        process_factory=lambda **kwargs: created.update(kwargs) or FakeProcess(FakeApi()),
    )
    ctx = context(
        execution=demand(model_ref="/models/qwen.gguf", model_format="gguf")
    )

    setup = provider.setup_intent(ctx)
    provider.create_runtime(ctx, setup)

    assert setup.configuration["attention_backend"] == "triton"
    assert setup.configuration["max_seq_len"] == 8192
    assert created["launch_policy"].attention_backend == "triton"
    assert created["launch_policy"].max_seq_len == 8192
    assert created["cuda_home"] == "/usr/local/cuda-12.6"
    assert created["library_paths"] == ("/opt/freetoken/lib",)


class GatewayApi(FakeApi):
    def __init__(self, *, input_tokens: int = 10, chunks=None) -> None:
        super().__init__()
        self.input_tokens = input_tokens
        self.chunks = list(chunks or [])
        self.counted: list[dict] = []
        self.stream_payloads: list[dict] = []

    async def chat_input_tokens(self, payload):
        self.counted.append(dict(payload))
        return self.input_tokens

    async def chat_stream(self, payload):
        self.stream_payloads.append(dict(payload))
        for chunk in self.chunks:
            yield chunk


def gateway_job(
    *,
    stream: bool = False,
    max_tokens: int = 16,
    adapter_id: str = FREETOKEN_CHAT_ADAPTER,
    extra_request: Mapping[str, object] | None = None,
) -> JobRequest:
    return JobRequest(
        job_id="gw",
        capability="llm.chat",
        payload={
            "schema_version": CHAT_JOB_SCHEMA,
            "adapter_id": adapter_id,
            "request": {
                "messages": [{"role": "user", "content": "hi"}],
                "stream": stream,
                "max_tokens": max_tokens,
                **dict(extra_request or {}),
            },
            "limits": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 120},
        },
    )


def gateway_executor(api: GatewayApi) -> FreeTokenExecutor:
    return FreeTokenExecutor(
        api=api,
        model_ref="/models/qwen.gguf",
        served_model_name="qwen3.6-35b-a3b",
        residency_metadata={},
    )


@pytest.mark.asyncio
async def test_gateway_chat_counts_input_and_binds_served_model() -> None:
    api = GatewayApi(input_tokens=60)

    result = await gateway_executor(api).execute(gateway_job(max_tokens=50))

    assert result.text == "hello"
    assert api.counted[0]["model"] == "qwen3.6-35b-a3b"
    assert api.chat_payloads[0]["model"] == "qwen3.6-35b-a3b"
    assert api.chat_payloads[0]["max_tokens"] == 50


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("job", "input_tokens", "code"),
    [
        (gateway_job(), 101, "context_length_exceeded"),
        (gateway_job(max_tokens=50), 71, "context_length_exceeded"),
        (gateway_job(adapter_id="llama-cpp-chat-v1"), 1, "unsupported_chat_adapter"),
        (gateway_job(extra_request={"model": "other"}), 1, "invalid_chat_job"),
        (gateway_job(max_tokens=51), 1, "output_limit_exceeded"),
        (gateway_job(stream=True), 1, "streaming_requires_event_sink"),
    ],
)
async def test_gateway_chat_rejections_never_reach_generation(
    job: JobRequest, input_tokens: int, code: str
) -> None:
    api = GatewayApi(input_tokens=input_tokens)

    with pytest.raises(JobExecutionError) as excinfo:
        await gateway_executor(api).execute(job)

    assert excinfo.value.code == code
    assert api.chat_payloads == []


class Sink:
    def __init__(self) -> None:
        self.events: list = []

    async def emit(self, event) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_gateway_stream_emits_validated_chunks_and_usage() -> None:
    chunks = [
        {"object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}}]},
        {"object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"reasoning_content": "think"}}]},
        {"object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"content": "hel"}}]},
        {"object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"content": "lo"}, "finish_reason": "stop"}]},
        {"object": "chat.completion.chunk", "choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 2, "total_tokens": 11}},
    ]
    api = GatewayApi(chunks=chunks)
    sink = Sink()

    result = await gateway_executor(api).execute_stream(gateway_job(stream=True), sink)

    assert result.text == "hello"
    assert result.metrics["total_tokens"] == 11
    assert [event.kind for event in sink.events] == ["chat.completion.chunk"] * 5
    assert api.stream_payloads[0]["stream_options"] == {"include_usage": True}
    assert api.counted, "stream input must be counted before generation"


@pytest.mark.asyncio
async def test_gateway_stream_rejects_invalid_provider_chunk() -> None:
    api = GatewayApi(chunks=[{"choices": [{}, {}]}])

    with pytest.raises(JobExecutionError) as excinfo:
        await gateway_executor(api).execute_stream(gateway_job(stream=True), Sink())

    assert excinfo.value.code == "invalid_provider_response"


@pytest.mark.asyncio
async def test_http_stream_and_input_token_endpoints() -> None:
    sse = (
        ": keepalive\n\n"
        'data: {"choices": [{"index": 0, "delta": {"content": "a"}}]}\n\n'
        "data: [DONE]\n\n"
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/chat/completions":
            return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})
        if request.url.path == "/v1/chat/completions/input_tokens":
            body = json.loads(request.content)
            return httpx.Response(200, json={"input_tokens": len(body["messages"]) * 7})
        return httpx.Response(404)

    api = HttpFreeTokenApi("http://127.0.0.1:1919", transport=httpx.MockTransport(handler))
    try:
        chunks = [chunk async for chunk in api.chat_stream({"messages": [], "stream": True})]
        count = await api.chat_input_tokens({"messages": [{"role": "user", "content": "x"}]})
    finally:
        await api.close()

    assert chunks == [{"choices": [{"index": 0, "delta": {"content": "a"}}]}]
    assert count == 7


@pytest.mark.asyncio
async def test_http_input_tokens_rejects_invalid_count() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"input_tokens": True})

    api = HttpFreeTokenApi("http://127.0.0.1:1919", transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(RuntimeError, match="invalid input_tokens"):
            await api.chat_input_tokens({"messages": []})
    finally:
        await api.close()


def test_command_adds_prefill_chunk_size() -> None:
    controller = FreeTokenSubprocessController(
        executable="ft",
        base_url="http://127.0.0.1:1919",
        model_ref="/models/qwen.gguf",
        served_model_name="astrumweaver",
        launch_policy=replace(launch_policy(), max_prefill_tokens=2048),
    )

    command = controller.command()

    assert command[command.index("--max-prefill-length") + 1] == "2048"


class ExitedProcess:
    pid = 4321
    returncode = 1

    async def wait(self) -> int:
        return 1


@pytest.mark.asyncio
async def test_stop_after_exit_only_probes_a_gone_group(monkeypatch) -> None:
    group = ProcessGroup()
    group.alive = False
    monkeypatch.setattr(freetoken_module.os, "killpg", group.killpg)
    controller = FreeTokenSubprocessController(
        executable="ft",
        base_url="http://127.0.0.1:1919",
        model_ref="/models/qwen.gguf",
        served_model_name="astrumweaver",
        launch_policy=launch_policy(),
    )
    controller._process = ExitedProcess()

    await controller.stop()

    assert group.signals == [0]
    assert controller._process is None


@pytest.mark.asyncio
async def test_stop_treats_foreign_group_as_not_ours(monkeypatch) -> None:
    signals: list[int] = []

    def killpg(pgid: int, signum: int) -> None:
        signals.append(signum)
        raise PermissionError(pgid)

    monkeypatch.setattr(freetoken_module.os, "killpg", killpg)
    controller = FreeTokenSubprocessController(
        executable="ft",
        base_url="http://127.0.0.1:1919",
        model_ref="/models/qwen.gguf",
        served_model_name="astrumweaver",
        launch_policy=launch_policy(),
    )
    controller._process = ExitedProcess()

    await controller.stop()

    assert signals == [0]
    assert controller._process is None


@pytest.mark.asyncio
async def test_restart_reaps_the_previous_group_first(monkeypatch) -> None:
    group = ProcessGroup(lingering_probes=1)
    group.alive = False
    order: list[str] = []

    class Process:
        pid = 9876
        returncode = None

        async def wait(self) -> int:
            self.returncode = 0
            return 0

    async def fake_create_subprocess_exec(*args, **kwargs):
        order.append(f"spawn after {len(group.signals)} signals")
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(freetoken_module.os, "killpg", group.killpg)
    controller = FreeTokenSubprocessController(
        executable="ft",
        base_url="http://127.0.0.1:1919",
        model_ref="/models/qwen.gguf",
        served_model_name="astrumweaver",
        launch_policy=launch_policy(),
    )
    controller._process = ExitedProcess()

    await controller.start()

    assert order == ["spawn after 2 signals"]
    assert controller.running


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra",
    [
        {"tools": [{"type": "function", "function": {"name": "f", "parameters": {}}}]},
        {"tool_choice": "required"},
    ],
)
async def test_tools_are_rejected_when_the_deployment_disables_them(extra) -> None:
    api = GatewayApi()
    executor = FreeTokenExecutor(
        api=api,
        model_ref="/models/qwen.gguf",
        served_model_name="qwen3.6-35b-a3b",
        residency_metadata={},
        tools_enabled=False,
    )

    with pytest.raises(JobExecutionError) as excinfo:
        await executor.execute(gateway_job(extra_request=extra))

    assert excinfo.value.code == "unsupported_feature"
    assert api.counted == [] and api.chat_payloads == []

    allowed = gateway_executor(GatewayApi())
    assert (await allowed.execute(gateway_job(extra_request=extra))).text == "hello"


@pytest.mark.parametrize("tools_enabled", [True, False])
def test_executor_declares_tools_serving_feature(tools_enabled: bool) -> None:
    from astrumweaver.serving import (
        DeploymentIdentity,
        ServingContract,
        ServingDeploymentDeclaration,
    )
    from astrumweaver.worker.runtime import require_executor_serving_features

    digest = "sha256:" + "a" * 64
    deployment = DeploymentIdentity(
        provider_id="freetoken",
        runtime_artifact_sha256=digest,
        adapter_artifact_sha256=digest,
        model_artifact_sha256=digest,
        execution_config_sha256=digest,
        quantization="UD-Q4_K_XL",
        tokenizer_artifact_sha256=digest,
        template_artifact_sha256=digest,
    )
    declaration = ServingDeploymentDeclaration(
        deployment=deployment,
        contracts=(
            ServingContract(
                deployment_revision=deployment.revision,
                capability="llm.chat",
                operation_schema="openai-chat-completions-v1",
                validation_evidence_sha256=digest,
                features=frozenset({"tools"}),
                limits={
                    "input_tokens": 10240,
                    "output_tokens": 2048,
                    "total_tokens": 12288,
                    "request_bytes": 65536,
                },
            ),
        ),
    )
    executor = FreeTokenExecutor(
        api=FakeApi(),
        model_ref="/models/qwen.gguf",
        served_model_name="qwen3.6-35b-a3b",
        residency_metadata={},
        tools_enabled=tools_enabled,
    )

    if tools_enabled:
        require_executor_serving_features(executor, declaration)
    else:
        with pytest.raises(RuntimeError, match="tools"):
            require_executor_serving_features(executor, declaration)


class ToolCallApi(GatewayApi):
    """Returns FreeToken's real non-stream tool-call shape (with index)."""

    async def chat(self, payload):
        self.chat_payloads.append(dict(payload))
        return {
            "id": "chatcmpl-0",
            "object": "chat.completion",
            "model": "qwen3.6-35b-a3b",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "reasoning_content": "thinking",
                        "tool_calls": [
                            {
                                "id": "call_synthetic-echo_0_e2c1e53b",
                                "index": 0,
                                "type": "function",
                                "function": {
                                    "name": "synthetic_echo",
                                    "arguments": '{"text": "tsumgi"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 275, "completion_tokens": 108, "total_tokens": 383},
        }


@pytest.mark.asyncio
async def test_gateway_tool_call_response_passes_gateway_normalization() -> None:
    from astrumweaver.gateway.chat import normalize_chat_completion

    result = await gateway_executor(ToolCallApi()).execute(gateway_job())

    call = result.outputs["choices"][0]["message"]["tool_calls"][0]
    assert "index" not in call
    normalized = normalize_chat_completion(
        result.outputs, profile_id="qwen3.6-35b-a3b", job_id="gw", created=1
    )
    assert normalized["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] == (
        '{"text": "tsumgi"}'
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("thinking", [None, False, True])
async def test_enable_thinking_is_sent_only_when_configured(thinking) -> None:
    api = GatewayApi()
    executor = FreeTokenExecutor(
        api=api,
        model_ref="/models/qwen.gguf",
        served_model_name="qwen3.6-35b-a3b",
        residency_metadata={},
        enable_thinking=thinking,
    )

    await executor.execute(gateway_job())

    if thinking is None:
        assert "chat_template_kwargs" not in api.chat_payloads[0]
    else:
        assert api.chat_payloads[0]["chat_template_kwargs"] == {"enable_thinking": thinking}
        assert api.counted[0]["chat_template_kwargs"] == {"enable_thinking": thinking}
