"""#247 R4-B1/B: synthetic, local-only R1 poller to advisory R4-A integration."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager

import pytest

from astrumweaver.gateway.audio import VoiceAlias
from astrumweaver.runtime.providers.voicevox_discovery import (
    VoicevoxDiscoveryConfig,
    VoicevoxDiscoveryResult,
    VoicevoxDiscoveryStatus,
    _catalog,
)
from astrumweaver.runtime.providers.voicevox_external_observation import (
    ExternalVoicevoxObservationCoordinator,
    ExternalVoicevoxObservationError,
)
from astrumweaver.runtime.providers.voicevox_external_plan import (
    ObservationPhase,
    OperatorPolicyStatus,
)
from astrumweaver.runtime.providers.voicevox_external_polling import (
    VoicevoxExternalPollingConfig,
)
from astrumweaver.runtime.providers.voicevox_external_reconcile import (
    ExternalVoicevoxReconcileConfig,
)

_UUID = "7ffcb7ce-00ec-4bdc-82cd-45a8889e43ff"
_OTHER = "ab888885-e22d-4fb5-8161-df19a8b97a02"
_POLICY = "sha256:" + "a" * 64
_SECRET = "private native voice text /audio_query?text=supersecret"


def _obs(port: int, *, style: int = 8, version: str = "0.15.0"):
    return VoicevoxDiscoveryResult(
        VoicevoxDiscoveryStatus.COMPATIBLE,
        "observed_compatible",
        _catalog(
            version,
            [{"speaker_uuid": _UUID, "styles": [{"id": style}]}],
            f"http://127.0.0.1:{port}",
        ),
    )


def _config(
    port: int,
    *,
    pin: str | None = None,
    style: int = 8,
    credit: str = "Test credit",
    license_id: str = "operator-license",
    policy: OperatorPolicyStatus = OperatorPolicyStatus.UNVERIFIED,
    policy_revision: str = _POLICY,
):
    return ExternalVoicevoxReconcileConfig(
        enabled=True,
        discovery=VoicevoxDiscoveryConfig(
            enabled=True,
            endpoint=f"http://127.0.0.1:{port}",
            timeout_seconds=0.5,
        ),
        expected_observed_digest=(
            _obs(port).snapshot.observed_digest if pin is None else pin
        ),
        voices=(
            VoiceAlias(
                alias="narrator",
                speaker_id=_UUID,
                style_id=str(style),
                locale="ja",
                license_id=license_id,
                attribution=credit,
            ),
        ),
        policy_status=policy,
        policy_revision=policy_revision,
    )


def _poll(port: int, *, interval: float = 1):
    return VoicevoxExternalPollingConfig(
        enabled=True,
        discovery=VoicevoxDiscoveryConfig(
            enabled=True,
            endpoint=f"http://127.0.0.1:{port}",
            timeout_seconds=0.5,
        ),
        interval_seconds=interval,
        max_backoff_seconds=3,
    )


def _coordinator(port: int, **kwargs):
    return ExternalVoicevoxObservationCoordinator(
        config=_config(port, **kwargs), polling=_poll(port)
    )


async def _wait(
    c: ExternalVoicevoxObservationCoordinator, phase: str, *, generation=None
):
    async def wait():
        while True:
            state = c.snapshot()
            plan = state.get("reconciliation", {}).get("plan", {})
            if plan.get("phase") == phase and (
                generation is None or plan.get("generation_counter") == generation
            ):
                return state
            await asyncio.sleep(0.005)

    return await asyncio.wait_for(wait(), 1.5)


@asynccontextmanager
async def fake_voicevox():
    """Disposable bounded HTTP server: strictly GET /version + /speakers."""
    requests: list[bytes] = []
    tasks: set[asyncio.Task] = set()
    state = {"version": "0.15.0", "style": 8, "bad": False}

    async def handle(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        try:
            req = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 1)
            first = req.split(b"\r\n", 1)[0]
            requests.append(first)
            method, path, version = first.split(b" ")
            assert method == b"GET" and version == b"HTTP/1.1"
            assert path in (b"/version", b"/speakers")
            if path == b"/version":
                body = json.dumps(state["version"]).encode()
            else:
                if state["bad"]:
                    body = b"{}"
                else:
                    body = json.dumps(
                        [
                            {
                                "speaker_uuid": _UUID,
                                "styles": [{"id": state["style"], "type": "talk"}],
                            }
                        ]
                    ).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                + body
            )
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            tasks.discard(task)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1], requests, state
    finally:
        server.close()
        await server.wait_closed()
        for task in tuple(tasks):
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _safe(payload):
    value = json.dumps(payload, sort_keys=True)
    assert payload["ready"] is False
    assert payload["can_apply"] is False
    assert payload["rights_verified"] is False
    assert payload["external_engine_verified"] is False
    assert payload["worker_runtime_instance_epoch"] is None
    for secret in (
        _UUID,
        _OTHER,
        _SECRET,
        "Test credit",
        "operator-license",
        "127.0.0.1",
        "/audio_query",
        "synthesis",
    ):
        assert secret not in value
    return payload


@pytest.mark.asyncio
async def test_disabled_zero_foreign_inspection_and_zero_io(monkeypatch):
    class Trap:
        def __getattribute__(self, attr):
            pytest.fail("disabled foreign access")

        def __bool__(self) -> bool:
            pytest.fail("disabled foreign truth testing")
            return False

    async def fail(*_args, **_kwargs):
        pytest.fail("disabled must never open a connection")

    monkeypatch.setattr(asyncio, "open_connection", fail)
    c = ExternalVoicevoxObservationCoordinator(
        config=ExternalVoicevoxReconcileConfig(
            enabled=False, discovery=Trap(), voices=Trap(), policy_revision=Trap()
        ),
        polling=Trap(),
    )
    await c.start()
    _safe(c.snapshot())
    assert c.snapshot()["reconciliation"]["active"] is False
    await c.close()
    _safe(c.snapshot())


@pytest.mark.asyncio
async def test_real_disposable_loopback_two_gets_unqualified_only():
    async with fake_voicevox() as (port, requests, _):
        c = _coordinator(port)
        try:
            await c.start()
            first = await _wait(c, ObservationPhase.CANDIDATE_UNQUALIFIED.value)
            assert first["reconciliation"]["plan"]["generation_counter"] == 1
            assert first["last_transition"]["requests"] == ["qualification_requested"]
            assert first["polling"]["observed_digest"].startswith("sha256:")
            _safe(first)
            assert requests == [b"GET /version HTTP/1.1", b"GET /speakers HTTP/1.1"]
            assert "can_apply" in c.to_json()
        finally:
            await c.close()


@pytest.mark.asyncio
async def test_wrong_pin_does_not_create_candidate_even_if_poll_observes_compatible():
    async with fake_voicevox() as (port, _, _):
        c = _coordinator(port, pin="sha256:" + "b" * 64)
        try:
            await c.start()
            state = await _wait(c, ObservationPhase.OBSERVATION_CHANGED.value)
            assert state["reconciliation"]["plan"]["candidate_signature"] is None
            assert state["polling"]["status"] == "compatible_observed"
            _safe(state)
        finally:
            await c.close()


@pytest.mark.asyncio
async def test_repeated_positive_then_absent_then_reappearance_preserves_retirement():
    port = 50021  # private synthetic probe only; no actual HTTP I/O
    c = _coordinator(port)
    calls = 0
    release: asyncio.Queue[None] = asyncio.Queue()

    async def scripted():
        nonlocal calls
        calls += 1
        if calls in (2, 3):
            return VoicevoxDiscoveryResult(VoicevoxDiscoveryStatus.ABSENT, _SECRET)
        return _obs(port)

    async def step(_seconds: float):
        await release.get()

    c._poller._probe.probe_once = scripted
    c._poller._sleep_interval = step
    try:
        await c.start()
        first = await _wait(
            c, ObservationPhase.CANDIDATE_UNQUALIFIED.value, generation=1
        )
        assert first["last_transition"]["requests"] == ["qualification_requested"]
        release.put_nowait(None)
        lost = await _wait(c, ObservationPhase.UNAVAILABLE.value, generation=1)
        assert lost["last_transition"]["requests"] == [
            "withdraw_prior_generation_requested"
        ]
        assert lost["reconciliation"]["plan"]["retirement_pending"] is True
        release.put_nowait(None)
        for _ in range(200):
            if (
                calls >= 3
                and c.snapshot()["last_transition"]["reason"] == "unavailable"
            ):
                break
            await asyncio.sleep(0.005)
        assert calls >= 3
        assert c.snapshot()["reconciliation"]["plan"]["generation_counter"] == 1
        release.put_nowait(None)
        restored = await _wait(
            c, ObservationPhase.CANDIDATE_UNQUALIFIED.value, generation=2
        )
        assert restored["reconciliation"]["plan"]["retirement_pending"] is True
        assert restored["reconciliation"]["plan"]["last_retired_generation"] == 1
        _safe(restored)
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_request_stop_is_synchronous_and_late_cancel_resistant_result_ignored():
    c = _coordinator(50021)
    started, release = asyncio.Event(), asyncio.Event()

    async def ignore_cancel():
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return _obs(50021)

    c._poller._probe.probe_once = ignore_cancel
    await c.start()
    await asyncio.wait_for(started.wait(), 1)
    c.request_stop()
    stopped = c.snapshot()
    assert stopped["reconciliation"]["active"] is False
    assert stopped["reconciliation"]["probe_pending"] is False
    assert stopped["polling"]["observed_digest"] is None
    _safe(stopped)
    release.set()
    await asyncio.wait_for(c.stop(), 1)
    assert c.snapshot()["reconciliation"]["plan"]["candidate_signature"] is None
    assert c._poller._task is None
    await c.close()


@pytest.mark.asyncio
async def test_reconfigure_revokes_before_await_then_joins_and_restarts_with_new_credit():
    async with fake_voicevox() as (port, requests, _):
        c = _coordinator(port)
        await c.start()
        first = await _wait(c, ObservationPhase.CANDIDATE_UNQUALIFIED.value)
        old_gen = first["reconciliation"]["plan"]["generation_counter"]
        old_poller = c._poller
        new_cfg = _config(port, credit="Other synthetic credit")
        intent = c.request_reconfigure(config=new_cfg, polling=_poll(port))
        revoked = c.snapshot()
        assert revoked["reconciliation"]["plan"]["candidate_signature"] is None
        assert revoked["reconciliation"]["plan"]["retirement_pending"] is True
        _safe(revoked)
        await c._apply_reconfiguration(intent)
        new = await _wait(
            c, ObservationPhase.CANDIDATE_UNQUALIFIED.value, generation=old_gen + 1
        )
        assert c._poller is not old_poller
        assert old_poller._task is None
        assert new["reconciliation"]["plan"]["retirement_pending"]
        assert "Other synthetic credit" not in c.to_json()
        assert requests.count(b"GET /version HTTP/1.1") >= 2
        await c.close()


@pytest.mark.asyncio
async def test_reconfigure_to_revoked_policy_keeps_no_task_or_candidate():
    async with fake_voicevox() as (port, _, _):
        c = _coordinator(port)
        await c.start()
        await _wait(c, ObservationPhase.CANDIDATE_UNQUALIFIED.value)
        await c.reconfigure(
            config=_config(port, policy=OperatorPolicyStatus.REVOKED),
            polling=_poll(port),
        )
        now = c.snapshot()
        assert now["reconciliation"]["plan"]["candidate_signature"] is None
        assert now["reconciliation"]["plan"]["retirement_pending"] is True
        assert c._poller._task is None
        _safe(now)
        await c.close()


@pytest.mark.asyncio
async def test_fatal_sleep_failure_withdraws_advisory_candidate():
    c = _coordinator(50021)

    async def scripted():
        return _obs(50021)

    async def fail_sleep(_seconds):
        raise RuntimeError(_SECRET)

    c._poller._probe.probe_once = scripted
    c._poller._sleep_interval = fail_sleep
    await c.start()
    for _ in range(200):
        if c._poller._task is not None and c._poller._task.done():
            break
        await asyncio.sleep(0.005)
    state = c.snapshot()
    assert state["reconciliation"]["plan"]["candidate_signature"] is None
    assert state["reconciliation"]["plan"]["retirement_pending"] is True
    assert state["polling"]["status"] == "failed"
    _safe(state)
    await c.close()


@pytest.mark.asyncio
async def test_close_terminal_idempotent_and_no_native_signals(monkeypatch):
    def fail(*_args, **_kwargs):
        pytest.fail("external VOICEVOX process must never be signalled")

    monkeypatch.setattr(os, "kill", fail)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail)
    c = _coordinator(50021)
    await c.start()
    c.request_close()
    _safe(c.snapshot())
    await c.close()
    await c.close()
    with pytest.raises(ExternalVoicevoxObservationError):
        await c.start()
    with pytest.raises(ExternalVoicevoxObservationError):
        c.request_reconfigure(config=_config(50021), polling=_poll(50021))


@pytest.mark.asyncio
async def test_reconfigure_mismatched_endpoint_is_redacted_and_does_not_stop_original():
    c = _coordinator(50021)
    with pytest.raises(ExternalVoicevoxObservationError) as exc:
        c.request_reconfigure(config=_config(50021), polling=_poll(50022))
    assert _SECRET not in str(exc.value)
    assert c._intent == 0
    await c.close()


@pytest.mark.asyncio
async def test_overflow_fails_closed_and_never_mints_worker_epoch():
    c = _coordinator(50021)
    c._poller._sequence = 2**63 - 1
    await c.start()
    for _ in range(200):
        if c._poller._task is not None and c._poller._task.done():
            break
        await asyncio.sleep(0.005)
    state = c.snapshot()
    assert state["reconciliation"]["active"] is False
    assert state["polling"]["status"] == "failed"
    _safe(state)
    await c.close()


@pytest.mark.asyncio
async def test_invalid_native_result_never_leaks_or_claims_positive():
    c = _coordinator(50021)

    async def forged():
        return object()

    c._poller._probe.probe_once = forged
    await c.start()
    state = await _wait(c, ObservationPhase.REVIEW_BLOCKED.value)
    assert state["reconciliation"]["plan"]["candidate_signature"] is None
    assert _SECRET not in c.to_json()
    _safe(state)
    await c.close()


@pytest.mark.asyncio
async def test_same_catalog_repeated_samples_do_not_churn_candidate_generation():
    port = 50021  # private synthetic R1 probe; no engine
    c = _coordinator(port)
    release: asyncio.Queue[None] = asyncio.Queue()

    async def repeat():
        return _obs(port)

    async def step(_seconds):
        await release.get()

    c._poller._probe.probe_once = repeat
    c._poller._sleep_interval = step
    try:
        await c.start()
        await _wait(c, ObservationPhase.CANDIDATE_UNQUALIFIED.value, generation=1)
        release.put_nowait(None)
        for _ in range(200):
            state = c.snapshot()
            if state["reconciliation"]["plan"]["last_sequence"] >= 2:
                break
            await asyncio.sleep(0.005)
        assert state["reconciliation"]["plan"]["generation_counter"] == 1
        assert state["last_transition"]["requests"] == []
        assert state["last_transition"]["reason"] == "candidate_unchanged"
        _safe(state)
    finally:
        await c.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["style", "version"])
async def test_catalog_style_or_version_drift_withdraws_without_operator_repin(kind):
    port = 50021  # only a private synthetic R1 probe
    c = _coordinator(port)
    release: asyncio.Queue[None] = asyncio.Queue()
    calls = 0

    async def scripted():
        nonlocal calls
        calls += 1
        if calls < 2:
            return _obs(port)
        if kind == "style":
            return _obs(port, style=9)
        return _obs(port, version="0.16.0")

    async def step(_seconds):
        await release.get()

    c._poller._probe.probe_once = scripted
    c._poller._sleep_interval = step
    try:
        await c.start()
        await _wait(c, ObservationPhase.CANDIDATE_UNQUALIFIED.value)
        release.put_nowait(None)
        drift = await _wait(c, ObservationPhase.OBSERVATION_CHANGED.value)
        assert drift["reconciliation"]["plan"]["generation_counter"] == 1
        assert drift["reconciliation"]["plan"]["candidate_signature"] is None
        assert drift["reconciliation"]["plan"]["retirement_pending"] is True
        assert drift["last_transition"]["requests"] == [
            "withdraw_prior_generation_requested"
        ]
        release.put_nowait(None)
        for _ in range(200):
            state = c.snapshot()
            if state["reconciliation"]["plan"]["last_sequence"] >= 3:
                break
            await asyncio.sleep(0.005)
        assert state["last_transition"]["requests"] == []
        assert state["reconciliation"]["plan"]["retirement_pending"]
        _safe(state)
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_stop_after_reconfigure_wins_and_never_spawns_a_second_live_poller():
    c = _coordinator(50021)

    async def valid():
        return _obs(50021)

    c._poller._probe.probe_once = valid
    await c.start()
    await _wait(c, ObservationPhase.CANDIDATE_UNQUALIFIED.value)
    prior = c._poller
    task = asyncio.create_task(
        c.reconfigure(
            config=_config(50021, credit="replacement credit"), polling=_poll(50021)
        )
    )
    await asyncio.sleep(0)
    c.request_stop()
    await asyncio.wait_for(task, 1.5)
    await asyncio.wait_for(c.stop(), 1.5)
    state = c.snapshot()
    assert not state["reconciliation"]["active"]
    assert state["reconciliation"]["plan"]["candidate_signature"] is None
    assert state["reconciliation"]["plan"]["retirement_pending"] is True
    assert prior._task is None
    assert c._poller._task is None
    _safe(state)
    await c.close()


@pytest.mark.asyncio
async def test_close_racing_reconfiguration_is_terminal_without_orphan_polling():
    c = _coordinator(50021)
    started, release = asyncio.Event(), asyncio.Event()

    async def slow():
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return _obs(50021)

    c._poller._probe.probe_once = slow
    await c.start()
    await asyncio.wait_for(started.wait(), 1)
    task = asyncio.create_task(
        c.reconfigure(
            config=_config(50021, policy_revision="sha256:" + "b" * 64),
            polling=_poll(50021),
        )
    )
    await asyncio.sleep(0)
    c.request_close()
    release.set()
    await asyncio.wait_for(task, 1.5)
    await asyncio.wait_for(c.close(), 1.5)
    state = c.snapshot()
    assert state["reconciliation"]["closed"] is True
    assert state["reconciliation"]["active"] is False
    assert state["reconciliation"]["plan"]["candidate_signature"] is None
    assert c._poller._task is None
    _safe(state)


@pytest.mark.asyncio
async def test_unhandled_reducer_handoff_error_becomes_terminal_blocked():
    c = _coordinator(50021)

    async def valid():
        return _obs(50021)

    c._poller._probe.probe_once = valid

    def fail(*_args):
        raise RuntimeError(_SECRET)

    c._sample_completed = fail
    await c.start()
    for _ in range(200):
        if c._poller._task is not None and c._poller._task.done():
            break
        await asyncio.sleep(0.005)
    state = c.snapshot()
    assert state["reconciliation"]["active"] is False
    assert state["reconciliation"]["plan"]["candidate_signature"] is None
    assert state["polling"]["status"] == "failed"
    assert _SECRET not in c.to_json()
    await c.close()


@pytest.mark.asyncio
async def test_cancelled_reconfigure_stop_and_restart_uses_only_new_operator_endpoint():
    """A cancellation inside old.stop must not resurrect the old endpoint."""
    old_port, new_port = 50021, 50022  # private synthetic probes; zero HTTP
    c = _coordinator(old_port)
    old_poller = c._poller
    started, release = asyncio.Event(), asyncio.Event()

    async def cancellation_resistant_old_probe():
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return _obs(old_port)

    old_poller._probe.probe_once = cancellation_resistant_old_probe
    await c.start()
    await asyncio.wait_for(started.wait(), 1)
    task = asyncio.create_task(
        c.reconfigure(config=_config(new_port), polling=_poll(new_port))
    )
    for _ in range(200):
        if c._update_pending is not None and old_poller._stop_requested:
            break
        await asyncio.sleep(0.005)
    assert c._update_pending is not None
    assert c._state.config.discovery.endpoint == f"http://127.0.0.1:{new_port}"
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    # At this point previous code cleared the new pending config, so start()
    # could activate an obsolete old-port poller after stop().
    c.request_stop()
    stopped = c.snapshot()
    assert stopped["reconciliation"]["plan"]["candidate_signature"] is None
    await c.stop()
    assert old_poller._task is None
    assert c._poller is not old_poller
    assert c._poller._config.discovery.endpoint == f"http://127.0.0.1:{new_port}"
    assert c._state.config.discovery.endpoint == f"http://127.0.0.1:{new_port}"
    observed: list[int] = []

    async def new_probe_only():
        observed.append(new_port)
        return _obs(new_port)

    c._poller._probe.probe_once = new_probe_only
    await c.start()
    await _wait(c, ObservationPhase.CANDIDATE_UNQUALIFIED.value)
    assert observed == [new_port]
    assert c.snapshot()["reconciliation"]["config_revision"] == c._state.config.revision
    _safe(c.snapshot())
    await c.close()


@pytest.mark.asyncio
async def test_start_rejects_private_mismatched_poller_without_observing_old_endpoint():
    """Defensive start admission checks config/poller pairing after recovery."""
    c = _coordinator(50021)
    wrong = _config(50022)
    # Test-only reflective state change emulates an interrupted old integration.
    state = c._state
    object.__setattr__(state, "config", wrong)
    with pytest.raises(ExternalVoicevoxObservationError):
        await c.start()
    assert c._poller._task is None
    assert c.snapshot()["reconciliation"]["active"] is False
    await c.close()
