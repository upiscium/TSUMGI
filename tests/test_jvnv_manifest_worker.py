"""JVNV manifest admission through daemon.run_worker with a synthetic child."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, replace
import hashlib
import json
import sys
from pathlib import Path
from uuid import UUID

import httpx
import pytest

from tsumgi.audio import ArtifactSpool, AudioOutputChannel
from tsumgi.control import InMemoryControlRepository, JobStatus
from tsumgi.control.api import create_app
from tsumgi.gateway.audio import AudioGatewayProfile, AudioProfileCatalog, VoiceAlias
from tsumgi.gateway.audio_ingress import AudioIngressLimits
from tsumgi.gateway.audio_speech_api import AudioSpeechLimits, create_audio_speech_router
from tsumgi.runtime import RuntimeHostFacts
from tsumgi.runtime.deployment import RuntimeDeploymentSpec
from tsumgi.runtime.contracts import (
    ExecutionDemand, GPUTopology, ModelDemand, ModelTopology, ResidencyPolicy,
)
from tsumgi.runtime.providers import jvnv_assets, style_bert_vits2_manifest, voicevox_manifest
from tsumgi.serving import LogicalServingProfile, ServingDeploymentDeclaration, resolve_profile
from tsumgi.worker import daemon
from tsumgi.worker.client import ControlClient


RIGHTS = "sha256:" + "1" * 64
EVIDENCE = "sha256:" + "2" * 64


# Receives the actual bootstrap frame, echoes its identity fields, then sends
# the valid PCM16 probe required by the real managed runtime.
CHILD = r'''
import hashlib,json,struct,sys,time
from pathlib import Path
FAULT=__FAULT__
def read():
 n=struct.unpack(">I",sys.stdin.buffer.read(4))[0]
 return json.loads(sys.stdin.buffer.read(n))
def send(x):
 b=json.dumps(x,separators=(",",":")).encode()
 sys.stdout.buffer.write(struct.pack(">I",len(b))+b); sys.stdout.buffer.flush()
r=read()
w=b"RIFF"+struct.pack("<I",36+24000)+b"WAVEfmt "+struct.pack("<IHHIIHH",16,1,1,44100,88200,2,16)+b"data"+struct.pack("<I",24000)+((b"\x00\x00" if FAULT=="silence" else b"\x01\x00")*12000)
reply={k:v for k,v in r.items() if k not in ("op","artifact_paths","probe_format")}
reply.update(op="ready",probe_status="synthesized",probe_pcm16=True,probe_format="pcm16-wav-v1",probe_rate=44100,probe_frames=12000,probe_bytes=len(w),probe_sha256=hashlib.sha256(w).hexdigest())
if FAULT=="digest": reply["probe_sha256"]="0"*64
if FAULT=="metadata": del reply["probe_frames"]
if FAULT=="oversize": reply["probe_bytes"]=2**30
send(reply)
if FAULT=="no_bytes": sys.exit(0)
if FAULT=="probe_gate":
 Path("header-sent").touch()
 while not Path("release-wav").exists(): time.sleep(.005)
sys.stdout.buffer.write(w); sys.stdout.buffer.flush()
while True:
 h=sys.stdin.buffer.read(4)
 if not h: break
 n=struct.unpack(">I",h)[0]; job=json.loads(sys.stdin.buffer.read(n))
 if FAULT=="job_gate":
  Path("entered").touch()
  while not Path("resume").exists(): time.sleep(.005)
 assert job["op"]=="infer" and job["epoch"]==r["epoch"] and job["text"]=="別の文"
 data=w[:-2]+b"\x02\x00"
 send(dict(version=1,op="audio",status="ok",epoch=r["epoch"],request_id=job["request_id"],wav_length=len(data)))
 sys.stdout.buffer.write(data); sys.stdout.buffer.flush()
'''


def _write(path: Path, obj: dict) -> str:
    path.write_text(json.dumps(obj), encoding="utf-8")
    return str(path)


@dataclass
class Harness:
    config: Path
    serving: ServingDeploymentDeclaration
    repo: InMemoryControlRepository
    statuses: list
    started: asyncio.Event
    release: asyncio.Event
    children: list
    app: object
    output: AudioOutputChannel


def _setup(tmp_path: Path, monkeypatch, *, hold=False, fault="none"):
    model = tmp_path / "model" / "jvnv-F1-jp"
    model.mkdir(parents=True)
    (model / jvnv_assets.WEIGHT_FILENAME).write_bytes(b"synthetic")
    (model / "config.json").write_text(json.dumps({"model_name": "jvnv-F1-jp", "data": {
        "sampling_rate": 44100, "spk2id": {"jvnv-F1-jp": 0}, "style2id": {"Neutral": 0}, "use_jp_extra": True}}))
    (model / "style_vectors.npy").write_bytes(b"synthetic")
    dependency = tmp_path / "dependency.lock"
    dependency.write_text("synthetic")
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    # Config path shape is validated by the real manifest config. Spawn argv
    # is redirected below to a stdlib-only test child.
    interp = tmp_path / "venv" / "bin" / "python"
    interp.parent.mkdir(parents=True)
    interp.write_bytes(b"synthetic interpreter placeholder")
    # Pin tiny, known fixture contents instead of bypassing asset verifiers.
    for name, path in (
        ("MODEL_SHA256", model / jvnv_assets.WEIGHT_FILENAME), ("CONFIG_SHA256", model / "config.json"),
        ("STYLES_SHA256", model / "style_vectors.npy"), ("INTERPRETER_SHA256", interp), ("LOCK_SHA256", dependency),
    ):
        monkeypatch.setattr(jvnv_assets, name, "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest())
    bert = model.parent / "bert"
    bert.mkdir()
    for name, (sha, _) in jvnv_assets._BERT_BLOBS.items():
        raw = name.encode()
        (bert / name).write_bytes(raw)
        pin = ((hashlib.sha256(raw).hexdigest(), None) if sha else
               (None, hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest()))
        monkeypatch.setitem(jvnv_assets._BERT_SIZES, name, len(raw))
        monkeypatch.setitem(jvnv_assets._BERT_BLOBS, name, pin)
    dictionary = tmp_path / "dictionary"
    dictionary.mkdir()
    for name in jvnv_assets._DICT_CORE:
        (dictionary / name).write_bytes(name.encode())
    dictionary_digest = jvnv_assets.dictionary_inventory_sha256(str(dictionary))
    cfg = style_bert_vits2_manifest.StyleBertManifestConfig(
        model_directory=str(model), interpreter_path=str(interp),
        dependency_lock_path=str(dependency), private_home=str(private),
        dictionary_directory=str(dictionary), dictionary_sha256=dictionary_digest,
        rights_review_sha256=RIGHTS, validation_evidence_sha256=EVIDENCE,
        enabled=True,
    )
    declaration = cfg.serving_declaration()
    deployment = RuntimeDeploymentSpec(
        provider_id=style_bert_vits2_manifest._ID,
        provider_config=asdict(cfg),
        demand=ExecutionDemand(
            model=ModelDemand("jvnv-F1-jp", "safetensors", ModelTopology.DENSE),
            residency_policy=ResidencyPolicy.CPU_GPU_HYBRID, gpu_topology=GPUTopology.NONE,
        ),
    )
    runtime_path = _write(tmp_path / "runtime.json", deployment.to_dict())
    serving_path = _write(tmp_path / "serving.json", declaration.to_dict())
    control = tmp_path / "worker.toml"
    control.write_text(
        '[worker]\nid = "jvnv-test"\nclass = "cpu"\n'
        'control_url = "http://127.0.0.1:18080"\ncapabilities = ["audio.synthesize"]\n'
        'gpu_count = 0\ngpu_uuids = []\nmax_concurrency = 1\ntelemetry_interval_seconds = 0\n'
        'poll_interval_seconds = 0.01\nheartbeat_interval_seconds = 0.05\n'
        f'[runtime]\nmanifest = "{runtime_path}"\nstartup_timeout_seconds = 4\n'
        f'[serving]\nmanifest = "{serving_path}"\n', encoding="utf-8",
    )
    real_spawn = asyncio.create_subprocess_exec
    started = asyncio.Event()
    release = asyncio.Event()
    children = []
    real_uid = style_bert_vits2_manifest.os.geteuid()
    # Use a fake compatibility UID only if CI is itself run as root. Do not
    # alter the uid used by path ownership checks on actual test fixtures.
    if real_uid == 0:
        from types import SimpleNamespace
        monkeypatch.setattr(style_bert_vits2_manifest, "os", SimpleNamespace(
            geteuid=lambda: 65534, path=style_bert_vits2_manifest.os.path))
    if not hold:
        release.set()

    async def spawn(*argv, **kwargs):
        # Preserve real asyncio subprocess ownership/process lifecycle, replacing
        # only the executable target with a tiny standard-library protocol peer.
        assert argv[0] == str(interp) and "-I" in argv and Path(argv[-1]).name == "jvnv_child.py"
        assert kwargs["cwd"] == kwargs["env"]["HOME"] == kwargs["env"]["TMPDIR"] == str(private)
        assert kwargs["env"]["CUDA_VISIBLE_DEVICES"] == "" and kwargs["env"]["HF_HUB_OFFLINE"] == "1"
        assert "TSUMGI_WORKER_TOKEN" not in kwargs["env"]
        child = await real_spawn(sys.executable, "-I", "-u", "-c",
                                 CHILD.replace("__FAULT__", repr(fault)), **kwargs)
        children.append(child)
        started.set()
        await release.wait()
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(daemon, "discover_runtime_host_facts",
                        lambda: RuntimeHostFacts(cpu_count=4, host_ram_mb=8192, architecture="x86_64"))
    monkeypatch.setenv("TSUMGI_WORKER_TOKEN", "synthetic-token")
    # Keep the lease shorter than the request deadline: otherwise the
    # repository correctly caps both the initial expiry and its renewal.
    repo = InMemoryControlRepository(lease_seconds=5)
    spool_root = tmp_path / "spool"
    spool_root.mkdir(mode=0o700)
    output = AudioOutputChannel(repo, ArtifactSpool(spool_root, max_bytes=65536,
                               max_total_bytes=131072, max_artifacts=2, ttl_seconds=30),
                                max_bytes=65536)
    app = create_app(repo, client_token="synthetic-client", worker_token="synthetic-token",
                     audio_output_channel=output)
    statuses = []

    async def observe(scope, receive, send):
        async def capture(message):
            if scope.get("path") == "/v1/workers/register" and message["type"] == "http.response.start":
                statuses.append(message["status"])
            await send(message)
        await app(scope, receive, capture)

    monkeypatch.setattr(daemon, "ControlClient", lambda *a, **kw: ControlClient(
        *a, **kw, transport=httpx.ASGITransport(app=observe),
    ))
    monkeypatch.setattr(daemon.uvicorn, "Config", lambda *_a, **_kw: object())
    monkeypatch.setattr(daemon.uvicorn, "Server", lambda _c: _HealthServer())
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda *_a: None)
    return Harness(control, declaration, repo, statuses, started, release, children, app, output)


class _HealthServer:
    should_exit = False

    async def serve(self):
        while not self.should_exit:
            await asyncio.sleep(0.01)


async def _cancel(task):
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_standard_manifest_registers_online_worker_once(tmp_path, monkeypatch):
    h = _setup(tmp_path, monkeypatch)
    task = asyncio.create_task(daemon.run_worker(str(h.config)))
    try:
        async with asyncio.timeout(4):
            while not h.statuses:
                if task.done(): await task
                await asyncio.sleep(.005)
        assert h.statuses == [201]
        worker, = h.repo.list_workers()
        assert worker.state.value == "online"
        assert worker.serving.deployment_revision == h.serving.deployment.revision
        assert worker.serving.contracts == h.serving.contracts
        assert UUID(worker.serving.runtime_instance.epoch).version == 4
    finally:
        await _cancel(task)


@pytest.mark.parametrize("case", ["disabled", "omitted", "provider", "serving", "voicevox", "model_pin",
                                  "runtime_pin", "semantic", "evidence", "setup", "bad_lock", "bad_weight",
                                  "unexpected_encoder", "extra_config", "slots"])
@pytest.mark.asyncio
async def test_invalid_manifest_metadata_never_registers(tmp_path, monkeypatch, case):
    h = _setup(tmp_path, monkeypatch)
    control, decl = h.config, h.serving
    runtime_path = tmp_path / "runtime.json"
    raw = json.loads(runtime_path.read_text())
    if case in ("disabled", "omitted", "provider", "setup", "extra_config"):
        if case == "disabled":
            raw["provider_config"]["enabled"] = False
        if case == "omitted":
            del raw["provider_config"]["enabled"]
        if case == "provider":
            raw["provider_id"] = "not-style-bert-vits2"
        if case == "setup":
            raw["setup_intent"] = {"provider_id": decl.deployment.provider_id, "model_preparation": "download"}
        if case == "extra_config":
            raw["provider_config"]["runner_path"] = "/different/runner"
        _write(runtime_path, raw)
    elif case in ("serving","model_pin","runtime_pin","semantic","evidence","voicevox"):
        dep = replace(decl.deployment, **{
            "serving": {"provider_id": "other"}, "voicevox": {"provider_id": "voicevox"},
            "model_pin": {"model_artifact_sha256": RIGHTS}, "runtime_pin": {"runtime_artifact_sha256": RIGHTS},
        }.get(case, {}))
        c = replace(decl.contracts[0], deployment_revision=dep.revision, **{
            "semantic": {"semantic_revision": RIGHTS}, "evidence": {"validation_evidence_sha256": RIGHTS}
        }.get(case, {}))
        _write(tmp_path / "serving.json", ServingDeploymentDeclaration(dep, (c,)).to_dict())
    elif case == "bad_lock":
        Path(raw["provider_config"]["dependency_lock_path"]).write_bytes(b"changed")
    elif case == "bad_weight":
        (Path(raw["provider_config"]["model_directory"]) / jvnv_assets.WEIGHT_FILENAME).write_bytes(b"changed")
    elif case == "unexpected_encoder":
        (Path(raw["provider_config"]["model_directory"]).parent/"bert"/"pytorch_model.bin").touch()
    elif case == "slots":
        control.write_text(control.read_text().replace("max_concurrency = 1", "max_concurrency = 2"))
    with pytest.raises((RuntimeError, ValueError)):
        await daemon.run_worker(str(control))
    assert not h.children and not h.statuses and not h.repo.list_workers()


@pytest.mark.parametrize("fault", ["metadata", "no_bytes", "oversize", "digest", "silence"])
@pytest.mark.asyncio
async def test_unqualified_boot_probe_never_registers_and_reaps_child(
    tmp_path, monkeypatch, fault,
):
    h = _setup(tmp_path, monkeypatch, fault=fault)
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(daemon.run_worker(str(h.config)), 5)
    assert not h.statuses and not h.repo.list_workers()
    assert len(h.children) == 1 and h.children[0].returncode is not None


def _profile(declaration, profile_id, kind, voice):
    dep, (contract,) = declaration.deployment, declaration.contracts
    logical = LogicalServingProfile(profile_id, dep.revision, contract.revision, contract.capability,
                                    contract.operation_schema)
    return AudioGatewayProfile(resolve_profile(logical, contract, dep), kind, (voice,))


async def test_actual_worker_renews_and_publishes_job_not_probe(tmp_path, monkeypatch):
    h = _setup(tmp_path, monkeypatch, fault="job_gate")
    repo = h.repo
    voicevox = voicevox_manifest.VoicevoxProviderConfig(enabled=True, engine_root="/vv/engine",
        private_home="/vv/home", port=50021, archive_sha256=voicevox_manifest.ARCHIVE_SHA256,
        rights_review_sha256=RIGHTS, validation_evidence_sha256=EVIDENCE)
    jv = _profile(h.serving, "jvnv-smoke", "model-tts",
        VoiceAlias("jvnv-f1-neutral","0","0","ja","CC-BY-SA-4.0","litagin / JVNV (CC BY-SA 4.0)"))
    vv = _profile(voicevox.serving_declaration(), "voicevox-smoke", "voicevox",
        VoiceAlias("zundamon-normal","388f246b-8c41-4ac1-8e2d-5d79f3ff56d9","3","ja","voicevox-zundamon","VOICEVOX:ずんだもん"))
    h.app.include_router(create_audio_speech_router(AudioProfileCatalog((vv, jv)), repo, h.output,
        client_auth="bearer", client_token="synthetic-audio",
        ingress_limits=AudioIngressLimits(request_bytes=4096, input_bytes=256, file_bytes=256,
            metadata_bytes=256, part_header_bytes=256, total_header_bytes=2048,
            receive_events=32, idle_timeout_seconds=1, total_timeout_seconds=2),
        limits=AudioSpeechLimits(output_bytes=65536, request_timeout_seconds=12,
                                max_pending_admissions=1, poll_interval_seconds=.01)))
    task = asyncio.create_task(daemon.run_worker(str(h.config)))
    try:
        async with httpx.AsyncClient(base_url="http://asgi", transport=httpx.ASGITransport(app=h.app)) as client, asyncio.timeout(5):
            while not h.statuses:
                if task.done():
                    await task
                await asyncio.sleep(.005)
            assert h.statuses == [201]
            worker, = repo.list_workers()
            epoch = worker.serving.runtime_instance.epoch
            def post(profile):
                return client.post("/v1/audio/speech", headers={"Authorization": "Bearer synthetic-audio"},
                    json={"model": profile.profile_id, "voice": profile.voices[0].alias,
                          "input": "別の文", "response_format": "wav"})
            absent = await post(vv)
            assert absent.status_code == 503 and absent.json()["error"]["code"] == "no_compatible_deployment"
            assert not repo.list_jobs()
            async with asyncio.TaskGroup() as tasks:
                speech = tasks.create_task(post(jv))
                while not (tmp_path / "private/entered").is_file():
                    if speech.done():
                        pytest.fail(f"speech returned early: {speech.result().status_code}")
                    await asyncio.sleep(.005)
                before, = repo.list_jobs()
                assert before.serving == jv.binding and before.status is JobStatus.RUNNING
                assert before.lease_token and before.lease_expires_at
                while True:
                    after = repo.get_job(before.job_id)
                    assert after.status is JobStatus.RUNNING and after.result is None
                    assert after.attempts == before.attempts == 1
                    assert after.assigned_worker_id == before.assigned_worker_id == worker.worker_id
                    assert after.claimed_runtime_instance_epoch == before.claimed_runtime_instance_epoch == epoch
                    assert after.lease_token == before.lease_token
                    if after.lease_expires_at > before.lease_expires_at:
                        break
                    await asyncio.sleep(.005)
                (tmp_path / "private/resume").touch()
            done = repo.get_job(before.job_id)
            assert done.status is JobStatus.SUCCEEDED and done.claimed_runtime_instance_epoch == epoch
            ref, = done.result.artifacts
            artifact = await client.get(f"/v1/jobs/{done.job_id}/audio/output/{ref.uri.split(':')[1]}",
                                        headers={"Authorization": "Bearer synthetic-client"})
            # #318 G3: the speech service releases the WAV once it holds the bytes,
            # so a concurrent client read is either served or already 404.
            assert speech.result().status_code == 200
            assert artifact.status_code in (200, 404)
            data = speech.result().content
            if artifact.status_code == 200:
                assert artifact.content == data
            assert ref.digest == "sha256:" + hashlib.sha256(data).hexdigest()
            assert data.endswith(b"\x02\x00") and len(data) == ref.size_bytes == 24044
    finally:
        await _cancel(task)
    assert len(h.children) == 1 and h.children[0].returncode is not None


@pytest.mark.asyncio
async def test_registration_withheld_at_child_spawn_barrier_then_released(tmp_path, monkeypatch):
    h = _setup(tmp_path, monkeypatch, hold=True)
    task = asyncio.create_task(daemon.run_worker(str(h.config)))
    try:
        await asyncio.wait_for(h.started.wait(), 3)
        assert not h.statuses and not h.repo.list_workers()
        assert len(h.children) == 1 and h.children[0].returncode is None
        # The actual synthetic child exists, but bootstrap/readiness and
        # registration are held at this deterministic spawn barrier.
        h.release.set()
        for _ in range(500):
            if h.statuses:
                break
            await asyncio.sleep(0.01)
        assert h.statuses == [201]
    finally:
        h.release.set()
        await _cancel(task)
    assert len(h.children) == 1 and h.children[0].returncode is not None


async def test_ready_header_alone_cannot_register(tmp_path, monkeypatch):
    h = _setup(tmp_path, monkeypatch, fault="probe_gate")
    task = asyncio.create_task(daemon.run_worker(str(h.config)))
    try:
        async with asyncio.timeout(5):
            while not (tmp_path / "private/header-sent").exists():
                if task.done():
                    await task
                await asyncio.sleep(.005)
            assert not h.statuses and not h.repo.list_workers()
            (tmp_path / "private/release-wav").touch()
            while not h.statuses:
                if task.done():
                    await task
                await asyncio.sleep(.005)
        record, = h.repo.list_workers()
        assert h.statuses == [201] and record.serving == h.serving.advertisement(epoch=record.serving.runtime_instance.epoch)
    finally:
        await _cancel(task)
    assert len(h.children) == 1 and h.children[0].returncode is not None
