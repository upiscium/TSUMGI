"""Strict, optional #318 policy keys of [audio_gateway]; defaults keep old behaviour."""

from __future__ import annotations

import pytest

from test_audio_gateway_daemon import TOKEN, _invalid, _no_spools, env  # noqa: F401


def _set(env, path, value):
    target = env.policy
    parts = path.split(".")
    for part in parts[:-1]:
        target = target.setdefault(part, {})
    target[parts[-1]] = value


def _limits(app_factory_env, monkeypatch):
    from tsumgi.control import audio_gateway_config as audio_config
    seen = {}
    for name in ("create_audio_speech_router", "create_audio_transcriptions_router"):
        original = getattr(audio_config, name)

        def record(*args, _name=name, _original=original, **kwargs):
            seen[_name] = kwargs["limits"]
            return _original(*args, **kwargs)

        monkeypatch.setattr(audio_config, name, record)
    app_factory_env.build()
    return seen


@pytest.mark.parametrize("configured", [False, True])
def test_retry_after_defaults_and_is_configurable(env, monkeypatch, configured):
    if configured:
        _set(env, "speech.retry_after_seconds", 9)
        _set(env, "transcriptions.retry_after_seconds", 11)
    seen = _limits(env, monkeypatch)
    assert seen["create_audio_speech_router"].retry_after_seconds == (9 if configured else 2)
    assert seen["create_audio_transcriptions_router"].retry_after_seconds == (
        11 if configured else 2)


@pytest.mark.parametrize("section", ["speech", "transcriptions"])
@pytest.mark.parametrize("value", [0, 31, True, 1.5, "2", -1])
def test_retry_after_is_strict(env, section, value):
    _set(env, f"{section}.retry_after_seconds", value)
    _invalid(env)
