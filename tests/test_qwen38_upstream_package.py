from pathlib import Path

def test_qwen38_upstream_runtime_is_separately_pinned() -> None:
    root = Path(__file__).resolve().parents[1]
    flake = (root/"flake.nix").read_text()
    core = (root/"nix/qwen38-llama-cpp.nix").read_text()
    bridge = (root/"nix/qwen38-llama-driver-bridge.nix").read_text()
    manager = (root/"nix/runtime-profile-support.nix").read_text()
    assert "runtime-qwen38-llama-cpp = runtimeQwen38;" in flake
    assert "runtime-llama-cpp = runtimeLlamaCpp;" in flake
    assert "b11430" in core
    assert "8345f333951c661d166b00e6f9362e553768f292" in core
    assert "sha256-j3n3UJPo+22cZeWRZ1EelUd3Z5QZEHPkiM6B79Wt5yM=" in core
    assert "ggml-org/llama.cpp" in core
    assert "qwen38-llama-cpp" in manager
    assert "runtime-qwen38-llama-cpp" in manager
    assert "astrumweaver-runtime-qwen38" in manager
    assert "astrumweaver-runtime-llama-cpp" in manager
    assert "env -i" in bridge
    assert '--reasoning off --flash-attn on &' in bridge
    assert "cuda_stub_is_not_a_driver" in bridge
    assert "cuda_driver_library_unavailable" in bridge
    assert "PrismML" not in core


def test_production_doc_includes_model_sha_and_rollback_gate() -> None:
    doc = (Path(__file__).resolve().parents[1]/"docs/qwen38-upstream-llama-cpp.md").read_text()
    assert "322e194ff79741c7baa497c240f677f54b201b0efab44ca8e50f122b39123482" in doc
    assert "Control" in doc and "rollback" in doc


def test_qwen38_and_bonsai_profiles_coexist_after_main_integration() -> None:
    """All opt-in runtimes remain independent of the legacy default profile."""
    root = Path(__file__).resolve().parents[1]
    flake = (root / "flake.nix").read_text()
    manager = (root / "nix/runtime-profile-support.nix").read_text()
    package_paths = {
        "llama-cpp": (
            "runtime-llama-cpp",
            "astrumweaver-runtime-llama-cpp",
        ),
        "qwen38-llama-cpp": (
            "runtime-qwen38-llama-cpp",
            "astrumweaver-runtime-qwen38",
        ),
        "bonsai-llama-cpp": (
            "runtime-bonsai-llama-cpp",
            "astrumweaver-runtime-bonsai",
        ),
    }
    # This source contract is supplemented by the real Nix flake evaluation,
    # Installer build and CLI status/invalid-package smoke in PR acceptance.
    for package, (attr, profile) in package_paths.items():
        assert f"{attr} =" in flake
        assert f'/nix/var/nix/profiles/{profile}' in manager
        assert f"{package})" in manager or f"{package}|" in manager
        assert f'runtime_attr="{attr}"' in manager
        for action in ("verify package", "ensure", "upgrade", "rollback", "status"):
            assert f"{action} {package}" in manager
    assert "runtime-qwen38-llama-cpp = runtimeQwen38;" in flake
    assert "runtime-bonsai-llama-cpp = runtimeBonsai;" in flake
    # The shared selector must retain separate case arms and the fail-closed
    # default for an unsupported runtime, not silently substitute a profile.
    selector = manager.split("require_llama_cpp() {", 1)[1].split(
        "managed_llama_cpp_profile() {", 1
    )[0]
    for package in package_paths:
        assert f"{package})" in selector or f"{package}|" in selector
    assert "unsupported runtime package" in selector
    assert "exit 2" in selector


def test_qwen38_bridge_requires_trusted_matched_host_driver_companions() -> None:
    root = Path(__file__).resolve().parents[1]
    bridge = (root / "nix/qwen38-llama-driver-bridge.nix").read_text()
    assert "cuda_driver_library_is_not_root_owned_read_only" in bridge
    assert "nvidia_driver_companion_not_root_owned_read_only" in bridge
    assert "nvidia_driver_version_mismatch" in bridge
    assert "libnvidia-ptxjitcompiler.so.1" in bridge
    assert "libnvidia-nvvm.so.4" in bridge
    assert "env -i" in bridge
    assert '"$@" --reasoning off --flash-attn on &' in bridge


def test_qwen38_archive_hash_is_consistent_between_nix_and_docs() -> None:
    import base64
    import re

    root = Path(__file__).resolve().parents[1]
    core = (root / "nix/qwen38-llama-cpp.nix").read_text()
    docs = (root / "docs/qwen38-upstream-llama-cpp.md").read_text()
    digest = "8f79f75093e8fb6d9c65e59167511e9547776794191073e488ce81efd5ade723"
    match = re.search(r'hash = "sha256-([^"]+)"', core)
    assert match is not None
    assert base64.b64decode(match.group(1)).hex() == digest
    assert digest in core and digest in docs
    assert "8345f333951c661d166b00e6f9362e553768f292" in docs


def test_profile_manager_refuses_unmanaged_rollback() -> None:
    text = (Path(__file__).resolve().parents[1] / "nix/runtime-profile-support.nix").read_text()
    section = text.split("      rollback)\n", 1)[1].split("      status)\n", 1)[0]
    assert "managed_llama_cpp_profile" in section
    assert "refusing to rollback unmanaged or mixed runtime profile" in section
    assert section.index("managed_llama_cpp_profile") < section.index("profile rollback")


def _qwen38_ctx():
    from astrumweaver import ResourceShape, WorkerSpec
    from astrumweaver.runtime import (
        ExecutionDemand, GPUTopology, ModelDemand, ModelTopology,
        ResidencyPolicy, RuntimeCompatibilityContext, RuntimeHostFacts,
    )
    worker = WorkerSpec(
        worker_id="qwen38-test",
        worker_class="gpu-dual",
        resources=ResourceShape(
            gpu_count=2, total_vram_mb=24576, max_single_gpu_vram_mb=12288,
        ),
        gpu_uuids=("GPU-qwen38-1", "GPU-qwen38-2"),
        capabilities=frozenset({"llm.chat", "text.generate"}),
    )
    demand = ExecutionDemand(
        model=ModelDemand(
            model_ref="/models/Qwen3.8-27B-UD-Q4_K_M.gguf",
            model_format="gguf",
            topology=ModelTopology.DENSE,
            estimated_size_mb=15702,
        ),
        residency_policy=ResidencyPolicy.VRAM_ONLY,
        gpu_topology=GPUTopology.MULTI_GPU,
        min_gpu_count=2,
    )
    return RuntimeCompatibilityContext(
        worker=worker, host=RuntimeHostFacts(cpu_count=8, host_ram_mb=32768),
        demand=demand,
    )


def test_qwen38_matches_explicit_installer_and_existing_managed_site_references() -> None:
    from astrumweaver.runtime.providers.llama_cpp import (
        LlamaCppProvider, LlamaCppProviderConfig, LLAMA_CPP_QWEN38_RUNTIME,
        LLAMA_CPP_QWEN38_PACKAGE, LLAMA_CPP_QWEN38_PROFILE,
    )
    for package_reference in (LLAMA_CPP_QWEN38_PACKAGE, LLAMA_CPP_QWEN38_PROFILE):
        cfg = LlamaCppProviderConfig(
            executable=LLAMA_CPP_QWEN38_RUNTIME,
            package_reference=package_reference,
            context_size=8192, jinja=True, gpu_layers="all", fit=False,
        )
        provider = LlamaCppProvider(cfg)
        ctx = _qwen38_ctx()
        setup = provider.setup_intent(ctx)
        assert setup.package_references == (package_reference,)
        assert setup.configuration["executable"] == LLAMA_CPP_QWEN38_RUNTIME
        runtime = provider.create_runtime(ctx, setup)
        assert runtime.process.command()[0] == LLAMA_CPP_QWEN38_RUNTIME


def test_qwen38_rejects_mismatched_install_intent_and_setup_tampering() -> None:
    from dataclasses import replace
    import pytest
    from astrumweaver.runtime.providers.llama_cpp import (
        LlamaCppProvider, LlamaCppProviderConfig, LLAMA_CPP_QWEN38_RUNTIME,
        LLAMA_CPP_QWEN38_PACKAGE, LLAMA_CPP_QWEN38_PROFILE,
    )
    invalid_pairs = [
        (LLAMA_CPP_QWEN38_RUNTIME, "llama-cpp"),
        ("llama-server", LLAMA_CPP_QWEN38_PACKAGE),
        ("llama-server", LLAMA_CPP_QWEN38_PROFILE),
    ]
    for executable, package_reference in invalid_pairs:
        with pytest.raises(ValueError, match="Qwen3.8 pinned runtime"):
            LlamaCppProviderConfig(
                executable=executable, package_reference=package_reference,
            )
    cfg = LlamaCppProviderConfig(
        executable=LLAMA_CPP_QWEN38_RUNTIME,
        package_reference=LLAMA_CPP_QWEN38_PACKAGE,
        context_size=8192, jinja=True, gpu_layers="all", fit=False,
    )
    provider = LlamaCppProvider(cfg)
    ctx = _qwen38_ctx()
    setup = provider.setup_intent(ctx)
    with pytest.raises(ValueError, match="Qwen3.8 setup must retain"):
        provider.create_runtime(ctx, replace(setup, package_references=("llama-cpp",)))
    with pytest.raises(ValueError, match="Qwen3.8 setup executable differs"):
        provider.create_runtime(
            ctx, replace(
                setup, configuration={**setup.configuration, "executable": "llama-server"},
            ),
        )



def test_absolute_qwen38_managed_profile_is_a_supported_manager_selector() -> None:
    manager = (Path(__file__).resolve().parents[1] / "nix/runtime-profile-support.nix").read_text()
    assert "qwen38-llama-cpp|/nix/var/nix/profiles/astrumweaver-runtime-qwen38)" in manager


def test_reviewed_generic_upstream_setup_cannot_inject_pinned_qwen38_executable() -> None:
    from dataclasses import replace
    import pytest
    from astrumweaver.runtime.providers.llama_cpp import (
        LlamaCppProvider, LlamaCppProviderConfig, LLAMA_CPP_QWEN38_RUNTIME,
    )
    context = _qwen38_ctx()
    provider = LlamaCppProvider(LlamaCppProviderConfig())
    intent = provider.setup_intent(context)
    with pytest.raises(ValueError, match="Qwen3.8 setup executable must be selected"):
        provider.create_runtime(
            context,
            replace(intent, configuration={
                **intent.configuration,
                "executable": LLAMA_CPP_QWEN38_RUNTIME,
            }),
        )
