"""Bound resource amplification during cancelled, still-blocked admissions (#137)."""

from __future__ import annotations

import asyncio
import threading

import pytest

from astrumweaver.control import (
    DeadlineExceededError,
    JobStatus,
    JobSubmission,
    StorageUnavailable,
    utc_now,
)
from datetime import timedelta
from astrumweaver.gateway.api import ChatGatewayService
from astrumweaver.gateway.chat import ChatGatewayError, ChatProfileCatalog
from test_chat_gateway_admission_cancellation import BlockedAdmissionRepository, _wait_for
from test_chat_gateway_api import _ConnectedRequest, register_worker, serving_values


_BODY = {
    "model": "local-code-v1",
    "messages": [{"role": "user", "content": "admission budget"}],
    "stream": True,
}


class CountingBlockedAdmissionRepository(BlockedAdmissionRepository):
    def __init__(self, *, fail_after_release: bool = False):
        super().__init__(fail_after_release=fail_after_release)
        self.calls = 0
        self._calls_lock = threading.Lock()

    def submit_job(self, *args, **kwargs):
        with self._calls_lock:
            self.calls += 1
        return super().submit_job(*args, **kwargs)


def _service(repo: CountingBlockedAdmissionRepository, *, budget: int = 2):
    profile, serving = serving_values(timeout=15)
    register_worker(repo, serving)
    return ChatGatewayService(
        repo, ChatProfileCatalog((profile,)),
        poll_interval_seconds=0.001,
        max_pending_admissions=budget,
    )


@pytest.mark.asyncio
async def test_cancelled_blocked_admission_holds_permit_and_rejects_flood():
    repo = CountingBlockedAdmissionRepository()
    service = _service(repo)
    requests = [
        asyncio.create_task(
            service.stream(_ConnectedRequest(), _BODY, request_size_bytes=80)
        )
        for _ in range(2)
    ]
    try:
        await _wait_for(lambda: repo.calls == 2)
        assert service._admissions_in_flight == 2
        for request in requests:
            request.cancel()
        for request in requests:
            with pytest.raises(asyncio.CancelledError):
                await request
        assert len(service._pending_admission_cleanup) == 2

        # A cancelled-client flood is rejected BEFORE new to_thread work.
        for _ in range(12):
            with pytest.raises(ChatGatewayError) as exc:
                await service.stream(
                    _ConnectedRequest(), _BODY, request_size_bytes=80
                )
            assert exc.value.status_code == 429
            assert exc.value.code == "admission_overloaded"
        assert repo.calls == 2  # No extra synchronous submit was scheduled.
        assert not repo.list_jobs()

        repo.release.set()
        await _wait_for(lambda: len(repo.list_jobs()) == 2)
        await _wait_for(lambda: all(
            job.status is JobStatus.CANCELLED for job in repo.list_jobs()
        ))
        await _wait_for(lambda: service._admissions_in_flight == 0)
        assert not service._pending_admission_cleanup
        assert service._unexpected_cleanup_failures == 0
        assert repo.get_worker("chat-worker").active_jobs == 0

        # Budget is reusable after the late admission has been handled.
        stream = await service.stream(
            _ConnectedRequest(), _BODY, request_size_bytes=80
        )
        assert service._admissions_in_flight == 0
        await stream.aclose()
    finally:
        repo.release.set()
        for request in requests:
            if not request.done():
                request.cancel()
        await asyncio.gather(*requests, return_exceptions=True)
        for job in repo.list_jobs():
            repo.cancel_job(job.job_id)


@pytest.mark.asyncio
async def test_failed_delayed_admission_releases_permit_without_phantom_job():
    repo = CountingBlockedAdmissionRepository(fail_after_release=True)
    service = _service(repo, budget=1)
    task = asyncio.create_task(
        service.stream(_ConnectedRequest(), _BODY, request_size_bytes=80)
    )
    try:
        await _wait_for(lambda: repo.calls == 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert service._admissions_in_flight == 1
        repo.release.set()
        await _wait_for(lambda: service._admissions_in_flight == 0)
        assert repo.list_jobs() == []
        assert not service._pending_admission_cleanup
        assert service._unexpected_cleanup_failures == 0
    finally:
        repo.release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)




class BlockedCancelRepository(CountingBlockedAdmissionRepository):
    def __init__(self):
        super().__init__()
        self.cancel_entered = threading.Event()
        self.cancel_release = threading.Event()

    def cancel_job(self, *args, **kwargs):
        self.cancel_entered.set()
        if not self.cancel_release.wait(timeout=4):
            raise RuntimeError("test cancellation was not unblocked")
        return super().cancel_job(*args, **kwargs)


@pytest.mark.asyncio
async def test_permit_remains_held_while_late_cancel_acknowledgement_is_blocked():
    repo = BlockedCancelRepository()
    service = _service(repo, budget=1)
    request = asyncio.create_task(
        service.stream(_ConnectedRequest(), _BODY, request_size_bytes=80)
    )
    try:
        await _wait_for(lambda: repo.calls == 1)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request

        # Admission is now permitted to finish, but cancellation itself stalls.
        repo.release.set()
        assert await asyncio.to_thread(repo.cancel_entered.wait, 2)
        assert service._admissions_in_flight == 1
        assert len(service._pending_admission_cleanup) == 1
        assert len(repo.list_jobs()) == 1
        assert repo.list_jobs()[0].status is JobStatus.QUEUED

        with pytest.raises(ChatGatewayError) as exc:
            await service.stream(
                _ConnectedRequest(), _BODY, request_size_bytes=80
            )
        assert exc.value.code == "admission_overloaded"
        assert repo.calls == 1

        repo.cancel_release.set()
        await _wait_for(
            lambda: repo.list_jobs()[0].status is JobStatus.CANCELLED
        )
        await _wait_for(lambda: service._admissions_in_flight == 0)
        assert not service._pending_admission_cleanup
        assert service._unexpected_cleanup_failures == 0
        assert repo.get_worker("chat-worker").active_jobs == 0
    finally:
        repo.release.set()
        repo.cancel_release.set()
        if not request.done():
            request.cancel()
        await asyncio.gather(request, return_exceptions=True)
        for job in repo.list_jobs():
            repo.cancel_job(job.job_id)


@pytest.mark.parametrize("budget", [0, -1, True, False, 1.5, "8", None])
def test_admission_budget_must_be_explicit_positive_integer(budget):
    profile, _ = serving_values()
    with pytest.raises(ValueError, match="max_pending_admissions"):
        ChatGatewayService(
            CountingBlockedAdmissionRepository(),
            ChatProfileCatalog((profile,)),
            max_pending_admissions=budget,
        )

@pytest.mark.asyncio
async def test_in_memory_late_admission_rechecks_real_deadline_after_thread_wait():
    repo = CountingBlockedAdmissionRepository()
    profile, serving = serving_values(timeout=0.15)
    register_worker(repo, serving)
    service = ChatGatewayService(
        repo, ChatProfileCatalog((profile,)), max_pending_admissions=1
    )
    request = asyncio.create_task(
        service.stream(_ConnectedRequest(), _BODY, request_size_bytes=80)
    )
    try:
        assert await asyncio.to_thread(repo.entered.wait, 2)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        await asyncio.sleep(0.3)  # Synthetic DB wait exceeds request deadline.
        repo.release.set()
        await _wait_for(lambda: service._admissions_in_flight == 0)
        assert repo.list_jobs() == []
        assert not service._pending_admission_cleanup
        assert service._unexpected_cleanup_failures == 0
    finally:
        repo.release.set()
        if not request.done():
            request.cancel()
        await asyncio.gather(request, return_exceptions=True)


@pytest.mark.asyncio
async def test_storage_failure_does_not_expose_database_diagnostics_to_chat_client():
    class StorageFailureRepository(CountingBlockedAdmissionRepository):
        def submit_job(self, *args, **kwargs):
            raise StorageUnavailable("synthetic private PostgreSQL diagnostic")

    repo = StorageFailureRepository()
    service = _service(repo, budget=1)
    with pytest.raises(ChatGatewayError) as exc:
        await service.stream(
            _ConnectedRequest(), _BODY, request_size_bytes=80
        )
    assert exc.value.status_code == 503
    assert exc.value.code == "admission_unavailable"
    assert "private" not in str(exc.value)
    assert service._admissions_in_flight == 0


def test_inmemory_direct_bounded_submission_rejects_stale_injected_now():
    repo = CountingBlockedAdmissionRepository()
    repo.release.set()
    deadline = utc_now() - timedelta(seconds=1)
    with pytest.raises(DeadlineExceededError, match="admission deadline expired"):
        repo.submit_job(
            JobSubmission(capability="code.review"),
            now=deadline - timedelta(days=1),
            admission_deadline_at=deadline,
        )
    assert repo.list_jobs() == []
