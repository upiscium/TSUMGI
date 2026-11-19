"""Operator console: read-only Worker status and Hugging Face model checks.

There is NO authentication here. The console is meant for an internal
network and is off unless ``[admin_ui] enabled = true``. Keep it off any
public listener (for example by not proxying ``/admin`` outside the LAN).
It can only read Worker state and ask the Hugging Face API about a model;
it never changes Workers, Jobs or deployments.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from ..transport import TELEMETRY_METADATA_KEY
from .model_check import HuggingFaceClient, ModelCheckError, check_model
from .models import WorkerRecord, utc_now
from .repository import ControlRepository

_STATIC = Path(__file__).with_name("admin_ui")
_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
# Telemetry older than this is shown as stale rather than as a live value.
TELEMETRY_STALE_SECONDS = 120.0


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
    telemetry = record.metadata.get(TELEMETRY_METADATA_KEY)
    telemetry = telemetry if isinstance(telemetry, Mapping) else {}
    collected = _parse_time(telemetry.get("collected_at"))
    telemetry_age = None if collected is None else _age_seconds(collected, now)
    live_gpus = {
        g.get("uuid"): g for g in telemetry.get("gpus", []) if isinstance(g, Mapping)
    }
    gpus = []
    for accelerator in record.spec.accelerators:
        live = live_gpus.get(accelerator.uuid, {})
        gpus.append({
            "uuid": accelerator.uuid,
            "name": accelerator.device_class,
            "memory_total_mb": accelerator.memory_mb,
            "memory_used_mb": live.get("memory_used_mb"),
            "utilization_percent": live.get("utilization_percent"),
            "temperature_c": live.get("temperature_c"),
            "power_w": live.get("power_w"),
        })
    runtime = telemetry.get("runtime") if isinstance(telemetry.get("runtime"), Mapping) else {}
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
        "context_tokens": runtime.get("context_tokens") or (contexts[-1] if contexts else None),
        "backend": runtime.get("provider_id"),
        "model": runtime.get("model"),
        "served_model_name": runtime.get("served_model_name"),
        "gpus": gpus,
        "host": telemetry.get("host") if isinstance(telemetry.get("host"), Mapping) else None,
        "telemetry_age_s": None if telemetry_age is None else round(telemetry_age, 1),
        "telemetry_stale": telemetry_age is None or telemetry_age > TELEMETRY_STALE_SECONDS,
        "telemetry_errors": telemetry.get("errors") or [],
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


def create_admin_router(
    repository: ControlRepository,
    *,
    profile_names: Mapping[str, str] | None = None,
    hf_client: HuggingFaceClient | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/admin")
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
        return _page("index.html", "text/html; charset=utf-8")

    @router.get("/app.js", include_in_schema=False)
    async def script() -> Response:
        return _page("app.js", "text/javascript; charset=utf-8")

    @router.get("/app.css", include_in_schema=False)
    async def stylesheet() -> Response:
        return _page("app.css", "text/css; charset=utf-8")

    @router.get("/api/workers")
    async def workers() -> JSONResponse:
        import asyncio

        statuses = await asyncio.to_thread(_statuses)
        return JSONResponse(
            {"generated_at": utc_now().isoformat(), "workers": statuses},
            headers={"Cache-Control": "no-store"},
        )

    @router.post("/api/model-check")
    async def model_check(request: Request) -> JSONResponse:
        import asyncio

        # A JSON content type forces a CORS preflight from other origins, so a
        # page on another site cannot trigger this from an operator's browser.
        if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
            return _error(415, "Content-Type は application/json にしてください")
        try:
            body = await request.json()
        except ValueError:
            return _error(400, "本文は JSON にしてください")
        url = body.get("url") if isinstance(body, dict) else None
        if not isinstance(url, str):
            return _error(400, '本文は {"url": "<Hugging Face の URL>"} の形にしてください')
        statuses = await asyncio.to_thread(_statuses)
        try:
            result = await check_model(url, fit_inputs(statuses), client)
        except ModelCheckError as exc:
            return _error(400, str(exc))
        except (httpx.HTTPError, TimeoutError):
            return _error(502, "Hugging Face に接続できません。しばらくしてから再試行してください")
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    return router


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status, headers={"Cache-Control": "no-store"})


__all__ = ["create_admin_router", "fit_inputs", "worker_status"]
