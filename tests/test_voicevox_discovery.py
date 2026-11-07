"""#247 R1: synthetic, local-only VOICEVOX discovery; no native engine used."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from contextlib import asynccontextmanager
from uuid import UUID

import pytest

from astrumweaver.runtime.providers.voicevox_discovery import (
    VoicevoxDiscoveryConfig,
    VoicevoxDiscoveryProbe,
    VoicevoxDiscoveryStatus,
)


def _speaker(uuid: str = "7ffcb7ce-00ec-4bdc-82cd-45a8889e43ff", style: int = 8):
    return {
        "name": "test-only",
        "speaker_uuid": uuid,
        "styles": [{"name": "test", "id": style, "type": "talk"}],
        "version": "1.0",
    }


def _good_reply(path: str) -> bytes:
    if path == "/version":
        return json.dumps("0.15.0").encode("utf-8")
    if path == "/speakers":
        return json.dumps([_speaker()]).encode("utf-8")
    raise AssertionError("probe requested an unexpected path")


@asynccontextmanager
async def fake_engine(
    reply: Callable[[str], bytes] = _good_reply,
    *,
    status: int = 200,
    content_type: str = "application/json",
    extra_headers: bytes = b"",
):
    """Disposable 127.0.0.1 HTTP server; no VOICEVOX process or voice data."""
    seen: list[bytes] = []
    tasks: set[asyncio.Task] = set()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        task = asyncio.current_task()
        tasks.add(task)
        try:
            request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=1)
            line = request.split(b"\r\n", 1)[0]
            seen.append(line)
            method, target, proto = line.split(b" ")
            assert method == b"GET" and proto == b"HTTP/1.1"
            body = reply(target.decode("ascii"))
            writer.write(
                f"HTTP/1.1 {status} TEST\r\n"
                f"Content-Type: {content_type}\r\n"
                f"Content-Length: {len(body)}\r\n".encode("ascii")
                + extra_headers
                + b"Connection: close\r\n\r\n"
                + body
            )
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            tasks.discard(task)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield port, seen
    finally:
        server.close()
        await server.wait_closed()
        for task in tuple(tasks):
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _probe(port: int, *, timeout: float = 1.0):
    cfg = VoicevoxDiscoveryConfig(
        enabled=True, endpoint=f"http://127.0.0.1:{port}", timeout_seconds=timeout
    )
    return VoicevoxDiscoveryProbe(cfg)


@pytest.mark.asyncio
async def test_disabled_mode_never_reads_candidate_or_opens_socket(monkeypatch):
    async def forbidden(*_args, **_kwargs):
        pytest.fail("disabled discovery must not open any network connection")

    monkeypatch.setattr(asyncio, "open_connection", forbidden)
    # No inspection of invalid endpoint when feature is disabled.
    probe = VoicevoxDiscoveryProbe(
        VoicevoxDiscoveryConfig(enabled=False, endpoint="not a URL")
    )
    result = await probe.probe_once()
    assert result.status is VoicevoxDiscoveryStatus.DISABLED
    assert result.snapshot is None and result.can_apply is False
    assert result.ready is False


@pytest.mark.asyncio
async def test_valid_local_engine_is_observed_but_never_automatically_admitted():
    async with fake_engine() as (port, requests):
        probe = _probe(port)
        result = await probe.probe_once()
        assert result.status is VoicevoxDiscoveryStatus.COMPATIBLE
        assert result.reason == "observed_compatible"
        assert result.ready is False and result.can_apply is False
        assert result.snapshot is not None
        assert result.snapshot.version == "0.15.0"
        assert tuple((s.speaker_uuid, s.style_id) for s in result.snapshot.styles) == (
            ("7ffcb7ce-00ec-4bdc-82cd-45a8889e43ff", 8),
        )
        assert result.snapshot.observed_digest.startswith("sha256:")
        assert requests == [b"GET /version HTTP/1.1", b"GET /speakers HTTP/1.1"]
        second = await probe.probe_once()
        assert second.snapshot == result.snapshot
    unavailable = await probe.probe_once()
    assert unavailable.status is VoicevoxDiscoveryStatus.ABSENT
    assert unavailable.snapshot is None and not unavailable.can_apply


@pytest.mark.asyncio
async def test_same_engine_catalog_different_order_has_identical_observed_digest():
    speaker_b = _speaker("45c52ce2-2a0f-4fdd-8c38-648a8e44ee71", 17)
    speaker_a = _speaker()
    iterations = 0

    def reply(path):
        nonlocal iterations
        if path == "/version":
            return json.dumps("0.15.0").encode()
        iterations += 1
        voices = [speaker_a, speaker_b] if iterations % 2 else [speaker_b, speaker_a]
        return json.dumps(voices).encode()

    async with fake_engine(reply) as (port, _requests):
        probe = _probe(port)
        first = await probe.probe_once()
        second = await probe.probe_once()
        assert first.snapshot is not None and second.snapshot is not None
        assert first.snapshot == second.snapshot
        assert [s.style_id for s in first.snapshot.styles] == [17, 8]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        b"[]",
        b"{}",
        b'{"speaker_uuid":"7ffcb7ce-00ec-4bdc-82cd-45a8889e43ff"}',
        b'[{"speaker_uuid":"nope","styles":[{"id":8}]}]',
        json.dumps([_speaker(style=-1)]).encode(),
        json.dumps([_speaker(style=True)]).encode(),
        json.dumps([_speaker(style=2**31)]).encode(),
        json.dumps([_speaker(), _speaker(style=11)]).encode(),
        json.dumps(
            [_speaker(), _speaker("45c52ce2-2a0f-4fdd-8c38-648a8e44ee71", 8)]
        ).encode(),
        json.dumps([_speaker() | {"styles": []}]).encode(),
        json.dumps([_speaker() | {"styles": [{"id": 8}, {"id": 8}]}]).encode(),
        json.dumps([_speaker() | {"styles": [{"id": 8, "type": "sing"}]}]).encode(),
        (
            b'[{"speaker_uuid":"7ffcb7ce-00ec-4bdc-82cd-45a8889e43ff",'
            b'"styles":[{"id":8,"id":9}]}]'
        ),
        b"not json",
        b"\xff\xfe",
    ],
)
async def test_malformed_or_ambiguous_speakers_never_qualify(payload):
    async with fake_engine(
        lambda path: payload if path == "/speakers" else b'"0.15.0"'
    ) as (port, _):
        result = await _probe(port).probe_once()
        assert result.status is VoicevoxDiscoveryStatus.INCOMPATIBLE
        assert result.snapshot is None and not result.can_apply


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "version",
    [b"null", b"23", b'"bad version"', b'""', b'{"x":1}', b'"0.15.0" "extra"'],
)
async def test_invalid_version_is_not_compatible(version):
    async with fake_engine(lambda path: version if path == "/version" else b"[]") as (
        port,
        requests,
    ):
        result = await _probe(port).probe_once()
        assert result.status is VoicevoxDiscoveryStatus.INCOMPATIBLE
        assert result.snapshot is None
        assert requests == [b"GET /version HTTP/1.1"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "content_type", "headers"),
    [
        (302, "application/json", b"Location: http://example.invalid/\r\n"),
        (500, "application/json", b""),
        (200, "text/plain", b""),
        (200, "application/json", b"Content-Encoding: gzip\r\n"),
        (200, "application/json", b"Content-Length: 2\r\n"),
    ],
)
async def test_http_errors_redirects_ambiguous_headers_never_qualify(
    status, content_type, headers
):
    async with fake_engine(
        status=status, content_type=content_type, extra_headers=headers
    ) as (port, _):
        result = await _probe(port).probe_once()
        assert result.status is VoicevoxDiscoveryStatus.INCOMPATIBLE
        assert result.snapshot is None


@pytest.mark.asyncio
async def test_oversized_speaker_response_does_not_qualify():
    async with fake_engine(
        lambda path: b"[]" + b" " * (256 * 1024) if path == "/speakers" else b'"0.15.0"'
    ) as (port, _):
        result = await _probe(port).probe_once()
        assert result.status is VoicevoxDiscoveryStatus.INCOMPATIBLE


@pytest.mark.asyncio
async def test_bounded_deadline_and_cancellation_leave_no_admission():
    hanging: set[asyncio.Task] = set()
    entered = asyncio.Event()
    blocker = asyncio.Event()

    async def handler(reader, writer):
        t = asyncio.current_task()
        hanging.add(t)
        try:
            await reader.readuntil(b"\r\n\r\n")
            entered.set()
            await blocker.wait()
        finally:
            hanging.discard(t)
            writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        timeout = await _probe(port, timeout=0.1).probe_once()
        assert timeout.status is VoicevoxDiscoveryStatus.INCOMPATIBLE
        assert timeout.reason == "probe_timeout"
        task = asyncio.create_task(_probe(port, timeout=1.0).probe_once())
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
    finally:
        blocker.set()
        server.close()
        await server.wait_closed()
        for task in tuple(hanging):
            task.cancel()
        await asyncio.gather(*hanging, return_exceptions=True)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://localhost:50021",
        "http://0.0.0.0:50021",
        "http://[::1]:50021",
        "http://127.0.0.2:50021",
        "http://127.0.0.1:80",
        "https://127.0.0.1:50021",
        "http://127.0.0.1:50021/",
        "http://127.0.0.1:50021/speakers",
        "http://127.0.0.1:50021?x=1",
        "http://foo@127.0.0.1:50021",
        "http://127.0.0.1:050021",
        "http://169.254.1.1:50021",
    ],
)
def test_noncanonical_endpoints_are_rejected(endpoint):
    with pytest.raises(ValueError, match="VOICEVOX discovery config is invalid"):
        VoicevoxDiscoveryConfig(enabled=True, endpoint=endpoint)


def test_invalid_config_cannot_opt_in_implicitly():
    for enabled in (None, 1, "true"):
        with pytest.raises(ValueError):
            VoicevoxDiscoveryConfig(enabled=enabled)
    for timeout in (0, -1, float("nan"), float("inf"), True):
        with pytest.raises(ValueError):
            VoicevoxDiscoveryConfig(enabled=True, timeout_seconds=timeout)
    # Verify UUID test data are canonical, not a fabricated engine identity.
    assert (
        str(UUID("7ffcb7ce-00ec-4bdc-82cd-45a8889e43ff"))
        == "7ffcb7ce-00ec-4bdc-82cd-45a8889e43ff"
    )


@pytest.mark.asyncio
async def test_concurrent_probes_are_serialized_within_one_probe_instance(monkeypatch):
    """Slow discovery must not open a second overlapping HTTP inspection."""
    probe = _probe(50022)
    entered = asyncio.Event()
    release = asyncio.Event()
    active = 0
    maximum = 0

    async def controlled_json(target: str, *, max_bytes: int):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        try:
            entered.set()
            await release.wait()
            return "0.15.0" if target == "/version" else [_speaker()]
        finally:
            active -= 1

    monkeypatch.setattr(probe, "_get_json", controlled_json)
    first = asyncio.create_task(probe.probe_once())
    second = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        second = asyncio.create_task(probe.probe_once())
        await asyncio.sleep(0)
        assert maximum == 1, "multiple discovery sessions inspected concurrently"
        release.set()
        results = await asyncio.wait_for(asyncio.gather(first, second), timeout=1)
        assert all(r.status is VoicevoxDiscoveryStatus.COMPATIBLE for r in results)
        assert maximum == 1
    finally:
        release.set()
        if second is not None:
            await asyncio.gather(first, second, return_exceptions=True)
        else:
            await asyncio.gather(first, return_exceptions=True)


@pytest.mark.asyncio
async def test_waiting_probe_has_same_total_deadline_as_active_probe(monkeypatch):
    """The lock queue is part of each probe's bounded budget."""
    probe = _probe(50022, timeout=0.08)
    gate = asyncio.Event()
    started = asyncio.Event()

    async def slow_json(target: str, *, max_bytes: int):
        started.set()
        await gate.wait()
        return "0.15.0" if target == "/version" else [_speaker()]

    monkeypatch.setattr(probe, "_get_json", slow_json)
    one = asyncio.create_task(probe.probe_once())
    try:
        await asyncio.wait_for(started.wait(), 1)
        two = asyncio.create_task(probe.probe_once())
        result_one, result_two = await asyncio.wait_for(
            asyncio.gather(one, two), timeout=0.5
        )
        assert result_one.reason == result_two.reason == "probe_timeout"
    finally:
        gate.set()
        await asyncio.gather(one, return_exceptions=True)


@pytest.mark.parametrize(
    "timeout",
    [
        10**1000,
        -(10**1000),
        2**4096,
        -(2**4096),
        float("inf"),
        float("-inf"),
        float("nan"),
        0,
        -1,
        6,
        True,
    ],
)
def test_extreme_timeout_values_are_rejected_with_redacted_config_error(timeout):
    """Input size must never escape as OverflowError or expose its value."""
    with pytest.raises(
        ValueError, match=r"^VOICEVOX discovery config is invalid$"
    ) as caught:
        VoicevoxDiscoveryConfig(enabled=True, timeout_seconds=timeout)
    assert type(caught.value) is ValueError


@pytest.mark.parametrize("timeout", [1, 5, 0.01, 5.0])
def test_finite_timeout_boundaries_remain_valid(timeout):
    config = VoicevoxDiscoveryConfig(enabled=True, timeout_seconds=timeout)
    assert config.timeout_seconds == timeout


def test_disabled_discovery_does_not_evaluate_even_extreme_timeout():
    """Opt-out remains inert and noninspecting regardless of optional inputs."""
    probe = VoicevoxDiscoveryProbe(
        VoicevoxDiscoveryConfig(
            enabled=False, endpoint="not a URL", timeout_seconds=10**1000
        )
    )
    assert probe._config.enabled is False


@pytest.mark.asyncio
@pytest.mark.parametrize("style_type", ["talk", "streaming_talk", None])
async def test_compatible_talk_style_variants_are_observed(style_type):
    """Upstream treats omitted type as talk and streaming_talk as talk-capable."""
    speaker = _speaker()
    if style_type is None:
        del speaker["styles"][0]["type"]
    else:
        speaker["styles"][0]["type"] = style_type

    async with fake_engine(
        lambda path: (
            json.dumps([speaker]).encode("utf-8")
            if path == "/speakers"
            else b'"0.15.0"'
        )
    ) as (port, requests):
        observed = await _probe(port).probe_once()

    assert observed.status is VoicevoxDiscoveryStatus.COMPATIBLE
    assert observed.snapshot is not None
    assert [
        (entry.speaker_uuid, entry.style_id) for entry in observed.snapshot.styles
    ] == [("7ffcb7ce-00ec-4bdc-82cd-45a8889e43ff", 8)]
    assert observed.ready is False and observed.can_apply is False
    assert requests == [b"GET /version HTTP/1.1", b"GET /speakers HTTP/1.1"]


@pytest.mark.asyncio
async def test_mixed_talk_and_streaming_talk_styles_are_observed_together():
    """The official talk-character catalog may contain both talk styles."""
    speaker = _speaker()
    speaker["styles"].append({"name": "stream", "id": 17, "type": "streaming_talk"})
    async with fake_engine(
        lambda path: (
            json.dumps([speaker]).encode("utf-8")
            if path == "/speakers"
            else b'"0.15.0"'
        )
    ) as (port, _):
        observed = await _probe(port).probe_once()

    assert observed.status is VoicevoxDiscoveryStatus.COMPATIBLE
    assert observed.snapshot is not None
    assert {style.style_id for style in observed.snapshot.styles} == {8, 17}
    assert observed.ready is False and observed.can_apply is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "style_type", ["sing", "singing_teacher", "frame_decode", "other", None, "", 0]
)
async def test_singing_unknown_and_malformed_types_are_rejected(style_type):
    """No singing or invented style is interpreted as ordinary synthesis."""
    speaker = _speaker()
    speaker["styles"][0]["type"] = style_type
    async with fake_engine(
        lambda path: (
            json.dumps([speaker]).encode("utf-8")
            if path == "/speakers"
            else b'"0.15.0"'
        )
    ) as (port, _):
        observed = await _probe(port).probe_once()

    assert observed.status is VoicevoxDiscoveryStatus.INCOMPATIBLE
    assert observed.snapshot is None
    assert observed.ready is False and observed.can_apply is False


@pytest.mark.asyncio
async def test_mixed_talk_and_singing_catalog_remains_fail_closed():
    speaker = _speaker()
    speaker["styles"].append({"name": "sing", "id": 17, "type": "sing"})
    async with fake_engine(
        lambda path: (
            json.dumps([speaker]).encode("utf-8")
            if path == "/speakers"
            else b'"0.15.0"'
        )
    ) as (port, _):
        observed = await _probe(port).probe_once()

    assert observed.status is VoicevoxDiscoveryStatus.INCOMPATIBLE
    assert observed.snapshot is None and not observed.ready and not observed.can_apply
