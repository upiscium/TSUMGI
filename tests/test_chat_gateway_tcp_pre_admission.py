"""#137: request transport must retain ownership across a blocked durable submit.

Only loopback sockets and the in-memory repository are used.  The submit gate
blocks *inside* asyncio.to_thread, not in the event loop or before dispatch.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import threading
from contextlib import asynccontextmanager
from datetime import timedelta

import pytest
import uvicorn

from astrumweaver import JobResult
from astrumweaver.control import InMemoryControlRepository, JobStatus, JobSubmission, utc_now
from astrumweaver.control.api import create_app
from astrumweaver.gateway.api import create_chat_router
from astrumweaver.gateway.chat import ChatProfileCatalog
from test_chat_gateway_api import CLIENT_TOKEN, EPOCH, WORKER_TOKEN, register_worker, serving_values


BODY = {"model": "local-code-v1", "messages": [{"role": "user", "content": "gate"}], "stream": True}
PATH = "/v1/chat/completions"


class GatedRepository(InMemoryControlRepository):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self._lock_calls = threading.Lock()
        self.calls = 0

    def submit_job(self, *args, **kwargs):
        with self._lock_calls:
            self.calls += 1
            first = self.calls == 1
        if first:
            self.entered.set()
            try:
                if not self.release.wait(8):
                    raise AssertionError("blocked test admission was never released")
                return super().submit_job(*args, **kwargs)
            finally:
                self.finished.set()
        return super().submit_job(*args, **kwargs)


async def eventually(predicate, *, label: str, seconds: float = 4):
    end = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < end:
        result = predicate()
        if result:
            return result
        await asyncio.sleep(0.01)
    pytest.fail(f"timed out waiting for {label}")


class ObservedApp:
    def __init__(self, app):
        self.app = app
        self.disconnect = asyncio.Event()
        self.handler_finished = asyncio.Event()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] != PATH:
            return await self.app(scope, receive, send)

        async def watched_receive():
            message = await receive()
            if message["type"] == "http.disconnect":
                self.disconnect.set()
            return message

        try:
            await self.app(scope, watched_receive, send)
        finally:
            self.handler_finished.set()


def gateway(repo, *, budget=1):
    profile, advert = serving_values(timeout=20)
    register_worker(repo, advert)
    app = create_app(repo, client_token=CLIENT_TOKEN, worker_token=WORKER_TOKEN,
                     maintenance_interval_seconds=60)
    app.include_router(create_chat_router(
        repo, ChatProfileCatalog((profile,)), client_auth="bearer",
        client_token=CLIENT_TOKEN, poll_interval_seconds=0.005,
        max_pending_admissions=budget,
    ))
    return ObservedApp(app)


def wire_request(body=BODY):
    data = json.dumps(body).encode()
    return (f"POST {PATH} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            f"Authorization: Bearer {CLIENT_TOKEN}\r\n"
            f"Content-Type: application/json\r\nContent-Length: {len(data)}\r\n"
            "Connection: close\r\n\r\n").encode() + data


@asynccontextmanager
async def loopback_server(app):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    sock.setblocking(False)
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(
        app, log_level="error", lifespan="off", access_log=False, http="h11",
        timeout_graceful_shutdown=1,
    ))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        await eventually(lambda: server.started, label="Uvicorn startup", seconds=6)
        yield port
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, 5)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            sock.close()


@asynccontextmanager
async def client_request(port):
    reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 3)
    try:
        writer.write(wire_request())
        await asyncio.wait_for(writer.drain(), 3)
        yield reader, writer
    finally:
        writer.close()
        with contextlib.suppress(OSError, asyncio.TimeoutError):
            await asyncio.wait_for(writer.wait_closed(), 3)


@asynccontextmanager
async def gated_gateway(*, budget=1):
    repo = GatedRepository()
    app = gateway(repo, budget=budget)
    try:
        yield repo, app
    finally:
        repo.release.set()  # Never leave an executor thread waiting on a failed assertion.
        await eventually(repo.finished.is_set, label="submit thread completion") if repo.entered.is_set() else None
        for job in repo.list_jobs():
            if job.status not in {JobStatus.CANCELLED, JobStatus.SUCCEEDED, JobStatus.FAILED}:
                repo.cancel_job(job.job_id)


async def assert_late_cancel(repo):
    await eventually(lambda: len(repo.list_jobs()) == 1, label="late durable Job")
    await eventually(lambda: repo.list_jobs()[0].status is JobStatus.CANCELLED,
                     label="live gateway late cancellation")
    assert repo.get_worker("chat-worker").active_jobs == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [True, False], ids=["sse", "plain"])
async def test_loopback_close_before_submit_result_is_seen_and_cancels_late_job(stream):
    async with gated_gateway() as (repo, app):
        async with loopback_server(app) as port:
            reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 3)
            try:
                body = {**BODY, "stream": stream}
                writer.write(wire_request(body))
                await asyncio.wait_for(writer.drain(), 3)
                assert await asyncio.to_thread(repo.entered.wait, 3)
                assert repo.list_jobs() == []  # Socket closes BEFORE admission result.
                assert not reader.at_eof()
                writer.close()
                await asyncio.wait_for(writer.wait_closed(), 3)
                # The ASGI receive path must notice the close while submit is blocked,
                # not only after a Job already exists or the request deadline fires.
                await eventually(app.disconnect.is_set, label="HTTP disconnect during admission", seconds=2)
                assert repo.list_jobs() == []
                repo.release.set()
                await assert_late_cancel(repo)
                await eventually(app.handler_finished.is_set, label="request scope exit")
            finally:
                repo.release.set()
                writer.close()
                with contextlib.suppress(OSError, asyncio.TimeoutError):
                    await asyncio.wait_for(writer.wait_closed(), 3)


def asgi_scope(data):
    return {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1", "method": "POST", "scheme": "http",
        "path": PATH, "raw_path": PATH.encode(), "query_string": b"",
        "root_path": "", "server": ("127.0.0.1", 0), "client": ("127.0.0.1", 0),
        "headers": [(b"authorization", b"Bearer " + CLIENT_TOKEN.encode()),
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(data)).encode())],
    }


@asynccontextmanager
async def asgi_request(app, body=BODY):
    data = json.dumps(body).encode()
    messages = asyncio.Queue()
    messages.put_nowait({"type": "http.request", "body": data, "more_body": False})
    sent = []

    async def receive():
        return await messages.get()

    async def send(message):
        sent.append(message)

    task = asyncio.create_task(app(asgi_scope(data), receive, send))
    try:
        yield messages, sent, task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_asgi_receive_disconnect_during_admission_before_first_sse():
    async with gated_gateway() as (repo, app):
        async with asgi_request(app) as (messages, sent, task):
            assert await asyncio.to_thread(repo.entered.wait, 3)
            assert sent == []
            messages.put_nowait({"type": "http.disconnect"})
            await eventually(app.disconnect.is_set, label="receive-side disconnect before SSE", seconds=2)
            assert not repo.list_jobs()
            repo.release.set()
            await assert_late_cancel(repo)
            await asyncio.wait_for(task, 3)
            # An ASGI fake send() may still capture an HTTP 499 JSON error
            # after disconnect. What must never leak is a model SSE token.
            assert not any(m["type"] == "http.response.body" and
                           b"data:" in m.get("body", b"") for m in sent)


@pytest.mark.asyncio
async def test_asgi_request_scope_cancelled_during_admission_retains_cleanup():
    async with gated_gateway() as (repo, app):
        async with asgi_request(app) as (_, sent, task):
            assert await asyncio.to_thread(repo.entered.wait, 3)
            assert sent == []
            task.cancel()  # Server shutdown / request-scope cancellation.
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3)
            assert app.handler_finished.is_set()
            assert repo.list_jobs() == []
            repo.release.set()
            await assert_late_cancel(repo)


@pytest.mark.asyncio
async def test_loopback_overload_flood_does_not_spawn_unbounded_admissions():
    async with gated_gateway(budget=1) as (repo, app):
        async with loopback_server(app) as port:
            async with client_request(port) as (_, first_writer):
                assert await asyncio.to_thread(repo.entered.wait, 3)
                first_writer.close()
                await asyncio.wait_for(first_writer.wait_closed(), 3)
                # Admission permit belongs to the late cleanup, even after disconnect.
                await eventually(app.disconnect.is_set, label="first close while permit busy")
                for _ in range(8):
                    async with client_request(port) as (reader, _):
                        response = await asyncio.wait_for(reader.read(), 3)
                        assert b"HTTP/1.1 429" in response
                        assert b'admission_overloaded' in response
                assert repo.calls == 1
                assert repo.list_jobs() == []
                repo.release.set()
                await assert_late_cancel(repo)
                # Slots become reusable: a new request reaches durable admission.
                async with client_request(port):
                    await eventually(lambda: repo.calls == 2, label="reusable admission permit")
                assert repo.calls == 2


@pytest.mark.asyncio
async def test_asgi_disconnect_does_not_retro_cancel_success_or_unowned_job(monkeypatch):
    async with gated_gateway() as (repo, app):
        original_cancel = repo.cancel_job
        cancel_calls = []

        def observed_cancel(job_id, **kwargs):
            cancel_calls.append(job_id)
            return original_cancel(job_id, **kwargs)

        monkeypatch.setattr(repo, "cancel_job", observed_cancel)
        async with asgi_request(app) as (messages, sent, task):
            assert await asyncio.to_thread(repo.entered.wait, 3)
            repo.release.set()
            await eventually(lambda: len(repo.list_jobs()) == 1, label="HTTP Job admission")
            owned = repo.list_jobs()[0]
            claimed = repo.claim_next_job("chat-worker", runtime_instance_epoch=EPOCH)
            assert claimed is not None and claimed.job_id == owned.job_id
            unowned = repo.submit_job(
                JobSubmission(capability="code.review", payload={"ordinary": True}),
            )
            repo.complete_job(owned.job_id, JobResult(outputs={"streamed": True}),
                              worker_id="chat-worker", lease_token=claimed.lease_token or "",
                              runtime_instance_epoch=EPOCH)
            await eventually(lambda: any(m["type"] == "http.response.body" and
                                  b"[DONE]" in m.get("body", b"") for m in sent), label="terminal SSE")
            messages.put_nowait({"type": "http.disconnect"})
            await asyncio.wait_for(task, 3)
            assert repo.get_job(owned.job_id).status is JobStatus.SUCCEEDED
            assert repo.get_job(unowned.job_id).status is JobStatus.QUEUED
            # A fully completed stream must not spawn a useless DB cancellation
            # or consume a separate post-admission cancellation permit.
            assert cancel_calls == []
