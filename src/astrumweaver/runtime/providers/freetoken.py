"""First-class FreeToken RuntimeProvider."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import signal
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

import httpx

from ...execution import (
    JobEvent,
    JobEventSink,
    JobExecutionError,
    JobExecutor,
    JobRequest,
    JobResult,
    ResidencyItem,
    ResidencyReport,
)
from ...gateway.chat import (
    CHAT_JOB_SCHEMA,
    FREETOKEN_CHAT_ADAPTER,
    ChatGatewayError,
    validate_chat_stream_chunk,
)
from ..contracts import (
    CompatibilityReason,
    GPUTopology,
    ManagedRuntime,
    ModelPreparationPolicy,
    ModelTopology,
    ResidencyPolicy,
    RuntimeCompatibility,
    RuntimeCompatibilityContext,
    RuntimeHealth,
    RuntimeHealthState,
    RuntimeProvider,
    RuntimeProviderInfo,
    RuntimeSetupIntent,
)


FREETOKEN_PROVIDER_ID = "freetoken"
FREETOKEN_CAPABILITIES = frozenset({"llm.chat", "text.generate"})
FREETOKEN_MODEL_FORMATS = frozenset(
    {"huggingface", "hf", "safetensors", "ftw", "gguf"}
)
# Local-file checkpoint formats; a remote reference is never downloaded for them.
_LOCAL_ONLY_FORMATS = frozenset({"ftw", "gguf"})
_ATTENTION_BACKEND = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def _nonblank(value: str, field_name: str) -> str:
    normalized = str(value).strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be blank")
    return normalized


def _absolute_path(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-blank path")
    path = value.strip()
    if not os.path.isabs(path) or ":" in path or "\x00" in path:
        raise ValueError(f"{field_name} must be an absolute path without ':'")
    return path


def _local_model_reference(model_ref: str) -> bool:
    path = Path(model_ref)
    return (
        path.is_absolute()
        or model_ref.startswith("./")
        or model_ref.startswith("../")
    )


def _model_preparation(model_ref: str) -> ModelPreparationPolicy:
    if _local_model_reference(model_ref):
        return ModelPreparationPolicy.REFERENCE_ONLY
    return ModelPreparationPolicy.DOWNLOAD


class FreeTokenMoeStrategy(StrEnum):
    AUTO = "auto"
    OFFLOAD = "offload"
    CPU = "cpu"
    HYBRID = "hybrid"


@dataclass(frozen=True, slots=True)
class FreeTokenProviderConfig:
    base_url: str = "http://127.0.0.1:1919"
    executable: str = "ft"
    package_reference: str = "freetoken[accel]"
    startup_timeout_seconds: float = 300.0
    request_timeout_seconds: float = 300.0
    served_model_name: str = "astrumweaver"
    moe_strategy: FreeTokenMoeStrategy = FreeTokenMoeStrategy.AUTO
    memory_ratio: float = 0.9
    moe_cache_size: int | None = None
    moe_cache_rate: float | None = None
    moe_cpu_threads: int | None = None
    moe_cpu_layers: str | int | float | None = None
    moe_hybrid_max_fetch: int | None = None
    max_running_requests: int = 1
    # Pre-Turing GPUs (TSUMGI FreeToken fork, sm_61) need the Triton attention
    # backend, a CUDA 12.x toolkit for JIT kernels and extra runtime libraries.
    attention_backend: str | None = None
    max_seq_len: int | None = None
    cuda_home: str | None = None
    library_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        base_url = _nonblank(self.base_url, "base_url").rstrip("/")
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost"}
            or parsed.port is None
        ):
            raise ValueError(
                "FreeToken provider requires a local loopback HTTP endpoint with port"
            )
        object.__setattr__(self, "base_url", base_url)
        object.__setattr__(
            self,
            "executable",
            _nonblank(self.executable, "executable"),
        )
        object.__setattr__(
            self,
            "package_reference",
            _nonblank(self.package_reference, "package_reference"),
        )
        object.__setattr__(
            self,
            "served_model_name",
            _nonblank(self.served_model_name, "served_model_name"),
        )
        object.__setattr__(
            self,
            "moe_strategy",
            FreeTokenMoeStrategy(self.moe_strategy),
        )

        if self.startup_timeout_seconds <= 0:
            raise ValueError("startup_timeout_seconds must be positive")
        if self.request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive")
        if not (0 < self.memory_ratio <= 1):
            raise ValueError("memory_ratio must be greater than 0 and at most 1")
        if self.max_running_requests <= 0:
            raise ValueError("max_running_requests must be positive")
        if self.moe_cache_size is not None and self.moe_cache_size <= 0:
            raise ValueError("moe_cache_size must be positive when set")
        if self.moe_cache_rate is not None and not (0 < self.moe_cache_rate <= 1):
            raise ValueError(
                "moe_cache_rate must be greater than 0 and at most 1 when set"
            )
        if self.moe_cache_size is not None and self.moe_cache_rate is not None:
            raise ValueError(
                "moe_cache_size and moe_cache_rate are mutually exclusive"
            )
        if self.moe_cpu_threads is not None and self.moe_cpu_threads <= 0:
            raise ValueError("moe_cpu_threads must be positive when set")
        if (
            self.moe_hybrid_max_fetch is not None
            and self.moe_hybrid_max_fetch < -1
        ):
            raise ValueError("moe_hybrid_max_fetch must be >= -1 when set")
        if isinstance(self.moe_cpu_layers, bool):
            raise TypeError("moe_cpu_layers must not be boolean")
        if self.attention_backend is not None and (
            not isinstance(self.attention_backend, str)
            or _ATTENTION_BACKEND.fullmatch(self.attention_backend) is None
        ):
            raise ValueError("attention_backend must be a short lowercase name")
        if self.max_seq_len is not None and (
            isinstance(self.max_seq_len, bool)
            or not isinstance(self.max_seq_len, int)
            or self.max_seq_len <= 0
        ):
            raise ValueError("max_seq_len must be a positive integer when set")
        if self.cuda_home is not None:
            object.__setattr__(
                self, "cuda_home", _absolute_path(self.cuda_home, "cuda_home")
            )
        object.__setattr__(
            self,
            "library_paths",
            tuple(
                _absolute_path(item, "library_paths entry")
                for item in self.library_paths
            ),
        )


@dataclass(frozen=True, slots=True)
class FreeTokenLaunchPolicy:
    gpu_uuid: str
    moe_strategy: FreeTokenMoeStrategy
    memory_ratio: float
    moe_cache_size: int | None
    moe_cache_rate: float | None
    moe_cpu_threads: int | None
    moe_cpu_layers: str | int | float | None
    moe_hybrid_max_fetch: int | None
    max_running_requests: int
    attention_backend: str | None = None
    max_seq_len: int | None = None


def _launch_policy(
    context: RuntimeCompatibilityContext,
    config: FreeTokenProviderConfig,
) -> FreeTokenLaunchPolicy:
    gpu_uuid = (
        context.worker.gpu_uuids[0]
        if len(context.worker.gpu_uuids) == 1
        else ""
    )
    return FreeTokenLaunchPolicy(
        gpu_uuid=gpu_uuid,
        moe_strategy=config.moe_strategy,
        memory_ratio=config.memory_ratio,
        moe_cache_size=config.moe_cache_size,
        moe_cache_rate=config.moe_cache_rate,
        moe_cpu_threads=config.moe_cpu_threads,
        moe_cpu_layers=config.moe_cpu_layers,
        moe_hybrid_max_fetch=config.moe_hybrid_max_fetch,
        max_running_requests=config.max_running_requests,
        attention_backend=config.attention_backend,
        max_seq_len=config.max_seq_len,
    )


@runtime_checkable
class FreeTokenApi(Protocol):
    async def health(self) -> bool: ...

    async def models(self) -> tuple[str, ...]: ...

    async def chat(self, payload: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def chat_stream(
        self,
        payload: Mapping[str, Any],
    ) -> AsyncIterator[Mapping[str, Any]]: ...

    async def chat_input_tokens(self, payload: Mapping[str, Any]) -> int: ...

    async def completion(self, payload: Mapping[str, Any]) -> Mapping[str, Any]: ...

    async def stats(self) -> Mapping[str, Any]: ...

    async def close(self) -> None: ...


class HttpFreeTokenApi:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 300.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            transport=transport,
        )

    async def _json(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        kwargs = {} if json is None else {"json": json}
        response = await self._client.request(method, path, **kwargs)
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, Mapping):
            raise RuntimeError("FreeToken returned a non-object response")
        return body

    async def health(self) -> bool:
        try:
            body = await self._json("GET", "/health")
        except (httpx.HTTPError, RuntimeError, ValueError):
            return False
        return body.get("status") == "ok"

    async def models(self) -> tuple[str, ...]:
        body = await self._json("GET", "/v1/models")
        data = body.get("data", ())
        if not isinstance(data, list):
            raise RuntimeError("FreeToken /v1/models returned invalid data")
        result: list[str] = []
        for item in data:
            if not isinstance(item, Mapping):
                continue
            model_id = item.get("id")
            if isinstance(model_id, str) and model_id.strip():
                result.append(model_id.strip())
        return tuple(result)

    async def chat(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return await self._json(
            "POST",
            "/v1/chat/completions",
            json=payload,
        )

    async def chat_stream(
        self,
        payload: Mapping[str, Any],
    ) -> AsyncIterator[Mapping[str, Any]]:
        async with self._client.stream(
            "POST",
            "/v1/chat/completions",
            json=dict(payload),
        ) as response:
            response.raise_for_status()
            async for raw_line in response.aiter_lines():
                line = raw_line.strip()
                if not line or line.startswith(":"):
                    continue
                if not line.startswith("data:"):
                    raise RuntimeError("FreeToken stream returned invalid SSE framing")
                data = line[5:].strip()
                if data == "[DONE]":
                    return
                try:
                    value = json.loads(data)
                except json.JSONDecodeError as exc:
                    raise RuntimeError("FreeToken stream returned invalid JSON") from exc
                if not isinstance(value, Mapping):
                    raise RuntimeError("FreeToken stream returned a non-object event")
                yield value

    async def chat_input_tokens(self, payload: Mapping[str, Any]) -> int:
        # Served by the TSUMGI FreeToken fork (llama.cpp-compatible shape).
        body = await self._json(
            "POST",
            "/v1/chat/completions/input_tokens",
            json=payload,
        )
        value = body.get("input_tokens")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RuntimeError(
                "FreeToken token-count endpoint returned invalid input_tokens"
            )
        return value

    async def completion(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return await self._json(
            "POST",
            "/v1/completions",
            json=payload,
        )

    async def stats(self) -> Mapping[str, Any]:
        return await self._json("GET", "/v1/stats")

    async def close(self) -> None:
        await self._client.aclose()


@runtime_checkable
class FreeTokenProcessController(Protocol):
    @property
    def running(self) -> bool: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...


class FreeTokenSubprocessController:
    def __init__(
        self,
        *,
        executable: str,
        base_url: str,
        model_ref: str,
        served_model_name: str,
        launch_policy: FreeTokenLaunchPolicy,
        cuda_home: str | None = None,
        library_paths: tuple[str, ...] = (),
    ) -> None:
        self.executable = executable
        self.base_url = base_url
        self.model_ref = model_ref
        self.served_model_name = served_model_name
        self.launch_policy = launch_policy
        self.cuda_home = cuda_home
        self.library_paths = tuple(library_paths)
        self._process: asyncio.subprocess.Process | None = None

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    def command(self) -> tuple[str, ...]:
        parsed = urlsplit(self.base_url)
        assert parsed.hostname is not None
        assert parsed.port is not None

        args: list[str] = [
            self.executable,
            "serve",
            "--model",
            self.model_ref,
            "--host",
            parsed.hostname,
            "--port",
            str(parsed.port),
            "--gpu",
            self.launch_policy.gpu_uuid,
            "--served-model-name",
            self.served_model_name,
            "--moe-strategy",
            self.launch_policy.moe_strategy.value,
            "--memory-ratio",
            str(self.launch_policy.memory_ratio),
            "--max-running-requests",
            str(self.launch_policy.max_running_requests),
        ]
        if self.launch_policy.moe_cache_size is not None:
            args.extend(
                ["--moe-cache-size", str(self.launch_policy.moe_cache_size)]
            )
        if self.launch_policy.moe_cache_rate is not None:
            args.extend(
                ["--moe-cache-rate", str(self.launch_policy.moe_cache_rate)]
            )
        if self.launch_policy.moe_cpu_threads is not None:
            args.extend(
                ["--moe-cpu-threads", str(self.launch_policy.moe_cpu_threads)]
            )
        if self.launch_policy.moe_cpu_layers is not None:
            args.extend(
                ["--moe-cpu-layers", str(self.launch_policy.moe_cpu_layers)]
            )
        if self.launch_policy.moe_hybrid_max_fetch is not None:
            args.extend(
                [
                    "--moe-hybrid-max-fetch",
                    str(self.launch_policy.moe_hybrid_max_fetch),
                ]
            )
        if self.launch_policy.attention_backend is not None:
            args.extend(
                ["--attention-backend", self.launch_policy.attention_backend]
            )
        if self.launch_policy.max_seq_len is not None:
            args.extend(
                ["--max-seq-len-override", str(self.launch_policy.max_seq_len)]
            )
        return tuple(args)

    def environment(
        self,
        base: Mapping[str, str] | None = None,
    ) -> dict[str, str] | None:
        """Inherited environment plus CUDA toolkit/library overrides, or None when
        nothing is overridden. GPU selection (CUDA_VISIBLE_DEVICES) is never touched."""
        if self.cuda_home is None and not self.library_paths:
            return None
        env = dict(os.environ if base is None else base)
        path_prefix: list[str] = []
        if self.cuda_home is not None:
            env["CUDA_HOME"] = self.cuda_home
            path_prefix.append(os.path.join(self.cuda_home, "bin"))
        executable_dir = os.path.dirname(self.executable)
        if os.path.isabs(executable_dir):
            # JIT builds look up ninja next to the FreeToken entry point (its venv).
            path_prefix.append(executable_dir)
        if path_prefix:
            env["PATH"] = os.pathsep.join(
                [*path_prefix, *([env["PATH"]] if env.get("PATH") else [])]
            )
        if self.library_paths:
            env["LD_LIBRARY_PATH"] = os.pathsep.join(
                [
                    *self.library_paths,
                    *([env["LD_LIBRARY_PATH"]] if env.get("LD_LIBRARY_PATH") else []),
                ]
            )
        return env

    async def start(self) -> None:
        if self.running:
            return
        try:
            # A new session makes the server the leader of a process group, so
            # stop() also reaches the scheduler/tokenizer children it spawns.
            env = self.environment()
            self._process = await asyncio.create_subprocess_exec(
                *self.command(),
                **({} if env is None else {"env": env}),
                start_new_session=True,
            )
        except OSError as exc:
            raise RuntimeError("failed to start FreeToken server process") from exc

    @staticmethod
    def _signal_group(pgid: int, signum: int) -> bool:
        try:
            os.killpg(pgid, signum)
        except ProcessLookupError:
            return False
        return True

    async def stop(self) -> None:
        process = self._process
        if process is None:
            return
        pgid = process.pid
        if process.returncode is None:
            self._signal_group(pgid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), timeout=15.0)
            except asyncio.TimeoutError:
                self._signal_group(pgid, signal.SIGKILL)
                await process.wait()
        # Children may outlive the parent and keep the GPU and model files open.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 15.0
        while self._signal_group(pgid, 0):
            if loop.time() >= deadline:
                self._signal_group(pgid, signal.SIGKILL)
                break
            await asyncio.sleep(0.2)
        self._process = None


class FreeTokenExecutor(JobExecutor):
    capabilities = FREETOKEN_CAPABILITIES

    def __init__(
        self,
        *,
        api: FreeTokenApi,
        model_ref: str,
        served_model_name: str,
        residency_metadata: Mapping[str, Any],
    ) -> None:
        self.api = api
        self.model_ref = _nonblank(model_ref, "model_ref")
        self.served_model_name = _nonblank(
            served_model_name,
            "served_model_name",
        )
        self.residency_metadata = dict(residency_metadata)
        self._inflight: dict[str, asyncio.Task[Any]] = {}

    def _payload(self, job: JobRequest) -> dict[str, Any]:
        payload = dict(job.payload)
        supplied_model = payload.pop("model", None)
        if supplied_model is not None and str(supplied_model) not in {
            self.model_ref,
            self.served_model_name,
        }:
            raise ValueError(
                "job model does not match the model bound to this FreeToken runtime"
            )
        payload["model"] = self.served_model_name
        payload["stream"] = False
        return payload

    def _gateway_chat_payload(
        self,
        job: JobRequest,
    ) -> tuple[dict[str, Any], dict[str, int]] | None:
        envelope = dict(job.payload)
        if envelope.get("schema_version") != CHAT_JOB_SCHEMA:
            return None
        if set(envelope) != {"schema_version", "adapter_id", "request", "limits"}:
            raise JobExecutionError(
                "invalid_chat_job",
                "chat job envelope uses unsupported fields",
                retryable=False,
            )
        if envelope.get("adapter_id") != FREETOKEN_CHAT_ADAPTER:
            raise JobExecutionError(
                "unsupported_chat_adapter",
                "chat job targets an unsupported provider adapter",
                retryable=False,
            )
        raw_request = envelope.get("request")
        raw_limits = envelope.get("limits")
        if not isinstance(raw_request, Mapping) or not isinstance(raw_limits, Mapping):
            raise JobExecutionError(
                "invalid_chat_job",
                "chat job request/limits must be objects",
                retryable=False,
            )
        request = dict(raw_request)
        if not isinstance(request.get("messages"), list):
            raise JobExecutionError(
                "invalid_chat_job",
                "chat job requires a messages array",
                retryable=False,
            )
        if type(request.get("stream")) is not bool:
            raise JobExecutionError(
                "invalid_chat_job",
                "chat job stream flag must be boolean",
                retryable=False,
            )
        output_tokens = request.get("max_tokens")
        if (
            isinstance(output_tokens, bool)
            or not isinstance(output_tokens, int)
            or output_tokens < 1
        ):
            raise JobExecutionError(
                "invalid_chat_job",
                "chat job max_tokens must be a positive integer",
                retryable=False,
            )
        limits: dict[str, int] = {}
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            value = raw_limits.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise JobExecutionError(
                    "invalid_chat_job",
                    "chat job token limits must be positive integers",
                    retryable=False,
                )
            limits[key] = value
        if (
            limits["input_tokens"] > limits["total_tokens"]
            or limits["output_tokens"] > limits["total_tokens"]
        ):
            raise JobExecutionError(
                "invalid_chat_job",
                "chat job token limit exceeds total context",
                retryable=False,
            )
        if output_tokens > limits["output_tokens"]:
            raise JobExecutionError(
                "output_limit_exceeded",
                "chat output budget exceeds the bound serving profile",
                retryable=False,
            )
        if request.pop("model", None) is not None:
            raise JobExecutionError(
                "invalid_chat_job",
                "gateway chat job must not supply a runtime model name",
                retryable=False,
            )
        request["model"] = self.served_model_name
        return request, limits

    async def _enforce_chat_limits(
        self,
        payload: Mapping[str, Any],
        limits: Mapping[str, int],
    ) -> None:
        input_tokens = await self.api.chat_input_tokens(payload)
        output_tokens = int(payload["max_tokens"])
        if input_tokens > limits["input_tokens"]:
            raise JobExecutionError(
                "context_length_exceeded",
                "chat input exceeds the deployed input-token limit",
                retryable=False,
            )
        if input_tokens + output_tokens > limits["total_tokens"]:
            raise JobExecutionError(
                "context_length_exceeded",
                "chat input plus output reservation exceeds deployed context",
                retryable=False,
            )

    async def execute(self, job: JobRequest) -> JobResult:
        if job.capability not in self.capabilities:
            raise ValueError(
                f"FreeToken executor does not support capability: {job.capability}"
            )

        gateway_chat = (
            self._gateway_chat_payload(job)
            if job.capability == "llm.chat"
            else None
        )
        if gateway_chat is None:
            payload = self._payload(job)
        else:
            payload, limits = gateway_chat
        if job.capability == "llm.chat":
            if not isinstance(payload.get("messages"), list):
                raise ValueError("llm.chat requires a messages list")
            if gateway_chat is not None:
                if payload.get("stream") is True:
                    raise JobExecutionError(
                        "streaming_requires_event_sink",
                        "streaming chat requires the fenced Job event channel",
                        retryable=False,
                    )
                await self._enforce_chat_limits(payload, limits)
            call = self.api.chat(payload)
        else:
            if not isinstance(payload.get("prompt"), str):
                raise ValueError("text.generate requires a prompt string")
            call = self.api.completion(payload)

        task = asyncio.create_task(call)
        self._inflight[job.job_id] = task
        try:
            response = await task
        finally:
            self._inflight.pop(job.job_id, None)

        text: str | None = None
        choices = response.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, Mapping):
                if job.capability == "llm.chat":
                    message = first.get("message")
                    if isinstance(message, Mapping):
                        content = message.get("content")
                        if isinstance(content, str):
                            text = content
                else:
                    candidate = first.get("text")
                    if isinstance(candidate, str):
                        text = candidate

        metrics: dict[str, int | float] = {}
        usage = response.get("usage")
        if isinstance(usage, Mapping):
            for key in (
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
            ):
                value = usage.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    metrics[key] = value

        return JobResult(
            outputs=dict(response),
            metrics=metrics,
            text=text,
            metadata={
                "runtime_provider": FREETOKEN_PROVIDER_ID,
                "model": self.model_ref,
                "served_model_name": self.served_model_name,
            },
        )

    async def execute_stream(
        self,
        job: JobRequest,
        sink: JobEventSink,
    ) -> JobResult:
        if job.capability != "llm.chat":
            return await self.execute(job)
        gateway_chat = self._gateway_chat_payload(job)
        if gateway_chat is None:
            return await self.execute(job)
        payload, limits = gateway_chat
        if payload.get("stream") is not True:
            return await self.execute(job)

        await self._enforce_chat_limits(payload, limits)
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}

        metrics: dict[str, int | float] = {}
        text_parts: list[str] = []
        current = asyncio.current_task()
        if current is None:
            raise RuntimeError("streaming execution task is unavailable")
        self._inflight[job.job_id] = current
        try:
            async for raw_chunk in self.api.chat_stream(payload):
                try:
                    chunk = validate_chat_stream_chunk(raw_chunk)
                except ChatGatewayError as exc:
                    raise JobExecutionError(
                        "invalid_provider_response",
                        "FreeToken returned an invalid chat stream chunk",
                        retryable=False,
                    ) from exc
                choices = chunk.get("choices")
                if isinstance(choices, list) and choices:
                    choice = choices[0]
                    if isinstance(choice, Mapping):
                        delta = choice.get("delta")
                        if isinstance(delta, Mapping):
                            content = delta.get("content")
                            if isinstance(content, str):
                                text_parts.append(content)
                usage = chunk.get("usage")
                if isinstance(usage, Mapping):
                    for key in (
                        "prompt_tokens",
                        "completion_tokens",
                        "total_tokens",
                    ):
                        value = usage.get(key)
                        if (
                            isinstance(value, (int, float))
                            and not isinstance(value, bool)
                        ):
                            metrics[key] = value
                await sink.emit(
                    JobEvent(
                        kind="chat.completion.chunk",
                        payload=chunk,
                    )
                )
        finally:
            if self._inflight.get(job.job_id) is current:
                self._inflight.pop(job.job_id, None)

        return JobResult(
            outputs={"streamed": True},
            metrics=metrics,
            text="".join(text_parts) if text_parts else None,
            metadata={
                "runtime_provider": FREETOKEN_PROVIDER_ID,
                "model": self.model_ref,
                "served_model_name": self.served_model_name,
            },
        )

    async def cancel(self, job_id: str) -> None:
        task = self._inflight.get(job_id)
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def residency(self) -> ResidencyReport:
        metadata = dict(self.residency_metadata)
        accelerator_memory_bytes: int | None = None
        try:
            stats = await self.api.stats()
        except Exception:
            stats = {}

        vram_bytes = stats.get("vram_bytes")
        if isinstance(vram_bytes, int) and not isinstance(vram_bytes, bool):
            # FreeToken initializes this counter to zero before any measured
            # GPU-memory sample arrives, so zero means "not observed yet".
            if vram_bytes > 0:
                accelerator_memory_bytes = vram_bytes

        model_stats = stats.get("model")
        if isinstance(model_stats, Mapping):
            observed_model = model_stats.get("id")
            if isinstance(observed_model, str):
                metadata["observed_model"] = observed_model
            observed_moe = model_stats.get("moe")
            if isinstance(observed_moe, bool):
                metadata["observed_moe"] = observed_moe

        return ResidencyReport(
            items=(
                ResidencyItem(
                    name=self.model_ref,
                    kind="freetoken-model",
                    accelerator_memory_bytes=accelerator_memory_bytes,
                    metadata=metadata,
                ),
            ),
            metadata={"runtime_provider": FREETOKEN_PROVIDER_ID},
        )


class FreeTokenManagedRuntime(ManagedRuntime):
    provider_id = FREETOKEN_PROVIDER_ID

    def __init__(
        self,
        *,
        api: FreeTokenApi,
        process: FreeTokenProcessController,
        context: RuntimeCompatibilityContext,
        model_ref: str,
        served_model_name: str,
        startup_timeout_seconds: float,
        launch_policy: FreeTokenLaunchPolicy,
    ) -> None:
        self.api = api
        self.process = process
        self.context = context
        self.model_ref = model_ref
        self.served_model_name = served_model_name
        self.startup_timeout_seconds = startup_timeout_seconds
        self.launch_policy = launch_policy
        self._closed = False
        self._executor = FreeTokenExecutor(
            api=api,
            model_ref=model_ref,
            served_model_name=served_model_name,
            residency_metadata={
                "residency_policy": context.demand.residency_policy.value,
                "gpu_topology": context.demand.gpu_topology.value,
                "gpu_uuid": launch_policy.gpu_uuid,
                "host_ram_mb": context.host.host_ram_mb,
                "moe_strategy": launch_policy.moe_strategy.value,
                "memory_ratio": launch_policy.memory_ratio,
                "moe_cache_size": launch_policy.moe_cache_size,
                "moe_cache_rate": launch_policy.moe_cache_rate,
                "moe_cpu_threads": launch_policy.moe_cpu_threads,
                "moe_cpu_layers": launch_policy.moe_cpu_layers,
                "moe_hybrid_max_fetch": launch_policy.moe_hybrid_max_fetch,
                "attention_backend": launch_policy.attention_backend,
                "max_seq_len": launch_policy.max_seq_len,
            },
        )

    async def _server_reachable(self) -> bool:
        return await self.api.health()

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("FreeToken runtime has already been released")

        reachable = await self._server_reachable()
        if reachable and not self.process.running:
            raise RuntimeError(
                "an external FreeToken server is already using the configured endpoint"
            )

        started_here = False
        if not self.process.running:
            await self.process.start()
            started_here = True

        try:
            deadline = (
                asyncio.get_running_loop().time()
                + self.startup_timeout_seconds
            )
            while True:
                if not self.process.running:
                    raise RuntimeError(
                        "FreeToken owned process exited before readiness; "
                        "inspect Worker service logs"
                    )
                if await self._server_reachable():
                    break
                if asyncio.get_running_loop().time() >= deadline:
                    raise RuntimeError(
                        "FreeToken server did not become ready before timeout"
                    )
                await asyncio.sleep(0.2)

            models = await self.api.models()
            if self.served_model_name not in models:
                raise RuntimeError(
                    "FreeToken server did not expose the configured model alias"
                )
        except Exception:
            if started_here:
                with contextlib.suppress(Exception):
                    await self.process.stop()
            raise

    async def stop(self) -> None:
        await self.process.stop()

    async def health(self) -> RuntimeHealth:
        reachable = await self._server_reachable()
        if not reachable:
            return RuntimeHealth(
                state=(
                    RuntimeHealthState.STARTING
                    if self.process.running
                    else RuntimeHealthState.STOPPED
                ),
                ready=False,
                metadata={"runtime_provider": FREETOKEN_PROVIDER_ID},
            )
        if not self.process.running:
            return RuntimeHealth(
                state=RuntimeHealthState.FAILED,
                ready=False,
                detail="external FreeToken process detected at managed endpoint",
                metadata={"runtime_provider": FREETOKEN_PROVIDER_ID},
            )
        try:
            models = await self.api.models()
        except Exception:
            return RuntimeHealth(
                state=RuntimeHealthState.DEGRADED,
                ready=False,
                detail="FreeToken model inventory is unavailable",
                metadata={"runtime_provider": FREETOKEN_PROVIDER_ID},
            )
        if self.served_model_name not in models:
            return RuntimeHealth(
                state=RuntimeHealthState.FAILED,
                ready=False,
                detail="configured FreeToken model alias is unavailable",
                metadata={"runtime_provider": FREETOKEN_PROVIDER_ID},
            )
        # An owned process may exit during the awaited inventory request.
        if not self.process.running:
            return RuntimeHealth(
                state=RuntimeHealthState.STOPPED,
                ready=False,
                metadata={"runtime_provider": FREETOKEN_PROVIDER_ID},
            )
        return RuntimeHealth(
            state=RuntimeHealthState.READY,
            ready=True,
            metadata={
                "runtime_provider": FREETOKEN_PROVIDER_ID,
                "model": self.model_ref,
                "served_model_name": self.served_model_name,
            },
        )

    async def residency(self) -> ResidencyReport:
        return await self._executor.residency()

    def executor(self) -> JobExecutor:
        return self._executor

    async def release(self) -> None:
        if self._closed:
            return
        await self.api.close()
        self._closed = True


class FreeTokenProvider(RuntimeProvider):
    def __init__(
        self,
        config: FreeTokenProviderConfig | None = None,
        *,
        api_factory: Any | None = None,
        process_factory: Any | None = None,
    ) -> None:
        self.config = config or FreeTokenProviderConfig()
        self._api_factory = api_factory or (
            lambda base_url: HttpFreeTokenApi(
                base_url,
                timeout_seconds=self.config.request_timeout_seconds,
            )
        )
        self._process_factory = process_factory or (
            lambda **kwargs: FreeTokenSubprocessController(**kwargs)
        )
        self._info = RuntimeProviderInfo(
            provider_id=FREETOKEN_PROVIDER_ID,
            display_name="FreeToken",
            description=(
                "RAM-heavy, VRAM-constrained MoE serving with explicit "
                "single-GPU ownership."
            ),
        )

    @property
    def info(self) -> RuntimeProviderInfo:
        return self._info

    def compatibility(
        self,
        context: RuntimeCompatibilityContext,
    ) -> RuntimeCompatibility:
        reasons: list[CompatibilityReason] = []
        demand = context.demand
        model = demand.model
        resources = context.worker.resources
        policy = _launch_policy(context, self.config)

        if context.host.architecture.lower() not in {"x86_64", "amd64"}:
            reasons.append(
                CompatibilityReason(
                    code="architecture-unsupported",
                    message=(
                        "current upstream FreeToken packages require Linux x86_64; "
                        f"host architecture is {context.host.architecture}"
                    ),
                )
            )

        if model.model_format not in FREETOKEN_MODEL_FORMATS:
            reasons.append(
                CompatibilityReason(
                    code="model-format-unsupported",
                    message=(
                        "AstrumWeaver FreeToken provider requires a Hugging Face/"
                        "safetensors, local FTW or local GGUF model reference"
                    ),
                )
            )

        if (
            model.model_format == "ftw"
            and not _local_model_reference(model.model_ref)
        ):
            reasons.append(
                CompatibilityReason(
                    code="ftw-reference-must-be-local",
                    message=(
                        "FTW is a local FreeToken checkpoint format; the v0.x "
                        "provider requires an explicit local FTW path"
                    ),
                )
            )

        if (
            model.model_format == "gguf"
            and not _local_model_reference(model.model_ref)
        ):
            reasons.append(
                CompatibilityReason(
                    code="gguf-reference-must-be-local",
                    message=(
                        "the FreeToken provider serves GGUF only from an explicit "
                        "local file path; it never downloads a GGUF reference"
                    ),
                )
            )

        if model.topology is not ModelTopology.MOE:
            reasons.append(
                CompatibilityReason(
                    code="model-topology-outside-provider-scope",
                    message=(
                        "AstrumWeaver v0.x intentionally scopes the FreeToken "
                        "provider to MoE/offload workloads; this is not a claim "
                        "that upstream FreeToken cannot serve dense models"
                    ),
                )
            )

        if (
            demand.gpu_topology is not GPUTopology.SINGLE_GPU
            or resources.gpu_count != 1
            or len(context.worker.gpu_uuids) != 1
        ):
            reasons.append(
                CompatibilityReason(
                    code="single-gpu-required",
                    message=(
                        "AstrumWeaver v0.x FreeToken provider requires a "
                        "single-GPU Worker and will not partially consume a "
                        "multi-GPU Worker"
                    ),
                )
            )

        if demand.residency_policy is ResidencyPolicy.VRAM_ONLY:
            reasons.append(
                CompatibilityReason(
                    code="vram-only-unsupported",
                    message=(
                        "AstrumWeaver v0.x FreeToken provider targets MoE "
                        "offload and does not claim a vram_only execution path"
                    ),
                )
            )
        elif demand.residency_policy is ResidencyPolicy.CPU_GPU_HYBRID:
            reasons.append(
                CompatibilityReason(
                    code="cpu-gpu-hybrid-supported",
                    message=(
                        "FreeToken MoE execution will use the selected "
                        f"{policy.moe_strategy.value} strategy with host RAM "
                        "available for expert offload/CPU execution"
                    ),
                    blocking=False,
                )
            )
        else:
            reasons.append(
                CompatibilityReason(
                    code="prefer-vram-moe-cache",
                    message=(
                        "FreeToken will prefer GPU residency within its memory "
                        "budget while retaining its MoE offload/cache policy"
                    ),
                    blocking=False,
                )
            )

        if policy.moe_strategy in {
            FreeTokenMoeStrategy.OFFLOAD,
            FreeTokenMoeStrategy.CPU,
            FreeTokenMoeStrategy.HYBRID,
            FreeTokenMoeStrategy.AUTO,
        }:
            reasons.append(
                CompatibilityReason(
                    code="host-ram-interconnect-sensitive",
                    message=(
                        "FreeToken MoE offload performance and feasible model "
                        "size depend on host RAM capacity/bandwidth and the "
                        "CPU-GPU interconnect; AstrumWeaver does not invent an "
                        "exact RAM requirement beyond declared demand"
                    ),
                    blocking=False,
                )
            )

        return RuntimeCompatibility(
            provider_id=FREETOKEN_PROVIDER_ID,
            reasons=tuple(reasons),
        )

    def setup_intent(
        self,
        context: RuntimeCompatibilityContext,
    ) -> RuntimeSetupIntent:
        report = self.compatibility(context)
        if not report.compatible:
            raise RuntimeError(
                "cannot build FreeToken setup intent for incompatible demand"
            )

        policy = _launch_policy(context, self.config)
        return RuntimeSetupIntent(
            provider_id=FREETOKEN_PROVIDER_ID,
            package_references=(self.config.package_reference,),
            configuration={
                "base_url": self.config.base_url,
                "executable": self.config.executable,
                "model_ref": context.demand.model.model_ref,
                "served_model_name": self.config.served_model_name,
                "gpu_uuid": policy.gpu_uuid,
                "moe_strategy": policy.moe_strategy.value,
                "memory_ratio": policy.memory_ratio,
                "moe_cache_size": policy.moe_cache_size,
                "moe_cache_rate": policy.moe_cache_rate,
                "moe_cpu_threads": policy.moe_cpu_threads,
                "moe_cpu_layers": policy.moe_cpu_layers,
                "moe_hybrid_max_fetch": policy.moe_hybrid_max_fetch,
                "max_running_requests": policy.max_running_requests,
                "attention_backend": policy.attention_backend,
                "max_seq_len": policy.max_seq_len,
                "capabilities": sorted(FREETOKEN_CAPABILITIES),
            },
            model_preparation=_model_preparation(
                context.demand.model.model_ref
            ),
            model_ref=context.demand.model.model_ref,
            requires_privilege=True,
            metadata={"runtime_provider": FREETOKEN_PROVIDER_ID},
        )

    def create_runtime(
        self,
        context: RuntimeCompatibilityContext,
        setup: RuntimeSetupIntent,
    ) -> ManagedRuntime:
        if setup.provider_id != FREETOKEN_PROVIDER_ID:
            raise ValueError(
                "setup intent does not belong to FreeToken provider"
            )

        report = self.compatibility(context)
        if not report.compatible:
            raise RuntimeError(
                "cannot create FreeToken runtime for incompatible demand"
            )

        cfg = dict(setup.configuration)
        expected_gpu_uuid = context.worker.gpu_uuids[0]
        configured_gpu_uuid = cfg.get("gpu_uuid")
        if (
            configured_gpu_uuid is not None
            and str(configured_gpu_uuid) != expected_gpu_uuid
        ):
            raise ValueError(
                "setup intent GPU UUID does not match FreeToken Worker ownership"
            )

        base_url = str(cfg.get("base_url") or self.config.base_url)
        model_ref = str(
            cfg.get("model_ref") or context.demand.model.model_ref
        )
        served_model_name = str(
            cfg.get("served_model_name") or self.config.served_model_name
        )
        executable = str(cfg.get("executable") or self.config.executable)
        policy = FreeTokenLaunchPolicy(
            gpu_uuid=expected_gpu_uuid,
            moe_strategy=FreeTokenMoeStrategy(
                cfg.get("moe_strategy", self.config.moe_strategy)
            ),
            memory_ratio=float(
                cfg.get("memory_ratio", self.config.memory_ratio)
            ),
            moe_cache_size=(
                int(cfg["moe_cache_size"])
                if cfg.get("moe_cache_size") is not None
                else None
            ),
            moe_cache_rate=(
                float(cfg["moe_cache_rate"])
                if cfg.get("moe_cache_rate") is not None
                else None
            ),
            moe_cpu_threads=(
                int(cfg["moe_cpu_threads"])
                if cfg.get("moe_cpu_threads") is not None
                else None
            ),
            moe_cpu_layers=cfg.get(
                "moe_cpu_layers",
                self.config.moe_cpu_layers,
            ),
            moe_hybrid_max_fetch=(
                int(cfg["moe_hybrid_max_fetch"])
                if cfg.get("moe_hybrid_max_fetch") is not None
                else None
            ),
            max_running_requests=int(
                cfg.get(
                    "max_running_requests",
                    self.config.max_running_requests,
                )
            ),
            attention_backend=cfg.get(
                "attention_backend",
                self.config.attention_backend,
            ),
            max_seq_len=(
                int(cfg["max_seq_len"])
                if cfg.get("max_seq_len") is not None
                else self.config.max_seq_len
            ),
        )

        api = self._api_factory(base_url)
        process = self._process_factory(
            executable=executable,
            base_url=base_url,
            model_ref=model_ref,
            served_model_name=served_model_name,
            launch_policy=policy,
            cuda_home=self.config.cuda_home,
            library_paths=self.config.library_paths,
        )

        return FreeTokenManagedRuntime(
            api=api,
            process=process,
            context=context,
            model_ref=model_ref,
            served_model_name=served_model_name,
            startup_timeout_seconds=self.config.startup_timeout_seconds,
            launch_policy=policy,
        )
