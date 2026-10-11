"""VRAM-only model start must not claim unobserved physical memory (#125)."""

from __future__ import annotations

import asyncio
import subprocess
from types import SimpleNamespace

import pytest

from astrumweaver.runtime.contracts import RuntimeHealthState
from astrumweaver.runtime.providers.dual_llama_cpp import (
    _read_selected_gpu_free_vram_mb,
)
from test_dual_llama_cpp import managed_pair


def _nvml_output(monkeypatch, output: str, *, returncode: int = 0):
    commands: list[list[str]] = []

    def fake_run(argv, **kwargs):
        commands.append(argv)
        return SimpleNamespace(stdout=output, stderr="private", returncode=returncode)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return commands


def test_memory_probe_matches_selected_gpu_not_first_row(monkeypatch):
    calls = _nvml_output(
        monkeypatch,
        "GPU-other, 12288, 50\nGPU-selected, 11264, 4096\n",
    )
    assert _read_selected_gpu_free_vram_mb("GPU-selected") == 4096
    assert calls == [[
        "nvidia-smi", "--query-gpu=uuid,memory.total,memory.free",
        "--format=csv,noheader,nounits",
    ]]


@pytest.mark.parametrize(
    "output",
    [
        "GPU-other, 11264, 8000\n",
        "GPU-selected, 11264, 4300\nGPU-selected, 11264, 4200\n",
        "GPU-selected, 11264, 8192\nGPU-other, 11264, 8000\nGPU-other, 11264, 8200\n",
        "GPU-selected, N/A, N/A\n",
        "GPU-selected, 11264, -1\n",
        "GPU-selected, 11264, 11265\n",
        "GPU-selected, 11264, NaN\n",
        "GPU-selected, 11264, 8000, unexpected\n",
        "",
    ],
)
def test_memory_probe_rejects_missing_duplicate_or_malformed(monkeypatch, output):
    _nvml_output(monkeypatch, output)
    with pytest.raises(RuntimeError, match="dual-gpu-vram-unknown"):
        _read_selected_gpu_free_vram_mb("GPU-selected")


def test_memory_probe_fails_closed_when_driver_errors(monkeypatch):
    _nvml_output(monkeypatch, "GPU-selected, 11264, 8000\n", returncode=1)
    with pytest.raises(RuntimeError, match="dual-gpu-vram-unknown"):
        _read_selected_gpu_free_vram_mb("GPU-selected")


def test_memory_probe_fails_closed_when_driver_times_out(monkeypatch):
    def timeout(_argv, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="nvidia-smi", timeout=5)
    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(RuntimeError, match="dual-gpu-vram-unknown"):
        _read_selected_gpu_free_vram_mb("GPU-selected")


@pytest.mark.asyncio
async def test_insufficient_live_vram_blocks_both_children_before_start(tmp_path):
    managed, embedding, decision = managed_pair(tmp_path)
    managed._gpu_memory_probe = lambda uuid: 4095
    async def no_child_start():
        pytest.fail("no native child may start when VRAM is insufficient")
    embedding.start = decision.start = no_child_start

    with pytest.raises(RuntimeError, match="dual-gpu-vram-insufficient"):
        await managed.start()
    assert embedding.stops == decision.stops == 1
    assert (await managed.health()).state is RuntimeHealthState.STOPPED


@pytest.mark.asyncio
async def test_unknown_live_vram_blocks_both_children_before_start(tmp_path):
    managed, embedding, decision = managed_pair(tmp_path)

    def unavailable(_uuid):
        raise RuntimeError("dual-gpu-vram-unknown")

    managed._gpu_memory_probe = unavailable
    async def no_child_start():
        pytest.fail("no child may start without trustworthy telemetry")
    embedding.start = decision.start = no_child_start
    with pytest.raises(RuntimeError, match="dual-gpu-vram-unknown"):
        await managed.start()
    assert embedding.stops == decision.stops == 1


@pytest.mark.asyncio
async def test_released_external_gpu_memory_allows_fresh_start(tmp_path):
    managed, embedding, decision = managed_pair(tmp_path)
    available = [4095, 4096]
    managed._gpu_memory_probe = lambda uuid: available.pop(0)
    with pytest.raises(RuntimeError, match="dual-gpu-vram-insufficient"):
        await managed.start()
    assert not (await managed.health()).ready
    await managed.start()
    assert (await managed.health()).ready
    assert available == []
    await managed.stop()
    await managed.release()


@pytest.mark.asyncio
async def test_competing_start_is_fenced_even_with_capacity_probe(tmp_path):
    managed, embedding, decision = managed_pair(tmp_path)
    probes = []
    managed._gpu_memory_probe = lambda uuid: probes.append(uuid) or 8000
    await managed.start()
    assert probes == ["GPU-dual-example"]
    await managed.stop()
    await managed.release()



@pytest.mark.asyncio
async def test_already_resident_start_is_idempotent_without_rechecking_own_vram(tmp_path):
    managed, embedding, decision = managed_pair(tmp_path)
    available = [4096, 0]
    managed._gpu_memory_probe = lambda _uuid: available.pop(0)

    await managed.start()
    assert (await managed.health()).ready
    # The two resident models may leave less free memory than the original
    # pre-start budget. That is not an external contention signal.
    await managed.start()
    assert (await managed.health()).ready
    assert available == [0], "second start must not re-probe occupied GPU"
    assert embedding.stops == decision.stops == 0
    await managed.stop()
    await managed.release()


@pytest.mark.asyncio
async def test_nonready_owned_children_fail_closed_before_attempting_restart(tmp_path):
    managed, embedding, decision = managed_pair(tmp_path)
    await managed.start()
    decision.health_state = RuntimeHealthState.FAILED
    async def unsafe_restart():
        pytest.fail("unhealthy owned child must not restart implicitly")
    embedding.start = decision.start = unsafe_restart

    with pytest.raises(RuntimeError, match="dual runtime no longer ready"):
        await managed.start()
    assert embedding.stops == decision.stops == 1
    assert not (await managed.health()).ready
