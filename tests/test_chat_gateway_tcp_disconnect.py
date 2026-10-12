"""Real loopback socket SIGKILL regression for issue #110.

No external services, model, GPU, or production database are required.
The Worker is an InMemory Control fixture and all clients are subprocesses.
"""
from __future__ import annotations

import asyncio
import contextlib
import socket
import sys
from time import monotonic

import pytest
import uvicorn

from astrumweaver.control import InMemoryControlRepository, JobStatus
from test_chat_gateway_api import (
    CLIENT_TOKEN,
    EPOCH,
    app_with_gateway,
    register_worker,
    serving_values,
)


_CLIENT_PROGRAM = """
import sys
import httpx
url, token, marker = sys.argv[1:]
with httpx.Client(timeout=None, trust_env=False) as client:
    with client.stream(
        "POST", url,
        headers={"authorization": "Bearer " + token},
        json={"model": "local-code-v1",
              "messages": [{"role": "user", "content": marker}],
              "stream": True},
    ) as response:
        if response.status_code != 200:
            raise SystemExit("chat request rejected")
        for _ in response.iter_lines():
            pass
"""


async def _eventually(check, *, timeout: float, purpose: str):
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        result = check()
        if result:
            return result
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting for {purpose}")


@pytest.mark.asyncio
async def test_real_tcp_client_process_sigkill_releases_claimed_stream_job():
    repository = InMemoryControlRepository()
    profile, advert = serving_values(timeout=20)
    register_worker(repository, advert)
    app = app_with_gateway(repository, profile)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    sock.setblocking(False)
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="error", lifespan="off", access_log=False)
    )
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        await _eventually(
            lambda: server.started, timeout=8, purpose="ASGI server startup"
        )
        url = f"http://127.0.0.1:{port}/v1/chat/completions"
        for marker, first_chunk in (("before-chunk", False), ("after-chunk", True)):
            existing = {job.job_id for job in repository.list_jobs()}
            client = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c", _CLIENT_PROGRAM, url, CLIENT_TOKEN, marker,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                job = await _eventually(
                    lambda: next(
                        (
                            j for j in repository.list_jobs()
                            if j.job_id not in existing
                        ),
                        None,
                    ),
                    timeout=8,
                    purpose="actual TCP request admission",
                )
                claimed = repository.claim_next_job(
                    "chat-worker", runtime_instance_epoch=EPOCH,
                )
                assert claimed is not None and claimed.job_id == job.job_id
                assert repository.get_job(job.job_id).status is JobStatus.RUNNING
                assert repository.get_worker("chat-worker").active_jobs == 1
                if first_chunk:
                    repository.append_job_event(
                        job.job_id,
                        worker_id="chat-worker",
                        lease_token=claimed.lease_token or "",
                        runtime_instance_epoch=EPOCH,
                        kind="chat.completion.chunk",
                        payload={
                            "choices": [{
                                "index": 0,
                                "delta": {"content": "partial"},
                                "finish_reason": None,
                            }],
                        },
                    )
                await asyncio.sleep(0.075)
                assert client.returncode is None
                client.kill()
                await asyncio.wait_for(client.wait(), timeout=3)
                await _eventually(
                    lambda: repository.get_job(job.job_id).status
                    is JobStatus.CANCELLED,
                    timeout=5,
                    purpose="durable Job cancellation from TCP close",
                )
                assert repository.get_worker("chat-worker").active_jobs == 0
            finally:
                if client.returncode is None:
                    client.kill()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(client.wait(), timeout=3)

        assert len(repository.list_jobs()) == 2
        assert all(
            job.status is JobStatus.CANCELLED
            for job in repository.list_jobs()
        )
    finally:
        server.should_exit = True
        with contextlib.suppress(Exception):
            await asyncio.wait_for(task, timeout=5)
        sock.close()
