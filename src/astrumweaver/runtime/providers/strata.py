"""First-class Strata RuntimeProvider.

Strata (https://github.com/Niko1221/Strata) serves large MoE models from GPU, RAM
and SSD. TSUMGI prepares a Strata run config once per host (`strata launch`),
then the Worker owns one foreground `strata serve` process for it. Readiness
comes from Strata's ready file; context limits are enforced by Strata itself
(`fit_max_tokens: false`), so there is no pre-flight token count.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
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
    STRATA_CHAT_ADAPTER,
    ChatGatewayError,
    validate_chat_stream_chunk,
)
from ...serving import ServingContract
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


STRATA_PROVIDER_ID = "strata"
# Strata has no /v1/completions; only OpenAI chat is bound.
STRATA_CAPABILITIES = frozenset({"llm.chat"})
STRATA_MODEL_FORMATS = frozenset({"gguf"})
# Strata's ContextOverflow message for OpenAI requests (serve/server.py).
_CONTEXT_OVERFLOW_MARKER = "exceeds the context window"


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


@dataclass(frozen=True, slots=True)
class StrataProviderConfig:
    # The Strata Terminal entry point (`.../bin/strata`).
    executable: str = "/opt/astrumweaver/strata/bin/strata"
    # Run config written by `strata launch` (engine argv, tokenizer, model name).
    run_config: str = "/opt/astrumweaver/strata/home/.local/share/strata/install/strata.json"
    # HOME and STRATA_DATA_DIR of the Strata install; engines and packs live there.
    home: str = "/opt/astrumweaver/strata/home"
    data_dir: str = "/opt/astrumweaver/strata/data"
    # Private directory for the ready file (the Worker unit's RuntimeDirectory).
    ready_dir: str = "/run/astrumweaver-worker"
    base_url: str = "http://127.0.0.1:8080"
    package_reference: str = "strata"
    served_model_name: str = "astrumweaver"
    # The run config's --max-context; the serving contract must match it.
    context_tokens: int = 16384
    parallel: int = 1
    startup_timeout_seconds: float = 900.0
    request_timeout_seconds: float = 600.0
    shutdown_timeout_seconds: float = 30.0
    # A file holding the API key; read at start and passed as STRATA_API_KEY.
    api_key_file: str | None = None
    tools_enabled: bool = True
    # None keeps the model's chat-template default; True/False is sent to every
    # Gateway chat job as chat_template_kwargs.enable_thinking.
    enable_thinking: bool | None = None

    def __post_init__(self) -> None:
        base_url = _nonblank(self.base_url, "base_url").rstrip("/")
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.port is None
        ):
            raise ValueError(
                "Strata provider requires http://127.0.0.1:<port> (Strata binds IPv4 loopback)"
            )
        object.__setattr__(self, "base_url", base_url)
        for name in ("executable", "run_config", "home", "data_dir", "ready_dir"):
            object.__setattr__(self, name, _absolute_path(getattr(self, name), name))
        if self.api_key_file is not None:
            object.__setattr__(
                self, "api_key_file", _absolute_path(self.api_key_file, "api_key_file")
            )
        object.__setattr__(
            self, "package_reference", _nonblank(self.package_reference, "package_reference")
        )
        object.__setattr__(
            self, "served_model_name", _nonblank(self.served_model_name, "served_model_name")
        )
        for name in ("context_tokens", "parallel"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.parallel > 8:
            raise ValueError("parallel must be at most 8")
        if self.startup_timeout_seconds <= 0:
            raise ValueError("startup_timeout_seconds must be positive")
        if self.request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive")
        if not (0 <= self.shutdown_timeout_seconds <= 300):
            raise ValueError("shutdown_timeout_seconds must be between 0 and 300")
        if type(self.tools_enabled) is not bool:
            raise TypeError("tools_enabled must be a boolean")
        if self.enable_thinking is not None and type(self.enable_thinking) is not bool:
            raise TypeError("enable_thinking must be a boolean or None")


@dataclass(frozen=True, slots=True)
class StrataRunConfig:
    """The fields of a Strata run config that TSUMGI binds and checks."""

    model_name: str
    model_path: str | None
    max_context: int | None
    fit_max_tokens: bool | None
    cuda: int | None

    @classmethod
    def load(cls, path: str) -> StrataRunConfig:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError("Strata run config is unreadable") from exc
        if not isinstance(data, Mapping):
            raise ValueError("Strata run config must be a JSON object")
        args = data.get("args")
        args = [str(item) for item in args] if isinstance(args, list) else []

        def flag(name: str) -> str | None:
            if name in args[:-1]:
                return args[args.index(name) + 1]
            return None

        max_context = flag("--max-context")
        fit = data.get("fit_max_tokens")
        cuda = data.get("cuda")
        return cls(
            model_name=str(data.get("model_name") or ""),
            model_path=flag("--native"),
            max_context=int(max_context) if max_context and max_context.isdigit() else None,
            fit_max_tokens=fit if isinstance(fit, bool) else None,
            cuda=cuda if isinstance(cuda, int) and not isinstance(cuda, bool) else None,
        )


class StrataHttpError(RuntimeError):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"Strata returned HTTP {status_code}: {message}")
        self.status_code = status_code
        self.message = message


def _error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:500]
    if isinstance(body, Mapping):
        error = body.get("error")
        if isinstance(error, Mapping) and isinstance(error.get("message"), str):
            return error["message"]
        if isinstance(error, str):
            return error
    return response.text[:500]


def _stream_error(error: Mapping[str, Any] | str) -> StrataHttpError:
    """An error event inside an SSE stream, with the HTTP status it stands for."""
    if isinstance(error, str):
        return StrataHttpError(500, error)
    message = str(error.get("message") or "")
    kind = error.get("type")
    status = 400 if kind == "invalid_request_error" else 409 if kind == "model_busy" else 500
    return StrataHttpError(status, message)


def _boolean_parameters(tools: object) -> dict[str, frozenset[str]]:
    """Top-level boolean parameters per tool name, from an OpenAI `tools` list."""
    result: dict[str, frozenset[str]] = {}
    if not isinstance(tools, list):
        return result
    for tool in tools:
        function = tool.get("function") if isinstance(tool, Mapping) else None
        if not isinstance(function, Mapping) or not isinstance(function.get("name"), str):
            continue
        params = function.get("parameters")
        props = params.get("properties") if isinstance(params, Mapping) else None
        if not isinstance(props, Mapping):
            continue
        names = set()
        for name, schema in props.items():
            kind = schema.get("type") if isinstance(schema, Mapping) else None
            if kind == "boolean" or (isinstance(kind, list) and "boolean" in kind and "string" not in kind):
                names.add(name)
        if names:
            result[function["name"]] = frozenset(names)
    return result


def _fix_boolean_arguments(arguments: object, booleans: frozenset[str]) -> object:
    """Strata 0.2.13 passes a boolean the model wrote as Python `True`/`False` through as a
    string (serve/frontend.py coerce_value). Restore booleans the tool schema declares."""
    if not booleans or not isinstance(arguments, str):
        return arguments
    try:
        decoded = json.loads(arguments)
    except ValueError:
        return arguments  # a stream fragment, or not an object at all
    if not isinstance(decoded, dict):
        return arguments
    changed = False
    for name in booleans:
        value = decoded.get(name)
        if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
            decoded[name] = value.strip().lower() == "true"
            changed = True
    return json.dumps(decoded, ensure_ascii=False) if changed else arguments


def _fix_tool_call(call: object, booleans: Mapping[str, frozenset[str]], name: str | None = None) -> object:
    if not isinstance(call, Mapping):
        return call
    function = call.get("function")
    if not isinstance(function, Mapping):
        return call
    tool = function.get("name") if isinstance(function.get("name"), str) else name
    fixed = _fix_boolean_arguments(function.get("arguments"), booleans.get(tool or "", frozenset()))
    if fixed is function.get("arguments"):
        return call
    return {**call, "function": {**function, "arguments": fixed}}


def _fix_stream_chunk(
    chunk: Mapping[str, Any],
    booleans: Mapping[str, frozenset[str]],
    call_names: dict[int, str],
) -> Mapping[str, Any]:
    """Restore booleans in a streamed tool call whose arguments arrive as one complete
    JSON object; partial fragments pass through unchanged."""
    choices = chunk.get("choices")
    if not isinstance(choices, list):
        return chunk
    fixed_choices: list[Any] = []
    changed = False
    for choice in choices:
        delta = choice.get("delta") if isinstance(choice, Mapping) else None
        calls = delta.get("tool_calls") if isinstance(delta, Mapping) else None
        if not isinstance(calls, list):
            fixed_choices.append(choice)
            continue
        fixed_calls = []
        for call in calls:
            index = call.get("index") if isinstance(call, Mapping) else None
            function = call.get("function") if isinstance(call, Mapping) else None
            if isinstance(index, int) and isinstance(function, Mapping) and isinstance(function.get("name"), str):
                call_names[index] = function["name"]
            fixed = _fix_tool_call(call, booleans, call_names.get(index) if isinstance(index, int) else None)
            changed = changed or fixed is not call
            fixed_calls.append(fixed)
        fixed_choices.append({**choice, "delta": {**delta, "tool_calls": fixed_calls}})
    return {**chunk, "choices": fixed_choices} if changed else chunk


@runtime_checkable
class StrataApi(Protocol):
    async def models(self) -> Mapping[str, str]: ...

    async def chat(self, payload: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def chat_stream(
        self,
        payload: Mapping[str, Any],
    ) -> AsyncIterator[Mapping[str, Any]]: ...

    async def close(self) -> None: ...


class HttpStrataApi:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 600.0,
        api_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        headers = {} if not api_key else {"authorization": f"Bearer {api_key}"}
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            headers=headers,
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
        if response.status_code >= 400:
            raise StrataHttpError(response.status_code, _error_message(response))
        body = response.json()
        if not isinstance(body, Mapping):
            raise RuntimeError("Strata returned a non-object response")
        return body

    async def models(self) -> Mapping[str, str]:
        """Model id -> load state (for example "loaded") from /v1/models."""
        body = await self._json("GET", "/v1/models")
        data = body.get("data", ())
        if not isinstance(data, list):
            raise RuntimeError("Strata /v1/models returned invalid data")
        result: dict[str, str] = {}
        for item in data:
            if not isinstance(item, Mapping):
                continue
            model_id = item.get("id")
            if not isinstance(model_id, str) or not model_id.strip():
                continue
            status = item.get("status")
            state = status.get("value") if isinstance(status, Mapping) else None
            result[model_id.strip()] = str(state or "")
        return result

    async def chat(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return await self._json("POST", "/v1/chat/completions", json=payload)

    async def chat_stream(
        self,
        payload: Mapping[str, Any],
    ) -> AsyncIterator[Mapping[str, Any]]:
        async with self._client.stream(
            "POST",
            "/v1/chat/completions",
            json=dict(payload),
        ) as response:
            if response.status_code >= 400:
                await response.aread()
                raise StrataHttpError(response.status_code, _error_message(response))
            async for raw_line in response.aiter_lines():
                line = raw_line.strip()
                if not line or line.startswith(":"):
                    continue
                if not line.startswith("data:"):
                    raise RuntimeError("Strata stream returned invalid SSE framing")
                data = line[5:].strip()
                if data == "[DONE]":
                    return
                try:
                    value = json.loads(data)
                except json.JSONDecodeError as exc:
                    raise RuntimeError("Strata stream returned invalid JSON") from exc
                if not isinstance(value, Mapping):
                    raise RuntimeError("Strata stream returned a non-object event")
                error = value.get("error")
                if isinstance(error, (Mapping, str)):
                    raise _stream_error(error)
                yield value

    async def close(self) -> None:
        await self._client.aclose()


@runtime_checkable
class StrataProcessController(Protocol):
    @property
    def running(self) -> bool: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    def ready_record(self) -> Mapping[str, Any] | None: ...


class StrataSubprocessController:
    def __init__(
        self,
        *,
        executable: str,
        run_config: str,
        base_url: str,
        home: str,
        data_dir: str,
        ready_dir: str,
        parallel: int,
        shutdown_timeout_seconds: float,
        api_key: str | None = None,
    ) -> None:
        self.executable = executable
        self.run_config = run_config
        self.base_url = base_url
        self.home = home
        self.data_dir = data_dir
        self.parallel = parallel
        self.shutdown_timeout_seconds = shutdown_timeout_seconds
        self.api_key = api_key
        parsed = urlsplit(base_url)
        assert parsed.port is not None
        self.ready_file = os.path.join(ready_dir, f"strata-{parsed.port}.ready.json")
        self._process: asyncio.subprocess.Process | None = None

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    def command(self) -> tuple[str, ...]:
        parsed = urlsplit(self.base_url)
        assert parsed.hostname is not None
        assert parsed.port is not None
        return (
            self.executable,
            "serve",
            "--profile",
            self.run_config,
            "--host",
            parsed.hostname,
            "--port",
            str(parsed.port),
            "--parallel",
            str(self.parallel),
            "--ready-file",
            self.ready_file,
            "--shutdown-timeout",
            f"{self.shutdown_timeout_seconds:g}",
            "--output",
            "jsonl",
        )

    def environment(self, base: Mapping[str, str] | None = None) -> dict[str, str]:
        """Inherited environment pointed at the Strata install. The key is passed
        through the environment, never argv. GPU selection is never touched."""
        env = dict(os.environ if base is None else base)
        env["HOME"] = self.home
        env["STRATA_DATA_DIR"] = self.data_dir
        env.pop("STRATA_API_KEY", None)
        if self.api_key is not None:
            env["STRATA_API_KEY"] = self.api_key
        return env

    def ready_record(self) -> Mapping[str, Any] | None:
        """The ready record when it belongs to this owned process, else None."""
        process = self._process
        if process is None or process.returncode is not None:
            return None
        try:
            record = json.loads(Path(self.ready_file).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(record, Mapping):
            return None
        pid = record.get("pid")
        if isinstance(pid, bool) or not isinstance(pid, int):
            return None
        if pid != process.pid:
            # strata serve may run the server in a child; it must be in our group.
            try:
                if os.getpgid(pid) != process.pid:
                    return None
            except OSError:
                return None
        if record.get("model_state") != "ready" or record.get("api_state") != "ready":
            return None
        return record

    def _remove_ready_file(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.ready_file)

    async def start(self) -> None:
        if self.running:
            return
        if self._process is not None:
            await self.stop()
        # Strata refuses an existing ready file; a leftover one is never ours.
        self._remove_ready_file()
        try:
            self._process = await asyncio.create_subprocess_exec(
                *self.command(),
                env=self.environment(),
                stdin=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            raise RuntimeError("failed to start Strata server process") from exc

    @staticmethod
    def _signal_group(pgid: int, signum: int) -> bool:
        try:
            os.killpg(pgid, signum)
        except (ProcessLookupError, PermissionError):
            return False
        return True

    async def stop(self) -> None:
        process = self._process
        if process is None:
            return
        pgid = process.pid
        if process.returncode is None:
            # Strata drains owned requests for --shutdown-timeout, then exits.
            self._signal_group(pgid, signal.SIGTERM)
            try:
                await asyncio.wait_for(
                    process.wait(), timeout=self.shutdown_timeout_seconds + 15.0
                )
            except asyncio.TimeoutError:
                self._signal_group(pgid, signal.SIGKILL)
                await process.wait()
        # The engine child may outlive the CLI and keep the GPU busy.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 15.0
        while self._signal_group(pgid, 0):
            if loop.time() >= deadline:
                self._signal_group(pgid, signal.SIGKILL)
                break
            await asyncio.sleep(0.2)
        self._remove_ready_file()
        self._process = None


class StrataExecutor(JobExecutor):
    capabilities = STRATA_CAPABILITIES

    def __init__(
        self,
        *,
        api: StrataApi,
        model_ref: str,
        served_model_name: str,
        context_tokens: int,
        residency_metadata: Mapping[str, Any],
        tools_enabled: bool = True,
        enable_thinking: bool | None = None,
    ) -> None:
        self.api = api
        self.tools_enabled = tools_enabled
        self.enable_thinking = enable_thinking
        self.context_tokens = context_tokens
        self.serving_features = {
            "llm.chat": frozenset({"tools"}) if tools_enabled else frozenset()
        }
        self.model_ref = _nonblank(model_ref, "model_ref")
        self.served_model_name = _nonblank(served_model_name, "served_model_name")
        self.residency_metadata = dict(residency_metadata)
        self._inflight: dict[str, asyncio.Task[Any]] = {}

    def validate_serving_contract(self, contract: ServingContract) -> None:
        """Strata enforces only prompt + max_tokens <= context, so a contract may
        not promise a tighter input limit or a different context."""
        if contract.capability != "llm.chat":
            return
        limits = contract.limits
        total = limits.get("total_tokens")
        if total != self.context_tokens:
            raise RuntimeError(
                "Strata serving contract total_tokens must equal the run config "
                f"context ({self.context_tokens})"
            )
        if limits.get("input_tokens") != total:
            raise RuntimeError(
                "Strata enforces only the combined context; the serving contract "
                "input_tokens must equal total_tokens"
            )

    def _gateway_chat_payload(self, job: JobRequest) -> dict[str, Any] | None:
        envelope = dict(job.payload)
        if envelope.get("schema_version") != CHAT_JOB_SCHEMA:
            return None
        if set(envelope) != {"schema_version", "adapter_id", "request", "limits"}:
            raise JobExecutionError(
                "invalid_chat_job",
                "chat job envelope uses unsupported fields",
                retryable=False,
            )
        if envelope.get("adapter_id") != STRATA_CHAT_ADAPTER:
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
        if limits["total_tokens"] != self.context_tokens:
            raise JobExecutionError(
                "invalid_chat_job",
                "chat job context differs from the bound Strata deployment",
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
        tool_choice = request.get("tool_choice")
        uses_tools = bool(request.get("tools")) or (
            tool_choice is not None and tool_choice != "none"
        )
        for message in request.get("messages", ()):
            if isinstance(message, Mapping) and (
                message.get("role") == "tool" or message.get("tool_calls") is not None
            ):
                uses_tools = True
                break
        if uses_tools and not self.tools_enabled:
            raise JobExecutionError(
                "unsupported_feature",
                "Strata serving deployment does not enable structured tools",
                retryable=False,
            )
        request["model"] = self.served_model_name
        if self.enable_thinking is not None:
            request["chat_template_kwargs"] = {"enable_thinking": self.enable_thinking}
        return request

    @staticmethod
    def _gateway_chat_response(
        response: Mapping[str, Any],
        booleans: Mapping[str, frozenset[str]] | None = None,
    ) -> dict[str, Any]:
        """Drop the stream-only tool_calls[].index from full messages and restore
        schema-declared booleans in tool arguments."""
        result = dict(response)
        choices = result.get("choices")
        if not isinstance(choices, list):
            return result
        normalized: list[Any] = []
        for choice in choices:
            message = choice.get("message") if isinstance(choice, Mapping) else None
            calls = message.get("tool_calls") if isinstance(message, Mapping) else None
            if isinstance(calls, list):
                message = dict(message)
                message["tool_calls"] = [
                    _fix_tool_call(
                        {k: v for k, v in call.items() if k != "index"}, booleans or {}
                    )
                    if isinstance(call, Mapping)
                    else call
                    for call in calls
                ]
                choice = {**choice, "message": message}
            normalized.append(choice)
        result["choices"] = normalized
        return result

    @staticmethod
    def _provider_error(exc: StrataHttpError) -> JobExecutionError:
        if exc.status_code == 400 and _CONTEXT_OVERFLOW_MARKER in exc.message:
            return JobExecutionError(
                "context_length_exceeded",
                "chat input plus output reservation exceeds deployed context",
                retryable=False,
            )
        if exc.status_code in {408, 409, 429}:
            # 409 is ModelBusy: the engine is loading or answering another request.
            return JobExecutionError(
                "provider_busy",
                "Strata is busy",
                retryable=True,
            )
        if exc.status_code in {401, 403, 404}:
            # A key or route mismatch is a deployment fault, not the client's.
            return JobExecutionError(
                "provider_error",
                "Strata refused the Worker's request",
                retryable=False,
            )
        if 400 <= exc.status_code < 500:
            return JobExecutionError(
                "invalid_request",
                "Strata rejected the chat request",
                retryable=False,
            )
        return JobExecutionError(
            "provider_error",
            "Strata failed to answer the chat request",
            retryable=True,
        )

    def _metadata(self) -> dict[str, Any]:
        return {
            "runtime_provider": STRATA_PROVIDER_ID,
            "model": self.model_ref,
            "served_model_name": self.served_model_name,
        }

    @staticmethod
    def _usage_metrics(usage: object, metrics: dict[str, int | float]) -> None:
        if not isinstance(usage, Mapping):
            return
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = usage.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                metrics[key] = value

    async def execute(self, job: JobRequest) -> JobResult:
        if job.capability not in self.capabilities:
            raise ValueError(
                f"Strata executor does not support capability: {job.capability}"
            )
        payload = self._gateway_chat_payload(job)
        if payload is None:
            raise JobExecutionError(
                "invalid_chat_job",
                "Strata serves only Gateway chat jobs",
                retryable=False,
            )
        if payload.get("stream") is True:
            raise JobExecutionError(
                "streaming_requires_event_sink",
                "streaming chat requires the fenced Job event channel",
                retryable=False,
            )

        task = asyncio.create_task(self.api.chat(payload))
        self._inflight[job.job_id] = task
        try:
            response = await task
        except StrataHttpError as exc:
            raise self._provider_error(exc) from exc
        except httpx.HTTPError as exc:
            raise JobExecutionError(
                "provider_error", "Strata is unreachable", retryable=True
            ) from exc
        except (RuntimeError, ValueError) as exc:
            raise JobExecutionError(
                "invalid_provider_response",
                "Strata returned an invalid chat response",
                retryable=False,
            ) from exc
        finally:
            self._inflight.pop(job.job_id, None)
        response = self._gateway_chat_response(
            response, _boolean_parameters(payload.get("tools"))
        )

        text: str | None = None
        choices = response.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], Mapping):
            message = choices[0].get("message")
            if isinstance(message, Mapping) and isinstance(message.get("content"), str):
                text = message["content"]
        metrics: dict[str, int | float] = {}
        self._usage_metrics(response.get("usage"), metrics)
        return JobResult(
            outputs=dict(response),
            metrics=metrics,
            text=text,
            metadata=self._metadata(),
        )

    async def execute_stream(self, job: JobRequest, sink: JobEventSink) -> JobResult:
        if job.capability != "llm.chat":
            return await self.execute(job)
        payload = self._gateway_chat_payload(job)
        if payload is None or payload.get("stream") is not True:
            return await self.execute(job)
        payload["stream_options"] = {"include_usage": True}
        booleans = _boolean_parameters(payload.get("tools"))
        call_names: dict[int, str] = {}

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
                        "Strata returned an invalid chat stream chunk",
                        retryable=False,
                    ) from exc
                if booleans:
                    chunk = _fix_stream_chunk(chunk, booleans, call_names)
                choices = chunk.get("choices")
                if isinstance(choices, list) and choices and isinstance(choices[0], Mapping):
                    delta = choices[0].get("delta")
                    if isinstance(delta, Mapping) and isinstance(delta.get("content"), str):
                        text_parts.append(delta["content"])
                self._usage_metrics(chunk.get("usage"), metrics)
                await sink.emit(JobEvent(kind="chat.completion.chunk", payload=chunk))
        except StrataHttpError as exc:
            raise self._provider_error(exc) from exc
        except httpx.HTTPError as exc:
            raise JobExecutionError(
                "provider_error", "Strata is unreachable", retryable=True
            ) from exc
        finally:
            if self._inflight.get(job.job_id) is current:
                self._inflight.pop(job.job_id, None)

        return JobResult(
            outputs={"streamed": True},
            metrics=metrics,
            text="".join(text_parts) if text_parts else None,
            metadata=self._metadata(),
        )

    async def cancel(self, job_id: str) -> None:
        task = self._inflight.get(job_id)
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def residency(self) -> ResidencyReport:
        return ResidencyReport(
            items=(
                ResidencyItem(
                    name=self.model_ref,
                    kind="strata-model",
                    metadata=dict(self.residency_metadata),
                ),
            ),
            metadata={"runtime_provider": STRATA_PROVIDER_ID},
        )


class StrataManagedRuntime(ManagedRuntime):
    provider_id = STRATA_PROVIDER_ID

    def __init__(
        self,
        *,
        api: StrataApi,
        process: StrataProcessController,
        context: RuntimeCompatibilityContext,
        model_ref: str,
        served_model_name: str,
        context_tokens: int,
        startup_timeout_seconds: float,
        tools_enabled: bool = True,
        enable_thinking: bool | None = None,
    ) -> None:
        self.api = api
        self.process = process
        self.context = context
        self.model_ref = model_ref
        self.served_model_name = served_model_name
        self.startup_timeout_seconds = startup_timeout_seconds
        self._closed = False
        self._executor = StrataExecutor(
            api=api,
            model_ref=model_ref,
            served_model_name=served_model_name,
            context_tokens=context_tokens,
            residency_metadata={
                "residency_policy": context.demand.residency_policy.value,
                "gpu_topology": context.demand.gpu_topology.value,
                "gpu_uuids": list(context.worker.gpu_uuids),
                "host_ram_mb": context.host.host_ram_mb,
                "context_tokens": context_tokens,
            },
            tools_enabled=tools_enabled,
            enable_thinking=enable_thinking,
        )

    async def _model_state(self) -> str | None:
        try:
            models = await self.api.models()
        except Exception:
            return None
        return models.get(self.served_model_name, "")

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("Strata runtime has already been released")
        if not self.process.running and await self._model_state() is not None:
            raise RuntimeError(
                "an external Strata server is already using the configured endpoint"
            )

        started_here = False
        if not self.process.running:
            await self.process.start()
            started_here = True
        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self.startup_timeout_seconds
            while True:
                if not self.process.running:
                    raise RuntimeError(
                        "Strata owned process exited before readiness; "
                        "inspect Worker service logs"
                    )
                if self.process.ready_record() is not None:
                    break
                if loop.time() >= deadline:
                    raise RuntimeError("Strata server did not become ready before timeout")
                await asyncio.sleep(0.5)
            state = await self._model_state()
            if state is None or state == "":
                raise RuntimeError(
                    "Strata server did not expose the configured model name"
                )
        except Exception:
            if started_here:
                with contextlib.suppress(Exception):
                    await self.process.stop()
            raise

    async def stop(self) -> None:
        await self.process.stop()

    async def health(self) -> RuntimeHealth:
        meta = {"runtime_provider": STRATA_PROVIDER_ID}
        state = await self._model_state()
        if state is None:
            return RuntimeHealth(
                state=(
                    RuntimeHealthState.STARTING
                    if self.process.running
                    else RuntimeHealthState.STOPPED
                ),
                ready=False,
                metadata=meta,
            )
        if not self.process.running:
            return RuntimeHealth(
                state=RuntimeHealthState.FAILED,
                ready=False,
                detail="external Strata process detected at managed endpoint",
                metadata=meta,
            )
        if state == "":
            return RuntimeHealth(
                state=RuntimeHealthState.FAILED,
                ready=False,
                detail="configured Strata model name is unavailable",
                metadata=meta,
            )
        if self.process.ready_record() is None:
            return RuntimeHealth(state=RuntimeHealthState.STARTING, ready=False, metadata=meta)
        return RuntimeHealth(
            state=RuntimeHealthState.READY,
            ready=True,
            metadata={
                **meta,
                "model": self.model_ref,
                "served_model_name": self.served_model_name,
                "model_state": state,
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


class StrataProvider(RuntimeProvider):
    def __init__(
        self,
        config: StrataProviderConfig | None = None,
        *,
        api_factory: Any | None = None,
        process_factory: Any | None = None,
        run_config_loader: Any | None = None,
    ) -> None:
        self.config = config or StrataProviderConfig()
        self._api_factory = api_factory or (
            lambda base_url, api_key: HttpStrataApi(
                base_url,
                timeout_seconds=self.config.request_timeout_seconds,
                api_key=api_key,
            )
        )
        self._process_factory = process_factory or (
            lambda **kwargs: StrataSubprocessController(**kwargs)
        )
        self._run_config_loader = run_config_loader or StrataRunConfig.load
        self._info = RuntimeProviderInfo(
            provider_id=STRATA_PROVIDER_ID,
            display_name="Strata",
            description=(
                "Large MoE serving from GPU, host RAM and SSD with a prepared, "
                "pinned run config."
            ),
        )

    @property
    def info(self) -> RuntimeProviderInfo:
        return self._info

    def compatibility(self, context: RuntimeCompatibilityContext) -> RuntimeCompatibility:
        reasons: list[CompatibilityReason] = []
        demand = context.demand
        model = demand.model
        resources = context.worker.resources

        if context.host.architecture.lower() not in {"x86_64", "amd64"}:
            reasons.append(CompatibilityReason(
                code="architecture-unsupported",
                message=(
                    "the TSUMGI Strata provider is qualified on Linux x86_64 only; "
                    f"host architecture is {context.host.architecture}"
                ),
            ))
        if model.model_format not in STRATA_MODEL_FORMATS:
            reasons.append(CompatibilityReason(
                code="model-format-unsupported",
                message="the Strata provider serves a prepared local GGUF model",
            ))
        if not os.path.isabs(model.model_ref):
            reasons.append(CompatibilityReason(
                code="model-reference-must-be-local",
                message=(
                    "Strata models are downloaded and prepared by setup; the "
                    "Worker binds only an absolute local GGUF path"
                ),
            ))
        if model.topology is not ModelTopology.MOE:
            reasons.append(CompatibilityReason(
                code="model-topology-outside-provider-scope",
                message="the TSUMGI Strata provider is scoped to MoE models",
            ))
        if resources.gpu_count < 1 or len(context.worker.gpu_uuids) != resources.gpu_count:
            reasons.append(CompatibilityReason(
                code="gpu-required",
                message="the Strata provider owns every GPU of its Worker",
            ))
        if (
            demand.gpu_topology is GPUTopology.SINGLE_GPU
            and resources.gpu_count != 1
        ):
            reasons.append(CompatibilityReason(
                code="single-gpu-topology-mismatch",
                message="single_gpu demand requires a single-GPU Worker",
            ))
        if demand.residency_policy is ResidencyPolicy.VRAM_ONLY:
            reasons.append(CompatibilityReason(
                code="vram-only-unsupported",
                message="Strata keeps MoE experts in host RAM or on SSD",
            ))

        try:
            run_config = self._run_config_loader(self.config.run_config)
        except ValueError as exc:
            reasons.append(CompatibilityReason(
                code="run-config-unreadable",
                message=str(exc),
            ))
        else:
            if run_config.model_name != self.config.served_model_name:
                reasons.append(CompatibilityReason(
                    code="run-config-model-name-mismatch",
                    message="served_model_name must equal the run config model_name",
                ))
            if run_config.model_path != model.model_ref:
                reasons.append(CompatibilityReason(
                    code="run-config-model-mismatch",
                    message="the run config serves a different model file",
                ))
            if run_config.max_context != self.config.context_tokens:
                reasons.append(CompatibilityReason(
                    code="run-config-context-mismatch",
                    message="context_tokens must equal the run config --max-context",
                ))
            if run_config.fit_max_tokens is not False:
                reasons.append(CompatibilityReason(
                    code="run-config-clamps-max-tokens",
                    message=(
                        "the run config must set fit_max_tokens to false so Strata "
                        "rejects requests that exceed the context"
                    ),
                ))

        reasons.append(CompatibilityReason(
            code="host-ram-ssd-sensitive",
            message=(
                "Strata speed and feasible model size depend on host RAM, SSD and "
                "the CPU-GPU interconnect"
            ),
            blocking=False,
        ))
        return RuntimeCompatibility(provider_id=STRATA_PROVIDER_ID, reasons=tuple(reasons))

    def setup_intent(self, context: RuntimeCompatibilityContext) -> RuntimeSetupIntent:
        report = self.compatibility(context)
        if not report.compatible:
            raise RuntimeError("cannot build Strata setup intent for incompatible demand")
        return RuntimeSetupIntent(
            provider_id=STRATA_PROVIDER_ID,
            package_references=(self.config.package_reference,),
            configuration={
                "base_url": self.config.base_url,
                "executable": self.config.executable,
                "run_config": self.config.run_config,
                "model_ref": context.demand.model.model_ref,
                "served_model_name": self.config.served_model_name,
                "context_tokens": self.config.context_tokens,
                "parallel": self.config.parallel,
                "gpu_uuids": list(context.worker.gpu_uuids),
                "capabilities": sorted(STRATA_CAPABILITIES),
            },
            model_preparation=ModelPreparationPolicy.REFERENCE_ONLY,
            model_ref=context.demand.model.model_ref,
            requires_privilege=True,
            metadata={"runtime_provider": STRATA_PROVIDER_ID},
        )

    def create_runtime(
        self,
        context: RuntimeCompatibilityContext,
        setup: RuntimeSetupIntent,
    ) -> ManagedRuntime:
        if setup.provider_id != STRATA_PROVIDER_ID:
            raise ValueError("setup intent does not belong to Strata provider")
        report = self.compatibility(context)
        if not report.compatible:
            raise RuntimeError("cannot create Strata runtime for incompatible demand")

        cfg = dict(setup.configuration)
        for key, expected in (
            ("executable", self.config.executable),
            ("run_config", self.config.run_config),
            ("served_model_name", self.config.served_model_name),
            ("context_tokens", self.config.context_tokens),
            ("base_url", self.config.base_url),
        ):
            if key in cfg and cfg[key] != expected:
                raise ValueError(f"setup intent {key} differs from the Strata provider")
        configured_gpus = cfg.get("gpu_uuids")
        if configured_gpus is not None and list(configured_gpus) != list(context.worker.gpu_uuids):
            raise ValueError("setup intent GPU UUIDs do not match Strata Worker ownership")

        api_key = None
        if self.config.api_key_file is not None:
            # Read once so the Worker client and the server always share one key.
            api_key = Path(self.config.api_key_file).read_text(encoding="utf-8").strip()
            if not api_key:
                raise ValueError("Strata API key file is empty")
        api = self._api_factory(self.config.base_url, api_key)
        process = self._process_factory(
            executable=self.config.executable,
            run_config=self.config.run_config,
            base_url=self.config.base_url,
            home=self.config.home,
            data_dir=self.config.data_dir,
            ready_dir=self.config.ready_dir,
            parallel=self.config.parallel,
            shutdown_timeout_seconds=self.config.shutdown_timeout_seconds,
            api_key=api_key,
        )
        return StrataManagedRuntime(
            api=api,
            process=process,
            context=context,
            model_ref=context.demand.model.model_ref,
            served_model_name=self.config.served_model_name,
            context_tokens=self.config.context_tokens,
            startup_timeout_seconds=self.config.startup_timeout_seconds,
            tools_enabled=self.config.tools_enabled,
            enable_thinking=self.config.enable_thinking,
        )
