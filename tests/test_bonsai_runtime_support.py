"""Opt-in, pinned Bonsai 2 PQ2_0 support. Stock llama.cpp must not regress."""
from __future__ import annotations

from dataclasses import replace
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
    LLAMA_CPP_BONSAI_MODEL_NAME,
    LLAMA_CPP_BONSAI_PACKAGE,
    LLAMA_CPP_BONSAI_RUNTIME,
    LLAMA_CPP_STANDARD_RUNTIME,
    LlamaCppProvider,
    LlamaCppProviderConfig,
)
from astrumweaver.setup.tui import default_runtime_catalog
from astrumweaver.setup.contracts import DeploymentPath


MODEL = "/models/" + LLAMA_CPP_BONSAI_MODEL_NAME


def _ctx(
    *,
    model_ref: str = MODEL,
    model_format: str = "gguf",
    topology: ModelTopology = ModelTopology.DENSE,
    estimated_size_mb: int | None = 6873,
    vram_mb: int = 11264,
) -> RuntimeCompatibilityContext:
    worker = WorkerSpec(
        worker_id="bonsai-test",
        worker_class="gpu-single",
        resources=ResourceShape(
            gpu_count=1,
            total_vram_mb=vram_mb,
            max_single_gpu_vram_mb=vram_mb,
        ),
        gpu_uuids=("GPU-test-bonsai",),
        capabilities=frozenset({"llm.chat", "text.generate"}),
    )
    demand = ExecutionDemand(
        model=ModelDemand(
            model_ref=model_ref,
            model_format=model_format,
            topology=topology,
            estimated_size_mb=estimated_size_mb,
        ),
        residency_policy=ResidencyPolicy.VRAM_ONLY,
        gpu_topology=GPUTopology.SINGLE_GPU,
    )
    return RuntimeCompatibilityContext(
        worker=worker,
        host=RuntimeHostFacts(cpu_count=8, host_ram_mb=16384),
        demand=demand,
    )


def test_standard_llama_cpp_remains_default_and_rejects_bonsai_pq2() -> None:
    default = LlamaCppProvider()
    assert default.config.runtime_flavor == "upstream"
    assert default.config.package_reference == "llama-cpp"
    assert default.config.executable == "llama-server"
    report = default.compatibility(_ctx())
    assert not report.compatible
    assert "bonsai-specialized-runtime-required" in {
        r.code for r in report.reasons
    }


def test_bonsai_is_explicit_and_uses_a_separate_managed_nix_profile() -> None:
    selected = LlamaCppProvider(
        LlamaCppProviderConfig(runtime_flavor="bonsai2-pq2")
    )
    assert selected.config.executable == LLAMA_CPP_BONSAI_RUNTIME
    assert selected.config.package_reference == LLAMA_CPP_BONSAI_PACKAGE
    assert selected.config.jinja is True
    report = selected.compatibility(_ctx())
    assert report.compatible, report.reasons

    setup = selected.setup_intent(_ctx())
    assert setup.provider_id == "llama-cpp"
    assert setup.package_references == (LLAMA_CPP_BONSAI_PACKAGE,)
    assert setup.configuration["runtime_flavor"] == "bonsai2-pq2"
    assert setup.configuration["model_ref"] == MODEL
    assert setup.configuration["executable"] == LLAMA_CPP_BONSAI_RUNTIME
    assert setup.configuration["jinja"] is True
    assert setup.configuration["fit"] is False
    assert setup.configuration["gpu_layers"] == "all"

    runtime = selected.create_runtime(_ctx(), setup)
    assert runtime.process.command()[0] == LLAMA_CPP_BONSAI_RUNTIME
    command = runtime.process.command()
    assert "--jinja" in command
    assert "--offline" in command
    assert "--device" in command
    assert command[command.index("--device") + 1] == "CUDA0"


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"model_ref": "/models/other.gguf"}, "bonsai-model-not-qualified"),
        ({"model_ref": "/models/Ternary-Bonsai-2-27B-PTQ1_0.gguf"}, "bonsai-model-not-qualified"),
        ({"model_format": "safetensors"}, "model-format-unsupported"),
        ({"topology": ModelTopology.MOE}, "bonsai-topology-mismatch"),
        ({"estimated_size_mb": 6000}, "bonsai-model-size-understated"),
        ({"vram_mb": 4096}, "model-exceeds-single-gpu-vram"),
    ],
)
def test_rejects_unqualified_models_and_unsafe_estimates(kwargs, reason) -> None:
    provider = LlamaCppProvider(
        LlamaCppProviderConfig(runtime_flavor="bonsai2-pq2")
    )
    report = provider.compatibility(_ctx(**kwargs))
    assert not report.compatible
    assert reason in {r.code for r in report.reasons}


@pytest.mark.parametrize("flavor", ["bonsai", "PQ2_0", "", "custom"])
def test_runtime_variant_rejects_unknown_values(flavor) -> None:
    with pytest.raises(ValueError, match="runtime_flavor"):
        LlamaCppProviderConfig(runtime_flavor=flavor)


def test_bonsai_cannot_override_a_pinned_runtime_path_or_package() -> None:
    with pytest.raises(ValueError, match="pinned Nix runtime executable"):
        LlamaCppProviderConfig(
            runtime_flavor="bonsai2-pq2",
            executable="/tmp/ambient-llama-server",
        )
    with pytest.raises(ValueError, match="pinned Nix runtime package"):
        LlamaCppProviderConfig(
            runtime_flavor="bonsai2-pq2",
            package_reference="another-package",
        )
    with pytest.raises(ValueError, match="Chat/Tool Call only"):
        LlamaCppProviderConfig(runtime_flavor="bonsai2-pq2", decision=True)


def test_reviewed_intent_rejects_runtime_substitution() -> None:
    provider = LlamaCppProvider(
        LlamaCppProviderConfig(runtime_flavor="bonsai2-pq2")
    )
    setup = provider.setup_intent(_ctx())
    cfg = dict(setup.configuration)

    with pytest.raises(ValueError, match="runtime flavor"):
        provider.create_runtime(
            _ctx(), replace(setup, configuration={**cfg, "runtime_flavor": "upstream"})
        )
    with pytest.raises(ValueError, match="Bonsai executable"):
        provider.create_runtime(
            _ctx(), replace(setup, configuration={**cfg, "executable": LLAMA_CPP_STANDARD_RUNTIME})
        )
    with pytest.raises(ValueError, match="Bonsai model"):
        provider.create_runtime(
            _ctx(), replace(setup, configuration={**cfg, "model_ref": "/models/not-bonsai.gguf"})
        )


def test_systemd_catalog_preserves_upstream_and_can_select_bonsai() -> None:
    provider = default_runtime_catalog(deployment_path=DeploymentPath.SYSTEMD).get("llama-cpp")
    assert provider is not None
    assert provider.config.runtime_flavor == "upstream"
    assert provider.config.executable == LLAMA_CPP_STANDARD_RUNTIME
    selected = type(provider)(replace(provider.config, runtime_flavor="bonsai2-pq2"))
    assert selected.config.executable == LLAMA_CPP_BONSAI_RUNTIME
    assert selected.setup_intent(_ctx()).package_references == (LLAMA_CPP_BONSAI_PACKAGE,)


def test_pins_are_source_controlled_and_profile_does_not_replace_default() -> None:
    root = Path(__file__).resolve().parents[1]
    core = (root / "nix/bonsai-llama-cpp.nix").read_text()
    manager = (root / "nix/runtime-profile-support.nix").read_text()
    bridge = (root / "nix/bonsai-llama-driver-bridge.nix").read_text()
    flake = (root / "flake.nix").read_text()

    assert "prism-b10772-e8fd209" in core
    assert "sha256-9aRsTtu8dTPiysls6RkSBMegNlXq9015Y5Rp9QyNmYw=" in core
    assert "runtime-bonsai-llama-cpp = runtimeBonsai;" in flake
    assert 'runtime_attr="runtime-bonsai-llama-cpp"' in manager
    assert "astrumweaver-runtime-bonsai" in manager
    assert "astrumweaver-runtime-llama-cpp" in manager
    assert "env -i" in bridge
    assert "cuda_driver_library_unavailable" in bridge
    assert "cuda_stub_is_not_a_driver" in bridge


def test_bonsai_setup_plan_uses_separate_package_reference() -> None:
    from astrumweaver.runtime import (
        RuntimeCatalog, RuntimeSelection, RuntimeSelectionMode,
    )
    from astrumweaver.setup import (
        PrivilegeMode, SetupActionKind, SetupHostSnapshot, build_runtime_setup_plan,
    )
    selected = LlamaCppProvider(
        LlamaCppProviderConfig(runtime_flavor="bonsai2-pq2")
    )
    ctx = _ctx()
    snapshot = SetupHostSnapshot(
        runtime_host=ctx.host,
        deployment_path=DeploymentPath.SYSTEMD,
        os_id="ubuntu",
        os_version="24.04",
        service_manager="systemd",
        package_manager="nix",
        available_commands=frozenset({"nix", "systemctl", "nvidia-smi"}),
        privilege_mode=PrivilegeMode.ROOT,
    )
    plan = build_runtime_setup_plan(
        catalog=RuntimeCatalog([selected]),
        context=ctx,
        selection=RuntimeSelection(
            mode=RuntimeSelectionMode.EXPLICIT, provider_id="llama-cpp"
        ),
        snapshot=snapshot,
    )
    references = [
        action.payload["package_reference"]
        for action in plan.actions
        if action.kind is SetupActionKind.ENSURE_PACKAGE
    ]
    assert references == ["bonsai-llama-cpp"]
    assert all(
        action.kind is not SetupActionKind.DOWNLOAD_MODEL
        for action in plan.actions
    )


def test_tui_can_opt_in_without_switching_normal_catalog() -> None:
    from astrumweaver.setup.tui import _PROVIDER_OPTIONS, configure_provider

    class ScriptedInput:
        def __init__(self):
            self.prompts: list[str] = []

        def ask(self, prompt: str) -> str:
            self.prompts.append(prompt)
            if "Customize runtime" in prompt:
                return "y"
            if "runtime (upstream/bonsai2-pq2)" in prompt:
                return "bonsai2-pq2"
            return ""

        def write(self, text: str = "") -> None:
            pass

    provider = default_runtime_catalog(deployment_path=DeploymentPath.SYSTEMD).get("llama-cpp")
    assert provider is not None
    ui = ScriptedInput()
    configured = configure_provider(ui, provider)
    assert configured.config.runtime_flavor == "bonsai2-pq2"
    assert configured.config.executable == LLAMA_CPP_BONSAI_RUNTIME
    assert configured.config.package_reference == LLAMA_CPP_BONSAI_PACKAGE
    assert configured.config.jinja is True
    assert len(ui.prompts) == len(_PROVIDER_OPTIONS["llama-cpp"]) + 1
    assert provider.config.runtime_flavor == "upstream"


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


def test_bonsai_model_bytes_must_match_exact_size_and_sha256(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hashlib
    import astrumweaver.runtime.providers.llama_cpp as native

    local_model = tmp_path / native.LLAMA_CPP_BONSAI_MODEL_NAME
    local_model.write_bytes(b"incorrect")
    with pytest.raises(RuntimeError, match="model size"):
        native._verify_bonsai_model_bytes(str(local_model))

    expected = b"CORRECT_PINNED"
    monkeypatch.setattr(native, "LLAMA_CPP_BONSAI_MODEL_SIZE_BYTES", len(expected))
    monkeypatch.setattr(
        native, "LLAMA_CPP_BONSAI_MODEL_SHA256", hashlib.sha256(expected).hexdigest()
    )

    local_model.write_bytes(b"incorrect_data")
    with pytest.raises(RuntimeError, match="SHA-256"):
        native._verify_bonsai_model_bytes(str(local_model))

    local_model.write_bytes(expected)
    native._verify_bonsai_model_bytes(str(local_model))
    with pytest.raises(RuntimeError, match="cannot be read"):
        native._verify_bonsai_model_bytes(str(tmp_path / "missing.gguf"))


def test_bonsai_requires_vram_only_and_a_single_gpu() -> None:
    provider = LlamaCppProvider(LlamaCppProviderConfig(runtime_flavor="bonsai2-pq2"))
    from dataclasses import replace

    base = _ctx()
    different = replace(
        base,
        demand=replace(base.demand, residency_policy=ResidencyPolicy.PREFER_VRAM),
    )
    report = provider.compatibility(different)
    assert "bonsai-vram-only" in {reason.code for reason in report.reasons}
    assert not report.compatible

    another_gpu = replace(
        base,
        worker=replace(
            base.worker,
            resources=ResourceShape(
                gpu_count=2,
                total_vram_mb=22528,
                max_single_gpu_vram_mb=11264,
            ),
            gpu_uuids=("GPU-test-one", "GPU-test-two"),
        ),
        demand=replace(
            base.demand,
            gpu_topology=GPUTopology.MULTI_GPU,
            min_gpu_count=2,
        ),
    )
    report = provider.compatibility(another_gpu)
    assert "bonsai-single-gpu-only" in {reason.code for reason in report.reasons}


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("model_ref", "/other/Ternary-Bonsai-2-27B-PQ2_0.gguf", "model path"),
        ("gpu_layers", 0, "full-VRAM"),
        ("fit", True, "full-VRAM"),
        ("jinja", False, "Jinja"),
        ("offline", False, "offline"),
        ("no_webui", False, "offline"),
        ("context_size", 4096, "at least 8192"),
    ],
)
def test_reviewed_bonsai_setup_rejects_tampered_runtime_settings(
    field: str, value: object, expected: str,
) -> None:
    provider = LlamaCppProvider(
        LlamaCppProviderConfig(runtime_flavor="bonsai2-pq2")
    )
    ctx = _ctx()
    setup = provider.setup_intent(ctx)
    data = dict(setup.configuration)
    data[field] = value
    with pytest.raises(ValueError, match=expected):
        provider.create_runtime(ctx, replace(setup, configuration=data))


def test_bonsai_flavor_sets_coding_context_minimum() -> None:
    assert LlamaCppProviderConfig(runtime_flavor="bonsai2-pq2").context_size == 8192
    assert LlamaCppProviderConfig(
        runtime_flavor="bonsai2-pq2", context_size=32768
    ).context_size == 32768
    assert LlamaCppProviderConfig().context_size == 4096


def test_bonsai_pins_verified_native_tool_and_memory_flags() -> None:
    provider = LlamaCppProvider(
        LlamaCppProviderConfig(runtime_flavor="bonsai2-pq2")
    )
    ctx = _ctx()
    command = provider.create_runtime(
        ctx, provider.setup_intent(ctx)
    ).process.command()
    assert command[command.index("--reasoning") + 1] == "off"
    assert command[command.index("--flash-attn") + 1] == "on"
    assert command[command.index("--ctx-size") + 1] == "8192"
    assert "--jinja" in command

    unqualified = _ctx(model_ref="/models/upstream-qwen.gguf")
    upstream = LlamaCppProvider()
    args = upstream.create_runtime(
        unqualified, upstream.setup_intent(unqualified)
    ).process.command()
    assert "--reasoning" not in args
    assert "--flash-attn" not in args


def test_default_upstream_reviewed_intent_remains_schema_stable() -> None:
    upstream = LlamaCppProvider()
    intent = upstream.setup_intent(_ctx(model_ref="/models/upstream-Q4_K_M.gguf"))
    assert "runtime_flavor" not in intent.configuration
    assert intent.package_references == ("llama-cpp",)
    assert intent.configuration["executable"] == "llama-server"


def test_upstream_flavor_rejects_bonsai_pinned_executable_or_package() -> None:
    with pytest.raises(ValueError, match="upstream llama.cpp cannot use"):
        LlamaCppProviderConfig(
            runtime_flavor="upstream", executable=LLAMA_CPP_BONSAI_RUNTIME
        )
    with pytest.raises(ValueError, match="upstream llama.cpp cannot use"):
        LlamaCppProviderConfig(
            runtime_flavor="upstream", package_reference=LLAMA_CPP_BONSAI_PACKAGE
        )


def test_tampered_bonsai_setup_package_is_rejected() -> None:
    provider = LlamaCppProvider(LlamaCppProviderConfig(runtime_flavor="bonsai2-pq2"))
    ctx = _ctx()
    setup = provider.setup_intent(ctx)
    with pytest.raises(ValueError, match="only the pinned Nix package"):
        provider.create_runtime(
            ctx,
            replace(setup, package_references=("llama-cpp",)),
        )


@pytest.mark.asyncio
async def test_managed_bonsai_hash_failure_prevents_native_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock
    import astrumweaver.runtime.providers.llama_cpp as native

    provider = LlamaCppProvider(LlamaCppProviderConfig(runtime_flavor="bonsai2-pq2"))
    ctx = _ctx()
    runtime = provider.create_runtime(ctx, provider.setup_intent(ctx))
    monkeypatch.setattr(runtime.api, "health", AsyncMock(return_value=False))
    called = AsyncMock()
    monkeypatch.setattr(runtime.process, "start", called)

    def reject(_: str) -> None:
        raise RuntimeError("Bonsai 2 model SHA-256 does not match pinned artifact")

    monkeypatch.setattr(native, "_verify_bonsai_model_bytes", reject)
    with pytest.raises(RuntimeError, match="SHA-256"):
        await runtime.start()
    called.assert_not_awaited()


def test_bonsai_disk_size_floor_is_ceil_of_verified_bytes() -> None:
    import math
    import astrumweaver.runtime.providers.llama_cpp as native
    assert math.ceil(native.LLAMA_CPP_BONSAI_MODEL_SIZE_BYTES / (1024 * 1024)) == 6873
    assert LlamaCppProvider(
        LlamaCppProviderConfig(runtime_flavor="bonsai2-pq2")
    ).compatibility(_ctx(estimated_size_mb=6873)).compatible


def test_cuda_driver_wrapper_requires_trusted_root_owned_library() -> None:
    root=Path(__file__).resolve().parents[1]
    bridge=(root/"nix/bonsai-llama-driver-bridge.nix").read_text()
    assert "cuda_driver_library_is_not_root_owned_read_only" in bridge
    assert 'stat -c %u' in bridge and 'stat -c %a' in bridge

def test_bonsai_nix_bridge_passes_pinned_nvidia_ptx_jit_and_nvvm_libraries() -> None:
    """Actual Pascal native model loading needs companions beyond libcuda."""
    bridge = (Path(__file__).resolve().parents[1] / "nix/bonsai-llama-driver-bridge.nix").read_text()
    assert "libnvidia-ptxjitcompiler.so.1" in bridge
    assert "libnvidia-nvvm.so.4" in bridge
    assert "required_nvidia_driver_companion_unavailable" in bridge
    assert "nvidia_driver_companion_not_root_owned_read_only" in bridge
    assert '"$private/libnvidia-ptxjitcompiler.so.1"' in bridge
    assert '"$private/libnvidia-nvvm.so.4"' in bridge
    assert "nvidia_driver_version_mismatch" in bridge
    assert "ptx_companion" in bridge and "nvvm_companion" in bridge
