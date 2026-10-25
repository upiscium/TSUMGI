"""#174: PCM16 input is staged separately, bound on admission and lease-fenced."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from io import BytesIO
import json
from types import SimpleNamespace
import wave

import httpx
import pytest

from astrumweaver import ResourceShape, WorkerSpec
from astrumweaver.audio import ArtifactSpool
from astrumweaver.audio import AudioInputChannel
from astrumweaver.control import (
    InMemoryControlRepository, JobSubmission, PostgresControlRepository,
    WorkerRegistration, JobStatus, ClientAuthMode,
)
from astrumweaver.control.api import create_app
from astrumweaver.execution import JobResult
from astrumweaver.transport import PROTOCOL_VERSION, SERVING_EXTENSION
from test_audio_artifact_authority import EPOCH, serving_values
from test_audio_artifact_authority_postgres import isolated_database


CLIENT = {"authorization": "Bearer client-secret"}
WORKER = {"authorization": "Bearer worker-secret"}


def wav_bytes(frames=160, *, rate=16000, channels=1, width=2):
    bio = BytesIO()
    with wave.open(bio, "wb") as source:
        source.setnchannels(channels)
        source.setsampwidth(width)
        source.setframerate(rate)
        source.writeframes(bytes(frames * channels * width))
    return bio.getvalue()


WAV = wav_bytes()


def make_env(tmp_path, repo=None, *, clock=None, register=True, quota=2):
    root = tmp_path / "private-input"
    root.mkdir(mode=0o700)
    repo = repo or InMemoryControlRepository(lease_seconds=60)
    binding, serving = serving_values("audio.transcribe")
    if register:
        repo.register_worker(WorkerRegistration(
            spec=WorkerSpec(
                worker_id="owner", worker_class="cpu-test",
                resources=ResourceShape(),
                capabilities=frozenset({"audio.transcribe"}),
            ),
            serving=serving,
            max_concurrency=2,
        ))
    spool = ArtifactSpool(
        root, max_bytes=512, max_total_bytes=1024,
        max_artifacts=quota, ttl_seconds=30,
        **({"clock": clock} if clock else {}),
    )
    channel = AudioInputChannel(
        repo, spool, max_bytes=512, max_decoded_bytes=640,
        max_duration_seconds=10,
    )
    app = create_app(
        repo, client_token="client-secret", worker_token="worker-secret",
        audio_input_channel=channel,
    )
    return SimpleNamespace(repo=repo, channel=channel, app=app, binding=binding)


def submission(env, *, capability="audio.transcribe", **extra):
    return {
        "protocol_version": PROTOCOL_VERSION,
        "extensions": [SERVING_EXTENSION],
        "capability": capability,
        "serving": env.binding.to_dict(),
        "deadline_at": (datetime.now(UTC) + timedelta(minutes=3)).isoformat(),
        "max_attempts": 2,
        "payload": {"language": "ja"},
        **extra,
    }


def claim_headers(record, *, attempt=None, lease=None, epoch=EPOCH):
    return {
        **WORKER,
        "x-audio-attempt": str(record["attempts"] if attempt is None else attempt),
        "x-audio-lease-token": record["lease_token"] if lease is None else lease,
        "x-audio-runtime-epoch": epoch,
    }


def token(ref):
    assert ref["uri"].startswith("tsumgi-audio-input:")
    return ref["uri"].split(":", 1)[1]


async def stage(client, data=WAV, *, headers=None):
    return await client.post(
        "/v1/audio/input/stage",
        content=data,
        headers=({**CLIENT, "content-type": "audio/wav"} if headers is None else headers),
    )


async def admit(client, env, ref, **overrides):
    return await client.post(
        f"/v1/audio/input/{token(ref)}/jobs",
        json=submission(env, **overrides), headers=CLIENT,
    )


async def claim(client):
    return await client.post(
        "/v1/workers/owner/jobs/claim",
        json={
            "protocol_version": PROTOCOL_VERSION,
            "extensions": [SERVING_EXTENSION],
            "runtime_instance_epoch": EPOCH,
        },
        headers=WORKER,
    )


def input_url(claimed, ref, worker="owner"):
    return (
        f"/v1/workers/{worker}/jobs/{claimed['job_id']}"
        f"/audio/input/{token(ref)}"
    )


@pytest.mark.asyncio
async def test_client_stage_admit_worker_lease_read_and_terminal_cleanup(tmp_path):
    env = make_env(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env.app), base_url="http://control",
    ) as client:
        staged = await stage(client)
        assert staged.status_code == 201, staged.text
        ref = staged.json()
        assert ref["media_type"] == "audio/wav"
        assert ref["digest"].startswith("sha256:")
        assert ref["size_bytes"] == len(WAV)
        assert WAV not in json.dumps(ref).encode()

        admitted = await admit(client, env, ref)
        assert admitted.status_code == 201, admitted.text
        job = admitted.json()
        assert job["status"] == "queued"
        assert job["payload"]["audio_input"] == ref
        assert WAV not in json.dumps(job).encode()
        assert env.repo.get_job(job["job_id"]).payload["audio_input"] == ref

        again = await admit(client, env, ref)
        assert again.status_code == 409  # one-time pre-admission binding
        claimed = await claim(client)
        assert claimed.status_code == 200, claimed.text
        record = claimed.json()
        assert record["job_id"] == job["job_id"]
        assert record["attempts"] == 1
        url = input_url(record, ref)
        assert (await client.get(url, headers=CLIENT)).status_code == 401
        assert (await client.get(url, headers=claim_headers(record, lease="old"))).status_code == 409
        assert (await client.get(url, headers=claim_headers(record, attempt=2))).status_code == 409
        assert (await client.get(url, headers=claim_headers(record, epoch="stale"))).status_code == 409
        assert (await client.get(input_url(record, ref, "other"),
                                 headers=claim_headers(record))).status_code == 409

        downloaded = await client.get(url, headers=claim_headers(record))
        assert downloaded.status_code == 200
        assert downloaded.content == WAV
        assert downloaded.headers["cache-control"] == "no-store"

        done = env.repo.complete_job(
            job["job_id"],
            JobResult(text="recognized"),
            worker_id="owner", lease_token=record["lease_token"],
            runtime_instance_epoch=EPOCH,
        )
        assert done.status is JobStatus.SUCCEEDED
        assert (await client.get(url, headers=claim_headers(record))).status_code == 409
        assert env.channel.reap() == 1
        assert env.channel.reap() == 0
        assert not env.channel._inputs


@pytest.mark.asyncio
async def test_keyed_admission_retry_recovers_same_job_without_rebinding(tmp_path):
    env = make_env(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env.app), base_url="http://control",
    ) as client:
        ref = (await stage(client)).json()
        url = f"/v1/audio/input/{token(ref)}/jobs"
        body = submission(env, idempotency_key="audio-input-replay")
        first = await client.post(url, json=body, headers=CLIENT)
        assert first.status_code == 201, first.text
        again = await client.post(url, json=body, headers=CLIENT)
        assert again.status_code == 201, again.text
        assert again.json()["job_id"] == first.json()["job_id"]
        assert len(env.channel._inputs) == 1
        assert len(env.channel._spool._records) == 1

        altered = await client.post(
            url,
            json={**body, "payload": {"language": "en"}},
            headers=CLIENT,
        )
        assert altered.status_code == 409
        changed_key = await client.post(
            url, json={**body, "idempotency_key": "different"}, headers=CLIENT,
        )
        assert changed_key.status_code == 409
        unkeyed = await client.post(
            url, json={**body, "idempotency_key": None}, headers=CLIENT,
        )
        assert unkeyed.status_code == 409
        assert len(env.channel._inputs) == 1
        claimed = (await claim(client)).json()
        assert claimed["job_id"] == first.json()["job_id"]
        result = await client.get(input_url(claimed, ref), headers=claim_headers(claimed))
        assert result.status_code == 200 and result.content == WAV


@pytest.mark.asyncio
async def test_admission_failure_discards_staged_bytes_and_prevents_reuse(tmp_path):
    env = make_env(tmp_path, register=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env.app), base_url="http://control",
    ) as client:
        staged = await stage(client)
        ref = staged.json()
        assert staged.status_code == 201
        rejected = await admit(client, env, ref)
        assert rejected.status_code == 503, rejected.text
        assert not env.channel._inputs
        assert not env.channel._spool._records
        assert env.channel._spool._total_bytes == 0
        assert (await admit(client, env, ref)).status_code == 409


@pytest.mark.asyncio
async def test_malformed_job_submission_discards_pre_admission_input(tmp_path):
    env = make_env(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env.app), base_url="http://control",
    ) as client:
        ref = (await stage(client)).json()
        bad = await client.post(
            f"/v1/audio/input/{token(ref)}/jobs",
            json={"protocol_version": PROTOCOL_VERSION},
            headers=CLIENT,
        )
        assert bad.status_code == 422
        assert not env.channel._inputs
        assert not env.channel._spool._records


@pytest.mark.asyncio
async def test_wrong_protocol_aborts_staged_input_before_admission(tmp_path):
    env = make_env(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env.app), base_url="http://control",
    ) as client:
        ref = (await stage(client)).json()
        bad = await client.post(
            f"/v1/audio/input/{token(ref)}/jobs",
            json={**submission(env), "protocol_version": "invalid"},
            headers=CLIENT,
        )
        assert bad.status_code == 409
        assert env.channel._inputs == {}
        assert env.channel._spool._records == {}


@pytest.mark.asyncio
async def test_unbound_legacy_transcription_job_is_rejected_before_admission(tmp_path):
    env = make_env(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env.app), base_url="http://control",
    ) as client:
        ref = (await stage(client)).json()
        reply = await client.post(
            f"/v1/audio/input/{token(ref)}/jobs",
            json={
                "protocol_version": PROTOCOL_VERSION,
                "capability": "audio.transcribe",
                "payload": {"language": "ja"},
            },
            headers=CLIENT,
        )
        assert reply.status_code == 422, reply.text
        assert "serving-bound" in reply.text
        assert not env.channel._inputs
        assert not env.channel._spool._records
        assert (await admit(client, env, ref)).status_code == 409


@pytest.mark.asyncio
async def test_cancel_and_retry_fence_old_claim_but_preserve_input(tmp_path):
    env = make_env(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env.app), base_url="http://control",
    ) as client:
        ref = (await stage(client)).json()
        job = (await admit(client, env, ref)).json()
        first = (await claim(client)).json()
        url = input_url(first, ref)
        assert (await client.get(url, headers=claim_headers(first))).status_code == 200

        env.repo.fail_job(
            job["job_id"], "retry", retryable=True, worker_id="owner",
            lease_token=first["lease_token"], runtime_instance_epoch=EPOCH,
        )
        assert env.channel.reap() == 0  # QUEUED is eligible for another attempt
        assert (await client.get(url, headers=claim_headers(first))).status_code == 409
        second = (await claim(client)).json()
        assert second["attempts"] == 2
        assert (await client.get(url, headers=claim_headers(second))).content == WAV
        env.repo.cancel_job(job["job_id"])
        assert env.channel.reap() == 1
        assert (await client.get(url, headers=claim_headers(second))).status_code == 404


@pytest.mark.asyncio
async def test_pcm16_only_max_decoded_bytes_and_authentication(tmp_path):
    env = make_env(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env.app), base_url="http://control",
    ) as client:
        trials = [
            (WAV, WORKER, 401),
            (WAV, {**CLIENT, "content-type": "audio/mp3"}, 415),
            (b"RIFF" + b"0" * 4 + b"WAVE", {**CLIENT, "content-type": "audio/wav"}, 422),
            (wav_bytes(width=1), {**CLIENT, "content-type": "audio/wav"}, 422),
            (wav_bytes(rate=64000), {**CLIENT, "content-type": "audio/wav"}, 422),
            (wav_bytes(frames=400), {**CLIENT, "content-type": "audio/wav"}, 413),
            (wav_bytes(frames=350), {**CLIENT, "content-type": "audio/wav"}, 413),
            (wav_bytes(frames=350, channels=1, width=2)[:510],
             {**CLIENT, "content-type": "audio/wav"}, 422),
        ]
        for raw, headers, expected in trials:
            res = await stage(client, raw, headers=headers)
            assert res.status_code == expected, (res.status_code, res.text)
        assert not env.channel._inputs

        ordinary = await client.post(
            "/v1/jobs", json=submission(env), headers=CLIENT,
        )
        assert ordinary.status_code == 422
        forged = await client.post(
            "/v1/jobs",
            json=submission(env, capability="audio.synthesize",
                            payload={"audio_input": {"uri": "fake"}}),
            headers=CLIENT,
        )
        assert forged.status_code == 422


def test_input_channel_requires_finite_spool_quota(tmp_path):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    spool = ArtifactSpool(root, max_bytes=512, ttl_seconds=30)
    with pytest.raises(ValueError, match="finite aggregate"):
        AudioInputChannel(
            InMemoryControlRepository(), spool, max_bytes=512,
            max_decoded_bytes=2048,
        )


@pytest.mark.asyncio
async def test_expired_pre_admission_stage_is_not_bound(tmp_path):
    time = SimpleNamespace(now=100.0)
    env = make_env(tmp_path, clock=lambda: time.now)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env.app), base_url="http://control",
    ) as client:
        ref = (await stage(client)).json()
        time.now = 130.0
        failed = await admit(client, env, ref)
        assert failed.status_code == 409
        assert env.channel.reap() == 0
        assert not env.channel._inputs


@pytest.mark.asyncio
async def test_disabled_routes_and_non_bearer_configuration(tmp_path):
    env = make_env(tmp_path)
    no_input_app = create_app(
        env.repo, client_token="client-secret", worker_token="worker-secret",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=no_input_app), base_url="http://control",
    ) as client:
        assert (await stage(client)).status_code == 404
        assert (await client.get(
            "/v1/workers/owner/jobs/job/audio/input/token",
            headers=WORKER,
        )).status_code == 404
    with pytest.raises(ValueError, match="client bearer"):
        create_app(
            env.repo, client_token=None, worker_token="worker-secret",
            client_auth=ClientAuthMode.NONE, audio_input_channel=env.channel,
        )


@pytest.mark.asyncio
async def test_real_postgres_audio_input_job_record_and_worker_read(tmp_path, isolated_database):
    repo = PostgresControlRepository(isolated_database, lease_seconds=60)
    env = make_env(tmp_path, repo=repo)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env.app), base_url="http://control",
    ) as client:
        ref = (await stage(client)).json()
        url = f"/v1/audio/input/{token(ref)}/jobs"
        body = submission(env, idempotency_key="pg-stage-replay")
        admitted = await client.post(url, json=body, headers=CLIENT)
        assert admitted.status_code == 201, admitted.text
        replay = await client.post(url, json=body, headers=CLIENT)
        assert replay.status_code == 201, replay.text
        assert replay.json()["job_id"] == admitted.json()["job_id"]
        job = admitted.json()
        durable = repo.get_job(job["job_id"])
        assert durable.payload["audio_input"] == ref
        assert WAV not in json.dumps(job).encode()
        claimed = (await claim(client)).json()
        res = await client.get(input_url(claimed, ref), headers=claim_headers(claimed))
        assert res.status_code == 200 and res.content == WAV
        repo.cancel_job(job["job_id"])
        assert env.channel.reap() == 1
        assert not env.channel._spool._records
