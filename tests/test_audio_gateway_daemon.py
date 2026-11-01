"""Source-only Control composition tests: fake Postgres, ASGI, no runtime/DB."""

from __future__ import annotations

from copy import deepcopy
import json
from types import SimpleNamespace

import httpx
import pytest

from astrumweaver.audio.input_channel import AudioInputChannel
from astrumweaver.audio.output_channel import AudioOutputChannel
from astrumweaver.control import InMemoryControlRepository
from astrumweaver.control import audio_gateway_config as audio_config
from astrumweaver.control import daemon
from astrumweaver.control.auth import ClientAuthMode
from astrumweaver.gateway.audio import AudioProfileCatalog
from test_audio_voice_api import make_profile


TOKEN = "dedicated-audio-test-token"
AUTH = {"Authorization": "Bearer " + TOKEN}
AUDIO_PATHS = {
    "/v1/audio/voices", "/v1/audio/speech", "/v1/audio/transcriptions",
}
AUDIO_TRANSPORT_PATHS = {
    "/v1/audio/inputs", "/v1/audio/inputs/jobs",
    "/v1/workers/{worker_id}/jobs/{job_id}/audio/input",
    "/v1/workers/{worker_id}/jobs/{job_id}/audio/output",
    "/v1/jobs/{job_id}/audio/output/{artifact_id}",
}


def _toml(document):
    lines = []

    def table(data, prefix=""):
        if prefix:
            lines.append(f"[{prefix}]")
        for key, value in data.items():
            if type(value) is not dict:
                literal = str(value).lower() if type(value) is float else json.dumps(value)
                lines.append(f"{key} = {literal}")
        for key, value in data.items():
            if type(value) is dict:
                table(value, f"{prefix}.{key}" if prefix else key)

    table(document)
    return "\n".join(lines) + "\n"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRUMWEAVER_DATABASE_URL", "postgresql://unused.invalid/test")
    monkeypatch.setenv("ASTRUMWEAVER_WORKER_TOKEN", "worker-test-token")
    monkeypatch.setenv("ASTRUMWEAVER_CLIENT_TOKEN", "control-test-token")
    monkeypatch.setenv("ASTRUMWEAVER_AUDIO_CLIENT_TOKEN", TOKEN)
    repository = InMemoryControlRepository()
    monkeypatch.setattr(daemon, "PostgresControlRepository", lambda *a, **kw: repository)
    catalog = tmp_path / "reviewed-audio.json"
    document = AudioProfileCatalog(tuple(make_profile(kind) for kind in (
        "voicevox", "model-tts", "model-stt",
    ))).to_dict()
    catalog.write_text(json.dumps(document), encoding="utf-8")
    roots = [tmp_path / name for name in ("audio-input", "audio-output")]
    for root in roots:
        root.mkdir(mode=0o700)
    policy = {
        "enabled": True, "catalog": str(catalog),
        "ingress": {
            "request_bytes": 2048, "input_bytes": 128, "file_bytes": 1024,
            "metadata_bytes": 256, "part_header_bytes": 256,
            "total_header_bytes": 2048, "receive_events": 16,
            "idle_timeout_seconds": 0.5, "total_timeout_seconds": 1.0,
        },
        "speech": {
            "output_bytes": 512, "request_timeout_seconds": 2.0,
            "max_pending_admissions": 2, "poll_interval_seconds": 0.005,
        },
        "transcriptions": {
            "text_bytes": 128, "request_timeout_seconds": 1.5,
            "max_pending_admissions": 1, "poll_interval_seconds": 0.01,
        },
        "pcm": {"max_decoded_bytes": 128, "max_duration_seconds": 1},
        "input_spool": {
            "root": str(roots[0]), "ttl_seconds": 60.0,
            "max_total_bytes": 4096, "max_artifacts": 4,
        },
        "output_spool": {
            "root": str(roots[1]), "ttl_seconds": 30.0,
            "max_total_bytes": 2048, "max_artifacts": 3,
        },
    }
    config = tmp_path / "control.toml"

    def build(policy=policy, *, control_auth="bearer", extra=None):
        value = {"control": {"client_auth": control_auth}}
        if policy is not None:
            value["audio_gateway"] = policy
        value.update(extra or {})
        config.write_text(_toml(value), encoding="utf-8")
        return daemon.build_app(str(config))

    return SimpleNamespace(
        build=build, policy=policy, roots=roots, catalog=catalog,
        document=document, repository=repository, config=config,
    )


def _no_spools(env):
    assert all(not list(root.iterdir()) for root in env.roots)


def _invalid(env):
    with pytest.raises(RuntimeError, match="^audio_gateway configuration is invalid$") as error:
        env.build()
    assert error.value.__suppress_context__
    for private in (TOKEN, str(env.catalog), str(env.roots[0]), "private-data"):
        assert private not in str(error.value)
    _no_spools(env)


@pytest.mark.parametrize("disabled", [None, {}, {"enabled": False}, "retained"])
def test_disabled_has_no_audio_side_effects(env, monkeypatch, disabled):
    def forbidden(*args, **kwargs):
        pytest.fail("disabled audio performed audio I/O or constructed a spool")

    monkeypatch.setattr(audio_config, "_load_catalog", forbidden)
    monkeypatch.setattr(audio_config, "_check_root", forbidden)
    monkeypatch.setattr(audio_config, "ArtifactSpool", forbidden)
    monkeypatch.delenv("ASTRUMWEAVER_AUDIO_CLIENT_TOKEN")
    if disabled == "retained":
        disabled = deepcopy(env.policy)
        disabled["enabled"] = False
        disabled["catalog"] = "/not-present/private-data.json"
        disabled["input_spool"]["root"] = "/not-present/input"
    app = env.build(disabled)
    assert not AUDIO_PATHS.intersection(app.openapi()["paths"])
    assert "/v1/models" not in app.openapi()["paths"]
    _no_spools(env)


def test_enabled_constructs_exact_factories_with_same_repository_and_limits(env, monkeypatch):
    captured = {}
    for name in ("create_audio_voice_router", "create_audio_speech_router",
                 "create_audio_transcriptions_router"):
        original = getattr(audio_config, name)

        def record(*args, _name=name, _original=original, **kwargs):
            captured[_name] = (args, kwargs)
            return _original(*args, **kwargs)

        monkeypatch.setattr(audio_config, name, record)
    baseline = set(env.build(None).openapi()["paths"])
    app = env.build()
    assert set(app.openapi()["paths"]) - baseline == AUDIO_PATHS | AUDIO_TRANSPORT_PATHS
    assert set(app.openapi()["paths"]["/v1/audio/voices"]) == {"get"}
    assert set(app.openapi()["paths"]["/v1/audio/speech"]) == {"post"}
    assert set(app.openapi()["paths"]["/v1/audio/transcriptions"]) == {"post"}
    for args, kwargs in captured.values():
        assert type(args[0]) is AudioProfileCatalog
        assert kwargs["client_auth"] is ClientAuthMode.BEARER
        assert kwargs["client_token"] == TOKEN
    speech_args, speech_kw = captured["create_audio_speech_router"]
    stt_args, stt_kw = captured["create_audio_transcriptions_router"]
    output, input_channel = speech_args[2], stt_args[2]
    assert type(output) is AudioOutputChannel
    assert type(input_channel) is AudioInputChannel
    assert output.repository is input_channel.repository is env.repository
    assert speech_args[1] is stt_args[1] is env.repository
    assert input_channel.spool is not output._spool
    for key, value in env.policy["speech"].items():
        assert getattr(speech_kw["limits"], key) == value
    for key, value in env.policy["transcriptions"].items():
        assert getattr(stt_kw["limits"], key) == value
    for key, value in env.policy["ingress"].items():
        assert getattr(stt_kw["ingress_limits"], key) == value
    assert speech_kw["ingress_limits"] is stt_kw["ingress_limits"]
    assert input_channel.max_bytes == 1024
    assert input_channel.max_decoded_bytes == 128
    assert input_channel.max_duration_seconds == 1
    assert output.max_bytes == 512
    for spool, policy in ((input_channel.spool, env.policy["input_spool"]),
                          (output._spool, env.policy["output_spool"])):
        assert spool._ttl == policy["ttl_seconds"]
        assert spool._max_total_bytes == policy["max_total_bytes"]
        assert spool._max_artifacts == policy["max_artifacts"]


@pytest.mark.parametrize("control_auth", ["bearer"])
@pytest.mark.asyncio
async def test_audio_requires_dedicated_bearer_independently_of_control(env, monkeypatch, control_auth):
    if control_auth == "none":
        monkeypatch.delenv("ASTRUMWEAVER_CLIENT_TOKEN")
    app = env.build(control_auth=control_auth)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        for headers in ({}, {"Authorization": "Bearer control-test-token"},
                        {"Authorization": "Bearer worker-test-token"},
                        {"Authorization": "Bearer anon"},
                        {"Authorization": "Bearer wrong"}):
            for method, path in (("GET", "/v1/audio/voices"),
                                 ("POST", "/v1/audio/speech"),
                                 ("POST", "/v1/audio/transcriptions")):
                response = await client.request(method, path, headers=headers)
                assert response.status_code == 401
                assert response.headers["www-authenticate"] == "Bearer"
                assert response.headers["cache-control"] == "no-store"
                assert TOKEN not in response.text
        voices = await client.get("/v1/audio/voices?model=synthetic-voicevox", headers=AUTH)
        assert voices.status_code == 200
        assert voices.json()["model"] == "synthetic-voicevox"
        speech = await client.post("/v1/audio/speech", headers=AUTH, json={})
        assert speech.status_code == 400
        transcription = await client.post("/v1/audio/transcriptions", headers=AUTH, content=b"bad")
        assert transcription.status_code == 415
        assert (await client.get("/v1/models", headers=AUTH)).status_code == 404
        original = await client.get("/v1/jobs/nonexistent")
        assert original.status_code == (404 if control_auth == "none" else 401)
    assert not env.repository.list_jobs()


def test_enabled_none_control_auth_fails_before_spool_allocation(env):
    with pytest.raises(RuntimeError, match="audio_gateway requires control.client_auth=bearer"):
        env.build(control_auth="none")
    _no_spools(env)
    # The existing non-audio anonymous mode remains available while audio is disabled.
    disabled = env.build(None, control_auth="none")
    assert "/v1/audio/speech" not in disabled.openapi()["paths"]


@pytest.mark.asyncio
async def test_authenticated_post_routes_use_the_constructed_channels(env, monkeypatch):
    from astrumweaver.gateway.audio_speech_api import AudioSpeechService
    from astrumweaver.gateway.audio_transcriptions_api import AudioTranscriptionsService
    from test_audio_speech_api import WAV

    seen = []

    async def speech(self, request, catalog, ingress):
        assert self.channel.repository is self.repository is env.repository
        assert type(self.channel) is AudioOutputChannel
        seen.append("speech")
        return WAV

    async def transcriptions(self, request, catalog, ingress):
        assert self.channel.repository is self.repository is env.repository
        assert type(self.channel) is AudioInputChannel
        seen.append("transcriptions")
        return {"text": "synthetic transcription"}

    monkeypatch.setattr(AudioSpeechService, "complete", speech)
    monkeypatch.setattr(AudioTranscriptionsService, "complete", transcriptions)
    app = env.build()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        response = await client.post("/v1/audio/speech", headers=AUTH)
        assert response.status_code == 200
        assert response.content == WAV
        assert response.headers["content-type"] == "audio/wav"
        response = await client.post("/v1/audio/transcriptions", headers=AUTH)
        assert response.status_code == 200
        assert response.json() == {"text": "synthetic transcription"}
    assert seen == ["speech", "transcriptions"]
    assert not env.repository.list_jobs()


@pytest.mark.parametrize("token", [None, "", "bad token", "日本語", "anon",
                                   "control-test-token", "worker-test-token"])
def test_missing_or_invalid_audio_token_never_falls_back(env, monkeypatch, token):
    if token is None:
        monkeypatch.delenv("ASTRUMWEAVER_AUDIO_CLIENT_TOKEN")
    else:
        monkeypatch.setenv("ASTRUMWEAVER_AUDIO_CLIENT_TOKEN", token)
    _invalid(env)


@pytest.mark.parametrize("key", ["catalog", "ingress", "speech", "transcriptions",
                                  "pcm", "input_spool", "output_spool"])
def test_enabled_requires_every_policy_table(env, key):
    del env.policy[key]
    _invalid(env)


@pytest.mark.parametrize("path,value", [
    ("enabled", "true"), ("enabled", 1), ("private-data", TOKEN),
    ("ingress", []), ("ingress.request_bytes", True),
    ("ingress.input_bytes", 0), ("ingress.file_bytes", 20),
    ("ingress.metadata_bytes", -1), ("ingress.receive_events", 0),
    ("ingress.part_header_bytes", 9000), ("ingress.total_header_bytes", 0),
    ("ingress.request_bytes", 100), ("ingress.idle_timeout_seconds", float("nan")),
    ("ingress.total_timeout_seconds", float("inf")),
    ("ingress.total_timeout_seconds", 0.1), ("ingress.extra", TOKEN),
    ("speech.output_bytes", 0), ("speech.output_bytes", 17 * 1024 * 1024),
    ("speech.request_timeout_seconds", 0), ("speech.request_timeout_seconds", 301),
    ("speech.max_pending_admissions", True), ("speech.max_pending_admissions", 0),
    ("speech.poll_interval_seconds", 3), ("speech.extra", TOKEN),
    ("transcriptions.text_bytes", False), ("transcriptions.text_bytes", 0),
    ("transcriptions.request_timeout_seconds", float("nan")),
    ("transcriptions.max_pending_admissions", 1025),
    ("transcriptions.poll_interval_seconds", 0),
    ("pcm.max_decoded_bytes", 0), ("pcm.max_decoded_bytes", 1024),
    ("pcm.max_duration_seconds", 0), ("pcm.max_duration_seconds", 1.5),
    ("pcm.extra", TOKEN), ("input_spool.max_total_bytes", 100),
    ("input_spool.ttl_seconds", 1.5), ("input_spool.ttl_seconds", float("inf")),
    ("input_spool.max_artifacts", True), ("output_spool.max_artifacts", 0),
    ("output_spool.ttl_seconds", 2), ("output_spool.extra", TOKEN),
])
def test_strict_config_rejects_malformed_unknown_or_unbounded_policy(env, path, value):
    parts = path.split(".")
    target = env.policy
    for part in parts[:-1]:
        target = target[part]
    target[parts[-1]] = value
    _invalid(env)


@pytest.mark.parametrize("section", ["ingress", "speech", "transcriptions", "pcm",
                                      "input_spool", "output_spool"])
def test_nested_policy_has_no_implicit_defaults(env, section):
    env.policy[section].pop(next(iter(env.policy[section])))
    _invalid(env)


@pytest.mark.parametrize("value", [[], "private-data", False, 1])
def test_audio_section_must_be_a_table(env, value):
    with pytest.raises(RuntimeError, match="^audio_gateway configuration is invalid$"):
        env.build(value)
    _no_spools(env)


@pytest.mark.parametrize("raw", [
    b"{private-data", b"\xff", b"[]", b"{}", b"NaN", b"Infinity",
    b'{"schema_version":"audio-gateway-catalog-v1","profiles":[],"profiles":[]}',
    b'{"schema_version":"audio-gateway-catalog-v1","profiles":[],"private-data":1}',
    b" " * (1024 * 1024 + 1),
])
def test_strict_catalog_json_failures_are_sanitized_before_spool_creation(env, raw):
    env.catalog.write_bytes(raw)
    _invalid(env)


@pytest.mark.parametrize("case", ["empty", "no-speech", "no-stt", "missing-limits",
                                  "invalid-limits", "extra-profile", "semantic-pin"])
def test_catalog_requires_valid_pinned_profiles_for_both_operations(env, case):
    doc = deepcopy(env.document)
    if case == "empty":
        doc["profiles"] = []
    elif case == "no-speech":
        doc["profiles"] = [make_profile("model-stt").to_dict()]
    elif case == "no-stt":
        doc["profiles"] = [make_profile("voicevox").to_dict()]
    elif case == "missing-limits":
        doc["profiles"][0]["contract"]["limits"] = {}
    elif case == "invalid-limits":
        doc["profiles"][0]["contract"]["limits"]["request_bytes"] = 0
    elif case == "extra-profile":
        doc["profiles"][0]["private-data"] = TOKEN
    else:
        doc["profiles"][0]["contract"]["semantic_revision"] = "sha256:" + "f" * 64
    env.catalog.write_text(json.dumps(doc), encoding="utf-8")
    _invalid(env)


def test_missing_catalog_is_sanitized(env):
    env.policy["catalog"] += ".private-data-missing"
    _invalid(env)


@pytest.mark.parametrize("case", ["missing", "file", "mode", "symlink", "ancestor",
                                  "relative", "traversal", "same", "nested"])
def test_spool_roots_fail_closed_without_allocation(env, tmp_path, case):
    root = tmp_path / "private-data"
    if case == "missing":
        pass
    elif case == "file":
        root.write_text("private-data")
    elif case == "mode":
        root.mkdir(mode=0o755)
    elif case == "symlink":
        root.symlink_to(env.roots[0], target_is_directory=True)
    elif case == "ancestor":
        root.mkdir(mode=0o777)
        root.chmod(0o777)
        root = root / "child"
        root.mkdir(mode=0o700)
    elif case == "relative":
        root = "relative/private-data"
    elif case == "traversal":
        root = str(env.roots[0]) + "/../audio-input"
    elif case == "same":
        root = env.roots[0]
    else:
        root = env.roots[0] / "nested"
        root.mkdir(mode=0o700)
    env.policy["output_spool"]["root"] = str(root)
    if case == "nested":
        with pytest.raises(RuntimeError, match="^audio_gateway configuration is invalid$"):
            env.build()
        assert list(env.roots[0].iterdir()) == [root]
        assert not list(root.iterdir())
        assert not list(env.roots[1].iterdir())
    else:
        _invalid(env)


def test_spool_constructor_failure_does_not_leak_os_error(env, monkeypatch):
    def unavailable(*args, **kwargs):
        raise OSError(f"private-data {env.roots[0]} {TOKEN}")

    monkeypatch.setattr(audio_config, "ArtifactSpool", unavailable)
    _invalid(env)


def test_malformed_toml_is_sanitized(env):
    env.config.write_text('[audio_gateway]\nsecret = "private-data\n', encoding="utf-8")
    with pytest.raises(RuntimeError, match="^Control configuration is invalid$"):
        daemon.build_app(str(env.config))
    _no_spools(env)


@pytest.mark.asyncio
async def test_original_chat_embedding_decision_and_models_are_unchanged(env, tmp_path):
    from test_chat_gateway_daemon import catalog_document as chat_catalog
    from test_decision_gateway_daemon import catalog_document as decision_catalog
    from test_embedding_gateway_daemon import catalog_document as embedding_catalog

    extra = {}
    for name, factory in (("chat", chat_catalog), ("embedding", embedding_catalog),
                          ("decision", decision_catalog)):
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(factory()), encoding="utf-8")
        extra[f"{name}_gateway"] = {"enabled": True, "catalog": str(path)}
    before = env.build(None, extra=extra)
    after = env.build(extra=extra)
    before_paths, after_paths = before.openapi()["paths"], after.openapi()["paths"]
    assert set(after_paths) - set(before_paths) == AUDIO_PATHS | AUDIO_TRANSPORT_PATHS
    assert all(after_paths[path] == value for path, value in before_paths.items())
    assert {"/v1/models", "/v1/chat/completions", "/v1/embeddings",
            "/v1/decisions"} <= set(after_paths)
    responses = []
    for app in (before, after):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            response = await client.get("/v1/models", headers={"Authorization": "Bearer control-test-token"})
            assert response.status_code == 200
            responses.append(response.json())
    assert responses[0] == responses[1]
    assert "synthetic-voicevox" not in json.dumps(responses[1])


@pytest.mark.asyncio
async def test_audio_transport_uses_base_control_and_worker_authority(env):
    """Mounted public APIs are not enough: worker media routes must be wired."""
    from test_audio_speech_api import WAV

    app = env.build()
    assert AUDIO_TRANSPORT_PATHS <= set(app.openapi()["paths"])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        for headers in ({}, AUTH, {"Authorization": "Bearer worker-test-token"}):
            response = await client.post("/v1/audio/inputs", headers={
                **headers, "Content-Type": "audio/wav",
            }, content=WAV)
            assert response.status_code == 401
        authorized = await client.post("/v1/audio/inputs", headers={
            "Authorization": "Bearer control-test-token", "Content-Type": "audio/wav",
        }, content=WAV)
        assert authorized.status_code == 201, authorized.text
        receipt = authorized.json()
        assert receipt["uri"].startswith("tsumgi-audio-input:")
        assert receipt["size_bytes"] == len(WAV)
        assert not env.repository.list_jobs()

        worker_endpoint = "/v1/workers/synthetic/jobs/job1/audio/input"
        for headers in ({}, AUTH, {"Authorization": "Bearer control-test-token"}):
            assert (await client.get(worker_endpoint, headers=headers)).status_code == 401
        assert (await client.get(worker_endpoint, headers={
            "Authorization": "Bearer worker-test-token",
        })).status_code == 422  # claim/lease/epoch headers still mandatory

        published = "/v1/jobs/job1/audio/output/receipt"
        assert (await client.get(published)).status_code == 401
        assert (await client.get(published, headers=AUTH)).status_code == 401
        assert (await client.get(published, headers={
            "Authorization": "Bearer control-test-token",
        })).status_code == 404
