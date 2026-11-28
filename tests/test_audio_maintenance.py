"""Expiry is a local privacy deadline, not a successful database operation."""

import asyncio

import pytest

from tsumgi.control import InMemoryControlRepository
from tsumgi.control.api import create_app
from test_audio_input_channel import WAV, make_channel
from test_audio_output_channel import new_env


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["repository", "output_spool"])
async def test_maintenance_reaps_expired_input_despite_unrelated_failure(
    tmp_path, monkeypatch, failure,
):
    repo = InMemoryControlRepository()
    output = new_env(tmp_path, repository=repo).channel
    channel = make_channel(tmp_path, repo)
    channel.stage(WAV)
    artifact, = (tmp_path / "input").glob("*/*")
    assert artifact.read_bytes() == WAV
    expired = channel.spool._clock() + 301
    monkeypatch.setattr(channel.spool, "_clock", lambda: expired)

    def unavailable():
        raise OSError("unavailable")

    target, method = ((repo, "expire_stale_workers") if failure == "repository"
                      else (output, "reap_expired"))
    monkeypatch.setattr(target, method, unavailable)
    app = create_app(
        repo, client_token="client", worker_token="worker",
        audio_input_channel=channel, audio_output_channel=output,
        maintenance_interval_seconds=0.01,
    )
    async with app.router.lifespan_context(app):
        async with asyncio.timeout(2):
            while artifact.exists():
                await asyncio.sleep(0.01)


@pytest.mark.asyncio
@pytest.mark.parametrize("repository_down", [False, True])
async def test_maintenance_releases_terminal_inputs_and_consumed_outputs(
    tmp_path, monkeypatch, repository_down,
):
    """#318 G3: release does not wait for the TTL; consumed outputs need no database."""
    repo = InMemoryControlRepository()
    output = new_env(tmp_path, repository=repo).channel
    channel = make_channel(tmp_path, repo)
    calls = []
    monkeypatch.setattr(channel, "reap_terminal", lambda: calls.append("terminal") or 0)
    monkeypatch.setattr(output, "reap_consumed", lambda: calls.append("consumed") or 0)
    if repository_down:
        def unavailable():
            raise OSError("unavailable")
        monkeypatch.setattr(repo, "expire_stale_workers", unavailable)
    app = create_app(
        repo, client_token="client", worker_token="worker",
        audio_input_channel=channel, audio_output_channel=output,
        maintenance_interval_seconds=0.01,
    )
    async with app.router.lifespan_context(app):
        async with asyncio.timeout(2):
            while "consumed" not in calls or ("terminal" not in calls):
                await asyncio.sleep(0.01)
    assert {"terminal", "consumed"} <= set(calls)
