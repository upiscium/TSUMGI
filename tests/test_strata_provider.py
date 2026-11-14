from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Mapping
from pathlib import Path

import httpx
import pytest

from astrumweaver import ResourceShape, WorkerSpec
from astrumweaver.execution import JobExecutionError, JobRequest
from astrumweaver.gateway.chat import CHAT_JOB_SCHEMA, FREETOKEN_CHAT_ADAPTER, STRATA_CHAT_ADAPTER
from astrumweaver.runtime import (
    ExecutionDemand,
    GPUTopology,
    ModelDemand,
    ModelTopology,
    ResidencyPolicy,
    RuntimeCompatibilityContext,
    RuntimeHealthState,
    RuntimeHostFacts,
)
from astrumweaver.runtime.deployment import _PROVIDER_TYPES
from astrumweaver.runtime.providers.strata import (
    HttpStrataApi,
    StrataExecutor,
    StrataHttpError,
    StrataManagedRuntime,
    StrataProvider,
    StrataProviderConfig,
    StrataRunConfig,
    StrataSubprocessController,
)
from astrumweaver.serving import ServingContract
from astrumweaver.worker.runtime import require_executor_serving_features

MODEL = "/models/qwen36/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf"
NAME = "qwen3.6-35b-a3b-ud-q4_k_xl"
OVERFLOW = (
    "request exceeds the context window: prompt (12000 tokens) + max tokens (2048) "
    "exceeds the context (12288); requests are never truncated"
)


def run_config(**overrides) -> StrataRunConfig:
    values = dict(model_name=NAME, model_path=MODEL, max_context=12288, fit_max_tokens=False, cuda=12)
    values.update(overrides)
    return StrataRunConfig(**values)


def config(**overrides) -> StrataProviderConfig:
    values = dict(served_model_name=NAME, context_tokens=12288)
    values.update(overrides)
    return StrataProviderConfig(**values)


def ctx(
    *,
    model_ref: str = MODEL,
    topology: ModelTopology = ModelTopology.MOE,
    residency: ResidencyPolicy = ResidencyPolicy.CPU_GPU_HYBRID,
    gpus: tuple[str, ...] = ("GPU-a",),
) -> RuntimeCompatibilityContext:
    worker = WorkerSpec(
        worker_id="altair",
        worker_class="gpu-single" if len(gpus) == 1 else "gpu-multi",
        resources=ResourceShape(gpu_count=len(gpus), total_vram_mb=11264 * len(gpus), max_single_gpu_vram_mb=11264),
        gpu_uuids=gpus,
        capabilities=frozenset({"llm.chat"}),
    )
    demand = ExecutionDemand(
        model=ModelDemand(model_ref=model_ref, model_format="gguf", topology=topology, estimated_size_mb=21000),
        residency_policy=residency,
        gpu_topology=GPUTopology.SINGLE_GPU if len(gpus) == 1 else GPUTopology.MULTI_GPU,
    )
    return RuntimeCompatibilityContext(
        worker=worker, host=RuntimeHostFacts(cpu_count=8, host_ram_mb=32768, architecture="x86_64"), demand=demand
    )


def provider(rc: StrataRunConfig | None = None, **kwargs) -> StrataProvider:
    return StrataProvider(config(**kwargs), run_config_loader=lambda path: rc or run_config())


# --- config and run config -------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"base_url": "http://10.0.0.1:8080"},
        {"base_url": "http://127.0.0.1"},
        {"base_url": "http://localhost:8080"},
        {"executable": "strata"},
        {"run_config": "relative.json"},
        {"ready_dir": "/run/a:b"},
        {"parallel": 0},
        {"parallel": 9},
        {"context_tokens": True},
        {"shutdown_timeout_seconds": 301},
        {"api_key_file": "key.txt"},
    ],
)
def test_config_rejects_unsafe_values(kwargs) -> None:
    with pytest.raises((ValueError, TypeError)):
        config(**kwargs)


def test_run_config_reads_the_bound_fields(tmp_path: Path) -> None:
    path = tmp_path / "strata-qwen36.json"
    path.write_text(json.dumps({
        "exe": "component:strata-engine",
        "args": ["--native", MODEL, "--max-context", "12288", "--prefill", "auto", "--mtp", "2"],
        "model_name": NAME,
        "cuda": 12,
        "fit_max_tokens": False,
    }))
    assert StrataRunConfig.load(str(path)) == run_config()


def test_run_config_unreadable_is_a_value_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unreadable"):
        StrataRunConfig.load(str(tmp_path / "missing.json"))


# --- compatibility ----------------------------------------------------------


def test_matching_run_config_is_compatible() -> None:
    report = provider().compatibility(ctx())
    assert report.compatible, [r.code for r in report.reasons if r.blocking]


@pytest.mark.parametrize(
    ("rc", "code"),
    [
        (run_config(model_name="other"), "run-config-model-name-mismatch"),
        (run_config(model_path="/models/other.gguf"), "run-config-model-mismatch"),
        (run_config(max_context=16384), "run-config-context-mismatch"),
        (run_config(fit_max_tokens=None), "run-config-clamps-max-tokens"),
        (run_config(fit_max_tokens=True), "run-config-clamps-max-tokens"),
    ],
)
def test_run_config_must_match_the_deployment(rc, code) -> None:
    report = provider(rc).compatibility(ctx())
    assert not report.compatible
    assert code in {r.code for r in report.reasons if r.blocking}


@pytest.mark.parametrize(
    ("context", "code"),
    [
        (ctx(topology=ModelTopology.DENSE), "model-topology-outside-provider-scope"),
        (ctx(residency=ResidencyPolicy.VRAM_ONLY), "vram-only-unsupported"),
        (ctx(model_ref="models/x.gguf"), "model-reference-must-be-local"),
    ],
)
def test_out_of_scope_demands_are_blocking(context, code) -> None:
    report = StrataProvider(config(), run_config_loader=lambda path: run_config(model_path=context.demand.model.model_ref)).compatibility(context)
    assert code in {r.code for r in report.reasons if r.blocking}


def test_unreadable_run_config_is_blocking() -> None:
    def loader(path: str) -> StrataRunConfig:
        raise ValueError("Strata run config is unreadable")

    report = StrataProvider(config(), run_config_loader=loader).compatibility(ctx())
    assert "run-config-unreadable" in {r.code for r in report.reasons if r.blocking}


def test_create_runtime_rejects_tampered_setup() -> None:
    p = provider()
    intent = p.setup_intent(ctx())
    tampered = type(intent)(
        provider_id=intent.provider_id,
        package_references=intent.package_references,
        configuration={**intent.configuration, "run_config": "/tmp/other.json"},
        model_preparation=intent.model_preparation,
        model_ref=intent.model_ref,
        requires_privilege=intent.requires_privilege,
        metadata=intent.metadata,
    )
    with pytest.raises(ValueError, match="run_config"):
        p.create_runtime(ctx(), tampered)


def test_strata_is_a_registered_runtime_provider() -> None:
    assert _PROVIDER_TYPES["strata"] == (StrataProvider, StrataProviderConfig)


# --- process ---------------------------------------------------------------


def controller(tmp_path: Path, **kwargs) -> StrataSubprocessController:
    values = dict(
        executable="/opt/strata/bin/strata",
        run_config="/opt/strata/run.json",
        base_url="http://127.0.0.1:8080",
        home="/opt/strata/home",
        data_dir="/opt/strata/data",
        ready_dir=str(tmp_path),
        parallel=1,
        shutdown_timeout_seconds=30.0,
    )
    values.update(kwargs)
    return StrataSubprocessController(**values)


def test_command_runs_strata_serve_in_the_foreground(tmp_path: Path) -> None:
    assert controller(tmp_path).command() == (
        "/opt/strata/bin/strata", "serve", "--profile", "/opt/strata/run.json",
        "--host", "127.0.0.1", "--port", "8080", "--parallel", "1",
        "--ready-file", str(tmp_path / "strata-8080.ready.json"),
        "--shutdown-timeout", "30", "--output", "jsonl",
    )


def test_environment_points_at_the_install_and_keeps_the_key_out_of_argv(tmp_path: Path) -> None:
    c = controller(tmp_path, api_key="s3cret")
    env = c.environment({"PATH": "/usr/bin", "STRATA_API_KEY": "stale", "CUDA_VISIBLE_DEVICES": "GPU-a"})
    assert env["HOME"] == "/opt/strata/home"
    assert env["STRATA_DATA_DIR"] == "/opt/strata/data"
    assert env["STRATA_API_KEY"] == "s3cret"
    assert env["CUDA_VISIBLE_DEVICES"] == "GPU-a"
    assert all("s3cret" not in arg for arg in c.command())
    assert "STRATA_API_KEY" not in controller(tmp_path).environment({"STRATA_API_KEY": "stale"})


class FakeProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode: int | None = None


def write_ready(tmp_path: Path, **record) -> None:
    base = {"pid": 4242, "instance_id": "i1", "model_state": "ready", "api_state": "ready"}
    base.update(record)
    (tmp_path / "strata-8080.ready.json").write_text(json.dumps(base))


def test_ready_record_requires_our_process_and_ready_states(tmp_path: Path, monkeypatch) -> None:
    c = controller(tmp_path)
    c._process = FakeProcess(4242)
    assert c.ready_record() is None
    write_ready(tmp_path)
    assert c.ready_record()["instance_id"] == "i1"
    write_ready(tmp_path, model_state="loading")
    assert c.ready_record() is None
    monkeypatch.setattr(os, "getpgid", lambda pid: 4242 if pid == 5000 else 1)
    write_ready(tmp_path, pid=5000)
    assert c.ready_record() is not None
    write_ready(tmp_path, pid=6000)
    assert c.ready_record() is None
    c._process.returncode = 0
    write_ready(tmp_path)
    assert c.ready_record() is None


async def test_start_removes_a_stale_ready_file(tmp_path: Path, monkeypatch) -> None:
    write_ready(tmp_path)
    captured: dict = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        assert not (tmp_path / "strata-8080.ready.json").exists()
        return FakeProcess(4242)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    c = controller(tmp_path)
    await c.start()
    assert captured["kwargs"]["start_new_session"] is True
    assert captured["kwargs"]["env"]["HOME"] == "/opt/strata/home"


# --- HTTP API ---------------------------------------------------------------


async def test_http_api_parses_models_and_surfaces_overflow() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"object": "list", "data": [{"id": NAME, "status": {"value": "loaded"}}]})
        return httpx.Response(400, json={"error": {"type": "invalid_request_error", "message": OVERFLOW}})

    api = HttpStrataApi("http://127.0.0.1:8080", api_key="k", transport=httpx.MockTransport(handler))
    assert await api.models() == {NAME: "loaded"}
    with pytest.raises(StrataHttpError) as exc:
        await api.chat({"messages": []})
    assert exc.value.status_code == 400 and "exceeds the context window" in exc.value.message
    with pytest.raises(StrataHttpError):
        async for _ in api.chat_stream({"messages": [], "stream": True}):
            pass
    await api.close()


async def test_http_api_sends_the_bearer_key() -> None:
    seen: list[str | None] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        return httpx.Response(200, json={"data": []})

    api = HttpStrataApi("http://127.0.0.1:8080", api_key="k", transport=httpx.MockTransport(handler))
    await api.models()
    assert seen == ["Bearer k"]
    await api.close()


# --- executor ---------------------------------------------------------------


class FakeApi:
    def __init__(self, *, error: StrataHttpError | None = None, response: Mapping | None = None, chunks=()) -> None:
        self.error = error
        self.response = response or {
            "id": "c1", "object": "chat.completion", "created": 1, "model": NAME,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
        }
        self.chunks = list(chunks)
        self.payloads: list[dict] = []

    async def models(self):
        return {NAME: "loaded"}

    async def chat(self, payload):
        self.payloads.append(dict(payload))
        if self.error:
            raise self.error
        return self.response

    async def chat_stream(self, payload):
        self.payloads.append(dict(payload))
        if self.error:
            raise self.error
        for chunk in self.chunks:
            yield chunk

    async def close(self):
        pass


def executor(api: FakeApi, **kwargs) -> StrataExecutor:
    values = dict(api=api, model_ref=MODEL, served_model_name=NAME, context_tokens=12288, residency_metadata={})
    values.update(kwargs)
    return StrataExecutor(**values)


def gateway_job(*, stream: bool = False, adapter: str = STRATA_CHAT_ADAPTER, total: int = 12288, **request) -> JobRequest:
    return JobRequest(
        job_id="j1",
        capability="llm.chat",
        payload={
            "schema_version": CHAT_JOB_SCHEMA,
            "adapter_id": adapter,
            "request": {"messages": [{"role": "user", "content": "hi"}], "stream": stream, "max_tokens": 64, **request},
            "limits": {"input_tokens": total, "output_tokens": 2048, "total_tokens": total},
        },
    )


async def test_gateway_chat_binds_model_and_thinking_without_a_token_count() -> None:
    api = FakeApi()
    result = await executor(api, enable_thinking=False).execute(gateway_job())
    assert result.text == "ok"
    assert result.metrics["prompt_tokens"] == 5
    assert api.payloads == [{
        "messages": [{"role": "user", "content": "hi"}], "stream": False, "max_tokens": 64,
        "model": NAME, "chat_template_kwargs": {"enable_thinking": False},
    }]


async def test_strata_context_overflow_becomes_context_length_exceeded() -> None:
    api = FakeApi(error=StrataHttpError(400, OVERFLOW))
    with pytest.raises(JobExecutionError) as exc:
        await executor(api).execute(gateway_job())
    assert exc.value.code == "context_length_exceeded" and exc.value.retryable is False


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [(400, "invalid_request", False), (409, "provider_busy", True), (429, "provider_busy", True),
     (401, "provider_error", False), (404, "provider_error", False), (503, "provider_error", True)],
)
async def test_other_strata_errors_are_mapped(status, code, retryable) -> None:
    api = FakeApi(error=StrataHttpError(status, "nope"))
    with pytest.raises(JobExecutionError) as exc:
        await executor(api).execute(gateway_job())
    assert (exc.value.code, exc.value.retryable) == (code, retryable)


@pytest.mark.parametrize(
    ("job", "code"),
    [
        (gateway_job(adapter=FREETOKEN_CHAT_ADAPTER), "unsupported_chat_adapter"),
        (gateway_job(total=16384), "invalid_chat_job"),
        (gateway_job(max_tokens=4096), "output_limit_exceeded"),
        (gateway_job(model="x"), "invalid_chat_job"),
        (gateway_job(stream=True), "streaming_requires_event_sink"),
        (JobRequest(job_id="j", capability="llm.chat", payload={"messages": []}), "invalid_chat_job"),
    ],
)
async def test_invalid_jobs_never_reach_strata(job, code) -> None:
    api = FakeApi()
    with pytest.raises(JobExecutionError) as exc:
        await executor(api).execute(job)
    assert exc.value.code == code
    assert api.payloads == []


async def test_tools_are_rejected_when_disabled() -> None:
    api = FakeApi()
    job = gateway_job(tools=[{"type": "function", "function": {"name": "f", "parameters": {}}}])
    with pytest.raises(JobExecutionError) as exc:
        await executor(api, tools_enabled=False).execute(job)
    assert exc.value.code == "unsupported_feature"
    assert executor(api, tools_enabled=False).serving_features == {"llm.chat": frozenset()}


async def test_tool_call_index_is_dropped_from_full_messages() -> None:
    call = {"index": 0, "id": "call_1", "type": "function", "function": {"name": "f", "arguments": "{}"}}
    api = FakeApi(response={
        "id": "c1", "object": "chat.completion", "created": 1, "model": NAME,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": None, "tool_calls": [call]}, "finish_reason": "tool_calls"}],
    })
    result = await executor(api).execute(gateway_job())
    assert "index" not in result.outputs["choices"][0]["message"]["tool_calls"][0]


class Sink:
    def __init__(self) -> None:
        self.events: list = []

    async def emit(self, event) -> None:
        self.events.append(event)


async def test_stream_emits_chunks_and_requests_usage() -> None:
    chunks = [
        {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": NAME,
         "choices": [{"index": 0, "delta": {"content": "o"}, "finish_reason": None}]},
        {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": NAME,
         "choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6}},
    ]
    api = FakeApi(chunks=chunks)
    sink = Sink()
    result = await executor(api).execute_stream(gateway_job(stream=True), sink)
    assert [e.kind for e in sink.events] == ["chat.completion.chunk"] * 2
    assert result.text == "o" and result.metrics["total_tokens"] == 6
    assert api.payloads[0]["stream_options"] == {"include_usage": True}


async def test_stream_overflow_is_mapped_before_any_chunk() -> None:
    api = FakeApi(error=StrataHttpError(400, OVERFLOW))
    sink = Sink()
    with pytest.raises(JobExecutionError) as exc:
        await executor(api).execute_stream(gateway_job(stream=True), sink)
    assert exc.value.code == "context_length_exceeded" and sink.events == []


# --- serving contract -------------------------------------------------------


def contract(**limits) -> ServingContract:
    values = {"input_tokens": 12288, "output_tokens": 2048, "total_tokens": 12288, "request_bytes": 65536}
    values.update(limits)
    return ServingContract(
        deployment_revision="sha256:" + "0" * 64,
        capability="llm.chat",
        operation_schema="openai-chat-completions-v1",
        validation_evidence_sha256="sha256:" + "1" * 64,
        features=frozenset({"tools"}),
        limits=values,
    )


def test_contract_must_match_what_strata_enforces() -> None:
    e = executor(FakeApi())
    e.validate_serving_contract(contract())
    with pytest.raises(RuntimeError, match="input_tokens must equal total_tokens"):
        e.validate_serving_contract(contract(input_tokens=10240))
    with pytest.raises(RuntimeError, match="total_tokens must equal"):
        e.validate_serving_contract(contract(input_tokens=16384, total_tokens=16384))


def test_worker_feature_check_runs_the_executor_contract_hook() -> None:
    class Declaration:
        contracts = (contract(input_tokens=10240),)

    with pytest.raises(RuntimeError, match="input_tokens"):
        require_executor_serving_features(executor(FakeApi()), Declaration())
    Declaration.contracts = (contract(),)
    require_executor_serving_features(executor(FakeApi()), Declaration())


# --- managed runtime --------------------------------------------------------


class FakeController:
    def __init__(self, *, ready_after: int = 1, exits: bool = False) -> None:
        self.running = False
        self.ready_after = ready_after
        self.exits = exits
        self.polls = 0
        self.started = 0
        self.stopped = 0

    async def start(self) -> None:
        self.started += 1
        self.running = not self.exits

    async def stop(self) -> None:
        self.stopped += 1
        self.running = False

    def ready_record(self):
        if not self.running:
            return None
        self.polls += 1
        return {"instance_id": "i"} if self.polls >= self.ready_after else None


class OfflineApi(FakeApi):
    def __init__(self, *, online_after_start: bool = True, models: Mapping | None = None) -> None:
        super().__init__()
        self.online = False
        self.online_after_start = online_after_start
        self._models = models if models is not None else {NAME: "loaded"}
        self.closed = 0

    async def models(self):
        if not self.online:
            raise httpx.ConnectError("refused")
        return self._models

    async def close(self):
        self.closed += 1


def runtime(api: OfflineApi, process: FakeController) -> StrataManagedRuntime:
    original = process.start

    async def start():
        await original()
        api.online = api.online_after_start and process.running

    process.start = start
    return StrataManagedRuntime(
        api=api, process=process, context=ctx(), model_ref=MODEL, served_model_name=NAME,
        context_tokens=12288, startup_timeout_seconds=5.0,
    )


async def test_runtime_waits_for_the_ready_file_and_reports_ready() -> None:
    api, process = OfflineApi(), FakeController(ready_after=2)
    rt = runtime(api, process)
    await rt.start()
    health = await rt.health()
    assert health.ready and health.state is RuntimeHealthState.READY
    await rt.stop()
    await rt.release()
    await rt.release()
    assert process.stopped == 1 and api.closed == 1


async def test_runtime_fails_fast_when_the_process_exits() -> None:
    rt = runtime(OfflineApi(), FakeController(exits=True))
    with pytest.raises(RuntimeError, match="exited before readiness"):
        await rt.start()


async def test_runtime_rejects_an_external_server() -> None:
    api = OfflineApi()
    api.online = True
    rt = runtime(api, FakeController())
    with pytest.raises(RuntimeError, match="external Strata server"):
        await rt.start()


async def test_runtime_stops_when_the_model_name_is_missing() -> None:
    api, process = OfflineApi(models={"other": "loaded"}), FakeController()
    rt = runtime(api, process)
    with pytest.raises(RuntimeError, match="model name"):
        await rt.start()
    assert process.stopped == 1


def test_provider_reads_the_key_file_once_for_client_and_server(tmp_path: Path) -> None:
    key = tmp_path / "key"
    key.write_text("k1\n")
    seen: dict = {}

    def api_factory(base_url, api_key):
        seen["client"] = api_key
        return FakeApi()

    def process_factory(**kwargs):
        seen["server"] = kwargs["api_key"]
        return FakeController()

    p = StrataProvider(config(api_key_file=str(key)), api_factory=api_factory,
                       process_factory=process_factory, run_config_loader=lambda path: run_config())
    p.create_runtime(ctx(), p.setup_intent(ctx()))
    assert seen == {"client": "k1", "server": "k1"}
    key.write_text("  \n")
    with pytest.raises(ValueError, match="empty"):
        p.create_runtime(ctx(), p.setup_intent(ctx()))


async def test_http_stream_error_events_keep_their_meaning() -> None:
    events = {
        "overflow": {"error": {"type": "invalid_request_error", "message": OVERFLOW}},
        "busy": {"error": {"type": "model_busy", "message": "busy"}},
        "engine": {"error": {"type": "server_error", "message": "engine died"}},
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        kind = json.loads(request.content)["kind"]
        body = 'data: {"id":"c","choices":[]}\n\ndata: ' + json.dumps(events[kind]) + "\n\n"
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    api = HttpStrataApi("http://127.0.0.1:8080", transport=httpx.MockTransport(handler))
    statuses = {}
    for kind in events:
        with pytest.raises(StrataHttpError) as exc:
            async for _ in api.chat_stream({"kind": kind}):
                pass
        statuses[kind] = exc.value.status_code
    await api.close()
    assert statuses == {"overflow": 400, "busy": 409, "engine": 500}
    assert StrataExecutor._provider_error(StrataHttpError(400, OVERFLOW)).code == "context_length_exceeded"


class BrokenApi(FakeApi):
    def __init__(self, error: BaseException) -> None:
        super().__init__()
        self.failure = error

    async def chat(self, payload):
        raise self.failure

    async def chat_stream(self, payload):
        raise self.failure
        yield  # pragma: no cover


@pytest.mark.parametrize(
    ("error", "code", "retryable"),
    [
        (httpx.ConnectError("refused"), "provider_error", True),
        (httpx.ReadTimeout("slow"), "provider_error", True),
        (ValueError("not json"), "invalid_provider_response", False),
    ],
)
async def test_transport_and_body_failures_are_job_errors(error, code, retryable) -> None:
    with pytest.raises(JobExecutionError) as exc:
        await executor(BrokenApi(error)).execute(gateway_job())
    assert (exc.value.code, exc.value.retryable) == (code, retryable)


async def test_stream_transport_failure_is_retryable() -> None:
    with pytest.raises(JobExecutionError) as exc:
        await executor(BrokenApi(httpx.ConnectError("refused"))).execute_stream(gateway_job(stream=True), Sink())
    assert (exc.value.code, exc.value.retryable) == ("provider_error", True)


async def test_cancel_aborts_the_inflight_request() -> None:
    started = asyncio.Event()

    class SlowApi(FakeApi):
        async def chat(self, payload):
            started.set()
            await asyncio.sleep(60)

    e = executor(SlowApi())
    job = asyncio.create_task(e.execute(gateway_job()))
    await started.wait()
    await e.cancel("j1")
    with pytest.raises(asyncio.CancelledError):
        await job
    assert e._inflight == {}


TOOLS = [{"type": "function", "function": {"name": "route", "parameters": {"type": "object", "properties": {
    "avoid_tolls": {"type": "boolean"}, "note": {"type": "string"}, "years": {"type": "integer"}}}}}]


async def test_schema_booleans_are_restored_in_tool_arguments() -> None:
    raw = '{"avoid_tolls":"True","note":"True","years":5}'
    call = {"index": 0, "id": "call_1", "type": "function", "function": {"name": "route", "arguments": raw}}
    api = FakeApi(response={
        "id": "c1", "object": "chat.completion", "created": 1, "model": NAME,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": None, "tool_calls": [call]}, "finish_reason": "tool_calls"}],
    })
    result = await executor(api).execute(gateway_job(tools=TOOLS))
    args = json.loads(result.outputs["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"])
    assert args == {"avoid_tolls": True, "note": "True", "years": 5}


async def test_arguments_without_schema_booleans_are_untouched() -> None:
    raw = '{"avoid_tolls":"True"}'
    call = {"id": "call_1", "type": "function", "function": {"name": "other", "arguments": raw}}
    api = FakeApi(response={
        "id": "c1", "object": "chat.completion", "created": 1, "model": NAME,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": None, "tool_calls": [call]}, "finish_reason": "tool_calls"}],
    })
    result = await executor(api).execute(gateway_job(tools=TOOLS))
    assert result.outputs["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] == raw


async def test_stream_restores_complete_arguments_and_keeps_fragments() -> None:
    def chunk(calls):
        return {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": NAME,
                "choices": [{"index": 0, "delta": {"tool_calls": calls}, "finish_reason": None}]}

    chunks = [
        chunk([{"index": 0, "id": "call_1", "type": "function", "function": {"name": "route", "arguments": ""}}]),
        chunk([{"index": 0, "function": {"arguments": '{"avoid_tolls":"False","years":2}'}}]),
        chunk([{"index": 1, "id": "call_2", "type": "function", "function": {"name": "route", "arguments": '{"avoid_'}}]),
    ]
    sink = Sink()
    await executor(FakeApi(chunks=chunks)).execute_stream(gateway_job(stream=True, tools=TOOLS), sink)
    sent = [e.payload["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"] for e in sink.events]
    assert json.loads(sent[1]) == {"avoid_tolls": False, "years": 2}
    assert sent[2] == '{"avoid_'


@pytest.mark.parametrize("fragment", ['"True"}', "true", '"True"', '{"avoid_'])
def test_non_object_fragments_pass_through(fragment) -> None:
    from astrumweaver.runtime.providers.strata import _fix_boolean_arguments

    assert _fix_boolean_arguments(fragment, frozenset({"avoid_tolls"})) == fragment


def test_boolean_parameters_follow_type_lists() -> None:
    from astrumweaver.runtime.providers.strata import _boolean_parameters

    tools = [{"type": "function", "function": {"name": "t", "parameters": {"properties": {
        "a": {"type": ["boolean", "null"]}, "b": {"type": ["string", "boolean"]}, "c": {"type": "boolean"}}}}}]
    assert _boolean_parameters(tools) == {"t": frozenset({"a", "c"})}


async def test_stream_first_delta_with_name_and_lowercase_value() -> None:
    chunks = [{"id": "c", "object": "chat.completion.chunk", "created": 1, "model": NAME,
               "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                   "function": {"name": "route", "arguments": '{"avoid_tolls":"true"}'}}]}, "finish_reason": None}]}]
    sink = Sink()
    await executor(FakeApi(chunks=chunks)).execute_stream(gateway_job(stream=True, tools=TOOLS), sink)
    args = sink.events[0].payload["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"]
    assert json.loads(args) == {"avoid_tolls": True}
