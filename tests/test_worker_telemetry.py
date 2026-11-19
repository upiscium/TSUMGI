from __future__ import annotations

import asyncio
import stat

import httpx
import pytest

from astrumweaver import JobResult, ResidencyReport, ResourceShape, WorkerSpec
from astrumweaver.control import InMemoryControlRepository
from astrumweaver.control.api import create_app
from astrumweaver.worker import ControlClient, WorkerRuntime
from astrumweaver.worker.telemetry import (
    TELEMETRY_METADATA_KEY,
    TELEMETRY_SCHEMA,
    WorkerTelemetry,
    parse_meminfo,
    parse_nvidia_smi,
    runtime_summary,
)

A = "GPU-aaaaaaaa-0000-0000-0000-000000000000"
B = "GPU-bbbbbbbb-0000-0000-0000-000000000000"
SMI = (
    f"{B}, NVIDIA GeForce RTX 3060, 12288, 11020, 97, 61, 120.50\n"
    f"{A}, NVIDIA GeForce GTX 1080 Ti, 11264, 10741, 0, 44, [N/A]\n"
)


def test_parse_nvidia_smi_keeps_configured_gpus_in_configured_order():
    gpus = parse_nvidia_smi(SMI, [A, B])
    assert [g["uuid"] for g in gpus] == [A, B]
    assert gpus[0] == {
        "uuid": A,
        "name": "NVIDIA GeForce GTX 1080 Ti",
        "memory_total_mb": 11264.0,
        "memory_used_mb": 10741.0,
        "utilization_percent": 0.0,
        "temperature_c": 44.0,
        "power_w": None,
    }
    assert [g["uuid"] for g in parse_nvidia_smi(SMI, [B])] == [B]


def test_parse_meminfo_reports_mib():
    mem = parse_meminfo("MemTotal:       33554432 kB\nMemAvailable:   16777216 kB\nHugePages_Total: 0\n")
    assert mem["MemTotal"] == 32768 and mem["MemAvailable"] == 16384


@pytest.mark.parametrize(
    "provider, config, model, expected",
    [
        ("strata", {"served_model_name": "qwen", "context_tokens": 65536}, "/m/Q.gguf",
         {"provider_id": "strata", "model": "Q.gguf", "served_model_name": "qwen", "context_tokens": 65536}),
        ("llama-cpp", {"model_alias": "fast", "context_size": 196608}, "/v/M.gguf",
         {"provider_id": "llama-cpp", "model": "M.gguf", "served_model_name": "fast", "context_tokens": 196608}),
        (None, None, None, {"provider_id": None}),
    ],
)
def test_runtime_summary(provider, config, model, expected):
    assert runtime_summary(provider, config, model) == expected


def _fake_smi(tmp_path, body, code=0):
    script = tmp_path / "nvidia-smi"
    script.write_text(f"#!/bin/sh\ncat <<'EOF'\n{body}EOF\nexit {code}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def test_collect_reports_gpus_host_and_runtime(tmp_path):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal: 33554432 kB\nMemAvailable: 8388608 kB\n")
    telemetry = WorkerTelemetry(
        gpu_uuids=[A],
        runtime={"provider_id": "strata"},
        nvidia_smi=_fake_smi(tmp_path, SMI),
        meminfo_path=str(meminfo),
        disk_path=str(tmp_path),
    )
    snap = telemetry.collect()
    assert snap["schema"] == TELEMETRY_SCHEMA
    assert snap["runtime"] == {"provider_id": "strata"}
    assert [g["memory_used_mb"] for g in snap["gpus"]] == [10741.0]
    assert snap["host"]["memory_total_mb"] == 32768
    assert snap["host"]["memory_available_mb"] == 8192
    assert snap["host"]["cpu_count"] >= 1 and snap["host"]["disk_free_mb"] >= 0
    assert "errors" not in snap


def test_collect_records_failures_instead_of_raising(tmp_path):
    telemetry = WorkerTelemetry(
        gpu_uuids=[A, B],
        nvidia_smi=_fake_smi(tmp_path, f"{A}, x, 1, 1, 1, 1, 1\n"),
        meminfo_path=str(tmp_path / "missing"),
    )
    snap = telemetry.collect()
    assert [g["uuid"] for g in snap["gpus"]] == [A]
    assert any("not visible" in e and B in e for e in snap["errors"])
    assert "meminfo unavailable" in snap["errors"]

    failing = WorkerTelemetry(gpu_uuids=[A], nvidia_smi=_fake_smi(tmp_path, "", code=9))
    assert failing.collect()["errors"][0] == "nvidia-smi exit 9"
    missing = WorkerTelemetry(gpu_uuids=[A], nvidia_smi=str(tmp_path / "absent"))
    assert missing.collect()["errors"][0] == "nvidia-smi unavailable"


@pytest.mark.asyncio
async def test_run_refreshes_until_stopped_and_survives_errors(tmp_path, monkeypatch):
    telemetry = WorkerTelemetry(interval_seconds=0.01)
    calls = []

    def collect():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return {"schema": TELEMETRY_SCHEMA, "n": len(calls)}

    monkeypatch.setattr(telemetry, "collect", collect)
    stop = asyncio.Event()
    task = asyncio.create_task(telemetry.run(stop))
    while len(calls) < 3:
        await asyncio.sleep(0.005)
    stop.set()
    await asyncio.wait_for(task, 1)
    assert telemetry.latest()["n"] >= 2


WORKER_TOKEN = "worker-secret"


class IdleExecutor:
    capabilities = frozenset({"debug.echo"})

    async def execute(self, job):
        return JobResult(outputs={})

    async def cancel(self, job_id):
        return None

    async def residency(self):
        return ResidencyReport()


@pytest.mark.asyncio
async def test_heartbeat_carries_latest_telemetry_to_control():
    repo = InMemoryControlRepository()
    app = create_app(repo, client_token=None, worker_token=WORKER_TOKEN, client_auth="none",
                     maintenance_interval_seconds=60)
    client = ControlClient("http://control", WORKER_TOKEN, transport=httpx.ASGITransport(app=app))
    telemetry = WorkerTelemetry()
    telemetry._latest = {"schema": TELEMETRY_SCHEMA, "host": {"cpu_count": 8}}
    runtime = WorkerRuntime(
        spec=WorkerSpec(worker_id="w1", worker_class="cpu-test", resources=ResourceShape(),
                        capabilities=frozenset({"debug.echo"})),
        max_concurrency=1, executor=IdleExecutor(), client=client,
        heartbeat_interval_seconds=1, telemetry=telemetry,
    )
    try:
        await runtime.register()
        async with runtime._control_lock:
            await runtime._heartbeat_locked()
    finally:
        await client.aclose()
    stored = repo.get_worker("w1").metadata
    assert stored[TELEMETRY_METADATA_KEY] == {"schema": TELEMETRY_SCHEMA, "host": {"cpu_count": 8}}
    assert stored["runtime"] == "astrumweaver-worker"
