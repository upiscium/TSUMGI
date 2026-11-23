"""#247 preview-only, real loopback wire to a *synthetic* VOICEVOX server.

No installed VOICEVOX, user audio, production Control, Worker, DB, activation,
server package management, or remote network access.
"""

from __future__ import annotations

import asyncio
import os

import httpx
import pytest

from astrumweaver.audio.voicevox_local_preview import (
    LocalVoicevoxPreviewConfig,
    create_local_voicevox_preview_app,
)
from test_audio_input_channel import WAV
from test_voicevox_http import fake_engine
from test_voicevox_provider import UUID

TOKEN = "synthetic-preview-0123456789abcdef-secure"


def config(source: str, **overrides) -> LocalVoicevoxPreviewConfig:
    values = dict(
        endpoint=source,
        speaker_uuid=UUID,
        style_id=3,
        bearer_token=TOKEN,
        enabled=True,
        accept_unverified_engine=True,
        accept_native_text_url_logging=True,
        timeout_seconds=3.0,
    )
    values.update(overrides)
    return LocalVoicevoxPreviewConfig(**values)


def client(app: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://preview.test",
        timeout=4.0,
    )


def auth() -> dict[str, str]:
    return {"authorization": "Bearer " + TOKEN}


@pytest.mark.asyncio
async def test_text_to_wav_over_actual_disposable_loopback_wire():
    async with fake_engine() as (engine, endpoint):
        app = create_local_voicevox_preview_app(config(endpoint))
        async with client(app) as user:
            response = await user.post(
                "/preview/audio/speech",
                json={"input": "こんにちは、テストです"},
                headers=auth(),
            )
        assert response.status_code == 200
        assert response.content == WAV
        assert response.headers["content-type"] == "audio/wav"
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-tsumgi-voicevox-preview-only"] == "true"
        paths = [row[1].split("?", 1)[0] for row in engine.seen]
        assert paths == ["/version", "/speakers", "/audio_query", "/synthesis"]
        assert engine.seen[2][0] == engine.seen[3][0] == "POST"
        assert all("Authorization" not in str(row) for row in engine.seen)


@pytest.mark.asyncio
async def test_voice_observation_never_advertises_control_readiness():
    async with fake_engine() as (engine, endpoint):
        app = create_local_voicevox_preview_app(config(endpoint))
        async with client(app) as user:
            response = await user.get("/preview/audio/voices", headers=auth())
        assert response.status_code == 200
        data = response.json()
        assert data["preview_only"] is True
        assert data["control_authoritative"] is False
        assert data["ready"] is False and data["rights_verified"] is False
        assert data["data"] == [{"speaker_id": UUID, "style_id": 3}]
        assert [x[1] for x in engine.seen] == ["/version", "/speakers"]


@pytest.mark.asyncio
@pytest.mark.parametrize("header", [None, "Bearer wrong", "Basic token"])
async def test_auth_failure_does_not_probe_engine_or_echo_token(header):
    async with fake_engine() as (engine, endpoint):
        app = create_local_voicevox_preview_app(config(endpoint))
        headers = {} if header is None else {"authorization": header}
        async with client(app) as user:
            response = await user.post(
                "/preview/audio/speech", json={"input": "秘密"},
                headers=headers,
            )
        assert response.status_code == 401
        assert engine.seen == []
        assert TOKEN not in response.text and "秘密" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("body,status", [
    (b'{"input":"hello","input":"attack"}', 400),
    (b'{"input":null}', 400),
    (b'{"input":" "}', 400),
    (b'{"input":"ok","voice":"unapproved"}', 400),
    (b'{"input":NaN}', 400),
    (b'{"input":"hello"', 400),
    (b'\xff\xfe', 400),
    (b'{"input":"hello"}' + b" " * 4100, 413),
])
async def test_bad_requests_never_contact_engine(body, status):
    async with fake_engine() as (engine, endpoint):
        app = create_local_voicevox_preview_app(config(endpoint))
        async with client(app) as user:
            response = await user.post(
                "/preview/audio/speech",
                content=body,
                headers=auth() | {"content-type": "application/json"},
            )
        assert response.status_code == status
        assert engine.seen == []
        assert TOKEN not in response.text and "hello" not in response.text


@pytest.mark.asyncio
async def test_missing_approved_voice_never_posts_private_text():
    async with fake_engine(mode="wrong_speaker") as (engine, endpoint):
        app = create_local_voicevox_preview_app(config(endpoint))
        async with client(app) as user:
            result = await user.post(
                "/preview/audio/speech",
                json={"input": "do-not-send-to-unapproved-speaker"},
                headers=auth(),
            )
        assert result.status_code == 503
        assert [x[1] for x in engine.seen] == ["/version", "/speakers"]
        assert "do-not-send" not in result.text


@pytest.mark.asyncio
async def test_broken_wav_is_never_forwarded_as_audio():
    async with fake_engine(mode="bad_wav") as (_engine, endpoint):
        app = create_local_voicevox_preview_app(config(endpoint))
        async with client(app) as user:
            response = await user.post(
                "/preview/audio/speech", json={"input": "audio"},
                headers=auth(),
            )
        assert response.status_code == 503
        assert response.headers["content-type"] == "application/json"
        assert b"bad-voice-content" not in response.content


@pytest.mark.asyncio
async def test_absent_or_unresponsive_engine_fails_closed():
    async with fake_engine(mode="hang") as (engine, endpoint):
        app = create_local_voicevox_preview_app(
            config(endpoint, timeout_seconds=1.0)
        )
        async with client(app) as user:
            response = await user.post(
                "/preview/audio/speech", json={"input": "private-timeout-speech-phrase"},
                headers=auth(),
            )
        assert response.status_code in (503, 504)
        assert b"private-timeout-speech-phrase" not in response.content
        assert engine.seen


def test_default_disabled_and_no_silent_operator_consent():
    endpoint = "http://127.0.0.1:50021"
    assert LocalVoicevoxPreviewConfig(
        endpoint=endpoint, speaker_uuid="invalid",
        style_id=-1, bearer_token="invalid", enabled=False,
    ).enabled is False
    with pytest.raises(ValueError):
        create_local_voicevox_preview_app(LocalVoicevoxPreviewConfig(
            endpoint=endpoint, speaker_uuid=UUID, style_id=3,
            bearer_token=TOKEN,
        ))
    for changes in (
        {"accept_unverified_engine": False},
        {"accept_native_text_url_logging": False},
        {"endpoint": "http://example.org:50021"},
        {"endpoint": "http://127.0.0.1:50021/private"},
        {"speaker_uuid": "random"},
        {"style_id": True},
        {"style_id": -1},
        {"bearer_token": "short"},
        {"timeout_seconds": float("nan")},
        {"max_output_bytes": 17 * 1024 * 1024},
    ):
        with pytest.raises(ValueError):
            config(endpoint, **changes)


@pytest.mark.asyncio
async def test_no_external_process_control_even_on_success(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("preview tried to manage the external engine")

    monkeypatch.setattr(os, "kill", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    async with fake_engine() as (_engine, endpoint):
        app = create_local_voicevox_preview_app(config(endpoint))
        async with client(app) as user:
            response = await user.post(
                "/preview/audio/speech", json={"input": "audio"},
                headers=auth(),
            )
        assert response.status_code == 200


def test_dedicated_preview_is_not_implicitly_mounted_in_production():
    from astrumweaver.control.api import create_app as control_app
    import astrumweaver.gateway.api as gateway_module

    assert callable(control_app) and gateway_module is not None
    # No production service imports this module or advertises preview.
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src" / "astrumweaver"
    for directory in ("control", "worker", "gateway"):
        for path in (root / directory).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                assert all("voicevox_local_preview" not in name for name in names)


# ---- review follow-ups ---------------------------------------------------------------

import logging  # noqa: E402

from astrumweaver.audio import voicevox_local_preview as preview  # noqa: E402
from astrumweaver.execution import JobExecutionError  # noqa: E402
from astrumweaver.runtime.providers.voicevox import _validate_query  # noqa: E402


def post(user, body, headers=None):
    return user.post("/preview/audio/speech", content=body, headers={**auth(), **(headers or {})})


@pytest.mark.asyncio
async def test_a_realistic_audio_query_is_accepted_not_a_503():
    """About 300 moras (13 nodes each): over the Worker adapter's 2048-node budget."""
    async with fake_engine(mode="large_query") as (engine, endpoint):
        app = create_local_voicevox_preview_app(config(endpoint))
        async with client(app) as user:
            response = await user.post("/preview/audio/speech", json={"input": "コ" * 300}, headers=auth())
        assert response.status_code == 200 and response.content == WAV
    # and that is why the preview passes its own budget
    mora = {"a": 1, "b": 2, "c": 3, "d": 4, "e": 5, "f": 6}
    big = {"accent_phrases": [{"moras": [dict(mora) for _ in range(10)]} for _ in range(30)]}
    with pytest.raises(JobExecutionError) as refused:
        _validate_query(big, 256 * 1024)  # the adapter's default budget refuses it
    assert refused.value.code == "invalid_query"
    assert _validate_query(big, 256 * 1024, max_nodes=16384) is big


@pytest.mark.asyncio
async def test_the_query_budget_is_still_bounded():
    huge = {"accent_phrases": [{"moras": [{"a": 1}] * 250} for _ in range(250)]}  # about 190k nodes
    with pytest.raises(JobExecutionError) as refused:
        _validate_query(huge, 256 * 1024, max_nodes=16384)
    assert refused.value.code == "invalid_query"


@pytest.mark.asyncio
@pytest.mark.parametrize("content_type,status", [
    ("application/json", 200), ("application/json; charset=utf-8", 200),
    ("Application/JSON;charset=UTF-8", 200), ("text/plain", 400),
    ("application/json; charset=latin-1", 400), ("application/jsonx", 400),
])
async def test_content_type_may_carry_the_utf8_charset_only(content_type, status):
    async with fake_engine() as (_engine, endpoint):
        async with client(create_local_voicevox_preview_app(config(endpoint))) as user:
            response = await post(user, '{"input":"こんにちは"}'.encode(), {"content-type": content_type})
        assert response.status_code == status


def test_a_short_token_is_refused_and_a_long_one_accepted():
    for token in ("a" * 24, "a" * 31, "has space " * 4, ""):
        with pytest.raises(ValueError):
            config("http://127.0.0.1:50021", bearer_token=token)
    assert config("http://127.0.0.1:50021", bearer_token="a" * 32).enabled


@pytest.mark.asyncio
async def test_only_one_speech_request_runs_at_a_time():
    async with fake_engine(mode="hang") as (engine, endpoint):
        app = create_local_voicevox_preview_app(config(endpoint, timeout_seconds=10.0))
        async with client(app) as user:
            first = asyncio.create_task(user.post("/preview/audio/speech", json={"input": "一"}, headers=auth()))
            await asyncio.wait_for(engine.hit.wait(), 3)
            second = await user.post("/preview/audio/speech", json={"input": "二"}, headers=auth())
            assert second.status_code == 429 and second.json()["error"]["code"] == "busy"
            engine.resume.set()
            assert (await first).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", [("POST", "/preview/audio/speech"), ("GET", "/preview/audio/voices")])
async def test_a_query_string_is_refused_after_authentication(method, path):
    async with fake_engine() as (engine, endpoint):
        async with client(create_local_voicevox_preview_app(config(endpoint))) as user:
            denied = await user.request(method, path + "?text=秘密")
            assert denied.status_code == 401
            refused = await user.request(method, path + "?text=秘密", headers=auth(), json={"input": "x"} if method == "POST" else None)
        assert refused.status_code == 400 and engine.seen == []


@pytest.mark.asyncio
async def test_voices_requires_authentication_without_probing():
    async with fake_engine() as (engine, endpoint):
        async with client(create_local_voicevox_preview_app(config(endpoint))) as user:
            for headers in ({}, {"authorization": "Bearer wrong"}, {"authorization": "Basic x"}):
                assert (await user.get("/preview/audio/voices", headers=headers)).status_code == 401
        assert engine.seen == []


@pytest.mark.asyncio
@pytest.mark.parametrize("text,status", [
    ("a" * 1024, 200), ("a" * 1025, 413),
    ("あ" * 341, 200),   # 1023 bytes
    ("あ" * 342, 413),   # 1026 bytes: fewer characters than the limit, more bytes
])
async def test_the_text_limit_counts_utf8_bytes(text, status):
    async with fake_engine() as (engine, endpoint):
        async with client(create_local_voicevox_preview_app(config(endpoint))) as user:
            response = await user.post("/preview/audio/speech", json={"input": text}, headers=auth())
        assert response.status_code == status
        if status == 413:
            assert engine.seen == []


@pytest.mark.asyncio
async def test_a_chunked_body_without_content_length_is_still_bounded():
    async def chunks():
        for _ in range(8):
            yield b" " * 1024

    async with fake_engine() as (engine, endpoint):
        async with client(create_local_voicevox_preview_app(config(endpoint))) as user:
            response = await user.post("/preview/audio/speech", content=chunks(),
                                       headers={**auth(), "content-type": "application/json"})
        assert response.status_code == 413 and engine.seen == []


@pytest.mark.asyncio
async def test_an_engine_redirect_is_refused_without_following_or_echoing():
    async with fake_engine(mode="query_redirect") as (engine, endpoint):
        async with client(create_local_voicevox_preview_app(config(endpoint))) as user:
            response = await user.post("/preview/audio/speech", json={"input": "秘密の文章"}, headers=auth())
        assert response.status_code == 503 and "秘密" not in response.text
        assert [row[1].split("?", 1)[0] for row in engine.seen] == ["/version", "/speakers", "/audio_query"]


@pytest.mark.asyncio
async def test_the_preview_itself_never_logs_or_prints_the_text_or_token(caplog, capsys):
    caplog.set_level(logging.DEBUG)
    async with fake_engine() as (_engine, endpoint):
        async with client(create_local_voicevox_preview_app(config(endpoint))) as user:
            await user.post("/preview/audio/speech", json={"input": "極秘の入力文"}, headers=auth())
            await user.post("/preview/audio/speech", json={"input": "極秘の入力文"}, headers={"authorization": "Bearer " + "x" * 40})
    captured = capsys.readouterr()
    seen = caplog.text + captured.out + captured.err
    assert "極秘" not in seen and TOKEN not in seen


def test_main_binds_loopback_only_and_needs_the_token_and_both_consents(monkeypatch):
    captured = {}

    def fake_run(app, **kwargs):
        captured.update(kwargs)

    import uvicorn

    monkeypatch.setattr(uvicorn, "run", fake_run)
    base = ["voicevox-preview", "--speaker-uuid", UUID, "--style-id", "3",
            "--accept-unverified-engine", "--accept-native-text-url-logging"]
    monkeypatch.setenv("TSUMGI_VOICEVOX_PREVIEW_TOKEN", TOKEN)
    monkeypatch.setattr("sys.argv", base)
    preview.main()
    assert captured["host"] == "127.0.0.1" and captured["access_log"] is False and captured["port"] == 9811
    for argv in (base[:-1], base[:-2] + ["--accept-native-text-url-logging"]):  # a consent flag missing
        monkeypatch.setattr("sys.argv", argv)
        with pytest.raises(ValueError):
            preview.main()
    monkeypatch.setenv("TSUMGI_VOICEVOX_PREVIEW_TOKEN", "")
    monkeypatch.setattr("sys.argv", base)
    with pytest.raises(ValueError):
        preview.main()
    monkeypatch.setenv("TSUMGI_VOICEVOX_PREVIEW_TOKEN", TOKEN)
    for port in ("80", "70000"):
        monkeypatch.setattr("sys.argv", base + ["--port", port])
        with pytest.raises(SystemExit):
            preview.main()
    monkeypatch.setattr("sys.argv", ["voicevox-preview", "--list-voices", "--endpoint", "http://example.com:50021"])
    with pytest.raises(SystemExit) as raised:
        preview.main()  # a clean usage error, not a traceback
    assert raised.value.code == 2


@pytest.mark.asyncio
async def test_a_close_that_never_finishes_cannot_hold_the_request_past_its_deadline(monkeypatch):
    """An engine that never reads keeps the closing socket from flushing."""
    import time

    async def never_closed(self):
        await asyncio.sleep(60)

    monkeypatch.setattr(asyncio.StreamWriter, "wait_closed", never_closed)
    async with fake_engine() as (_engine, endpoint):
        port = int(endpoint.rsplit(":", 1)[1])
        started = time.monotonic()
        answer = await preview._loopback_post(
            port, "/audio_query?speaker=3", body=b"", accept="application/json", max_bytes=4096,
        )
        assert answer and time.monotonic() - started < 5
