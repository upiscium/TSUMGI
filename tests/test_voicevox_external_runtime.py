"""#247 R2: synthetic-only tests for a NON-OWNING external VOICEVOX runtime."""

from __future__ import annotations

import asyncio
import os

import pytest

from astrumweaver.execution import ResidencyReport
from astrumweaver.runtime.contracts import (
    ManagedRuntime,
    RuntimeHealthState,
)
from astrumweaver.runtime.providers.voicevox_discovery import (
    VoicevoxDiscoveryConfig,
    VoicevoxDiscoveryResult,
    VoicevoxDiscoveryStatus,
    VoicevoxObservedCatalog,
    VoicevoxObservedStyle,
)
from astrumweaver.runtime.providers.voicevox_external import (
    ExternalVoicevoxManagedRuntime,
    ExternalVoicevoxRuntimeError,
)


def _observation(digest: str = "a", *, version: str = "0.15.0"):
    return VoicevoxDiscoveryResult(
        status=VoicevoxDiscoveryStatus.COMPATIBLE,
        reason="observed_compatible",
        snapshot=VoicevoxObservedCatalog(
            version=version,
            styles=(
                VoicevoxObservedStyle(
                    speaker_uuid="7ffcb7ce-00ec-4bdc-82cd-45a8889e43ff",
                    style_id=8,
                ),
            ),
            observed_digest="sha256:" + digest * 64,
        ),
    )


def _absent():
    return VoicevoxDiscoveryResult(
        status=VoicevoxDiscoveryStatus.ABSENT,
        reason="endpoint_unreachable",
    )


def _incompatible():
    return VoicevoxDiscoveryResult(
        status=VoicevoxDiscoveryStatus.INCOMPATIBLE,
        reason="incompatible_response",
    )


class ScriptedProbe:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = 0

    async def probe_once(self):
        self.calls += 1
        assert self.results, "unexpected probe call"
        next_result = self.results.pop(0)
        if isinstance(next_result, BaseException):
            raise next_result
        return next_result


class BlockingProbe:
    def __init__(self, result=None, *, suppress_cancellation=False):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0
        self.cancelled = 0
        self.result = result or _observation()
        self.suppress_cancellation = suppress_cancellation

    async def probe_once(self):
        self.calls += 1
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            if not self.suppress_cancellation:
                raise
            await self.release.wait()
        return self.result


def _runtime(probe, *, enabled=True):
    runtime = ExternalVoicevoxManagedRuntime(
        config=VoicevoxDiscoveryConfig(
            enabled=enabled, endpoint="http://127.0.0.1:50021"
        ),
    )
    # Internal test seam ONLY. Public construction always uses the reviewed
    # cancellation-propagating R1 discovery probe, not arbitrary user code.
    runtime._probe = probe
    return runtime


def _check_denied(runtime):
    with pytest.raises(
        ExternalVoicevoxRuntimeError, match="^voicevox_external_not_admitted$"
    ) as caught:
        runtime.executor()
    assert str(caught.value) == "voicevox_external_not_admitted"


@pytest.mark.asyncio
async def test_disabled_config_inspects_nothing_even_when_endpoint_invalid():
    runtime = ExternalVoicevoxManagedRuntime(
        config=VoicevoxDiscoveryConfig(enabled=False, endpoint="invalid://URL"),
    )
    runtime._probe = ScriptedProbe()  # private synthetic-only override
    assert isinstance(runtime, ManagedRuntime)
    await runtime.start()
    health = await runtime.health()
    assert health.state is RuntimeHealthState.STOPPED
    assert not health.ready
    assert runtime.observation is None
    _check_denied(runtime)
    await runtime.stop()
    await runtime.release()


@pytest.mark.asyncio
async def test_compatible_external_engine_is_only_observed_not_authorized():
    probe = ScriptedProbe(_observation(), _observation())
    runtime = _runtime(probe)
    assert isinstance(runtime, ManagedRuntime)
    assert runtime.provider_id == "voicevox"
    _check_denied(runtime)
    assert runtime.observation is None
    assert (await runtime.health()).state is RuntimeHealthState.STOPPED
    assert probe.calls == 0

    await runtime.start()
    assert probe.calls == 1
    assert runtime.observation == _observation()
    result = await runtime.health()
    assert probe.calls == 2
    assert result.state is RuntimeHealthState.DEGRADED
    assert result.ready is False
    assert result.detail == "voicevox_external_observed_unqualified"
    assert result.metadata.get("external_engine_verified") is False
    assert result.metadata.get("admitted") is False
    _check_denied(runtime)
    await runtime.start()  # idempotent, no extra probing
    assert probe.calls == 2


@pytest.mark.asyncio
async def test_no_engine_boot_observation_later_arrival_and_disappearance():
    probe = ScriptedProbe(_absent(), _observation(), _absent(), _observation("b"))
    runtime = _runtime(probe)
    await runtime.start()
    assert runtime.observation is not None
    assert runtime.observation.status is VoicevoxDiscoveryStatus.ABSENT
    assert (await runtime.health()).detail == "voicevox_external_observed_unqualified"
    assert runtime.observation.snapshot is not None
    assert (await runtime.health()).detail == "voicevox_external_absent"
    assert runtime.observation.snapshot is None
    assert (await runtime.health()).detail == "voicevox_external_observed_unqualified"
    assert runtime.observation.snapshot.observed_digest.endswith("b" * 64)
    _check_denied(runtime)
    assert probe.calls == 4


@pytest.mark.asyncio
async def test_incompatible_and_untrusted_results_never_admit():
    probe = ScriptedProbe(_incompatible(), _observation())
    runtime = _runtime(probe)
    await runtime.start()
    assert runtime.observation is not None
    assert runtime.observation.status is VoicevoxDiscoveryStatus.INCOMPATIBLE
    assert runtime._health().detail == "voicevox_external_incompatible"
    health = await runtime.health()
    assert not health.ready
    assert health.state is RuntimeHealthState.DEGRADED
    assert runtime.observation.status is VoicevoxDiscoveryStatus.COMPATIBLE
    _check_denied(runtime)


@pytest.mark.asyncio
async def test_transport_failures_are_redacted_and_recoverable_as_observation():
    probe = ScriptedProbe(
        RuntimeError("private-engine-url-or-voice-text"),
        _observation(),
    )
    runtime = _runtime(probe)
    await runtime.start()
    assert runtime.observation is None
    assert runtime._health().detail == "voicevox_external_probe_failed"
    health = await runtime.health()
    assert health.state is RuntimeHealthState.DEGRADED
    assert not health.ready
    assert runtime.observation is not None
    assert runtime.observation.status is VoicevoxDiscoveryStatus.COMPATIBLE
    assert "private" not in repr(runtime)
    assert "private" not in repr(health)
    _check_denied(runtime)


@pytest.mark.asyncio
async def test_malformed_probe_result_fails_closed_and_no_private_repr():
    probe = ScriptedProbe(
        "https://private.example.test/token",
        _observation(),
    )
    runtime = _runtime(probe)
    await runtime.start()
    assert runtime.observation is None
    assert runtime._health().detail == "voicevox_external_probe_failed"
    first = await runtime.health()
    assert first.state is RuntimeHealthState.DEGRADED
    assert first.ready is False
    assert runtime.observation is not None
    assert runtime.observation.status is VoicevoxDiscoveryStatus.COMPATIBLE
    assert "private.example" not in repr(runtime)
    _check_denied(runtime)


@pytest.mark.asyncio
async def test_stopped_runtime_does_not_probe_and_can_be_restarted():
    probe = ScriptedProbe(_observation(), _observation("b"))
    runtime = _runtime(probe)
    await runtime.start()
    await runtime.stop()
    await runtime.stop()
    assert runtime.observation is None
    assert (await runtime.health()).state is RuntimeHealthState.STOPPED
    assert probe.calls == 1
    _check_denied(runtime)
    await runtime.start()
    assert runtime.observation.snapshot.observed_digest.endswith("b" * 64)
    assert probe.calls == 2
    await runtime.release()


@pytest.mark.asyncio
async def test_release_is_terminal_and_idempotent():
    probe = ScriptedProbe(_observation())
    runtime = _runtime(probe)
    await runtime.start()
    await runtime.release()
    await runtime.release()
    await runtime.stop()
    assert runtime.observation is None
    assert (await runtime.health()).state is RuntimeHealthState.STOPPED
    _check_denied(runtime)
    with pytest.raises(
        ExternalVoicevoxRuntimeError, match="^voicevox_external_released$"
    ):
        await runtime.start()
    assert probe.calls == 1


@pytest.mark.asyncio
async def test_stop_during_start_cancels_probe_and_fences_late_success():
    probe = BlockingProbe()
    runtime = _runtime(probe)
    start = asyncio.create_task(runtime.start())
    await asyncio.wait_for(probe.started.wait(), timeout=1)
    await runtime.stop()
    with pytest.raises(
        ExternalVoicevoxRuntimeError, match="^voicevox_external_revoked$"
    ):
        await asyncio.wait_for(start, timeout=1)
    assert probe.cancelled == 1
    assert runtime.observation is None
    assert (await runtime.health()).state is RuntimeHealthState.STOPPED
    _check_denied(runtime)


@pytest.mark.asyncio
async def test_stop_during_uncooperative_probe_cannot_restore_stale_generation():
    probe = BlockingProbe(suppress_cancellation=True)
    runtime = _runtime(probe)
    start = asyncio.create_task(runtime.start())
    await asyncio.wait_for(probe.started.wait(), 1)
    await runtime.stop()
    assert runtime.observation is None
    assert (await runtime.health()).state is RuntimeHealthState.STOPPED
    probe.release.set()
    with pytest.raises(
        ExternalVoicevoxRuntimeError, match="^voicevox_external_revoked$"
    ):
        await asyncio.wait_for(start, 1)
    assert runtime.observation is None
    _check_denied(runtime)


@pytest.mark.asyncio
async def test_release_during_health_cancels_probe_and_never_restore():
    probe = ScriptedProbe(_observation())
    runtime = _runtime(probe)
    await runtime.start()
    blocked = BlockingProbe(_observation("b"), suppress_cancellation=True)
    runtime._probe = blocked  # local fake only; never touching the engine
    health_call = asyncio.create_task(runtime.health())
    await asyncio.wait_for(blocked.started.wait(), 1)
    await runtime.release()
    assert runtime.observation is None
    blocked.release.set()
    latest = await asyncio.wait_for(health_call, 1)
    assert latest.state is RuntimeHealthState.STOPPED
    assert not latest.ready
    assert runtime.observation is None
    _check_denied(runtime)


@pytest.mark.asyncio
async def test_caller_cancellation_propagates_and_clears_stale_observation():
    probe = BlockingProbe()
    runtime = _runtime(probe)
    task = asyncio.create_task(runtime.start())
    await asyncio.wait_for(probe.started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert runtime.observation is None
    assert (await runtime.health()).state is RuntimeHealthState.STOPPED


@pytest.mark.asyncio
async def test_residency_never_claims_external_engine_gpu_or_model_bytes():
    runtime = _runtime(ScriptedProbe(_observation()))
    await runtime.start()
    result = await runtime.residency()
    assert isinstance(result, ResidencyReport)
    assert result.items == ()
    assert result.metadata.get("external_engine_residency") == "unverified"
    _check_denied(runtime)


def test_rejects_unreviewed_config_and_public_probe_injection():
    with pytest.raises(ValueError):
        ExternalVoicevoxManagedRuntime(config=object())
    with pytest.raises(TypeError):
        ExternalVoicevoxManagedRuntime(
            config=VoicevoxDiscoveryConfig(enabled=True), probe=object()
        )
    with pytest.raises(TypeError):
        ExternalVoicevoxManagedRuntime(
            config=VoicevoxDiscoveryConfig(enabled=True), probe=ScriptedProbe()
        )


@pytest.mark.asyncio
async def test_start_and_health_never_spawn_stop_or_signal_external_engine(monkeypatch):
    async def forbid_subprocess(*_args, **_kwargs):
        pytest.fail("non-owning runtime must never spawn native engine")

    def forbid_signal(*_args, **_kwargs):
        pytest.fail("non-owning runtime must never send process signals")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbid_subprocess)
    monkeypatch.setattr(os, "kill", forbid_signal)
    runtime = _runtime(ScriptedProbe(_observation(), _observation()))
    await runtime.start()
    await runtime.health()
    await runtime.stop()
    await runtime.release()
    _check_denied(runtime)


@pytest.mark.asyncio
async def test_native_http_shape_uses_only_local_fake_get_and_stop_preserves_server():
    """Fake TCP engine stays running after TSUMGI stops its own runtime."""
    import json

    requests: list[bytes] = []
    workers: set[asyncio.Task] = set()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        task = asyncio.current_task()
        workers.add(task)
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 1)
            line = raw.split(b"\r\n", 1)[0]
            requests.append(line)
            method, target, version = line.split(b" ")
            assert method == b"GET" and version == b"HTTP/1.1"
            if target == b"/version":
                data = b'"0.15.0"'
            elif target == b"/speakers":
                data = json.dumps(
                    [
                        {
                            "name": "fake",
                            "speaker_uuid": "7ffcb7ce-00ec-4bdc-82cd-45a8889e43ff",
                            "version": "mock",
                            "styles": [
                                {"id": 8, "name": "fake", "type": "streaming_talk"}
                            ],
                        }
                    ]
                ).encode("utf-8")
            else:
                pytest.fail("R2 may only use the read-only R1 paths")
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(data)}\r\n\r\n".encode("ascii")
                + data
            )
            await writer.drain()
        finally:
            writer.close()
            workers.discard(task)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        runtime = ExternalVoicevoxManagedRuntime(
            config=VoicevoxDiscoveryConfig(
                enabled=True, endpoint=f"http://127.0.0.1:{port}"
            )
        )
        await runtime.start()
        assert runtime.observation is not None
        assert runtime.observation.status is VoicevoxDiscoveryStatus.COMPATIBLE
        health = await runtime.health()
        assert health.state is RuntimeHealthState.DEGRADED and not health.ready
        _check_denied(runtime)
        await runtime.stop()
        assert runtime.observation is None
        assert server.is_serving(), "TSUMGI stop must not stop external engine"
        assert requests == [
            b"GET /version HTTP/1.1",
            b"GET /speakers HTTP/1.1",
            b"GET /version HTTP/1.1",
            b"GET /speakers HTTP/1.1",
        ]
        await runtime.release()
        assert server.is_serving(), "TSUMGI release must not stop engine"
    finally:
        server.close()
        await server.wait_closed()
        for task in tuple(workers):
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)


@pytest.mark.asyncio
async def test_missing_engine_on_genuine_loopback_is_unavailable_without_install():
    server = await asyncio.start_server(
        lambda _reader, writer: writer.close(), "127.0.0.1", 0
    )
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    runtime = ExternalVoicevoxManagedRuntime(
        config=VoicevoxDiscoveryConfig(
            enabled=True, endpoint=f"http://127.0.0.1:{port}"
        )
    )
    await runtime.start()
    assert runtime.observation is not None
    assert runtime.observation.status is VoicevoxDiscoveryStatus.ABSENT
    health = await runtime.health()
    assert not health.ready
    _check_denied(runtime)
    await runtime.release()


@pytest.mark.asyncio
async def test_health_stop_late_observation_is_revoked_without_restarting():
    probe = ScriptedProbe(_observation())
    runtime = _runtime(probe)
    await runtime.start()
    pending = BlockingProbe(_observation("b"), suppress_cancellation=True)
    runtime._probe = pending
    call = asyncio.create_task(runtime.health())
    await asyncio.wait_for(pending.started.wait(), 1)
    await runtime.stop()
    assert runtime.observation is None
    pending.release.set()
    result = await asyncio.wait_for(call, 1)
    assert result.state is RuntimeHealthState.STOPPED
    assert not result.ready
    assert runtime.observation is None


@pytest.mark.asyncio
async def test_two_simultaneous_start_calls_do_not_probe_concurrently():
    pending = BlockingProbe()
    runtime = _runtime(pending)
    first = asyncio.create_task(runtime.start())
    await asyncio.wait_for(pending.started.wait(), 1)
    second = asyncio.create_task(runtime.start())
    await asyncio.sleep(0)
    assert pending.calls == 1
    pending.release.set()
    await asyncio.wait_for(asyncio.gather(first, second), 1)
    assert pending.calls == 1
    assert runtime.observation is not None
    assert (await runtime.residency()).items == ()


@pytest.mark.asyncio
async def test_waiting_start_is_revoked_if_stop_precedes_lock_acquisition():
    pending = BlockingProbe(suppress_cancellation=True)
    runtime = _runtime(pending)
    first = asyncio.create_task(runtime.start())
    await asyncio.wait_for(pending.started.wait(), 1)
    second = asyncio.create_task(runtime.start())
    await asyncio.sleep(0)
    await runtime.stop()
    pending.release.set()
    results = await asyncio.wait_for(
        asyncio.gather(first, second, return_exceptions=True), 1
    )
    assert len(results) == 2
    assert all(isinstance(r, ExternalVoicevoxRuntimeError) for r in results)
    assert pending.calls == 1
    assert runtime.observation is None
    assert (await runtime.health()).state is RuntimeHealthState.STOPPED


@pytest.mark.asyncio
async def test_cancelled_health_rechecks_and_discards_prior_observation():
    probe = ScriptedProbe(_observation())
    runtime = _runtime(probe)
    await runtime.start()
    pending = BlockingProbe()
    runtime._probe = pending
    task = asyncio.create_task(runtime.health())
    await asyncio.wait_for(pending.started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert runtime.observation is None
    health = runtime._health()
    assert health.state is RuntimeHealthState.DEGRADED
    assert not health.ready
    _check_denied(runtime)


@pytest.mark.asyncio
async def test_invalid_compatible_without_observed_catalog_stays_unqualified():
    inconsistent = VoicevoxDiscoveryResult(
        status=VoicevoxDiscoveryStatus.COMPATIBLE,
        reason="observed_compatible",
        snapshot=None,
    )
    runtime = _runtime(ScriptedProbe(inconsistent))
    await runtime.start()
    assert runtime.observation is None
    assert runtime._health().detail == "voicevox_external_probe_failed"
    _check_denied(runtime)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected_reason"),
    [
        (VoicevoxDiscoveryStatus.COMPATIBLE, "observed_compatible"),
        (VoicevoxDiscoveryStatus.ABSENT, "endpoint_unreachable"),
        (VoicevoxDiscoveryStatus.INCOMPATIBLE, "incompatible_response"),
    ],
)
async def test_observed_reason_is_fixed_allowlist_never_private_probe_text(
    status, expected_reason
):
    """A synthetic fake must not be able to inject private reason diagnostics."""
    forged = VoicevoxDiscoveryResult(
        status=status,
        reason="secret-user-text+http://private.example.test/token",
        snapshot=(
            _observation().snapshot
            if status is VoicevoxDiscoveryStatus.COMPATIBLE
            else None
        ),
    )
    runtime = _runtime(ScriptedProbe(forged))
    await runtime.start()
    observation = runtime.observation
    assert observation is not None
    assert observation.reason == expected_reason
    assert "secret" not in repr(observation)
    assert "private.example" not in repr(observation)
    assert "secret" not in repr(runtime._health())
    _check_denied(runtime)
