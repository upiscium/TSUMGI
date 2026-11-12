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


def config(endpoint: str, **overrides) -> LocalVoicevoxPreviewConfig:
    values = dict(
        endpoint=endpoint,
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
    (b'{"input":"hello","input":"attack"}', 503),
    (b'{"input":null}', 400),
    (b'{"input":" "}', 400),
    (b'{"input":"ok","voice":"unapproved"}', 400),
    (b'{"input":NaN}', 503),
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
    from astrumweaver.gateway.api import create_app as gateway_app

    assert callable(control_app) and callable(gateway_app)
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
