"""#215 B6b: real disposable loopback HTTP wire, fake VOICEVOX JSON/WAV only."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlsplit

import pytest
from test_audio_input_channel import WAV
from test_voicevox_provider import (
    UUID,
    FakeMedia,
    job,
)
from test_voicevox_provider import (
    config as pinned_adapter,
)

from astrumweaver.execution import JobExecutionError
from astrumweaver.runtime.providers.voicevox import VoicevoxExecutor
from astrumweaver.runtime.providers.voicevox_http import (
    VoicevoxHttpConfig,
    VoicevoxHttpError,
    VoicevoxLoopbackHttpApi,
)


class SyntheticEngine:
    def __init__(self, *, mode: str = "ok") -> None:
        self.mode = mode
        self.seen: list[tuple[str, str, dict[str, str], bytes]] = []
        self.hit = asyncio.Event()
        self.resume = asyncio.Event()
        self.closed = False

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            parts = head.decode("ascii").split("\r\n")
            method, target, _http_version = parts[0].split(" ")
            headers: dict[str, str] = {}
            for line in parts[1:]:
                if not line:
                    continue
                k, v = line.split(":", 1)
                headers[k.lower()] = v.strip()
            body = await reader.readexactly(int(headers.get("content-length", "0")))
            self.seen.append((method, target, headers, body))
            self.hit.set()
            if self.mode == "hang":
                await self.resume.wait()
            parsed = urlsplit(target)
            if parsed.path == "/version":
                content = json.dumps(
                    "wrong-version"
                    if self.mode == "wrong_version"
                    else "test-version.1"
                ).encode()
                media = "application/json"
            elif parsed.path == "/speakers":
                content = json.dumps(
                    [
                        {
                            "speaker_uuid": (
                                "c3b5b742-0000-4000-a000-000000000002"
                                if self.mode == "wrong_speaker"
                                else UUID
                            ),
                            "styles": [{"id": 3, "name": "synthetic"}],
                        }
                    ],
                ).encode()
                media = "application/json"
            elif parsed.path == "/audio_query":
                content = json.dumps(
                    {
                        "accent_phrases": [],
                        "outputSamplingRate": 24000,
                        "outputStereo": False,
                    }
                ).encode()
                media = "application/json"
            elif parsed.path == "/synthesis":
                content = WAV if self.mode != "bad_wav" else b"bad-voice-content"
                media = "audio/wav"
            else:
                content = b"not found"
                media = "text/plain"
            if self.mode == "duplicate_json" and parsed.path == "/version":
                content = b'{"x":1,"x":2}'
            if self.mode == "nan_json" and parsed.path == "/version":
                content = b'{"x":NaN}'
            if self.mode == "response_overlimit" and parsed.path == "/version":
                content = b"v" * 600
            if self.mode == "no_length_overlimit" and parsed.path == "/version":
                content = b"v" * 600
            if self.mode == "status" and parsed.path == "/version":
                code = "503 Unavailable"
            elif self.mode == "redirect" and parsed.path == "/version":
                code = "302 Redirect"
            else:
                code = "200 OK"
            if self.mode == "bad_type" and parsed.path == "/version":
                media = "text/html"
            lines = [
                f"HTTP/1.1 {code}",
                f"Content-Type: {media}",
                "Connection: close",
            ]
            if self.mode == "gzip" and parsed.path == "/version":
                lines.append("Content-Encoding: gzip")
            if self.mode == "duplicate_length" and parsed.path == "/version":
                lines += [f"Content-Length: {len(content)}"] * 2
            elif (
                self.mode == "chunked"
                and parsed.path == "/synthesis"
                or self.mode == "bad_chunk"
                and parsed.path == "/synthesis"
            ):
                lines.append("Transfer-Encoding: chunked")
            elif self.mode not in ("without_length", "no_length_overlimit"):
                lines.append(f"Content-Length: {len(content)}")
            raw = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")
            if self.mode == "bad_chunk" and parsed.path == "/synthesis":
                raw += b"not-hex\r\nBAD\r\n0\r\n\r\n"
            elif self.mode == "huge_chunk" and parsed.path == "/synthesis":
                raw += b"ffffffff\r\n"
            elif self.mode == "chunked" and parsed.path == "/synthesis":
                first, second = content[:19], content[19:]
                raw += (
                    f"{len(first):x}\r\n".encode()
                    + first
                    + b"\r\n"
                    + f"{len(second):x}\r\n".encode()
                    + second
                    + b"\r\n0\r\n\r\n"
                )
            else:
                raw += content
            writer.write(raw)
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()


@asynccontextmanager
async def fake_engine(*, mode: str = "ok"):
    engine = SyntheticEngine(mode=mode)
    server = await asyncio.start_server(engine.handle, "127.0.0.1", 0)
    try:
        endpoint = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        yield engine, endpoint
    finally:
        engine.resume.set()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_real_loopback_synthetic_voicevox_wire_matches_adapter_and_one_artifact(
    monkeypatch,
):
    # No real engine or native model; this tests exact HTTP request framing.
    monkeypatch.setenv("HTTP_PROXY", "http://untrusted.invalid:9999")
    monkeypatch.setenv("HTTPS_PROXY", "https://untrusted.invalid:9999")
    async with fake_engine() as (server, endpoint):
        api = VoicevoxLoopbackHttpApi(
            VoicevoxHttpConfig(
                endpoint=endpoint,
                adapter=pinned_adapter(),
            )
        )
        assert await api.version() == "test-version.1"
        assert (await api.speakers())[0]["speaker_uuid"] == UUID
        assert (
            json.loads(await api.bounded_json(path="/version", max_bytes=256))
            == "test-version.1"
        )
        media = FakeMedia()
        result = await VoicevoxExecutor(config=pinned_adapter(), api=api).execute_audio(
            job(text="秘密の日本語の音声"),
            media,
        )
        assert result.text is None and len(result.artifacts) == 1
        assert media.calls == [(WAV, pinned_adapter().max_output_bytes)]
        path_methods = [
            (method, urlsplit(target).path) for method, target, _, _ in server.seen
        ]
        assert ("POST", "/audio_query") in path_methods
        assert ("POST", "/synthesis") in path_methods
        _, target, headers, _ = next(
            x for x in server.seen if urlsplit(x[1]).path == "/audio_query"
        )
        params = parse_qs(urlsplit(target).query)
        assert params == {"speaker": ["3"], "text": ["秘密の日本語の音声"]}
        assert headers["host"].startswith("127.0.0.1:")
        assert "authorization" not in headers
        assert "cookie" not in headers
        _, target, headers, posted = next(
            x for x in server.seen if urlsplit(x[1]).path == "/synthesis"
        )
        assert parse_qs(urlsplit(target).query) == {"speaker": ["3"]}
        assert headers["content-type"] == "application/json"
        assert json.loads(posted)["accent_phrases"] == []
        assert "秘密の日本語の音声".encode() not in posted
        await api.close()
        await api.close()
        with pytest.raises(VoicevoxHttpError):
            await api.version()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    [
        "status",
        "redirect",
        "bad_type",
        "gzip",
        "duplicate_length",
        "duplicate_json",
        "nan_json",
        "response_overlimit",
        "no_length_overlimit",
    ],
)
async def test_bad_probe_uses_only_fixed_error_and_never_follows_redirect(mode):
    async with fake_engine(mode=mode) as (server, endpoint):
        api = VoicevoxLoopbackHttpApi(
            VoicevoxHttpConfig(
                endpoint=endpoint,
                adapter=pinned_adapter(),
            )
        )
        with pytest.raises(VoicevoxHttpError) as caught:
            await api.version()
        assert str(caught.value) == "VOICEVOX loopback HTTP transport unavailable"
        assert len(server.seen) == 1
        await api.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", ["wrong_version", "wrong_speaker", "bad_wav", "bad_chunk"]
)
async def test_adapter_rejects_fake_engine_drift_or_bad_audio_before_stage(mode):
    async with fake_engine(mode=mode) as (_server, endpoint):
        api = VoicevoxLoopbackHttpApi(
            VoicevoxHttpConfig(
                endpoint=endpoint,
                adapter=pinned_adapter(),
            )
        )
        media = FakeMedia()
        with pytest.raises(JobExecutionError) as caught:
            await VoicevoxExecutor(config=pinned_adapter(), api=api).execute_audio(
                job(text="private-source-secret"), media
            )
        assert caught.value.retryable is False
        assert not media.calls
        assert "private-source-secret" not in str(caught.value)
        await api.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["chunked", "without_length"])
async def test_streamed_or_eof_delimited_wav_is_bounded_and_staged_once(mode):
    async with fake_engine(mode=mode) as (_server, endpoint):
        api = VoicevoxLoopbackHttpApi(
            VoicevoxHttpConfig(
                endpoint=endpoint,
                adapter=pinned_adapter(),
            )
        )
        media = FakeMedia()
        await VoicevoxExecutor(config=pinned_adapter(), api=api).execute_audio(
            job(),
            media,
        )
        assert len(media.calls) == 1
        await api.close()


@pytest.mark.asyncio
async def test_timeout_and_cancellation_revoke_no_automatic_retry():
    async with fake_engine(mode="hang") as (server, endpoint):
        api = VoicevoxLoopbackHttpApi(
            VoicevoxHttpConfig(
                endpoint=endpoint,
                adapter=pinned_adapter(),
                request_timeout_seconds=0.05,
            )
        )
        with pytest.raises(VoicevoxHttpError):
            await api.version()
        assert len(server.seen) == 1  # No hidden retry after timeout.
        await api.close()

    async with fake_engine(mode="hang") as (server, endpoint):
        api = VoicevoxLoopbackHttpApi(
            VoicevoxHttpConfig(
                endpoint=endpoint,
                adapter=pinned_adapter(),
                request_timeout_seconds=1,
            )
        )
        task = asyncio.create_task(api.version())
        await asyncio.wait_for(server.hit.wait(), 0.5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(server.seen) == 1
        await api.close()


@pytest.mark.asyncio
async def test_bounded_json_rejects_unreviewed_url_or_size_without_network():
    async with fake_engine() as (server, endpoint):
        api = VoicevoxLoopbackHttpApi(
            VoicevoxHttpConfig(
                endpoint=endpoint,
                adapter=pinned_adapter(),
            )
        )
        for path, maximum in (
            ("/settings", 10),
            ("/version", 257),
            ("/speakers", 300000),
        ):
            with pytest.raises(VoicevoxHttpError):
                await api.bounded_json(path=path, max_bytes=maximum)
        with pytest.raises(VoicevoxHttpError):
            await api.audio_query(text="a" * 1025, speaker=3)
        for bad in (True, -1, 2**31):
            with pytest.raises(VoicevoxHttpError):
                await api.audio_query(text="日本語", speaker=bad)
        with pytest.raises(VoicevoxHttpError):
            await api.synthesis(query={"raw": "private"}, speaker=3)
        assert not server.seen
        await api.close()


@pytest.mark.parametrize(
    "bad_endpoint",
    [
        "http://localhost:50021",
        "http://0.0.0.0:50021",
        "https://127.0.0.1:50021",
        "http://127.0.0.1:50021/path",
        "http://127.0.0.1:50021/?text=private",
        "http://user:pass@127.0.0.1:50021",
        "http://127.0.0.1:bad",
        "http://127.0.0.1:80",
    ],
)
def test_only_exact_ipv4_loopback_endpoint_permitted(bad_endpoint):
    with pytest.raises(ValueError, match="configuration is invalid"):
        VoicevoxHttpConfig(endpoint=bad_endpoint, adapter=pinned_adapter())


@pytest.mark.asyncio
async def test_query_logging_not_emitted_by_transport(caplog):
    caplog.set_level("DEBUG")
    phrase = "個人情報の検証用文字列"
    async with fake_engine() as (_server, endpoint):
        api = VoicevoxLoopbackHttpApi(
            VoicevoxHttpConfig(
                endpoint=endpoint,
                adapter=pinned_adapter(),
            )
        )
        value = await api.audio_query(text=phrase, speaker=3)
        assert value["accent_phrases"] == []
        assert phrase not in caplog.text
        assert phrase.encode("utf-8").hex() not in caplog.text
        await api.close()


@pytest.mark.asyncio
async def test_request_line_limit_fails_before_any_network_transfer():
    async with fake_engine() as (server, endpoint):
        # Native text URL-encoding has a much larger wire footprint than the
        # character count. Reject rather than log/truncate or retry.
        config = pinned_adapter(max_text_bytes=8192)
        api = VoicevoxLoopbackHttpApi(
            VoicevoxHttpConfig(
                endpoint=endpoint,
                adapter=config,
            )
        )
        with pytest.raises(VoicevoxHttpError):
            await api.audio_query(text="あ" * 4000, speaker=3)
        assert not server.seen
        await api.close()


@pytest.mark.asyncio
async def test_synthetic_owned_runtime_with_real_tcp_transport_and_fake_process(
    tmp_path,
):
    from dataclasses import replace

    from test_voicevox_managed_runtime import FakeProcess, make_spec

    from astrumweaver.runtime.contracts import RuntimeHealthState
    from astrumweaver.runtime.providers.voicevox_managed import (
        VoicevoxOwnedManagedRuntime,
    )

    # The process owner here is deliberately FAKE: a live arbitrary server is
    # not proof of actual executable/port ownership. Tests wire composition,
    # not native VOICEVOX readiness or secure deployment.
    async with fake_engine() as (server, endpoint):
        spec = replace(make_spec(tmp_path), endpoint=endpoint)
        api = VoicevoxLoopbackHttpApi(
            VoicevoxHttpConfig(endpoint=endpoint, adapter=spec.adapter),
        )
        owned = VoicevoxOwnedManagedRuntime(
            config=spec,
            process=FakeProcess(),
            engine=api,
        )
        assert (await owned.health()).state is RuntimeHealthState.STOPPED
        await owned.start()
        assert (await owned.health()).ready is True
        result = await owned.executor().execute_audio(job(), FakeMedia())
        assert len(result.artifacts) == 1
        assert len(server.seen) >= 5
        await owned.release()
        with pytest.raises(VoicevoxHttpError):
            await api.version()
