"""Operator console: read-only Worker status and Hugging Face model checks.

The console is off unless ``[admin_ui] enabled = true``. With
``username`` and ``password_hash`` configured it requires HTTP Basic
authentication on every route (see ``admin_auth``); without them it has NO
authentication and must stay on an internal network.
It can only read Worker state and ask the Hugging Face API about a model;
it never changes Workers, Jobs or deployments.

Everything a Worker reports is untrusted display data. ``worker_status``
copies only known fields and normalizes their types, so one Worker with
odd telemetry cannot break the list, the page, or the model check.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable, Collection, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from ..transport import TELEMETRY_METADATA_KEY
from .model_check import HuggingFaceClient, ModelCheckError, UpstreamError, check_model
from .models import WorkerRecord, utc_now
from .repository import ControlRepository

_STATIC = Path(__file__).with_name("admin_ui")
_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
# Telemetry older than this is shown as stale rather than as a live value.
TELEMETRY_STALE_SECONDS = 120.0
MAX_REQUEST_BYTES = 4096
MODEL_CHECK_TIMEOUT_SECONDS = 30.0
_HOST_FIELDS = (
    "cpu_count", "load_1m", "load_5m", "memory_total_mb", "memory_available_mb",
    "disk_total_mb", "disk_free_mb",
)
_MAX_TEXT = 200
_MAX_ERRORS = 5


def _num(value: Any) -> int | float | None:
    """A finite number, or None. Booleans, strings and NaN/inf are not numbers."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def _text(value: Any, limit: int = _MAX_TEXT) -> str | None:
    return value[:limit] if isinstance(value, str) and value else None


def _age_seconds(then: datetime, now: datetime) -> float:
    return max(0.0, (now - then).total_seconds())


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def worker_status(
    record: WorkerRecord,
    now: datetime,
    profile_names: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """One Worker as the console shows it. Missing facts stay ``None``."""
    raw = record.metadata.get(TELEMETRY_METADATA_KEY)
    telemetry: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
    collected = _parse_time(telemetry.get("collected_at"))
    telemetry_age = None if collected is None else _age_seconds(collected, now)

    live_gpus: dict[str, Mapping[str, Any]] = {}
    reported = telemetry.get("gpus")
    for gpu in reported if isinstance(reported, list) else ():
        if isinstance(gpu, Mapping) and isinstance(gpu.get("uuid"), str):
            live_gpus[gpu["uuid"]] = gpu
    gpus = []
    for accelerator in record.spec.accelerators:
        live = live_gpus.get(accelerator.uuid, {})
        gpus.append({
            "uuid": accelerator.uuid,
            "name": accelerator.device_class,
            "memory_total_mb": accelerator.memory_mb,
            "memory_used_mb": _num(live.get("memory_used_mb")),
            "utilization_percent": _num(live.get("utilization_percent")),
            "temperature_c": _num(live.get("temperature_c")),
            "power_w": _num(live.get("power_w")),
        })

    # Workers registered without accelerator records (older configs) still list
    # their GPUs: identity from the registration, the rest from telemetry.
    known = {accelerator.uuid for accelerator in record.spec.accelerators}
    for uuid in record.spec.gpu_uuids:
        if uuid in known:
            continue
        live = live_gpus.get(uuid, {})
        gpus.append({
            "uuid": uuid,
            "name": _text(live.get("name"), 64),
            "memory_total_mb": _num(live.get("memory_total_mb")),
            "memory_used_mb": _num(live.get("memory_used_mb")),
            "utilization_percent": _num(live.get("utilization_percent")),
            "temperature_c": _num(live.get("temperature_c")),
            "power_w": _num(live.get("power_w")),
        })

    raw_runtime = telemetry.get("runtime")
    runtime: Mapping[str, Any] = raw_runtime if isinstance(raw_runtime, Mapping) else {}
    context = _num(runtime.get("context_tokens"))
    raw_host = telemetry.get("host")
    host = None
    if isinstance(raw_host, Mapping):
        host = {key: _num(raw_host.get(key)) for key in _HOST_FIELDS}
    raw_errors = telemetry.get("errors")
    errors = [
        text for text in (_text(e) for e in (raw_errors if isinstance(raw_errors, list) else ()))
        if text
    ][:_MAX_ERRORS]

    serving = record.serving
    deployment_revision = None if serving is None else serving.deployment_revision
    contexts = (
        []
        if serving is None
        else sorted({
            int(c.limits["total_tokens"])
            for c in serving.contracts
            if isinstance(c.limits.get("total_tokens"), int)
        })
    )
    return {
        "worker_id": record.worker_id,
        "state": record.state.value,
        "worker_class": record.spec.worker_class,
        "last_seen_age_s": round(_age_seconds(record.last_seen_at, now), 1),
        "active_jobs": record.active_jobs,
        "max_concurrency": record.max_concurrency,
        "capabilities": sorted(record.spec.capabilities),
        "profile": (profile_names or {}).get(deployment_revision or ""),
        "deployment_revision": deployment_revision,
        "context_tokens": int(context) if context else (contexts[-1] if contexts else None),
        "backend": _text(runtime.get("provider_id"), 64),
        "model": _text(runtime.get("model")),
        "served_model_name": _text(runtime.get("served_model_name")),
        "gpus": gpus,
        "host": host,
        "telemetry_age_s": None if telemetry_age is None else round(telemetry_age, 1),
        "telemetry_stale": telemetry_age is None or telemetry_age > TELEMETRY_STALE_SECONDS,
        "telemetry_errors": errors,
    }


def fit_inputs(statuses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The facts ``estimate_fit`` needs, from online Workers with GPUs."""
    inputs = []
    for status in statuses:
        if status["state"] == "offline" or not status["gpus"]:
            continue
        host = status["host"] or {}
        inputs.append({
            "worker_id": status["worker_id"],
            "backend": status["backend"],
            "capabilities": status["capabilities"],
            "vram_total_mb": sum(g["memory_total_mb"] or 0 for g in status["gpus"]),
            "ram_total_mb": host.get("memory_total_mb"),
        })
    return inputs


def _request_host(request: Request) -> str:
    host = request.headers.get("host", "").strip().lower()
    if host.startswith("["):  # [::1]:9000
        return host[1:].split("]", 1)[0]
    return host.split(":", 1)[0]


def create_admin_router(
    repository: ControlRepository,
    *,
    profile_names: Mapping[str, str] | None = None,
    hf_client: HuggingFaceClient | None = None,
    allowed_hosts: Collection[str] | None = None,
    auth: Callable[[Request], None] | None = None,
) -> APIRouter:
    """The console routes.

    ``allowed_hosts`` optionally restricts the ``Host`` header, which blocks
    DNS-rebinding pages from reading the console through a browser. Empty or
    ``None`` means any host (the console is then protected by the network
    alone).
    """
    hosts = frozenset(h.strip().lower() for h in (allowed_hosts or ()) if h and h.strip())

    def check_host(request: Request) -> None:
        if hosts and _request_host(request) not in hosts:
            raise HTTPException(status_code=403, detail="この Host 名では管理画面を開けません")

    # The Host check runs first; authentication covers every route, pages included.
    dependencies = [Depends(check_host)]
    if auth is not None:
        dependencies.append(Depends(auth))
    router = APIRouter(prefix="/admin", dependencies=dependencies)
    client = hf_client or HuggingFaceClient()

    def _statuses() -> list[dict[str, Any]]:
        now = utc_now()
        return [worker_status(r, now, profile_names) for r in repository.list_workers()]

    def _page(name: str, media_type: str) -> Response:
        return FileResponse(
            _STATIC / name,
            media_type=media_type,
            headers={"Content-Security-Policy": _CSP, "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    @router.get("", include_in_schema=False)
    @router.get("/", include_in_schema=False)
    async def index() -> Response:
        setup = auth is not None and getattr(auth, "needs_setup", False)
        return _page("setup.html" if setup else "index.html", "text/html; charset=utf-8")

    @router.get("/setup.js", include_in_schema=False)
    async def setup_script() -> Response:
        return _page("setup.js", "text/javascript; charset=utf-8")

    @router.get("/app.js", include_in_schema=False)
    async def script() -> Response:
        return _page("app.js", "text/javascript; charset=utf-8")

    @router.get("/app.css", include_in_schema=False)
    async def stylesheet() -> Response:
        return _page("app.css", "text/css; charset=utf-8")

    @router.get("/api/workers")
    async def workers() -> JSONResponse:
        statuses = await asyncio.to_thread(_statuses)
        return JSONResponse(
            {"generated_at": utc_now().isoformat(), "workers": statuses},
            headers={"Cache-Control": "no-store"},
        )

    async def _json_body(request: Request) -> tuple[Any, JSONResponse | None]:
        # Basic credentials ride along on same-site requests automatically, so a
        # JSON content type (which forces a CORS preflight from other origins) is
        # the CSRF defence for every POST: do not relax it.
        if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
            return None, _error(415, "Content-Type は application/json にしてください")
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > MAX_REQUEST_BYTES:
            return None, _error(413, "リクエストが大きすぎます")
        raw = await request.body()
        if len(raw) > MAX_REQUEST_BYTES:
            return None, _error(413, "リクエストが大きすぎます")
        try:
            return await request.json(), None
        except ValueError:
            return None, _error(400, "本文は JSON にしてください")

    @router.post("/api/setup")
    async def setup(request: Request) -> JSONResponse:
        if auth is None or not hasattr(auth, "set_up"):
            return _error(404, "この設定では初期設定は使えません")
        body, failure = await _json_body(request)
        if failure is not None:
            return failure
        if not isinstance(body, dict):
            return _error(400, "本文は JSON のオブジェクトにしてください")
        try:
            await asyncio.to_thread(auth.set_up, request, body.get("code"), body.get("username"), body.get("password"))
        except HTTPException as exc:
            return JSONResponse(
                {"error": exc.detail}, status_code=exc.status_code, headers={**(exc.headers or {}), "Cache-Control": "no-store"}
            )
        return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})

    @router.post("/api/model-check")
    async def model_check(request: Request) -> JSONResponse:
        body, failure = await _json_body(request)
        if failure is not None:
            return failure
        url = body.get("url") if isinstance(body, dict) else None
        if not isinstance(url, str):
            return _error(400, '本文は {"url": "<Hugging Face の URL>"} の形にしてください')
        statuses = await asyncio.to_thread(_statuses)
        try:
            async with asyncio.timeout(MODEL_CHECK_TIMEOUT_SECONDS):
                result = await check_model(url, fit_inputs(statuses), client)
        except ModelCheckError as exc:
            return _error(400, str(exc))
        except UpstreamError as exc:
            return _error(502, str(exc))
        except (httpx.HTTPError, TimeoutError):
            return _error(502, "Hugging Face に接続できません。しばらくしてから再試行してください")
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    return router


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status, headers={"Cache-Control": "no-store"})


__all__ = ["create_admin_router", "fit_inputs", "worker_status"]
