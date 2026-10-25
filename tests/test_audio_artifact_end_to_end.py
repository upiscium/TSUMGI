"""Private Wave2E acceptance probe; no repository/source mutation."""

from datetime import UTC, datetime, timedelta
import json

import httpx
import pytest

from astrumweaver.audio.artifact_spool import ArtifactSpool
from astrumweaver.audio.input_channel import AudioInputChannel
from astrumweaver.audio.output_channel import AudioOutputChannel
from astrumweaver.control import JobStatus, PostgresControlRepository
from astrumweaver.control.api import create_app
from astrumweaver.transport import PROTOCOL_VERSION, SERVING_EXTENSION
from test_audio_artifact_authority import EPOCH, setup_claim
from test_audio_artifact_authority_postgres import isolated_database

IN_WAV = b"RIFF" + (25).to_bytes(4, "little") + b"WAVE" + b"fmt " + b"private-input-wave"
OUT_WAV = b"RIFF" + (28).to_bytes(4, "little") + b"WAVE" + b"fmt " + b"private-output-wave"

@pytest.mark.asyncio
async def test_full_input_to_output_artifact_path_real_postgres_and_disk(
    tmp_path, isolated_database,
):
    repo = PostgresControlRepository(isolated_database, lease_seconds=60)
    existing = setup_claim(repo, datetime.now(UTC), capability="audio.transcribe")

    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    input_root.mkdir(mode=0o700)
    output_root.mkdir(mode=0o700)
    input_channel = AudioInputChannel(
        repo,
        ArtifactSpool(
            input_root, max_bytes=256, ttl_seconds=240,
            max_total_bytes=512, max_artifacts=2,
        ),
        max_bytes=256,
    )
    output_channel = AudioOutputChannel(
        repo,
        ArtifactSpool(
            output_root, max_bytes=256, ttl_seconds=240,
            max_total_bytes=512, max_artifacts=2,
        ),
        max_bytes=256,
    )
    app = create_app(
        repo, client_token="client-secret", worker_token="worker-secret",
        audio_input_channel=input_channel,
        audio_output_channel=output_channel,
    )
    client_auth = {"authorization": "Bearer client-secret"}
    worker_auth = {"authorization": "Bearer worker-secret"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://control",
    ) as client:
        staged_input = await client.post(
            "/v1/audio/inputs", content=IN_WAV,
            headers={**client_auth, "content-type": "audio/wav"},
        )
        assert staged_input.status_code == 201, staged_input.text
        input_reference = staged_input.json()
        assert input_reference["size_bytes"] == len(IN_WAV)

        admitted = await client.post(
            "/v1/audio/inputs/jobs",
            headers=client_auth,
            json={
                "protocol_version": PROTOCOL_VERSION,
                "extensions": [SERVING_EXTENSION],
                "capability": "audio.transcribe",
                "serving": existing.serving.to_dict(),
                "deadline_at": (datetime.now(UTC) + timedelta(seconds=120)).isoformat(),
                "idempotency_key": "wave2e-e2e-singlecontrol-pg",
                "audio_input_uri": input_reference["uri"],
            },
        )
        assert admitted.status_code == 201, admitted.text
        job_id = admitted.json()["job_id"]
        persisted = repo.get_job(job_id)
        assert persisted.payload["audio_input"] == input_reference
        assert IN_WAV not in repr(persisted.payload).encode()
        assert OUT_WAV not in repr(persisted.payload).encode()

        claim = repo.claim_next_job("other", runtime_instance_epoch=EPOCH)
        assert claim is not None and claim.job_id == job_id
        claim_headers = {
            **worker_auth,
            "x-audio-lease-token": claim.lease_token,
            "x-audio-attempt": str(claim.attempts),
            "x-audio-runtime-epoch": EPOCH,
        }
        download_input_url = f"/v1/workers/other/jobs/{job_id}/audio/input"
        assert (await client.get(download_input_url, headers=client_auth)).status_code == 401
        assert (await client.get(download_input_url, headers={
            **claim_headers, "x-audio-runtime-epoch": "wrong",
        })).status_code == 409
        current_input = await client.get(download_input_url, headers=claim_headers)
        assert current_input.status_code == 200, current_input.text
        assert current_input.content == IN_WAV

        uploaded_output = await client.post(
            f"/v1/workers/other/jobs/{job_id}/audio/output",
            content=OUT_WAV,
            headers={**claim_headers, "content-type": "audio/wav"},
        )
        assert uploaded_output.status_code == 201, uploaded_output.text
        output_reference = uploaded_output.json()
        output_uri = output_reference["uri"]
        output_id = output_uri.removeprefix("tsumgi-audio:")
        download_output_url = f"/v1/jobs/{job_id}/audio/output/{output_id}"
        assert (await client.get(download_output_url, headers=client_auth)).status_code == 404

        completed = await client.post(
            f"/v1/workers/other/jobs/{job_id}/complete",
            headers=worker_auth,
            json={
                "protocol_version": PROTOCOL_VERSION,
                "extensions": [SERVING_EXTENSION],
                "lease_token": claim.lease_token,
                "runtime_instance_epoch": EPOCH,
                "result": {
                    "outputs": {}, "artifacts": [output_reference],
                    "metrics": {}, "text": None, "metadata": {},
                },
            },
        )
        assert completed.status_code == 200, completed.text
        assert completed.json()["status"] == "succeeded"
        durable = repo.get_job(job_id)
        assert durable.status is JobStatus.SUCCEEDED
        assert durable.result.artifacts[0].uri == output_uri
        assert OUT_WAV not in repr(durable.result).encode()
        assert IN_WAV not in json.dumps(completed.json()).encode()

        assert (await client.get(download_output_url, headers=worker_auth)).status_code == 401
        response = await client.get(download_output_url, headers=client_auth)
        assert response.status_code == 200, response.text
        assert response.content == OUT_WAV
        assert response.headers["cache-control"] == "no-store"

        # A terminal Job cannot give its Worker another input grant.
        assert (await client.get(download_input_url, headers=claim_headers)).status_code == 409
        assert input_channel.spool._records
        assert output_channel._spool._records
