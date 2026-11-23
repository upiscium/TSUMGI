"""VOICEVOX manifest admission through the real Worker daemon (synthetic native edges)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tomllib
from dataclasses import asdict, replace
from uuid import UUID

import httpx
import pytest

from astrumweaver.control import InMemoryControlRepository
from astrumweaver.control.api import create_app
from astrumweaver.runtime import RuntimeHostFacts
from astrumweaver.runtime.deployment import RuntimeDeploymentSpec, provider_from_deployment
from astrumweaver.runtime.contracts import (
    ExecutionDemand, GPUTopology, ModelDemand, ModelTopology, ResidencyPolicy,
)
from astrumweaver.runtime.providers import voicevox_managed, voicevox_manifest
from astrumweaver.serving import ServingDeploymentDeclaration
from astrumweaver.worker import daemon
from astrumweaver.worker.client import ControlClient


ARCHIVE = "sha256:483633de02700cd918731538543a22ce895877aeb09450a990ff38af6e4c26c0"
RIGHTS = "sha256:" + "1" * 64
EVIDENCE = "sha256:" + "2" * 64


class RegistrationObserved(Exception):
    """Stop at the real WorkerRuntime.register boundary, before any claim."""


class FakeClient:
    def __init__(self, *_args, **_kwargs):
        self.registrations = []
        self.registered = asyncio.Event()

    async def register(self, **kwargs):
        self.registrations.append(kwargs)
        self.registered.set()
        raise RegistrationObserved

    async def aclose(self):
        return None


class FakeHealthServer:
    def __init__(self, _config):
        self.should_exit = False

    async def serve(self):
        while not self.should_exit:
            await asyncio.sleep(0)


class FakeProcess:
    def __init__(self, *, hold_start=False):
        self.launch_calls = 0
        self.obs = None
        self.start_entered = asyncio.Event()
        self.start_release = asyncio.Event()
        if not hold_start:
            self.start_release.set()

    async def endpoint_owner(self):
        return self.obs.endpoint_owner_pid if self.obs else None

    async def inspect(self):
        return self.obs

    async def launch(self, *, config, nonce):
        self.launch_calls += 1
        self.start_entered.set()
        await self.start_release.wait()
        self.obs = voicevox_managed.VoicevoxProcessObservation(
            pid=9091, uid=config.owner_uid, endpoint_owner_pid=9091,
            executable_sha256=config.adapter.engine_artifact_sha256,
            model_artifact_sha256=config.adapter.model_artifact_sha256,
            endpoint=config.endpoint, worker_resource_id=config.worker_resource_id,
            deployment_revision=config.serving.deployment.revision, launch_nonce=nonce,
        )

    async def stop_owned(self, *, nonce):
        if self.obs is not None and self.obs.launch_nonce == nonce:
            self.obs = None


class MetadataPeer:
    def __init__(self, *, hold_metadata=False, fail=None):
        self.hold_metadata = hold_metadata
        self.fail = fail
        self.metadata_entered = asyncio.Event()
        self.metadata_release = asyncio.Event()
        self.probes = 0
        self.supervisor_drift = asyncio.Event()
        if not hold_metadata:
            self.metadata_release.set()

    async def request(self, method, path, **kwargs):
        assert path in ("/version", "/speakers")
        assert method == "GET" and kwargs["max_bytes"] > 0
        if path == "/version":
            return b'"wrong"' if self.fail == "version" else b'"0.26.2"'
        self.probes += 1
        self.metadata_entered.set()
        await self.metadata_release.wait()
        drift = self.fail == "style" or (self.fail == "supervisor" and self.probes == 3)
        if self.fail == "supervisor" and drift:
            self.supervisor_drift.set()
        return json.dumps([{
            "speaker_uuid": "388f246b-8c41-4ac1-8e2d-5d79f3ff56d9", "name": "ずんだもん",
            "styles": [{"id": 3, "name": "ノーマル", "type": "talk" if drift else "streaming_talk"}],
        }], ensure_ascii=False).encode()


def _write_json(path, obj):
    path.write_text(json.dumps(obj), encoding="utf-8")
    return str(path)


def test_standard_identity_uses_known_native_pins_but_not_machine_paths():
    cfg = voicevox_manifest.VoicevoxProviderConfig(
        enabled=True, engine_root="/private/engine", private_home="/private/home", port=12345,
        archive_sha256=ARCHIVE, rights_review_sha256=RIGHTS, validation_evidence_sha256=EVIDENCE,
    )
    d = cfg.serving_declaration()
    assert d.deployment.runtime_artifact_sha256 == "sha256:acf74848282c0ff6f0aaf8514427c5f44b0992c26b48448d76b35a97dbe81a25"
    assert d.deployment.model_artifact_sha256 == "sha256:ecd35374d4182cd883cba5040376f7f888cc6ba248b1c2f4cea07cdb34bb1318"
    assert d.contracts[0].semantic_revision == "sha256:4fe4bdd77d156a864b50ba9b8c449bb3a8fe2a58944bc53a181f08502bbce393"
    assert d == replace(cfg, engine_root="/different/tree", private_home="/different/empty").serving_declaration()
    assert "/private" not in json.dumps(d.to_dict())
    assert d.deployment != replace(cfg, port=12346).serving_declaration().deployment
    assert d != replace(cfg, validation_evidence_sha256=RIGHTS).serving_declaration()
    assert ServingDeploymentDeclaration.from_dict(json.loads(json.dumps(d.to_dict()))) == d


@pytest.mark.parametrize("changes", [
    {"enabled": 1}, {"archive_sha256": EVIDENCE}, {"private_home": "/"},
    {"engine_root": "/private/../engine"}, {"private_home": "/private/engine/home"},
    {"private_home": "/private"}, {"engine_root": "//private/share"}, {"port": True},
    {"rights_review_sha256": ""}, {"validation_evidence_sha256": "not-a-hash"},
])
def test_manifest_config_refuses_unreviewed_values(changes):
    fields = dict(enabled=True, engine_root="/private/engine", private_home="/private/home",
                  port=12345, archive_sha256=ARCHIVE, rights_review_sha256=RIGHTS,
                  validation_evidence_sha256=EVIDENCE)
    with pytest.raises(ValueError, match="^VOICEVOX manifest configuration is invalid$"):
        voicevox_manifest.VoicevoxProviderConfig(**(fields | changes))


def _setup(tmp_path, monkeypatch, *, hold_start=False, hold_metadata=False, fail=None,
           worker_changes=None):
    cfg = voicevox_manifest.VoicevoxProviderConfig(
        enabled=True, engine_root=str(tmp_path / "engine"),
        private_home=str(tmp_path / "private"), port=12345,
        archive_sha256=ARCHIVE, rights_review_sha256=RIGHTS,
        validation_evidence_sha256=EVIDENCE,
    )
    deployment = RuntimeDeploymentSpec(
        provider_id=voicevox_manifest.VOICEVOX_PROVIDER_ID,
        provider_config=asdict(cfg),
        demand=ExecutionDemand(
            model=ModelDemand(
                model_ref="model/0.vvm", model_format="vvm", topology=ModelTopology.DENSE,
            ),
            residency_policy=ResidencyPolicy.CPU_GPU_HYBRID,
            gpu_topology=GPUTopology.NONE,
        ),
    )
    declaration = cfg.serving_declaration()
    runtime_path = _write_json(tmp_path / "runtime.json", deployment.to_dict())
    serving_path = _write_json(tmp_path / "serving.json", declaration.to_dict())
    control = tmp_path / "control.toml"
    worker = {
        "id": "vv-cpu-worker", "class": "cpu", "control_url": "http://127.0.0.1:18080",
        "capabilities": ["audio.synthesize"], "gpu_count": 0, "gpu_uuids": [],
        "max_concurrency": 1, "telemetry_interval_seconds": 0,
    } | (worker_changes or {})
    control.write_text(
        "[worker]\n" + "\n".join(
            f'{key} = {json.dumps(value)}' for key, value in worker.items()
        ) + f'\n[runtime]\nmanifest = "{runtime_path}"\nstartup_timeout_seconds = 2\n'
        f'[serving]\nmanifest = "{serving_path}"\n',
        encoding="utf-8",
    )
    assert tomllib.loads(control.read_text())["runtime"]["manifest"] == runtime_path

    process = FakeProcess(hold_start=hold_start)
    engine = MetadataPeer(hold_metadata=hold_metadata, fail=fail)
    monkeypatch.setattr(voicevox_manifest, "LinuxVoicevoxOwnedProcessControl", lambda **_: process)
    monkeypatch.setattr(voicevox_manifest.VoicevoxLoopbackHttpApi, "_request", engine.request)
    hashes = {
        str(Path(cfg.engine_root) / "run"): voicevox_manifest.EXECUTABLE_SHA256,
        str(Path(cfg.engine_root) / "model/0.vvm"): (
            RIGHTS if fail == "model_bytes" else voicevox_manifest.MODEL_SHA256
        ),
    }
    monkeypatch.setattr(voicevox_managed, "_digest_regular_file", hashes.__getitem__)
    monkeypatch.setattr(daemon, "discover_runtime_host_facts",
                        lambda: RuntimeHostFacts(cpu_count=4, host_ram_mb=4096, architecture="x86_64"))
    monkeypatch.setattr(voicevox_manifest.os, "geteuid", lambda: 1000)
    clients = []

    def make_client(*args, **kwargs):
        client = FakeClient(*args, **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(daemon, "ControlClient", make_client)
    monkeypatch.setattr(daemon.uvicorn, "Config", lambda *_a, **_kw: object())
    monkeypatch.setattr(daemon.uvicorn, "Server", FakeHealthServer)
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda *_a: None)
    monkeypatch.setenv("ASTRUMWEAVER_WORKER_TOKEN", "synthetic-token")
    return control, process, engine, declaration, clients


async def _cancel_worker(task):
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_registers_once_only_after_ready_and_independent_supervisor_probe(
    tmp_path, monkeypatch,
):
    config, process, engine, declaration, clients = _setup(
        tmp_path, monkeypatch, hold_start=True, hold_metadata=True,
    )
    task = asyncio.create_task(daemon.run_worker(str(config)))
    try:
        await asyncio.wait_for(process.start_entered.wait(), 2)
        assert not clients[0].registrations
        process.start_release.set()
        await asyncio.wait_for(engine.metadata_entered.wait(), 2)
        # Metadata remains deliberately blocked, so the daemon cannot register.
        assert not clients[0].registrations
        engine.metadata_release.set()
        with pytest.raises(RegistrationObserved):
            await asyncio.wait_for(task, 2)
        client, = clients
        assert len(client.registrations) == 1
        advertisement = client.registrations[0]["serving"]
        assert advertisement.deployment_revision == declaration.deployment.revision
        assert advertisement.contracts == declaration.contracts
        assert UUID(advertisement.runtime_instance.epoch).version == 4
        assert engine.probes >= 3 and process.launch_calls == 1
    finally:
        await _cancel_worker(task)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", [
    "disabled", "no-enable", "deployment", "model", "semantic", "cap", "concurrency",
    "bool-concurrency", "gpu", "limits", "evidence", "adapter", "declared_model",
    "other_provider", "missing_serving", "rights_review", "setup_intent",
    "ambiguous_runtime", "ambiguous_serving", "unexpected_option", "unreviewed_demand",
])
async def test_rejected_manifest_or_worker_shape_has_no_registration_or_launch(
    tmp_path, monkeypatch, case,
):
    worker = {"cap": {"capabilities": ["audio.synthesize", "debug.echo"]},
              "concurrency": {"max_concurrency": 2}, "bool-concurrency": {"max_concurrency": True},
              "gpu": {"gpu_count": 1}}.get(case, {})
    config, process, _, declaration, clients = _setup(tmp_path, monkeypatch, worker_changes=worker)
    runtime = tmp_path / "runtime.json"
    raw = json.loads(runtime.read_bytes())
    if case == "disabled":
        raw["provider_config"]["enabled"] = False
    elif case == "no-enable":
        del raw["provider_config"]["enabled"]
    elif case == "deployment":
        raw["provider_config"]["port"] += 1
    elif case == "rights_review":
        raw["provider_config"]["rights_review_sha256"] = EVIDENCE
    elif case == "model":
        raw["demand"]["model"]["model_ref"] = "model/other.vvm"
    elif case == "setup_intent":
        raw["setup_intent"] = {"provider_id": "voicevox", "model_preparation": "download"}
    elif case == "unexpected_option":
        raw["provider_config"]["engine_url"] = "ignored-value"
    elif case == "unreviewed_demand":
        raw["demand"]["min_gpu_count"] = False
    _write_json(runtime, raw)
    if case == "ambiguous_runtime":
        runtime.write_text(runtime.read_text().replace('"enabled": true', '"enabled": false, "enabled": true'))
    if case == "ambiguous_serving":
        sp = tmp_path / "serving.json"
        sp.write_text(sp.read_text().replace('"provider_id": "voicevox"',
                                            '"provider_id": "other", "provider_id": "voicevox"'))
    if case in ("adapter", "declared_model", "other_provider", "semantic", "evidence", "limits"):
        # Self-consistent changed documents, not broken serialized revision fields.
        dep = replace(declaration.deployment, **{
            "adapter": {"adapter_artifact_sha256": RIGHTS},
            "declared_model": {"model_artifact_sha256": RIGHTS},
            "other_provider": {"provider_id": "unrelated"},
        }.get(case, {}))
        contract = replace(declaration.contracts[0], deployment_revision=dep.revision, **{
            "semantic": {"semantic_revision": RIGHTS}, "evidence": {"validation_evidence_sha256": RIGHTS},
            "limits": {"limits": {"input_bytes": 2048, "request_bytes": 4096}},
        }.get(case, {}))
        _write_json(tmp_path / "serving.json", ServingDeploymentDeclaration(dep, (contract,)).to_dict())
    elif case == "missing_serving":
        config.write_text(config.read_text().split("[serving]")[0])
    with pytest.raises((RuntimeError, ValueError)):
        await daemon.run_worker(str(config))
    assert process.launch_calls == 0
    assert not any(c.registrations for c in clients)


@pytest.mark.parametrize("fail", ["version", "style", "model_bytes"])
async def test_runtime_observation_mismatch_never_registers(tmp_path, monkeypatch, fail):
    config, process, _, _, clients = _setup(tmp_path, monkeypatch, fail=fail)
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(daemon.run_worker(str(config)), 2)
    assert process.launch_calls == (0 if fail == "model_bytes" else 1)
    assert not clients[0].registrations and process.obs is None


async def test_manifest_registry_entry_is_not_a_setup_provider(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    deployment = daemon._load_runtime_deployment(str(tmp_path / "runtime.json"))
    provider = provider_from_deployment(deployment)
    with pytest.raises(ValueError, match="SetupPlan"):
        provider.setup_intent(None)
    with pytest.raises(ValueError, match="serving manifest"):
        provider.create_runtime(None, None)


@pytest.mark.parametrize("host_change", ["root", "aarch64"])
async def test_wrong_platform_or_root_never_launches(tmp_path, monkeypatch, host_change):
    config, process, _, _, clients = _setup(tmp_path, monkeypatch)
    if host_change == "root":
        monkeypatch.setattr(voicevox_manifest.os, "geteuid", lambda: 0)
    else:
        monkeypatch.setattr(daemon, "discover_runtime_host_facts", lambda: RuntimeHostFacts(2, 4096, "aarch64"))
    with pytest.raises(RuntimeError, match="incompatible"):
        await daemon.run_worker(str(config))
    assert process.launch_calls == 0 and not any(c.registrations for c in clients)


async def test_first_supervisor_probe_can_still_withhold_registration(tmp_path, monkeypatch):
    config, _, peer, _, clients = _setup(tmp_path, monkeypatch, fail="supervisor")
    denied = asyncio.Event()
    real_check = daemon.RuntimeHealthSupervisor.check

    async def check(self):
        result = await real_check(self)
        if not result:
            denied.set()
        return result

    monkeypatch.setattr(daemon.RuntimeHealthSupervisor, "check", check)
    task = asyncio.create_task(daemon.run_worker(str(config)))
    try:
        await asyncio.wait_for(denied.wait(), 2)
        assert peer.supervisor_drift.is_set() and not clients[0].registrations
    finally:
        await _cancel_worker(task)


async def test_standard_worker_registration_ack_and_epoch_readback(tmp_path, monkeypatch):
    config, _, metadata, declaration, _ = _setup(tmp_path, monkeypatch, hold_metadata=True)
    repo = InMemoryControlRepository()
    control = create_app(repo, client_token="synthetic-client", worker_token="synthetic-token")
    accepted = asyncio.Event()
    replies = []

    async def observe(scope, receive, send):
        async def capture(message):
            if scope.get("path") == "/v1/workers/register" and message["type"] == "http.response.start":
                replies.append(message["status"])
            await send(message)
        await control(scope, receive, capture)
        if scope.get("path") == "/v1/workers/register":
            accepted.set()

    monkeypatch.setattr(daemon, "ControlClient", lambda *a, **kw: ControlClient(
        *a, **kw, transport=httpx.ASGITransport(app=observe),
    ))
    task = asyncio.create_task(daemon.run_worker(str(config)))
    try:
        await asyncio.wait_for(metadata.metadata_entered.wait(), 2)
        assert not replies and not repo.list_workers()
        metadata.metadata_release.set()
        await asyncio.wait_for(accepted.wait(), 2)
        record, = repo.list_workers()
        assert record.serving == declaration.advertisement(epoch=record.serving.runtime_instance.epoch)
        assert UUID(record.serving.runtime_instance.epoch).version == 4
        assert record.worker_id == "vv-cpu-worker" and record.state.value == "online"
        assert replies == [201] and not repo.list_jobs()
    finally:
        await _cancel_worker(task)
