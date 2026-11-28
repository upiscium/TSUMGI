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


def test_transcription_credit_is_optional_and_strict(env, monkeypatch):
    _set(env, "transcriptions.credit", "Synthetic STT credit")
    seen = _limits(env, monkeypatch)
    assert seen["create_audio_transcriptions_router"].credit == "Synthetic STT credit"


@pytest.mark.parametrize("value", ["", "x" * 257, "a\nb", 3, True])
def test_transcription_credit_rejects_malformed(env, value):
    _set(env, "transcriptions.credit", value)
    _invalid(env)


def test_declared_stt_format_keys(env, monkeypatch):
    _set(env, "transcriptions.accepted_sample_rates", [16000])
    _set(env, "transcriptions.accepted_channels", [1])
    limits = _limits(env, monkeypatch)["create_audio_transcriptions_router"]
    assert limits.accepted_sample_rates == (16000,) and limits.accepted_channels == (1,)


@pytest.mark.parametrize("key,value", [
    ("accepted_sample_rates", [100]), ("accepted_sample_rates", "16000"),
    ("accepted_channels", [0]), ("accepted_channels", [1.0]),
])
def test_declared_stt_format_keys_are_strict(env, key, value):
    _set(env, f"transcriptions.{key}", value)
    _invalid(env)


@pytest.mark.parametrize("section,max_bytes", [("input_spool", 1024), ("output_spool", 512)])
def test_spool_disk_floor_default_and_override(env, monkeypatch, section, max_bytes):
    from tsumgi.control import audio_gateway_config as audio_config
    built = {}
    original = audio_config._SpoolConfig.floor

    def spy(self, size):
        floor = original(self, size)
        built[self.root.name] = floor
        return floor

    monkeypatch.setattr(audio_config._SpoolConfig, "floor", spy)
    _set(env, f"{section}.min_free_bytes", 12345)
    env.build()
    other = "output_spool" if section == "input_spool" else "input_spool"
    assert built[env.policy[section]["root"].rsplit("/", 1)[1]].min_free_bytes == 12345
    assert built[env.policy[other]["root"].rsplit("/", 1)[1]].min_free_bytes == 1024 ** 3


@pytest.mark.parametrize("section", ["input_spool", "output_spool"])
@pytest.mark.parametrize("value", [-1, 1.5, True, "1"])
def test_spool_disk_floor_is_strict(env, section, value):
    _set(env, f"{section}.min_free_bytes", value)
    _invalid(env)
