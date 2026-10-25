"""Wave2C: opt-in Control-local WAV output with claim and terminal fences."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace

import httpx
import pytest

from astrumweaver.audio import ArtifactSpool, AudioOutputChannel
from astrumweaver.control import InMemoryControlRepository, JobStatus, JobSubmission
from astrumweaver.control.api import create_app
from astrumweaver.transport import PROTOCOL_VERSION, SERVING_EXTENSION
from test_audio_artifact_authority import EPOCH, setup_claim
from test_audio_artifact_authority_postgres import isolated_database
from astrumweaver.control import PostgresControlRepository


WAV = b"RIFF" + (28).to_bytes(4, "little") + b"WAVE" + b"fmt " + b"test-output"
WORKER = {"authorization": "Bearer worker-secret"}
CLIENT = {"authorization": "Bearer client-secret"}


def new_env(tmp_path, repository=None, *, ttl=90, clock=None,
            max_artifacts=2, max_total_bytes=512):
    root = tmp_path / "audio-root"
    root.mkdir(mode=0o700)
    repo = repository or InMemoryControlRepository(lease_seconds=60)
    claim = setup_claim(repo, datetime.now(UTC))
    spool = ArtifactSpool(
        root, max_bytes=256, max_total_bytes=max_total_bytes,
        max_artifacts=max_artifacts, ttl_seconds=ttl,
        **({"clock": clock} if clock else {}),
    )
    channel = AudioOutputChannel(repo, spool, max_bytes=256)
    app = create_app(
        repo, client_token="client-secret", worker_token="worker-secret",
        audio_output_channel=channel,
    )
    return SimpleNamespace(repo=repo, claim=claim, app=app, channel=channel)


def output_url(claim, *, worker="owner"):
    return f"/v1/workers/{worker}/jobs/{claim.job_id}/audio/output"


def get_url(claim, reference):
    return f"/v1/jobs/{claim.job_id}/audio/output/{reference['uri'].split(':', 1)[1]}"


def worker_headers(claim, **changes):
    values = {
        **WORKER, "content-type": "audio/wav",
        "x-audio-lease-token": claim.lease_token,
        "x-audio-attempt": str(claim.attempts),
        "x-audio-runtime-epoch": EPOCH,
    }
    values.update(changes)
    return values


def completion_body(claim, references):
    return {
        "protocol_version": PROTOCOL_VERSION,
        "extensions": [SERVING_EXTENSION],
        "lease_token": claim.lease_token,
        "runtime_instance_epoch": EPOCH,
        "result": {"outputs": {}, "artifacts": list(references), "metrics": {},
                   "text": None, "metadata": {}},
    }


@pytest.mark.asyncio
async def test_wav_stage_is_private_until_durable_success(tmp_path):
    env = new_env(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env.app), base_url="http://control",
    ) as client:
        stage = await client.post(
            output_url(env.claim), content=WAV, headers=worker_headers(env.claim),
        )
        assert stage.status_code == 201, stage.text
        ref = stage.json()
        assert ref["uri"].startswith("tsumgi-audio:")
        assert ref["media_type"] == "audio/wav"
        assert ref["digest"].startswith("sha256:")
        assert ref["size_bytes"] == len(WAV)
        assert WAV not in json.dumps(ref).encode()
        url = get_url(env.claim, ref)

        assert (await client.get(url, headers=CLIENT)).status_code == 404
        assert (await client.get(url, headers=WORKER)).status_code == 401
        done = await client.post(
            f"/v1/workers/owner/jobs/{env.claim.job_id}/complete",
            json=completion_body(env.claim, [ref]), headers=WORKER,
        )
        assert done.status_code == 200, done.text
        assert done.json()["status"] == "succeeded"
        assert env.repo.get_job(env.claim.job_id).status is JobStatus.SUCCEEDED
        assert WAV not in json.dumps(done.json()).encode()
        assert env.repo.get_job(env.claim.job_id).result.artifacts[0].uri == ref["uri"]

        result = await client.get(url, headers=CLIENT)
        assert result.status_code == 200
        assert result.content == WAV
        assert result.headers["content-type"].startswith("audio/wav")
        assert result.headers["cache-control"] == "no-store"
        assert (await client.get(url + "x", headers=CLIENT)).status_code == 404


@pytest.mark.asyncio
async def test_staging_requires_current_worker_claim_and_bounded_valid_wav(tmp_path):
    env = new_env(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env.app), base_url="http://control",
    ) as client:
        trials = (
            (output_url(env.claim), {**worker_headers(env.claim), "authorization": "Bearer client-secret"}, WAV, 401),
            (output_url(env.claim, worker="other"), worker_headers(env.claim), WAV, 409),
            (output_url(env.claim), worker_headers(env.claim, **{"x-audio-lease-token": "stale"}), WAV, 409),
            (output_url(env.claim), worker_headers(env.claim, **{"x-audio-attempt": "2"}), WAV, 409),
            (output_url(env.claim), worker_headers(env.claim, **{"x-audio-runtime-epoch": "old"}), WAV, 409),
            (output_url(env.claim), worker_headers(env.claim, **{"x-audio-attempt": "true"}), WAV, 422),
            (output_url(env.claim), worker_headers(env.claim, **{"content-type": "audio/mpeg"}), WAV, 415),
            (output_url(env.claim), worker_headers(env.claim), b"bad data", 422),
            (output_url(env.claim), worker_headers(env.claim), WAV + b"z" * 256, 413),
        )
        for path, headers, body, status in trials:
            response = await client.post(path, headers=headers, content=body)
            assert response.status_code == status, (status, response.text)
            assert env.claim.lease_token not in response.text
        assert env.repo.get_job(env.claim.job_id).status is JobStatus.RUNNING


@pytest.mark.asyncio
async def test_forged_or_uncommitted_reference_cannot_publish(tmp_path):
    env = new_env(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env.app), base_url="http://control",
    ) as client:
        stage = await client.post(
            output_url(env.claim), content=WAV, headers=worker_headers(env.claim),
        )
        assert stage.status_code == 201
        ref = stage.json()
        forged = {**ref, "digest": "sha256:" + "0" * 64}
        other = {**ref, "uri": "https://example.invalid/secret.wav"}
        for wrong in (forged, other):
            response = await client.post(
                f"/v1/workers/owner/jobs/{env.claim.job_id}/complete",
                json=completion_body(env.claim, [wrong]), headers=WORKER,
            )
            assert response.status_code == 409
            assert env.repo.get_job(env.claim.job_id).status is JobStatus.RUNNING
            assert (await client.get(get_url(env.claim, ref), headers=CLIENT)).status_code == 404

        env.repo.cancel_job(env.claim.job_id)
        assert (await client.get(get_url(env.claim, ref), headers=CLIENT)).status_code == 404
        old = await client.post(
            output_url(env.claim), content=WAV, headers=worker_headers(env.claim),
        )
        assert old.status_code == 409


@pytest.mark.asyncio
async def test_expired_spool_is_unavailable_even_when_job_succeeded(tmp_path):
    clock = SimpleNamespace(value=100.0)
    env = new_env(tmp_path, ttl=10, clock=lambda: clock.value)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env.app), base_url="http://control",
    ) as client:
        stage = await client.post(
            output_url(env.claim), content=WAV, headers=worker_headers(env.claim),
        )
        assert stage.status_code == 201
        ref = stage.json()
        complete = await client.post(
            f"/v1/workers/owner/jobs/{env.claim.job_id}/complete",
            json=completion_body(env.claim, [ref]), headers=WORKER,
        )
        assert complete.status_code == 200
        clock.value = 110.0
        assert (await client.get(get_url(env.claim, ref), headers=CLIENT)).status_code == 404


@pytest.mark.asyncio
async def test_one_live_output_per_attempt_idempotent_retry_preserves_other_job_quota(tmp_path):
    env = new_env(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env.app), base_url="http://control",
    ) as client:
        first = await client.post(
            output_url(env.claim), content=WAV, headers=worker_headers(env.claim),
        )
        assert first.status_code == 201, first.text

        same = await client.post(
            output_url(env.claim), content=WAV, headers=worker_headers(env.claim),
        )
        assert same.status_code == 201, same.text
        assert same.json() == first.json()
        assert len(env.channel._staged) == 1

        different = await client.post(
            output_url(env.claim), content=WAV + b"different",
            headers=worker_headers(env.claim),
        )
        assert different.status_code == 409, different.text
        assert len(env.channel._staged) == 1

        now = datetime.now(UTC)
        queued = env.repo.submit_job(JobSubmission(
            capability="audio.synthesize", serving=env.claim.serving,
            deadline_at=now + timedelta(seconds=120),
        ), now=now)
        other = env.repo.claim_next_job(
            "other", runtime_instance_epoch=EPOCH, now=now,
        )
        assert other is not None and other.job_id == queued.job_id
        second = await client.post(
            output_url(other, worker="other"), content=WAV,
            headers=worker_headers(other),
        )
        assert second.status_code == 201, second.text
        assert second.json()["uri"] != first.json()["uri"]
        assert len(env.channel._staged) == 2


@pytest.mark.parametrize("limits", [
    {}, {"max_artifacts": 2}, {"max_total_bytes": 512},
])
def test_audio_output_requires_finite_spool_quotas(tmp_path, limits):
    root = tmp_path / "private-spool"
    root.mkdir(mode=0o700)
    spool = ArtifactSpool(
        root, max_bytes=256, ttl_seconds=30, **limits,
    )
    with pytest.raises(ValueError, match="finite aggregate spool quotas"):
        AudioOutputChannel(InMemoryControlRepository(), spool, max_bytes=256)


@pytest.mark.asyncio
async def test_expired_output_releases_quota_and_allows_reuse(tmp_path):
    clock = SimpleNamespace(value=10.0)
    env = new_env(tmp_path, ttl=5, clock=lambda: clock.value,
                  max_artifacts=1)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env.app), base_url="http://control",
    ) as client:
        staged = await client.post(
            output_url(env.claim), content=WAV,
            headers=worker_headers(env.claim),
        )
        assert staged.status_code == 201
        ref = staged.json()
        now = datetime.now(UTC)
        queued = env.repo.submit_job(JobSubmission(
            capability="audio.synthesize", serving=env.claim.serving,
            deadline_at=now + timedelta(seconds=120),
        ), now=now)
        other = env.repo.claim_next_job(
            "other", runtime_instance_epoch=EPOCH, now=now,
        )
        assert other is not None and other.job_id == queued.job_id

        # Distinct live claims compete for the bounded shared spool quota.
        full = await client.post(
            output_url(other, worker="other"), content=WAV,
            headers=worker_headers(other),
        )
        assert full.status_code == 422

        clock.value = 15.0
        assert env.channel.reap_expired() == 1
        assert env.channel.reap_expired() == 0
        assert not env.channel._staged
        assert (await client.get(get_url(env.claim, ref),
                                 headers=CLIENT)).status_code == 404
        reused = await client.post(
            output_url(other, worker="other"), content=WAV,
            headers=worker_headers(other),
        )
        assert reused.status_code == 201, reused.text
        assert reused.json()["uri"] != ref["uri"]


@pytest.mark.asyncio
async def test_incomplete_content_length_fails_before_artifact_staging():
    from fastapi import HTTPException
    from starlette.requests import Request
    from astrumweaver.control.api import _bounded_audio_body

    scope = {
        "type": "http", "method": "POST", "path": "/audio",
        "headers": [(b"content-length", str(len(WAV) + 3).encode())],
        "query_string": b"",
    }
    async def receive():
        return {"type": "http.request", "body": WAV, "more_body": False}

    with pytest.raises(HTTPException) as exc:
        await _bounded_audio_body(Request(scope, receive), 256)
    assert exc.value.status_code == 400
    assert exc.value.detail == "incomplete audio output"


def test_audio_output_requires_authentication_even_with_opt_in(tmp_path):
    from astrumweaver.control.auth import ClientAuthMode
    env = new_env(tmp_path)
    with pytest.raises(ValueError, match="client bearer authentication"):
        create_app(
            env.repo, client_token=None, worker_token="worker-secret",
            client_auth=ClientAuthMode.NONE, audio_output_channel=env.channel,
        )


@pytest.mark.asyncio
async def test_disabled_audio_routes_do_not_change_standard_api(tmp_path):
    repo = InMemoryControlRepository()
    app = create_app(repo, client_token="client-secret", worker_token="worker-secret")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://control",
    ) as client:
        assert (await client.post("/v1/workers/id/jobs/job/audio/output",
                                  content=WAV, headers=WORKER)).status_code == 404
        assert (await client.get("/v1/jobs/job/audio/output/id",
                                 headers=CLIENT)).status_code == 404


@pytest.mark.asyncio
async def test_real_postgres_terminal_output_round_trip(tmp_path, isolated_database):
    repo = PostgresControlRepository(isolated_database, lease_seconds=60)
    env = new_env(tmp_path, repository=repo)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env.app), base_url="http://control",
    ) as client:
        stage = await client.post(
            output_url(env.claim), content=WAV, headers=worker_headers(env.claim),
        )
        assert stage.status_code == 201, stage.text
        ref = stage.json()
        assert (await client.get(get_url(env.claim, ref), headers=CLIENT)).status_code == 404
        done = await client.post(
            f"/v1/workers/owner/jobs/{env.claim.job_id}/complete",
            json=completion_body(env.claim, [ref]), headers=WORKER,
        )
        assert done.status_code == 200, done.text
        # Read the actual PostgreSQL record, not the API response alone.
        durable = repo.get_job(env.claim.job_id)
        assert durable.status is JobStatus.SUCCEEDED
        assert durable.result.artifacts[0].uri == ref["uri"]
        assert WAV not in json.dumps(done.json()).encode()
        assert (await client.get(get_url(env.claim, ref), headers=CLIENT)).content == WAV
