"""#174: real loopback access logs and client-visible Job metrics, synthetic PCM."""

from base64 import b64encode
from datetime import UTC, datetime
import logging
from threading import Event

import httpx
import pytest

from astrumweaver.control import InMemoryControlRepository
from astrumweaver.control.api import create_app
from test_audio_artifact_authority import setup_claim
from test_audio_artifact_fault_acceptance import new_input, new_output
from test_audio_input_channel import pcm16_wav
from test_audio_output_channel import CLIENT, WORKER, completion_body, get_url, output_url, worker_headers
from test_audio_real_transport_faults import _live_control


@pytest.fixture
def access_log(caplog, monkeypatch):
    access = logging.getLogger("uvicorn.access")
    monkeypatch.setattr(access, "handlers", [caplog.handler])
    monkeypatch.setattr(access, "propagate", False)
    caplog.set_level(logging.INFO)
    caplog.set_level(logging.INFO, logger="uvicorn.access")
    return caplog


def test_wav_absent_from_control_logs_and_job_metrics(tmp_path, access_log, capfdbinary):
    # Same valid PCM16 headers but distinct, recognizable sample data. An
    # ASCII marker also catches logs that escape nonprintable WAV header bytes.
    samples = (b"INPUT-PCM-174!" * 4, b"OUTPUT-PCM-174" * 4)
    incoming, outgoing = (pcm16_wav(frames=len(s) // 2)[:44] + s for s in samples)
    repo = InMemoryControlRepository(lease_seconds=60)
    claim = setup_claim(repo, datetime.now(UTC))
    _, _, inputs = new_input(tmp_path, repo)
    _, _, outputs = new_output(tmp_path, repo)
    app = create_app(repo, client_token="client-secret", worker_token="worker-secret",
                     audio_input_channel=inputs, audio_output_channel=outputs)

    with _live_control(app, capture_access=True) as port, httpx.Client(
        base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=5,
    ) as client:
        uploaded = client.post("/v1/audio/inputs", content=incoming,
                               headers={**CLIENT, "content-type": "audio/wav"})
        assert uploaded.status_code == 201 and uploaded.json()["size_bytes"] == len(incoming)
        staged = client.post(output_url(claim), content=outgoing, headers=worker_headers(claim))
        assert staged.status_code == 201
        receipt = staged.json()
        body = completion_body(claim, [receipt])
        body["result"]["metrics"] = {"duration_seconds": 0.0035}
        done = client.post(f"/v1/workers/owner/jobs/{claim.job_id}/complete",
                           json=body, headers=WORKER)
        assert done.status_code == 200
        fetched = client.get(get_url(claim, receipt), headers=CLIENT)
        assert fetched.status_code == 200 and fetched.content == outgoing
        public_job = client.get(f"/v1/jobs/{claim.job_id}", headers=CLIENT)
        assert public_job.status_code == 200
        assert public_job.json()["result"]["metrics"] == body["result"]["metrics"]

    messages = [record.getMessage() for record in access_log.records if record.name == "uvicorn.access"]
    for route in ("/v1/audio/inputs", output_url(claim), get_url(claim, receipt)):
        assert any(f" {route} HTTP/" in message for message in messages)
    captured = capfdbinary.readouterr()
    for surface in (access_log.text.encode(), captured.out, captured.err, public_job.content):
        for value in (incoming, outgoing, *samples):
            assert value not in surface
            assert b64encode(value) not in surface


def test_cancel_during_output_transfer_withholds_wav(tmp_path, access_log, capfdbinary):
    marker = b"CANCEL-PCM-174"
    pcm = marker * 4
    wav = pcm16_wav(frames=len(pcm) // 2)[:44] + pcm
    split = 44 + len(marker)
    repo = InMemoryControlRepository(lease_seconds=60)
    claim = setup_claim(repo, datetime.now(UTC))
    _, _, outputs = new_output(tmp_path, repo)
    app = create_app(repo, client_token="client-secret", worker_token="worker-secret",
                     audio_output_channel=outputs)
    upload = output_url(claim)
    prefix_received = Event()

    async def observed_app(scope, receive, send):
        async def observed_receive():
            message = await receive()
            if message.get("body") and message.get("more_body"):
                prefix_received.set()
            return message

        await app(scope, observed_receive if scope.get("path") == upload else receive, send)

    with _live_control(observed_app, capture_access=True) as port, httpx.Client(
        base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=5,
    ) as client:
        # Use an actual receipt, not a guessed ID that would always return 404.
        staged = client.post(upload, content=wav, headers=worker_headers(claim))
        assert staged.status_code == 201
        receipt = staged.json()
        prefix_received.clear()
        cancel = f"/v1/jobs/{claim.job_id}/cancel"

        def retransmission():
            yield wav[:split]
            assert prefix_received.wait(5), "Control did not receive the partial WAV"
            cancelled = client.post(cancel, headers=CLIENT)
            assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
            yield wav[split:]

        rejected = client.post(upload, content=retransmission(), headers=worker_headers(claim))
        assert rejected.status_code == 409
        download = get_url(claim, receipt)
        hidden = client.get(download, headers=CLIENT)
        assert hidden.status_code == 404
        assert repo.get_job(claim.job_id).result is None

    messages = [record.getMessage() for record in access_log.records if record.name == "uvicorn.access"]
    for route, status in ((cancel, 200), (upload, 409), (download, 404)):
        assert any(f" {route} HTTP/" in msg and msg.endswith(f" {status}") for msg in messages)
    captured = capfdbinary.readouterr()
    for surface in (rejected.content, hidden.content, access_log.text.encode(), captured.out, captured.err):
        for value in (wav, wav[:split], wav[split:], pcm, marker):
            assert value not in surface
            assert b64encode(value) not in surface
