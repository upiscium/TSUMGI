"""Gateway cleanup when durable admission finishes after client cancellation (#137)."""

from __future__ import annotations

import asyncio
import threading

import pytest

from astrumweaver.control import InMemoryControlRepository, JobStatus
from astrumweaver.gateway.api import ChatGatewayService
from astrumweaver.gateway.chat import ChatProfileCatalog
from test_chat_gateway_api import _ConnectedRequest, register_worker, serving_values


class BlockedAdmissionRepository(InMemoryControlRepository):
    def __init__(self, *, fail_after_release: bool = False) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.fail_after_release = fail_after_release

    def submit_job(self, *args, **kwargs):
        self.entered.set()
        if not self.release.wait(timeout=4):
            raise RuntimeError("test admission was never unblocked")
        if self.fail_after_release:
            raise RuntimeError("synthetic failure with no durable Job")
        return super().submit_job(*args, **kwargs)


def _service(repo: BlockedAdmissionRepository) -> ChatGatewayService:
    profile, serving = serving_values(timeout=15)
    register_worker(repo, serving)
    return ChatGatewayService(
        repo, ChatProfileCatalog((profile,)), poll_interval_seconds=0.001
    )


async def _wait_for(test, *, seconds: float = 2):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while loop.time() < deadline:
        if test():
            return
        await asyncio.sleep(0.01)
    pytest.fail("eventual admission cancellation was not observed")


@pytest.mark.asyncio
async def test_cancel_inflight_threaded_admission_cancels_later_accepted_job():
    repo = BlockedAdmissionRepository()
    service = _service(repo)
    request = asyncio.create_task(
        service.stream(
            _ConnectedRequest(),
            {
                "model": "local-code-v1",
                "messages": [{"role": "user", "content": "disconnect-during-admission"}],
                "stream": True,
            },
            request_size_bytes=128,
        )
    )
    try:
        assert await asyncio.to_thread(repo.entered.wait, 2)
        assert not repo.list_jobs()
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        repo.release.set()
        await _wait_for(lambda: len(repo.list_jobs()) == 1)
        await _wait_for(
            lambda: repo.list_jobs()[0].status is JobStatus.CANCELLED
        )
        await _wait_for(lambda: not service._pending_admission_cleanup)
        assert repo.get_worker("chat-worker").active_jobs == 0
    finally:
        repo.release.set()
        for job in repo.list_jobs():
            repo.cancel_job(job.job_id)


@pytest.mark.asyncio
async def test_cancel_inflight_admission_that_fails_has_no_job_or_leaked_cleanup():
    repo = BlockedAdmissionRepository(fail_after_release=True)
    service = _service(repo)
    request = asyncio.create_task(
        service.stream(
            _ConnectedRequest(),
            {
                "model": "local-code-v1",
                "messages": [{"role": "user", "content": "failed-admission"}],
                "stream": True,
            },
            request_size_bytes=128,
        )
    )
    try:
        assert await asyncio.to_thread(repo.entered.wait, 2)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        repo.release.set()
        await _wait_for(lambda: not service._pending_admission_cleanup)
        assert repo.list_jobs() == []
        assert repo.get_worker("chat-worker").active_jobs == 0
    finally:
        repo.release.set()
