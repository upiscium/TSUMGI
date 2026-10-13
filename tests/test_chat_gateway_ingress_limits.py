"""Bound chat ingress memory and concurrency before durable admission (#137)."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from astrumweaver.control import InMemoryControlRepository
from astrumweaver.control.api import create_app
from astrumweaver.gateway.api import create_chat_router
from astrumweaver.gateway.chat import ChatProfileCatalog
from test_chat_gateway_api import (
    CLIENT_TOKEN, WORKER_TOKEN, register_worker, serving_values,
)


PATH = "/v1/chat/completions"


def make_app(*, timeout=2.0, pending=1):
    repository = InMemoryControlRepository()
    profile, serving = serving_values(timeout=timeout)
    register_worker(repository, serving)
    app = create_app(
        repository, client_token=CLIENT_TOKEN, worker_token=WORKER_TOKEN,
        maintenance_interval_seconds=60,
    )
    router = create_chat_router(
        repository, ChatProfileCatalog((profile,)),
        client_auth="bearer", client_token=CLIENT_TOKEN,
        max_pending_admissions=pending, poll_interval_seconds=0.005,
    )
    # The property is private, but it is meaningful to check the actual bound
    # rather than inferring behavior only from one HTTP status.
    endpoint = next(
        route.endpoint for route in router.routes
        if getattr(route, "path", None) == PATH
        and "POST" in getattr(route, "methods", set())
    )
    closure = dict(zip(
        endpoint.__code__.co_freevars,
        (cell.cell_contents for cell in endpoint.__closure__),
    ))
    service = closure["service"]
    app.include_router(router)
    return app, repository, service


def asgi_scope():
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "scheme": "http",
        "method": "POST",
        "path": PATH,
        "raw_path": PATH.encode("ascii"),
        "root_path": "",
        "query_string": b"",
        "headers": [
            (b"authorization", f"Bearer {CLIENT_TOKEN}".encode()),
            (b"content-type", b"application/json"),
        ],
        "client": ("testclient", 1234),
        "server": ("testserver", 80),
    }


async def eventually(predicate, timeout=2.0):
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("Gateway ingress condition was not reached")


@pytest.mark.asyncio
async def test_request_body_over_limit_is_rejected_without_job_submission():
    app, repo, service = make_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            PATH, content=b"x" * 65537,
            headers={"Authorization": f"Bearer {CLIENT_TOKEN}"},
        )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"
    assert repo.list_jobs() == []
    assert service._ingress_in_flight == 0


@pytest.mark.asyncio
async def test_waiting_request_upload_holds_ingress_permit_and_rejects_next():
    app, repo, service = make_app(pending=1)
    first_messages: asyncio.Queue[dict] = asyncio.Queue()
    first_sent: list[dict] = []

    async def receive_first():
        return await first_messages.get()

    async def send_first(msg):
        first_sent.append(msg)

    first = asyncio.create_task(app(asgi_scope(), receive_first, send_first))
    try:
        await eventually(lambda: service._ingress_in_flight == 1)
        second_sent: list[dict] = []

        async def receive_second():
            raise AssertionError("overloaded request must not read its body")

        async def send_second(msg):
            second_sent.append(msg)

        await asyncio.wait_for(
            app(asgi_scope(), receive_second, send_second), timeout=2
        )
        assert second_sent[0]["type"] == "http.response.start"
        assert second_sent[0]["status"] == 429
        assert any(b"admission_overloaded" in msg.get("body", b"") for msg in second_sent)
        assert service._ingress_in_flight == 1
        assert not repo.list_jobs()
        first_messages.put_nowait({"type": "http.disconnect"})
        await asyncio.wait_for(first, timeout=2)
        assert first_sent[0]["status"] == 499
    finally:
        if not first.done():
            first.cancel()
        await asyncio.gather(first, return_exceptions=True)
    assert service._ingress_in_flight == 0


@pytest.mark.asyncio
async def test_upload_deadline_releases_ingress_slot_without_admission():
    app, repo, service = make_app(timeout=0.08)
    sent: list[dict] = []
    incoming: asyncio.Queue[dict] = asyncio.Queue()

    async def receive():
        return await incoming.get()

    async def send(msg):
        sent.append(msg)

    await asyncio.wait_for(app(asgi_scope(), receive, send), timeout=2)
    assert sent[0]["status"] == 408
    assert b"request_timeout" in sent[-1].get("body", b"")
    assert service._ingress_in_flight == 0
    assert not repo.list_jobs()
