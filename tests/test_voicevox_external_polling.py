"""#247 R4-B0: synthetic loopback-only polling (no native VOICEVOX engine)."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest

from astrumweaver.runtime.providers.voicevox_discovery import (
    VoicevoxDiscoveryConfig,
    VoicevoxDiscoveryResult,
    VoicevoxDiscoveryStatus,
    _catalog,
)
from astrumweaver.runtime.providers.voicevox_external_polling import (
    ExternalVoicevoxPollingCoordinator,
    ExternalVoicevoxPollingError,
    VoicevoxExternalPollingConfig,
    VoicevoxExternalPollSnapshot,
    VoicevoxPollStatus,
)

_UUID = "7ffcb7ce-00ec-4bdc-82cd-45a8889e43ff"
_SECRET = "private-native-token-and-voice-text"


async def _valid_reply(path: str) -> tuple[int, bytes, str]:
    if path == "/version":
        return 200, b'"0.15.0"', "application/json"
    if path == "/speakers":
        content = [
            {
                "name": "synthetic-test-only",
                "speaker_uuid": _UUID,
                "styles": [{"id": 8, "name": "test", "type": "talk"}],
            }
        ]
        return 200, json.dumps(content).encode(), "application/json"
    pytest.fail("R4-B0 must never access a synthesis or unrelated HTTP route")


@asynccontextmanager
async def fake_voicevox(
    responder: Callable[[str], Awaitable[tuple[int, bytes, str]]] = _valid_reply,
):
    """Disposable loopback HTTP fixture, no real VOICEVOX processes or models."""
    lines: list[bytes] = []
    tasks: set[asyncio.Task] = set()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        task = asyncio.current_task()
        tasks.add(task)
        try:
            request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 1)
            first = request.split(b"\r\n", 1)[0]
            lines.append(first)
            method, target, version = first.split(b" ")
            assert method == b"GET" and version == b"HTTP/1.1"
            code, data, media_type = await responder(target.decode("ascii"))
            writer.write(
                f"HTTP/1.1 {code} TEST\r\n"
                f"Content-Type: {media_type}\r\n"
                f"Content-Length: {len(data)}\r\n"
                "Connection: close\r\n\r\n".encode("ascii")
                + data
            )
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            tasks.discard(task)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield port, lines, server
    finally:
        server.close()
        await server.wait_closed()
        for task in tuple(tasks):
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _config(
    port: int,
    *,
    timeout: float = 0.5,
    interval: float = 5.0,
    backoff: float = 30.0,
):
    return VoicevoxExternalPollingConfig(
        enabled=True,
        discovery=VoicevoxDiscoveryConfig(
            enabled=True,
            endpoint=f"http://127.0.0.1:{port}",
            timeout_seconds=timeout,
        ),
        interval_seconds=interval,
        max_backoff_seconds=backoff,
    )


async def _wait(
    poller: ExternalVoicevoxPollingCoordinator,
    status: VoicevoxPollStatus,
    *,
    sequence: int | None = None,
    timeout: float = 1.0,
):
    async def waiter():
        while True:
            s = poller.snapshot()
            if s.status is status and (sequence is None or s.sequence >= sequence):
                return s
            await asyncio.sleep(0.005)

    return await asyncio.wait_for(waiter(), timeout)


def _never_admitted(snapshot: VoicevoxExternalPollSnapshot):
    assert snapshot.ready is False
    assert snapshot.can_apply is False
    assert snapshot.worker_runtime_instance_epoch is None
    payload = snapshot.to_dict()
    assert payload["ready"] is False
    assert payload["can_apply"] is False
    assert payload["rights_verified"] is False
    assert payload["external_engine_verified"] is False
    assert payload["worker_runtime_instance_epoch"] is None
    return payload


def test_default_disabled_config_needs_no_endpoint_or_runtime_settings():
    cfg = VoicevoxExternalPollingConfig()
    assert cfg.enabled is False
    assert cfg.discovery.enabled is False
    poller = ExternalVoicevoxPollingCoordinator(config=cfg)
    state = poller.snapshot()
    assert state.status is VoicevoxPollStatus.DISABLED
    assert state.sequence == 0 and state.completed_probes == 0
    assert state.running is False
    _never_admitted(state)


@pytest.mark.asyncio
async def test_disabled_never_inspects_foreign_config_or_opens_network(monkeypatch):
    class Trap:
        def __getattribute__(self, name):
            pytest.fail(f"disabled accessed {name}")

        def __bool__(self) -> bool:
            pytest.fail("disabled coerced foreign value")
            return False

        def __eq__(self, value):
            pytest.fail("disabled compared foreign value")

    async def fail(*_args, **_kwargs):
        pytest.fail("disabled coordinator attempted a network operation")

    monkeypatch.setattr(asyncio, "open_connection", fail)
    cfg = VoicevoxExternalPollingConfig(
        enabled=False,
        discovery=Trap(),
        interval_seconds=Trap(),
        max_backoff_seconds=Trap(),
    )
    poller = ExternalVoicevoxPollingCoordinator(config=cfg)
    await poller.start()
    await poller.start()
    assert poller._task is None
    assert poller.snapshot().status is VoicevoxPollStatus.DISABLED
    await poller.close()
    await poller.close()
    assert poller.snapshot().status is VoicevoxPollStatus.STOPPED
    assert poller._task is None


@pytest.mark.parametrize(
    "interval,backoff",
    [
        (0, 30),
        (0.99, 30),
        (61, 61),
        (True, 30),
        (False, 30),
        (float("nan"), 30),
        (float("inf"), 30),
        (10**1000, 30),
        (5, 3),
        (1, 61),
        (5, True),
        (5, float("nan")),
        (5, float("inf")),
        (5, 10**1000),
    ],
)
def test_invalid_interval_and_backoff_are_redacted(interval, backoff):
    with pytest.raises(ValueError, match="VOICEVOX external polling config is invalid"):
        _config(50021, interval=interval, backoff=backoff)


def test_requires_exact_enabled_and_canonical_loopback_endpoint():
    with pytest.raises(ValueError):
        VoicevoxExternalPollingConfig(enabled=1)
    with pytest.raises(ValueError):
        ExternalVoicevoxPollingCoordinator(config=object())
    with pytest.raises(ValueError):
        VoicevoxExternalPollingConfig(
            enabled=True, discovery=VoicevoxDiscoveryConfig(enabled=False)
        )
    for endpoint in (
        "http://localhost:50021",
        "http://0.0.0.0:50021",
        "http://192.168.1.2:50021",
        "https://127.0.0.1:50021",
        "http://127.0.0.1:50021/private",
    ):
        with pytest.raises(ValueError):
            VoicevoxDiscoveryConfig(enabled=True, endpoint=endpoint)


def test_reflectively_modified_config_revalidated_before_coordinator():
    cfg = _config(50021)
    object.__setattr__(cfg.discovery, "endpoint", "http://127.0.0.1:50021/private")
    with pytest.raises(ValueError, match="VOICEVOX external polling config is invalid"):
        ExternalVoicevoxPollingCoordinator(config=cfg)
    cfg2 = _config(50021)
    object.__setattr__(cfg2, "interval_seconds", _SECRET)
    with pytest.raises(ValueError) as exc:
        ExternalVoicevoxPollingCoordinator(config=cfg2)
    assert _SECRET not in str(exc.value)


@pytest.mark.asyncio
async def test_loopback_valid_version_and_speakers_only_observed_not_admitted():
    async with fake_voicevox() as (port, requests, server):
        poller = ExternalVoicevoxPollingCoordinator(config=_config(port))
        assert poller.snapshot().status is VoicevoxPollStatus.IDLE
        await poller.start()
        await poller.start()
        state = await _wait(poller, VoicevoxPollStatus.COMPATIBLE_OBSERVED)
        assert state.sequence == 1
        assert state.completed_probes == 1
        assert state.consecutive_failures == 0
        assert state.running
        assert state.observed_digest.startswith("sha256:")
        _never_admitted(state)
        assert requests == [b"GET /version HTTP/1.1", b"GET /speakers HTTP/1.1"]
        payload = state.to_json()
        assert _UUID not in payload
        assert "127.0.0.1" not in payload
        assert "synthesize" not in payload
        assert server.is_serving()
        await poller.close()
        assert server.is_serving(), (
            "closing TSUMGI poller must not kill external server"
        )
        assert poller.snapshot().status is VoicevoxPollStatus.STOPPED
        assert poller.snapshot().observed_digest is None


@pytest.mark.asyncio
async def test_unavailable_loopback_port_observed_absent_without_spawning_engine():
    server = await asyncio.start_server(lambda _r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    poller = ExternalVoicevoxPollingCoordinator(config=_config(port))
    await poller.start()
    state = await _wait(poller, VoicevoxPollStatus.ABSENT)
    assert state.completed_probes == 1
    assert state.observed_digest is None
    _never_admitted(state)
    await poller.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body,code,media",
    [
        (b"{}", 200, "application/json"),
        (b'"garbage-version?"', 200, "application/json"),
        (b"[]", 200, "application/json"),
        (b"{}" * 140000, 200, "application/json"),
        (b'{"message":"' + _SECRET.encode() + b'"}', 503, "application/json"),
        (b'"/version"', 302, "application/json"),
        (b'"0.15.0"', 200, "text/plain"),
    ],
)
async def test_malformed_redirect_huge_or_foreign_http_is_incompatible(
    body, code, media
):
    async def reply(path):
        if path == "/speakers" and body == b'"/version"':
            pytest.fail("redirect must never be followed")
        return code, body, media

    async with fake_voicevox(reply) as (port, requests, _):
        poller = ExternalVoicevoxPollingCoordinator(config=_config(port))
        await poller.start()
        state = await _wait(poller, VoicevoxPollStatus.INCOMPATIBLE)
        assert state.observed_digest is None
        assert state.consecutive_failures == 1
        assert requests == [b"GET /version HTTP/1.1"]
        assert _SECRET not in state.to_json()
        await poller.close()


@pytest.mark.asyncio
async def test_timeout_becomes_incompatible_and_no_success_after_close():
    pending = asyncio.Event()
    started = asyncio.Event()

    async def slow(_path):
        started.set()
        await pending.wait()
        return 200, b'"0.15.0"', "application/json"

    async with fake_voicevox(slow) as (port, requests, _):
        poller = ExternalVoicevoxPollingCoordinator(config=_config(port, timeout=0.05))
        await poller.start()
        await asyncio.wait_for(started.wait(), 1)
        state = await _wait(poller, VoicevoxPollStatus.INCOMPATIBLE)
        assert state.observed_digest is None
        assert requests == [b"GET /version HTTP/1.1"]
        await poller.close()
        pending.set()
        assert not poller.snapshot().running


@pytest.mark.asyncio
async def test_synchronous_stop_revokes_positive_observation_before_task_join():
    async with fake_voicevox() as (port, _, server):
        poller = ExternalVoicevoxPollingCoordinator(config=_config(port))
        await poller.start()
        positive = await _wait(poller, VoicevoxPollStatus.COMPATIBLE_OBSERVED)
        poller.request_stop()
        stopped = poller.snapshot()
        assert stopped.status is VoicevoxPollStatus.STOPPED
        assert stopped.observed_digest is None
        assert stopped.sequence > positive.sequence
        assert not stopped.running
        await poller.stop()
        await poller.stop()
        assert poller._task is None
        assert server.is_serving()
        await poller.close()
        with pytest.raises(ExternalVoicevoxPollingError):
            await poller.start()


@pytest.mark.asyncio
async def test_deliberate_restart_only_after_owned_task_join_new_generation():
    async with fake_voicevox() as (port, requests, _):
        poller = ExternalVoicevoxPollingCoordinator(config=_config(port))
        await poller.start()
        first = await _wait(poller, VoicevoxPollStatus.COMPATIBLE_OBSERVED)
        poller.request_stop()
        with pytest.raises(ExternalVoicevoxPollingError):
            await poller.start()  # no overlapping old/new poll tasks
        await poller.stop()
        await poller.start()
        second = await _wait(
            poller, VoicevoxPollStatus.COMPATIBLE_OBSERVED, sequence=first.sequence + 2
        )
        assert second.sequence > first.sequence
        assert second.observed_digest == first.observed_digest
        assert requests == [b"GET /version HTTP/1.1", b"GET /speakers HTTP/1.1"] * 2
        await poller.close()


@pytest.mark.asyncio
async def test_close_cancels_pending_http_without_native_process_control(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow(_path):
        started.set()
        await release.wait()
        return 200, b'"0.15.0"', "application/json"

    def never(*_args, **_kwargs):
        pytest.fail("poller must not signal an external engine process")

    monkeypatch.setattr(os, "kill", never)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", never)
    async with fake_voicevox(slow) as (port, _, server):
        poller = ExternalVoicevoxPollingCoordinator(config=_config(port, timeout=1))
        await poller.start()
        await asyncio.wait_for(started.wait(), 1)
        old = poller.snapshot()
        poller.request_stop()
        assert poller.snapshot().status is VoicevoxPollStatus.STOPPED
        assert poller.snapshot().sequence > old.sequence
        await asyncio.wait_for(poller.close(), 1)
        assert poller._task is None
        release.set()
        await asyncio.sleep(0)
        assert poller.snapshot().status is VoicevoxPollStatus.STOPPED
        assert server.is_serving()


@pytest.mark.asyncio
async def test_uncooperative_private_synthetic_probe_cannot_restore_late_success():
    async with fake_voicevox() as (port, _, _):
        poller = ExternalVoicevoxPollingCoordinator(config=_config(port))
        started = asyncio.Event()
        release = asyncio.Event()

        class NonCooperative:
            async def probe_once(self):
                started.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    await release.wait()
                snap = _catalog(
                    "0.15.0",
                    [{"speaker_uuid": _UUID, "styles": [{"id": 8}]}],
                    f"http://127.0.0.1:{port}",
                )
                return VoicevoxDiscoveryResult(
                    VoicevoxDiscoveryStatus.COMPATIBLE,
                    "private-token",
                    snap,
                )

        # Private synthetic seam only: production always creates canonical R1.
        poller._probe = NonCooperative()
        await poller.start()
        await asyncio.wait_for(started.wait(), 1)
        poller.request_stop()
        assert poller.snapshot().status is VoicevoxPollStatus.STOPPED
        assert poller.snapshot().observed_digest is None
        with pytest.raises(ExternalVoicevoxPollingError):
            await poller.start()
        release.set()
        await asyncio.wait_for(poller.stop(), 1)
        assert poller.snapshot().observed_digest is None
        assert poller.snapshot().completed_probes == 0
        await poller.close()


@pytest.mark.asyncio
async def test_serialized_observation_digest_is_not_native_identity_or_rights():
    async with fake_voicevox() as (port, _, _):
        poller = ExternalVoicevoxPollingCoordinator(config=_config(port))
        await poller.start()
        state = await _wait(poller, VoicevoxPollStatus.COMPATIBLE_OBSERVED)
        fields = state.to_dict()
        assert fields["status"] == VoicevoxPollStatus.COMPATIBLE_OBSERVED.value
        assert fields["external_engine_verified"] is False
        assert fields["rights_verified"] is False
        assert fields["worker_runtime_instance_epoch"] is None
        assert not state.ready and not state.can_apply
        await poller.close()


def test_public_snapshot_revalidates_fields_before_serializing():
    original = VoicevoxExternalPollSnapshot(
        status=VoicevoxPollStatus.DISABLED,
        running=False,
        sequence=0,
        completed_probes=0,
        consecutive_failures=0,
    )
    assert original.to_dict()["status"] == "disabled"
    for field_name, secret in (
        ("observed_digest", _SECRET),
        ("status", _SECRET),
        ("sequence", _SECRET),
        ("running", _SECRET),
    ):
        snapshot = replace(original)
        object.__setattr__(snapshot, field_name, secret)
        with pytest.raises(ExternalVoicevoxPollingError) as caught:
            snapshot.to_dict()
        assert secret not in str(caught.value)
        with pytest.raises(ExternalVoicevoxPollingError):
            snapshot.to_json()


def test_invalid_public_snapshot_construction_fails_closed():
    with pytest.raises(ExternalVoicevoxPollingError):
        VoicevoxExternalPollSnapshot(
            status=VoicevoxPollStatus.COMPATIBLE_OBSERVED,
            running=True,
            sequence=1,
            completed_probes=1,
            consecutive_failures=0,
            observed_digest="raw-private-text",
        )
    with pytest.raises(ExternalVoicevoxPollingError):
        VoicevoxExternalPollSnapshot(
            status=VoicevoxPollStatus.ABSENT,
            running=True,
            sequence=True,
            completed_probes=1,
            consecutive_failures=1,
        )


@pytest.mark.asyncio
async def test_compatible_repeated_samples_are_single_flight_and_do_not_generate_worker_epoch():
    async with fake_voicevox() as (port, requests, _):
        poller = ExternalVoicevoxPollingCoordinator(config=_config(port))
        release = asyncio.Event()
        waiting = asyncio.Event()
        delays: list[float] = []

        async def controlled_sleep(seconds):
            delays.append(seconds)
            waiting.set()
            await release.wait()
            release.clear()
            waiting.clear()

        poller._sleep_interval = controlled_sleep
        await poller.start()
        first = await _wait(poller, VoicevoxPollStatus.COMPATIBLE_OBSERVED)
        await asyncio.wait_for(waiting.wait(), 1)
        release.set()
        second = await _wait(poller, VoicevoxPollStatus.COMPATIBLE_OBSERVED, sequence=2)
        assert second.completed_probes == 2
        assert second.observed_digest == first.observed_digest
        assert second.worker_runtime_instance_epoch is None
        assert requests == [b"GET /version HTTP/1.1", b"GET /speakers HTTP/1.1"] * 2
        assert delays[:2] == [5.0, 5.0]
        await poller.close()


@pytest.mark.asyncio
async def test_bounded_failure_backoff_then_success_resets_interval():
    async with fake_voicevox() as (port, _, _):
        poller = ExternalVoicevoxPollingCoordinator(config=_config(port, interval=2))
        calls = 0

        async def scripted():
            nonlocal calls
            calls += 1
            if calls <= 4:
                return VoicevoxDiscoveryResult(VoicevoxDiscoveryStatus.ABSENT, _SECRET)
            return VoicevoxDiscoveryResult(
                VoicevoxDiscoveryStatus.COMPATIBLE,
                _SECRET,
                _catalog(
                    "0.15.0",
                    [{"speaker_uuid": _UUID, "styles": [{"id": 8}]}],
                    f"http://127.0.0.1:{port}",
                ),
            )

        # Private scripted probe seam: never accepted via public constructor.
        poller._probe.probe_once = scripted
        waits: list[float] = []
        release: asyncio.Queue[None] = asyncio.Queue()

        async def controlled_sleep(seconds: float) -> None:
            waits.append(seconds)
            await release.get()

        poller._sleep_interval = controlled_sleep
        await poller.start()
        for count in range(1, 6):
            target = (
                VoicevoxPollStatus.ABSENT
                if count <= 4
                else VoicevoxPollStatus.COMPATIBLE_OBSERVED
            )
            state = await _wait(poller, target, sequence=count)
            assert state.completed_probes == count
            assert len(waits) == count
            if count < 5:
                release.put_nowait(None)
        assert waits[:5] == [2, 4, 8, 16, 2]
        await poller.close()


@pytest.mark.asyncio
async def test_fatal_internal_sleep_exception_fails_closed_without_exposing_error():
    async with fake_voicevox() as (port, _, _):
        poller = ExternalVoicevoxPollingCoordinator(config=_config(port))

        async def fail_sleep(_seconds):
            raise RuntimeError(_SECRET)

        poller._sleep_interval = fail_sleep
        await poller.start()
        state = await _wait(poller, VoicevoxPollStatus.FAILED)
        assert state.observed_digest is None
        assert _SECRET not in state.to_json()
        assert _SECRET not in repr(state)
        assert not state.running
        await poller.close()


@pytest.mark.asyncio
async def test_invalid_speakers_after_valid_version_are_rejected():
    async def malformed_speakers(path: str) -> tuple[int, bytes, str]:
        if path == "/version":
            return 200, b'"0.15.0"', "application/json"
        assert path == "/speakers"
        return (
            200,
            json.dumps(
                [{"speaker_uuid": _UUID, "styles": [{"id": 8}, {"id": 8}]}]
            ).encode(),
            "application/json",
        )

    async with fake_voicevox(malformed_speakers) as (port, requests, _):
        poller = ExternalVoicevoxPollingCoordinator(config=_config(port))
        await poller.start()
        state = await _wait(poller, VoicevoxPollStatus.INCOMPATIBLE)
        assert state.observed_digest is None
        assert requests == [b"GET /version HTTP/1.1", b"GET /speakers HTTP/1.1"]
        _never_admitted(state)
        await poller.close()


@pytest.mark.asyncio
async def test_private_probe_exception_and_forged_result_fail_closed():
    async with fake_voicevox() as (port, _, _):
        poller = ExternalVoicevoxPollingCoordinator(config=_config(port))

        async def malicious_probe():
            raise RuntimeError(_SECRET)

        poller._probe.probe_once = malicious_probe
        await poller.start()
        negative = await _wait(poller, VoicevoxPollStatus.INCOMPATIBLE)
        assert negative.completed_probes == 1
        assert _SECRET not in negative.to_json()
        await poller.stop()

        async def forged_probe():
            return VoicevoxDiscoveryResult(
                VoicevoxDiscoveryStatus.COMPATIBLE,
                _SECRET,
                _catalog(
                    "0.15.0",
                    [{"speaker_uuid": _UUID, "styles": [{"id": 8}]}],
                    "http://127.0.0.1:50021",
                ),
            )

        poller._probe.probe_once = forged_probe
        await poller.start()
        invalid = await _wait(poller, VoicevoxPollStatus.INCOMPATIBLE)
        assert invalid.observed_digest is None
        _never_admitted(invalid)
        await poller.close()


def test_public_snapshot_rejects_incoherent_counts_and_idle_running():
    for values in (
        {"status": VoicevoxPollStatus.IDLE, "running": True},
        {"completed_probes": 2, "sequence": 1},
        {
            "status": VoicevoxPollStatus.COMPATIBLE_OBSERVED,
            "running": True,
            "sequence": 1,
            "completed_probes": 1,
            "consecutive_failures": 1,
            "observed_digest": "sha256:" + "a" * 64,
        },
    ):
        fields = {
            "status": VoicevoxPollStatus.DISABLED,
            "running": False,
            "sequence": 0,
            "completed_probes": 0,
            "consecutive_failures": 0,
        }
        fields.update(values)
        with pytest.raises(ExternalVoicevoxPollingError):
            VoicevoxExternalPollSnapshot(**fields)
