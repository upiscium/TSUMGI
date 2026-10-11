"""Exercise fail-closed composite gates through the actual Worker entrypoint."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from astrumweaver.worker import daemon


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("max_concurrency", "serving_manifest", "error"),
    [
        (1, False, "reviewed serving manifest"),
        (0, True, "max_concurrency=1"),
        (2, True, "max_concurrency=1"),
    ],
    ids=["missing-serving", "zero-concurrency", "excess-concurrency"],
)
async def test_dual_daemon_rejects_unsafe_setup_before_runtime_creation(
    monkeypatch: pytest.MonkeyPatch,
    max_concurrency: int,
    serving_manifest: bool,
    error: str,
) -> None:
    """Reject the requested dual deployment before model loading or Control I/O."""
    config: dict[str, dict[str, object]] = {
        "worker": {
            "id": "dual-gate-test",
            "class": "cpu-test",
            "control_url": "http://unused-control",
            "capabilities": ["text.embed", "decision.system_one"],
            "max_concurrency": max_concurrency,
        },
        "runtime": {"manifest": "ignored-runtime.json"},
    }
    if serving_manifest:
        config["serving"] = {"manifest": "ignored-serving.json"}

    events: list[str] = []

    class FakeControlClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            events.append("client-created")

        async def aclose(self) -> None:
            events.append("client-closed")

    def unexpected_runtime(*_args: object, **_kwargs: object) -> None:
        pytest.fail("dual model runtime must not be created for invalid startup")

    monkeypatch.setenv("ASTRUMWEAVER_WORKER_TOKEN", "synthetic-test-token")
    monkeypatch.setattr(daemon, "_load_toml", lambda _path: config)
    monkeypatch.setattr(daemon, "ControlClient", FakeControlClient)
    monkeypatch.setattr(
        daemon,
        "_load_runtime_deployment",
        lambda _path: SimpleNamespace(provider_id="llama-cpp-dual"),
    )
    monkeypatch.setattr(
        daemon,
        "_load_serving_deployment",
        lambda _path: SimpleNamespace(),
    )
    monkeypatch.setattr(daemon, "discover_runtime_host_facts", unexpected_runtime)
    monkeypatch.setattr(daemon, "managed_runtime_from_deployment", unexpected_runtime)

    with pytest.raises(RuntimeError, match=error):
        await daemon.run_worker("ignored-worker.toml")

    assert events == ["client-created", "client-closed"]
