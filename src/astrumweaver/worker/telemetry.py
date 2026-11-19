"""Best-effort host and runtime telemetry sent with Worker heartbeats.

Operators need to see each Worker's live GPU memory, host memory and CPU,
and which backend and model it serves. A background task collects a
snapshot every ``interval_seconds``; heartbeats only attach the latest one,
so a slow ``nvidia-smi`` never delays a heartbeat. Collection failures are
reported inside the snapshot and never stop the Worker.

Inside an LXC container ``os.sched_getaffinity`` and ``/proc/meminfo``
(through lxcfs) describe the container, not the Proxmox host.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import subprocess
import time
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TELEMETRY_METADATA_KEY = "telemetry"
TELEMETRY_SCHEMA = "worker-telemetry-v1"

_GPU_FIELDS = (
    "uuid",
    "name",
    "memory.total",
    "memory.used",
    "utilization.gpu",
    "temperature.gpu",
    "power.draw",
)
_LOG = logging.getLogger(__name__)


def _number(raw: str) -> float | None:
    try:
        value = float(raw.strip())
    except ValueError:  # "[N/A]", "[Not Supported]"
        return None
    return value


def parse_nvidia_smi(output: str, gpu_uuids: Sequence[str]) -> list[dict[str, Any]]:
    """Parse ``--format=csv,noheader,nounits`` rows for the configured GPUs."""
    wanted = list(gpu_uuids)
    rows: dict[str, dict[str, Any]] = {}
    for line in output.splitlines():
        cells = [cell.strip() for cell in line.split(",")]
        if len(cells) != len(_GPU_FIELDS):
            continue
        uuid, name, total, used, util, temp, power = cells
        if wanted and uuid not in wanted:
            continue
        rows[uuid] = {
            "uuid": uuid,
            "name": name,
            "memory_total_mb": _number(total),
            "memory_used_mb": _number(used),
            "utilization_percent": _number(util),
            "temperature_c": _number(temp),
            "power_w": _number(power),
        }
    order = wanted or sorted(rows)
    return [rows[uuid] for uuid in order if uuid in rows]


def parse_meminfo(text: str) -> dict[str, int]:
    values: dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            values[key.strip()] = int(parts[0]) // 1024  # kB -> MiB
    return values


def runtime_summary(
    provider_id: str | None,
    provider_config: Mapping[str, Any] | None = None,
    model_ref: str | None = None,
) -> dict[str, Any]:
    """Static description of the served backend, from the runtime manifest."""
    config = dict(provider_config or {})
    summary: dict[str, Any] = {"provider_id": provider_id}
    if model_ref:
        summary["model"] = Path(str(model_ref)).name
    for key in ("served_model_name", "model_alias"):
        if isinstance(config.get(key), str):
            summary["served_model_name"] = config[key]
            break
    for key in ("context_tokens", "context_size", "max_seq_len"):
        value = config.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            summary["context_tokens"] = value
            break
    return summary


class WorkerTelemetry:
    """Collect a telemetry snapshot periodically; hand out the latest one."""

    def __init__(
        self,
        *,
        gpu_uuids: Sequence[str] = (),
        runtime: Mapping[str, Any] | None = None,
        interval_seconds: float = 30.0,
        nvidia_smi: str = "nvidia-smi",
        command_timeout_seconds: float = 5.0,
        meminfo_path: str = "/proc/meminfo",
        disk_path: str = "/",
    ) -> None:
        if not interval_seconds > 0:
            raise ValueError("telemetry interval must be positive")
        self.gpu_uuids = tuple(gpu_uuids)
        self.runtime = dict(runtime or {})
        self.interval_seconds = float(interval_seconds)
        self.nvidia_smi = nvidia_smi
        self.command_timeout_seconds = command_timeout_seconds
        self.meminfo_path = meminfo_path
        self.disk_path = disk_path
        self._latest: dict[str, Any] | None = None

    def latest(self) -> dict[str, Any] | None:
        return None if self._latest is None else dict(self._latest)

    def collect(self) -> dict[str, Any]:
        """Take one snapshot synchronously (run it in a thread)."""
        errors: list[str] = []
        snapshot: dict[str, Any] = {
            "schema": TELEMETRY_SCHEMA,
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "runtime": dict(self.runtime),
            "gpus": self._gpus(errors),
            "host": self._host(errors),
        }
        if errors:
            snapshot["errors"] = errors
        return snapshot

    async def refresh(self) -> dict[str, Any]:
        snapshot = await asyncio.to_thread(self.collect)
        self._latest = snapshot
        return snapshot

    async def run(self, stop: asyncio.Event) -> None:
        """Refresh until ``stop`` is set. Never raises on collection failure."""
        while not stop.is_set():
            started = time.monotonic()
            try:
                await self.refresh()
            except Exception:  # never let telemetry take the Worker down
                _LOG.warning("Worker telemetry collection failed", exc_info=True)
            delay = max(0.0, self.interval_seconds - (time.monotonic() - started))
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=delay)

    def _gpus(self, errors: list[str]) -> list[dict[str, Any]]:
        if not self.gpu_uuids:
            return []
        if shutil.which(self.nvidia_smi) is None and not Path(self.nvidia_smi).is_file():
            errors.append("nvidia-smi unavailable")
            return []
        try:
            result = subprocess.run(
                [
                    self.nvidia_smi,
                    "--query-gpu=" + ",".join(_GPU_FIELDS),
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=self.command_timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            errors.append(f"nvidia-smi failed: {type(exc).__name__}")
            return []
        if result.returncode != 0:
            errors.append(f"nvidia-smi exit {result.returncode}")
            return []
        gpus = parse_nvidia_smi(result.stdout, self.gpu_uuids)
        missing = [uuid for uuid in self.gpu_uuids if uuid not in {g["uuid"] for g in gpus}]
        if missing:
            errors.append("configured GPU not visible: " + ", ".join(missing))
        return gpus

    def _host(self, errors: list[str]) -> dict[str, Any]:
        host: dict[str, Any] = {}
        try:
            host["cpu_count"] = len(os.sched_getaffinity(0))
        except (AttributeError, OSError):
            host["cpu_count"] = os.cpu_count()
        with contextlib.suppress(OSError):
            host["load_1m"], host["load_5m"], _ = os.getloadavg()
        try:
            mem = parse_meminfo(Path(self.meminfo_path).read_text())
            host["memory_total_mb"] = mem.get("MemTotal")
            host["memory_available_mb"] = mem.get("MemAvailable")
        except OSError:
            errors.append("meminfo unavailable")
        try:
            usage = shutil.disk_usage(self.disk_path)
            host["disk_total_mb"] = usage.total // (1024 * 1024)
            host["disk_free_mb"] = usage.free // (1024 * 1024)
        except OSError:
            errors.append("disk usage unavailable")
        return host
