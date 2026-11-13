"""llama.cpp runs only the upstream flavor; the retired Bonsai flavor must stay rejected."""
from __future__ import annotations

from pathlib import Path

import pytest

from astrumweaver import ResourceShape, WorkerSpec
from astrumweaver.runtime import (
    ExecutionDemand,
    GPUTopology,
    ModelDemand,
    ModelTopology,
    ResidencyPolicy,
    RuntimeCompatibilityContext,
    RuntimeHostFacts,
)
from astrumweaver.runtime.providers.llama_cpp import (
    LLAMA_CPP_STANDARD_RUNTIME,
    LlamaCppProvider,
    LlamaCppProviderConfig,
)
from astrumweaver.setup.contracts import DeploymentPath
from astrumweaver.setup.tui import _PROVIDER_OPTIONS, default_runtime_catalog


def _ctx(model_ref: str = "/models/upstream-Q4_K_M.gguf") -> RuntimeCompatibilityContext:
    worker = WorkerSpec(
        worker_id="llama-test",
        worker_class="gpu-single",
        resources=ResourceShape(gpu_count=1, total_vram_mb=11264, max_single_gpu_vram_mb=11264),
        gpu_uuids=("GPU-test-llama",),
        capabilities=frozenset({"llm.chat", "text.generate"}),
    )
    demand = ExecutionDemand(
        model=ModelDemand(model_ref=model_ref, model_format="gguf", topology=ModelTopology.DENSE,
                          estimated_size_mb=6873),
        residency_policy=ResidencyPolicy.VRAM_ONLY,
        gpu_topology=GPUTopology.SINGLE_GPU,
    )
    return RuntimeCompatibilityContext(
        worker=worker, host=RuntimeHostFacts(cpu_count=8, host_ram_mb=16384), demand=demand,
    )


@pytest.mark.parametrize("flavor", ["bonsai2-pq2", "bonsai", "PQ2_0", "", "custom"])
def test_runtime_flavor_accepts_only_upstream(flavor) -> None:
    with pytest.raises(ValueError, match="runtime_flavor"):
        LlamaCppProviderConfig(runtime_flavor=flavor)
    assert LlamaCppProviderConfig(runtime_flavor="upstream").runtime_flavor == "upstream"


def test_systemd_catalog_uses_the_standard_runtime() -> None:
    provider = default_runtime_catalog(deployment_path=DeploymentPath.SYSTEMD).get("llama-cpp")
    assert provider is not None
    assert provider.config.runtime_flavor == "upstream"
    assert provider.config.executable == LLAMA_CPP_STANDARD_RUNTIME


def test_tui_no_longer_offers_a_runtime_flavor() -> None:
    assert "runtime_flavor" not in {spec.field_name for spec in _PROVIDER_OPTIONS["llama-cpp"]}


def test_default_upstream_reviewed_intent_remains_schema_stable() -> None:
    intent = LlamaCppProvider().setup_intent(_ctx())
    assert "runtime_flavor" not in intent.configuration
    assert intent.package_references == ("llama-cpp",)
    assert intent.configuration["executable"] == "llama-server"


def test_systemd_driver_maps_the_provider_to_managed_nix_profile_manager(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from astrumweaver.setup.systemd import create_systemd_driver
    manager = tmp_path / "runtime-manager"
    manager.write_text("#!/bin/sh\\nexit 0\\n")
    manager.chmod(0o755)
    monkeypatch.setenv("ASTRUMWEAVER_RUNTIME_PROFILE_MANAGER", str(manager))
    monkeypatch.setenv("ASTRUMWEAVER_SETUP_ROOT", str(tmp_path / "root"))
    driver = create_systemd_driver()
    assert driver.installers["llama-cpp"] == (str(manager), "ensure")
    assert driver.verifiers["llama-cpp"] == (str(manager), "verify")
