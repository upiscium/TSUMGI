from __future__ import annotations

import asyncio
from datetime import timedelta

import httpx
import pytest

from astrumweaver import JobResult
from astrumweaver.control import InMemoryControlRepository, JobStatus, JobSubmission
from astrumweaver.control.api import create_app
from astrumweaver.control.models import utc_now
from astrumweaver.gateway.api import ChatGatewayService, create_chat_router
from astrumweaver.gateway.chat import ChatProfileCatalog

from test_chat_gateway_api import (
    CLIENT_TOKEN,
    EPOCH,
    WORKER_TOKEN,
    register_worker,
    serving_values,
)

REQUEST = {"model": "local-code-v1", "messages": [{"role": "user", "content": "hi"}]}
HEADERS = {"authorization": f"Bearer {CLIENT_TOKEN}"}


def app_with(repository, gateway, **options):
    app = create_app(
        repository,
        client_token=CLIENT_TOKEN,
        worker_token=WORKER_TOKEN,
        maintenance_interval_seconds=60,
    )
    app.include_router(
        create_chat_router(
            repository,
            ChatProfileCatalog((gateway,)),
            client_auth="bearer",
            client_token=CLIENT_TOKEN,
            poll_interval_seconds=0.005,
            **options,
        )
    )
    return app


def occupy(repository, gateway):
    """Hold the only replica with a direct Job, like OpenCode's title request."""
    job = repository.submit_job(
        JobSubmission(
            capability="llm.chat",
            payload={"direct": True},
            deadline_at=utc_now() + timedelta(seconds=10),
            serving=gateway.binding,
        )
    )
    claimed = repository.claim_next_job("chat-worker", runtime_instance_epoch=EPOCH)
    assert claimed is not None and claimed.job_id == job.job_id
    return claimed


def finish(repository, claimed, text="ok"):
    repository.complete_job(
        claimed.job_id,
        JobResult(outputs={"choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }]}),
        worker_id="chat-worker",
        lease_token=claimed.lease_token or "",
        runtime_instance_epoch=EPOCH,
    )


async def post(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://control"
    ) as client:
        return await client.post("/v1/chat/completions", headers=HEADERS, json=REQUEST)


async def serve_next(repository, text="ok"):
    deadline = asyncio.get_running_loop().time() + 2
    while asyncio.get_running_loop().time() < deadline:
        claimed = repository.claim_next_job("chat-worker", runtime_instance_epoch=EPOCH)
        if claimed is not None:
            finish(repository, claimed, text)
            return claimed
        await asyncio.sleep(0.005)
    raise AssertionError("no job admitted")


@pytest.mark.asyncio
async def test_default_overload_is_fail_fast_with_retry_after():
    repository = InMemoryControlRepository()
    gateway, advertisement = serving_values()
    register_worker(repository, advertisement)
    occupy(repository, gateway)

    response = await post(app_with(repository, gateway))

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "overloaded"
    assert response.headers["retry-after"] == "1"
    assert len(repository.list_jobs()) == 1


@pytest.mark.asyncio
async def test_request_waits_for_the_busy_replica_then_runs():
    repository = InMemoryControlRepository()
    gateway, advertisement = serving_values()
    register_worker(repository, advertisement)
    title = occupy(repository, gateway)

    async def replica():
        await asyncio.sleep(0.3)
        finish(repository, title, "title")
        await serve_next(repository, "main answer")

    worker = asyncio.create_task(replica())
    response = await post(app_with(repository, gateway, admission_wait_seconds=5))
    await worker

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "main answer"
    # Only the title Job and the main Job exist: waiting created no extra Jobs.
    jobs = repository.list_jobs()
    assert len(jobs) == 2 and all(job.status is JobStatus.SUCCEEDED for job in jobs)


@pytest.mark.asyncio
async def test_wait_ends_with_429_when_the_replica_stays_busy():
    repository = InMemoryControlRepository()
    gateway, advertisement = serving_values()
    register_worker(repository, advertisement)
    occupy(repository, gateway)

    started = asyncio.get_running_loop().time()
    response = await post(app_with(repository, gateway, admission_wait_seconds=0.4))
    elapsed = asyncio.get_running_loop().time() - started

    assert response.status_code == 429
    assert 0.4 <= elapsed < 2
    assert len(repository.list_jobs()) == 1


@pytest.mark.asyncio
async def test_waiters_are_bounded():
    repository = InMemoryControlRepository()
    gateway, advertisement = serving_values()
    register_worker(repository, advertisement)
    occupy(repository, gateway)
    app = app_with(repository, gateway, admission_wait_seconds=1, max_admission_waiters=1)

    first = asyncio.create_task(post(app))
    await asyncio.sleep(0.1)
    started = asyncio.get_running_loop().time()
    second = await post(app)
    assert second.status_code == 429
    assert asyncio.get_running_loop().time() - started < 0.5
    assert (await first).status_code == 429


@pytest.mark.parametrize(
    "options",
    [
        {"admission_wait_seconds": -1},
        {"admission_wait_seconds": float("inf")},
        {"admission_wait_seconds": True},
        {"max_admission_waiters": 0},
        {"max_admission_waiters": 1.5},
    ],
)
def test_admission_wait_options_are_validated(options):
    gateway, _ = serving_values()
    with pytest.raises(ValueError):
        ChatGatewayService(
            InMemoryControlRepository(), ChatProfileCatalog((gateway,)), **options
        )
