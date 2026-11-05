"""#231 B6d: no-apply audio assembly source and disposable Linux acceptance."""

from __future__ import annotations

import base64
import json
import os
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from test_audio_input_channel import WAV
from test_audio_provider_binding import values
from test_voicevox_linux_process import setup as make_linux_process
from test_voicevox_provider import FakeMedia, job

from astrumweaver.contracts import ResourceShape, WorkerSpec
from astrumweaver.gateway.audio import (
    AudioGatewayProfile,
    AudioProfileCatalog,
    VoiceAlias,
    audio_semantic_revision,
)
from astrumweaver.runtime.contracts import RuntimeHealthState, RuntimeHostFacts
from astrumweaver.runtime.deployment import _PROVIDER_TYPES
from astrumweaver.runtime.providers.voicevox_http import VoicevoxHttpConfig
from astrumweaver.runtime.providers.voicevox_linux_process import (
    VoicevoxLinuxLaunchSpec,
)
from astrumweaver.runtime.providers.voicevox_managed import VoicevoxRuntimeFailure
from astrumweaver.runtime.providers.voicevox_source_assembly import (
    VoicevoxSourceAssemblyError,
    VoicevoxSourceAssemblyIntent,
    VoicevoxSourceAssemblyReview,
    review_voicevox_source_assembly,
)
from astrumweaver.serving import (
    LogicalServingProfile,
    ServingDeploymentDeclaration,
    resolve_profile,
)
from astrumweaver.setup import SetupPlan
from astrumweaver.setup.contracts import DeploymentPath, SetupHostSnapshot


def static_intent() -> VoicevoxSourceAssemblyIntent:
    """No disk/model/port access; use declarations containing private strings."""
    v = values("voicevox")
    runtime = v["runtime"]
    return VoicevoxSourceAssemblyIntent(
        profile_id=v["profile_id"],
        catalog=v["catalog"],
        declaration=v["declaration"],
        worker=v["worker"],
        snapshot=v["snapshot"],
        runtime=runtime,
        launch=VoicevoxLinuxLaunchSpec(
            executable_path=runtime.executable_path,
            fixed_args=("--host=127.0.0.1", "--port=51000"),
            stop_timeout_seconds=runtime.stop_timeout_seconds,
        ),
        http=VoicevoxHttpConfig(
            endpoint=runtime.endpoint,
            adapter=runtime.adapter,
            request_timeout_seconds=2.0,
        ),
    )


def test_disabled_is_constant_inert_and_never_touches_candidate(monkeypatch):
    class Poison:
        def __getattribute__(self, _name):
            raise AssertionError("disabled mode inspected a candidate")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("disabled mode touched filesystem or network")

    monkeypatch.setattr("builtins.open", forbidden)
    monkeypatch.setattr(os, "open", forbidden)
    review = review_voicevox_source_assembly(enabled=False, intent=Poison())
    assert type(review) is VoicevoxSourceAssemblyReview
    assert not review.can_apply and review.status == "disabled"
    assert review.assembly_digest is None and review.binding_digest is None
    assert not review.blockers
    assert review.to_dict()["can_apply"] is False
    with pytest.raises(VoicevoxSourceAssemblyError):
        review.compose_disposable_source_runtime()
    with pytest.raises(VoicevoxSourceAssemblyError):
        review_voicevox_source_assembly(enabled=1, intent=Poison())


def test_valid_static_review_redacts_private_metadata_and_preserves_stop_gates(
    monkeypatch,
):
    intent = static_intent()
    review = review_voicevox_source_assembly(enabled=True, intent=intent)
    assert review.enabled and review.status == "review_only_blocked"
    assert not review.can_apply and not isinstance(review, SetupPlan)
    assert review.provider_id == "voicevox"
    assert review.profile_id == intent.profile_id
    assert review.binding_digest.startswith("sha256:")
    assert review.assembly_digest.startswith("sha256:")
    assert "provider_runtime_registry_not_enabled" in review.blockers
    assert "native_execution_and_model_load_unqualified" in review.blockers
    assert "native_access_log_and_license_review_pending" in review.blockers
    assert "source_assembly_is_not_runtime_admission" in review.blockers
    assert (
        review.to_json()
        == review_voicevox_source_assembly(enabled=True, intent=intent).to_json()
    )
    assert json.loads(review.to_json()) == review.to_dict()
    for secret in (
        "/opt/private",
        "private-worker",
        "test-license",
        "Synthetic attribution",
        "--host=",
        "--port=",
        "GPU-PRIVATE-0",
        "token-private",
    ):
        assert secret not in review.to_json()
        assert secret not in repr(review)
        assert secret not in repr(intent)

    assert "voicevox" not in _PROVIDER_TYPES
    assert "style-bert-vits2" not in _PROVIDER_TYPES

    # Assembly construction is inert: no file hashing, socket or subprocess.
    def forbidden(*_args, **_kwargs):
        raise AssertionError("source-only constructor attempted I/O")

    monkeypatch.setattr(os, "open", forbidden)
    managed = review.compose_disposable_source_runtime()
    assert managed.provider_id == "voicevox"
    assert managed._process._proc is None
    assert managed._state is RuntimeHealthState.STOPPED
    assert not managed._released


def test_drifted_mutable_review_intent_fails_before_runtime_construction():
    intent = static_intent()
    review = review_voicevox_source_assembly(enabled=True, intent=intent)
    # Simulate malicious mutation through reflection (frozen dataclass bypass).
    object.__setattr__(intent, "profile_id", "changed-after-review")
    with pytest.raises(VoicevoxSourceAssemblyError, match="source assembly review"):
        review.compose_disposable_source_runtime()


@pytest.mark.parametrize(
    "mutation",
    [
        "wrong_http_endpoint",
        "wrong_http_adapter",
        "wrong_exec",
        "wrong_port",
        "missing_port",
        "wildcard_host",
        "wrong_host",
        "too_slow_stop",
        "too_slow_http",
        "wrong_worker",
        "wrong_profile",
        "gpu_worker",
        "unreviewed_interpreter",
    ],
)
def test_unqualified_components_are_rejected_with_constant_error(mutation):
    original = static_intent()
    intent = original
    if mutation == "wrong_http_endpoint":
        intent = replace(
            intent, http=replace(intent.http, endpoint="http://127.0.0.1:51001")
        )
    elif mutation == "wrong_http_adapter":
        intent = replace(
            intent,
            http=replace(
                intent.http,
                adapter=replace(intent.http.adapter, engine_version="other-v1"),
            ),
        )
    elif mutation == "wrong_exec":
        intent = replace(
            intent,
            launch=replace(
                intent.launch,
                executable_path="/opt/private/wrong-engine",
            ),
        )
    elif mutation == "wrong_port":
        intent = replace(
            intent,
            launch=replace(
                intent.launch,
                fixed_args=("--host=127.0.0.1", "--port=51001"),
            ),
        )
    elif mutation == "missing_port":
        intent = replace(
            intent,
            launch=replace(
                intent.launch,
                fixed_args=("--host=127.0.0.1",),
            ),
        )
    elif mutation == "wildcard_host":
        intent = replace(
            intent,
            launch=replace(
                intent.launch,
                fixed_args=("--host=0.0.0.0", "--port=51000"),
            ),
        )
    elif mutation == "wrong_host":
        intent = replace(
            intent,
            launch=replace(
                intent.launch,
                fixed_args=("--host", "10.10.10.2", "--port", "51000"),
            ),
        )
    elif mutation == "too_slow_stop":
        intent = replace(
            intent,
            launch=replace(
                intent.launch,
                stop_timeout_seconds=intent.runtime.stop_timeout_seconds + 1,
            ),
        )
    elif mutation == "too_slow_http":
        intent = replace(
            intent,
            http=replace(
                intent.http,
                request_timeout_seconds=(
                    intent.runtime.adapter.synthesis_timeout_seconds + 1
                ),
            ),
        )
    elif mutation == "wrong_worker":
        intent = replace(
            intent,
            worker=replace(intent.worker, worker_id="other-worker"),
        )
    elif mutation == "wrong_profile":
        intent = replace(intent, profile_id="missing-profile")
    elif mutation == "gpu_worker":
        intent = replace(
            intent,
            worker=replace(
                intent.worker,
                resources=ResourceShape(
                    gpu_count=1,
                    total_vram_mb=8192,
                    max_single_gpu_vram_mb=8192,
                ),
                gpu_uuids=("GPU-UNKNOWN",),
            ),
        )
    elif mutation == "unreviewed_interpreter":
        # An interpreter's code-bearing auxiliary MUST have a SHA-256 pin.
        with pytest.raises(ValueError):
            replace(
                intent.launch,
                fixed_args=("-I", "-u", "/opt/private/unpinned.py", "51000"),
            )
        return
    else:
        raise AssertionError(f"unhandled test mutation: {mutation}")
    with pytest.raises(
        VoicevoxSourceAssemblyError,
        match="^VOICEVOX source assembly review is invalid$",
    ):
        review_voicevox_source_assembly(enabled=True, intent=intent)


def test_model_tts_cannot_be_substituted_for_voicevox():
    v = values("style-bert-vits2")
    original = static_intent()
    mixed = replace(
        original,
        catalog=v["catalog"],
        declaration=v["declaration"],
        worker=v["worker"],
        snapshot=v["snapshot"],
        runtime=v["runtime"],
    )
    with pytest.raises(VoicevoxSourceAssemblyError):
        review_voicevox_source_assembly(enabled=True, intent=mixed)


def _script(*, uid: str, version: str, wav: bytes, drift_file: str) -> str:
    return f"""
import base64, json, os, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
UUID = {uid!r}
VERSION = {version!r}
WAV = base64.b64decode({base64.b64encode(wav).decode("ascii")!r})
DRIFT = {drift_file!r}
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *_args):
        pass  # Test-only; never conclude real engine logging is private.
    def send(self, body, content_type="application/json"):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def do_GET(self):
        if self.path == "/version":
            answer = "drifted" if os.path.exists(DRIFT) else VERSION
        elif self.path == "/speakers":
            answer = [{{"speaker_uuid": UUID, "styles": [{{"id":3,"name":"test"}}]}}]
        else:
            self.send_error(404)
            return
        self.send(json.dumps(answer).encode("utf-8"))
    def do_POST(self):
        route = urlsplit(self.path)
        params = parse_qs(route.query)
        if params.get("speaker") != ["3"]:
            self.send_error(400)
            return
        if route.path == "/audio_query":
            if not params.get("text"):
                self.send_error(400)
                return
            answer = {{"accent_phrases": [], "outputSamplingRate": 24000,
                       "outputStereo":False}}
            self.send(json.dumps(answer).encode("utf-8"))
        elif route.path == "/synthesis":
            length = int(self.headers["Content-Length"])
            if "accent_phrases" not in json.loads(
                self.rfile.read(length).decode("utf-8")
            ):
                self.send_error(400)
                return
            self.send(WAV, "audio/wav")
        else:
            self.send_error(404)
ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
"""


def real_intent(tmp_path: Path) -> VoicevoxSourceAssemblyIntent:
    from test_voicevox_provider import UUID

    drift = tmp_path / "drift-version"
    script = _script(
        uid=UUID,
        version="test-version.1",
        wav=WAV,
        drift_file=str(drift),
    )
    cfg, launch, _runner, _controller = make_linux_process(
        tmp_path,
        runner_code=script,
    )
    pin = cfg.adapter.voices[0]
    voice = VoiceAlias(
        alias=pin.alias,
        speaker_id=pin.speaker_uuid,
        style_id=str(pin.style_id),
        locale="ja",
        license_id=pin.license_id,
        attribution=pin.attribution,
    )
    semantic = audio_semantic_revision("voicevox", (voice,))
    contract = replace(
        cfg.serving.contracts[0],
        semantic_revision=semantic,
        limits={"input_bytes": 64, "request_bytes": 1024},
    )
    serving = ServingDeploymentDeclaration(
        deployment=cfg.serving.deployment,
        contracts=(contract,),
    )
    cfg = replace(
        cfg,
        adapter=replace(
            cfg.adapter,
            engine_version="test-version.1",
            semantic_revision=semantic,
        ),
        serving=serving,
    )
    logical = LogicalServingProfile(
        profile_id="synthetic-b6d-review",
        deployment_revision=serving.deployment.revision,
        serving_contract_revision=contract.revision,
        capability="audio.synthesize",
        operation_schema="openai-audio-speech-v1",
        required_features=frozenset({"ja", "wav"}),
    )
    catalog = AudioProfileCatalog(
        (
            AudioGatewayProfile(
                resolve_profile(logical, contract, serving.deployment),
                "voicevox",
                (voice,),
            ),
        )
    )
    worker = WorkerSpec(
        worker_id=cfg.worker_resource_id,
        worker_class="synthetic",
        resources=ResourceShape(),
        capabilities=frozenset({"audio.synthesize"}),
    )
    snapshot = SetupHostSnapshot(
        runtime_host=RuntimeHostFacts(cpu_count=4, host_ram_mb=8192),
        deployment_path=DeploymentPath.NIXOS,
        os_id="nixos",
    )
    return VoicevoxSourceAssemblyIntent(
        profile_id=logical.profile_id,
        catalog=catalog,
        declaration=serving,
        worker=worker,
        snapshot=snapshot,
        runtime=cfg,
        launch=launch,
        http=VoicevoxHttpConfig(
            endpoint=cfg.endpoint,
            adapter=cfg.adapter,
            request_timeout_seconds=0.2,
        ),
    )


@pytest.mark.asyncio
async def test_real_disposable_linux_http_wav_roundtrip_no_global_registry(tmp_path):
    intent = real_intent(tmp_path)
    review = review_voicevox_source_assembly(enabled=True, intent=intent)
    assert not review.can_apply
    runtime = review.compose_disposable_source_runtime()
    media = FakeMedia()
    try:
        assert (await runtime.health()).state is RuntimeHealthState.STOPPED
        await runtime.start()
        health = await runtime.health()
        assert health.ready and health.state is RuntimeHealthState.READY
        result = await runtime.executor().execute_audio(
            job(
                text="日本語の合成検証",
                metadata={
                    "serving_semantic_revision": intent.runtime.adapter.semantic_revision
                },
            ),
            media,
        )
        assert len(result.artifacts) == 1 and result.text is None
        assert media.calls and media.calls[0][0] == WAV

        # A live version change invalidates the previously qualified session.
        (tmp_path / "drift-version").write_text("drift")
        health = await runtime.health()
        assert not health.ready and health.state is RuntimeHealthState.FAILED
        with pytest.raises(VoicevoxRuntimeFailure):
            await runtime.executor().execute_audio(
                job(text="secret text"),
                FakeMedia(),
            )
    finally:
        await runtime.release()
    assert not (await runtime.health()).ready
    assert "voicevox" not in _PROVIDER_TYPES


@pytest.mark.asyncio
async def test_foreign_listener_is_never_adopted_or_terminated(tmp_path):
    import socket

    intent = real_intent(tmp_path)
    port = urlsplit(intent.runtime.endpoint).port
    with socket.socket() as foreign:
        foreign.bind(("127.0.0.1", port))
        foreign.listen(1)
        review = review_voicevox_source_assembly(enabled=True, intent=intent)
        runtime = review.compose_disposable_source_runtime()
        with pytest.raises(VoicevoxRuntimeFailure):
            await runtime.start()
        assert not (await runtime.health()).ready
        assert runtime._process._proc is None
        assert foreign.getsockname()[1] == port
        await runtime.release()


@pytest.mark.asyncio
async def test_real_model_file_mutation_before_start_is_denied_without_child(tmp_path):
    intent = real_intent(tmp_path)
    review = review_voicevox_source_assembly(enabled=True, intent=intent)
    runtime = review.compose_disposable_source_runtime()
    # The review was pure: only the native process preflight can hash bytes.
    Path(intent.runtime.model_artifact_path).write_bytes(b"altered model fixture")
    with pytest.raises(VoicevoxRuntimeFailure):
        await runtime.start()
    assert runtime._process._proc is None
    assert not (await runtime.health()).ready
    await runtime.release()


@pytest.mark.parametrize(
    ("field_owner", "attribute", "tampered"),
    (
        ("runtime", "endpoint", "http://127.0.0.1:PRIVATE_SECRET_PORT"),
        ("launch", "fixed_args", None),
        ("http", "request_timeout_seconds", "PRIVATE_SECRET_TIMEOUT"),
        ("worker", "resources", None),
    ),
)
def test_mutated_frozen_input_is_rejected_with_fixed_redacted_error(
    field_owner,
    attribute,
    tampered,
):
    """Reflective drift must not reveal private configuration in exceptions."""
    intent = static_intent()
    before = review_voicevox_source_assembly(enabled=True, intent=intent)
    object.__setattr__(getattr(intent, field_owner), attribute, tampered)
    for operation in (
        lambda: review_voicevox_source_assembly(enabled=True, intent=intent),
        before.compose_disposable_source_runtime,
    ):
        with pytest.raises(VoicevoxSourceAssemblyError) as error:
            operation()
        assert str(error.value) == "VOICEVOX source assembly review is invalid"
        assert "PRIVATE_SECRET" not in str(error.value)


def test_unrelated_positional_port_token_cannot_qualify_launch():
    intent = static_intent()
    mutated = replace(
        intent,
        launch=replace(intent.launch, fixed_args=("--not-an-engine-port", "51000")),
    )
    with pytest.raises(VoicevoxSourceAssemblyError):
        review_voicevox_source_assembly(enabled=True, intent=mutated)


def test_reflective_multi_field_drift_does_not_bypass_review_digest():
    intent = static_intent()
    reviewed = review_voicevox_source_assembly(enabled=True, intent=intent)
    # Keep the two executable paths internally consistent while drifting
    # both values away from the originally approved configuration.
    object.__setattr__(intent.runtime, "executable_path", "/opt/private/unreviewed")
    object.__setattr__(intent.launch, "executable_path", "/opt/private/unreviewed")
    # The new declaration is still self-consistent, but no longer the
    # reviewed version. An assembly review never constitutes an authorization.
    drift = review_voicevox_source_assembly(enabled=True, intent=intent)
    assert drift.assembly_digest != reviewed.assembly_digest
    with pytest.raises(VoicevoxSourceAssemblyError):
        reviewed.compose_disposable_source_runtime()


def test_disposable_source_constructor_makes_no_file_socket_or_process_io(
    monkeypatch,
):
    import asyncio
    import socket

    intent = static_intent()
    reviewed = review_voicevox_source_assembly(enabled=True, intent=intent)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("disposable constructor attempted I/O")

    with monkeypatch.context() as patch:
        patch.setattr("builtins.open", forbidden)
        patch.setattr(os, "open", forbidden)
        patch.setattr(os, "stat", forbidden)
        patch.setattr(Path, "read_bytes", forbidden)
        patch.setattr(socket, "socket", forbidden)
        patch.setattr(asyncio, "create_subprocess_exec", forbidden)
        runtime = reviewed.compose_disposable_source_runtime()
        assert runtime._process._proc is None
        assert runtime._state is RuntimeHealthState.STOPPED
    assert not reviewed.can_apply


def test_pinned_auxiliary_port_must_be_directly_adjacent():
    intent = static_intent()
    pin = intent.runtime.adapter.engine_artifact_sha256
    auxiliary = "/opt/private/pinned-runner.py"
    normal = replace(
        intent.launch,
        fixed_args=("-I", "-u", auxiliary, "51000"),
        auxiliary_path=auxiliary,
        auxiliary_sha256=pin,
    )
    assert (
        review_voicevox_source_assembly(
            enabled=True, intent=replace(intent, launch=normal)
        ).status
        == "review_only_blocked"
    )
    unrelated = replace(
        normal, fixed_args=("-I", "-u", auxiliary, "--unrelated", "51000")
    )
    with pytest.raises(VoicevoxSourceAssemblyError):
        review_voicevox_source_assembly(
            enabled=True, intent=replace(intent, launch=unrelated)
        )
