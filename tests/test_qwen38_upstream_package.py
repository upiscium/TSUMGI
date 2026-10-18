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
    assert "cuda_stub_is_not_a_driver" in bridge
    assert "cuda_driver_library_unavailable" in bridge
    assert "PrismML" not in core


def test_production_doc_includes_model_sha_and_rollback_gate() -> None:
    doc = (Path(__file__).resolve().parents[1]/"docs/qwen38-upstream-llama-cpp.md").read_text()
    assert "322e194ff79741c7baa497c240f677f54b201b0efab44ca8e50f122b39123482" in doc
    assert "Control" in doc and "rollback" in doc
