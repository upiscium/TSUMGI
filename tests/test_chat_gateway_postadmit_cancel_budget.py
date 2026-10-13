"""Post-admission disconnects cannot amplify blocked cancellation threads."""

import asyncio
import threading
from contextlib import contextmanager
from datetime import timedelta

import pytest

from astrumweaver.control import (
    DeadlineExceededError, InMemoryControlRepository, JobStatus, JobSubmission,
    PostgresControlRepository, utc_now,
)
import astrumweaver.gateway.api as gateway_module
from astrumweaver.gateway.api import ChatGatewayService
from astrumweaver.gateway.chat import ChatGatewayError, ChatProfileCatalog
from test_chat_gateway_admission_cancellation import _wait_for
from test_chat_gateway_api import _DisconnectedRequest, _ConnectedRequest, register_worker, serving_values


_BODY = {"model": "local-code-v1", "messages": [{"role": "user", "content": "disconnect"}]}


class BlockingCancelRepository(InMemoryControlRepository):
    def __init__(self):
        super().__init__()
        self.release = threading.Event()
        self.entered = threading.Event()
        self.calls = 0
        self.deadlines = []
        self.lock = threading.Lock()
        self.fail = False

    def cancel_job(self, job_id, *, now=None, admission_deadline_at=None):
        with self.lock:
            self.calls += 1
            self.deadlines.append(admission_deadline_at)
            self.entered.set()
        if not self.release.wait(4):
            raise RuntimeError("blocked test cancellation")
        if self.fail:
            raise RuntimeError("private SQL secret")
        return super().cancel_job(job_id, now=now, admission_deadline_at=admission_deadline_at)


def _service(repo, *, budget=2):
    profile, serving = serving_values(timeout=15)
    register_worker(repo, serving)
    return ChatGatewayService(
        repo, ChatProfileCatalog((profile,)), poll_interval_seconds=0.001,
        max_pending_admissions=1, max_postadmit_cancellations=budget,
    )


@pytest.mark.asyncio
async def test_disconnected_admitted_flood_rejects_extra_threads_and_retains_slots():
    repo = BlockingCancelRepository()
    service = _service(repo)
    tasks = []
    try:
        for _ in range(2):
            task = asyncio.create_task(service.complete(_DisconnectedRequest(), _BODY, request_size_bytes=70))
            tasks.append(task)
            await _wait_for(lambda: repo.calls == len(tasks))
        assert service._admissions_in_flight == 0
        assert len(service._postadmit_cancellations) == 2
        for _ in range(12):
            with pytest.raises(ChatGatewayError) as exc:
                await service.complete(_DisconnectedRequest(), _BODY, request_size_bytes=70)
            assert exc.value.code == "client_disconnected"
        assert repo.calls == 2
        assert len(service._postadmit_cancellations) == 2
        assert service._unresolved_cancellations == 12
        assert all(job.status is JobStatus.QUEUED for job in repo.list_jobs())
        assert all(deadline is not None and deadline > utc_now() for deadline in repo.deadlines)
    finally:
        repo.release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
    await _wait_for(lambda: not service._postadmit_cancellations)
    assert repo.calls == 2
    assert sum(job.status is JobStatus.CANCELLED for job in repo.list_jobs()) == 2
    assert sum(job.status is JobStatus.QUEUED for job in repo.list_jobs()) == 12
    with pytest.raises(ChatGatewayError):
        await service.complete(_DisconnectedRequest(), _BODY, request_size_bytes=70)
    assert repo.calls == 3


@pytest.mark.asyncio
async def test_cancelled_caller_does_not_release_slot_before_thread_outcome(caplog):
    repo = BlockingCancelRepository()
    service = _service(repo, budget=1)
    caller = asyncio.create_task(service.complete(_DisconnectedRequest(), _BODY, request_size_bytes=70))
    try:
        await _wait_for(lambda: repo.calls == 1)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert len(service._postadmit_cancellations) == 1
        with pytest.raises(ChatGatewayError):
            await service.complete(_DisconnectedRequest(), _BODY, request_size_bytes=70)
        assert repo.calls == 1
        repo.fail = True
    finally:
        repo.release.set()
        await asyncio.gather(caller, return_exceptions=True)
    await _wait_for(lambda: not service._postadmit_cancellations)
    assert service._unresolved_cancellations == 2
    assert "private SQL secret" not in caplog.text
    assert all(job.status is JobStatus.QUEUED for job in repo.list_jobs())


@pytest.mark.asyncio
async def test_naturally_terminal_completion_does_not_attempt_cancellation():
    repo = BlockingCancelRepository()
    service = _service(repo)
    original = repo.submit_job

    def submit_terminal(*args, **kwargs):
        job = original(*args, **kwargs)
        repo.cancel_job_direct(job.job_id)
        return job

    repo.cancel_job_direct = InMemoryControlRepository.cancel_job.__get__(repo)
    repo.submit_job = submit_terminal
    with pytest.raises(ChatGatewayError) as exc:
        await service.complete(_ConnectedRequest(), _BODY, request_size_bytes=70)
    assert exc.value.code == "request_cancelled"
    assert repo.calls == 0
    assert not service._postadmit_cancellations


@pytest.mark.asyncio
async def test_cancel_waiter_deadline_does_not_release_blocked_thread_slot(monkeypatch):
    # A client should get a bounded response even if the DB thread is stuck.
    # The thread and its permit remain until its actual disposition is known.
    monkeypatch.setattr(gateway_module, "_POSTADMIT_CANCEL_TIMEOUT_SECONDS", 0.06)
    repo = BlockingCancelRepository()
    service = _service(repo, budget=1)
    task = asyncio.create_task(
        service.complete(_DisconnectedRequest(), _BODY, request_size_bytes=70)
    )
    try:
        await _wait_for(lambda: repo.calls == 1)
        with pytest.raises(ChatGatewayError) as exc:
            await asyncio.wait_for(task, 2)
        assert exc.value.code == "client_disconnected"
        assert len(service._postadmit_cancellations) == 1
        assert repo.list_jobs()[0].status is JobStatus.QUEUED
    finally:
        repo.release.set()
        await asyncio.gather(task, return_exceptions=True)
    await _wait_for(lambda: not service._postadmit_cancellations)
    assert service._unresolved_cancellations == 1
    assert repo.list_jobs()[0].status is JobStatus.QUEUED


def test_inmemory_optional_cancel_deadline_keeps_unbounded_nonchat_behavior():
    repo = InMemoryControlRepository()
    job = repo.submit_job(JobSubmission(capability="code.review"))
    with pytest.raises(DeadlineExceededError):
        repo.cancel_job(job.job_id, admission_deadline_at=utc_now() - timedelta(seconds=1))
    assert repo.get_job(job.job_id).status is JobStatus.QUEUED
    assert repo.cancel_job(job.job_id).status is JobStatus.CANCELLED


def test_postgres_cancel_passes_optional_deadline_to_transaction(monkeypatch):
    repo = PostgresControlRepository("postgresql://unused/unused")
    seen = []

    @contextmanager
    def transaction(*, admission_deadline_at=None):
        seen.append(admission_deadline_at)
        raise RuntimeError("transaction intercepted without connecting")
        yield

    monkeypatch.setattr(repo, "_transaction", transaction)
    deadline = utc_now() + timedelta(seconds=5)
    with pytest.raises(RuntimeError, match="intercepted"):
        repo.cancel_job("unused", admission_deadline_at=deadline)
    with pytest.raises(RuntimeError, match="intercepted"):
        repo.cancel_job("unused")
    assert seen == [deadline, None]


@pytest.mark.parametrize("budget", [0, -1, True, 1.5, "2", None])
def test_postadmit_budget_requires_positive_integer(budget):
    profile, _ = serving_values()
    with pytest.raises(ValueError, match="max_postadmit_cancellations"):
        ChatGatewayService(InMemoryControlRepository(), ChatProfileCatalog((profile,)),
                           max_postadmit_cancellations=budget)
