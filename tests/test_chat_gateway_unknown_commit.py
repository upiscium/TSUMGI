"""Deterministic lost-COMMIT-ack simulation without an external database."""

import asyncio
import threading

import pytest
from starlette.requests import Request

from astrumweaver.control import InMemoryControlRepository, JobStatus, StorageUnavailable
from astrumweaver.gateway.api import ChatGatewayService
from astrumweaver.gateway.chat import ChatGatewayError, ChatProfileCatalog
from test_chat_gateway_admission_cancellation import _wait_for
from test_chat_gateway_api import _ConnectedRequest, register_worker, serving_values


BODY = {"model": "local-code-v1", "messages": [{"role": "user", "content": "hello"}], "stream": True}


class LostAckRepository(InMemoryControlRepository):
    def __init__(self, *, block=False, fail_cancel=False, fail_read=False, commit=True):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        if not block:
            self.release.set()
        self.fail_cancel = fail_cancel
        self.fail_read = fail_read
        self.commit = commit
        self.submissions = 0
        self.reads = 0
        self.cancels = 0
        self.keys = []

    def submit_job(self, submission, **kwargs):
        self.submissions += 1
        self.keys.append(submission.idempotency_key)
        self.entered.set()
        if not self.release.wait(3):
            raise RuntimeError("test submission not released")
        if self.commit:
            super().submit_job(submission, **kwargs)
        raise StorageUnavailable("synthetic lost COMMIT acknowledgement")

    def get_job_by_idempotency_key(self, key, **kwargs):
        self.reads += 1
        if self.fail_read:
            raise StorageUnavailable("synthetic readback unavailable")
        return super().get_job_by_idempotency_key(key, **kwargs)

    def cancel_job(self, job_id, **kwargs):
        self.cancels += 1
        if self.fail_cancel:
            raise StorageUnavailable("synthetic cancel acknowledgement lost")
        return super().cancel_job(job_id, **kwargs)


def service(repo):
    profile, serving = serving_values(timeout=15)
    register_worker(repo, serving)
    return ChatGatewayService(repo, ChatProfileCatalog((profile,)), poll_interval_seconds=0.001, max_pending_admissions=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("commit,fail_cancel,fail_read,expected_unknown", [
    (True, False, False, 0),
    (False, False, False, 1),
    (True, True, False, 1),
    (True, False, True, 1),
])
async def test_connected_lost_ack_reconciles_without_duplicate(commit, fail_cancel, fail_read, expected_unknown):
    repo = LostAckRepository(commit=commit, fail_cancel=fail_cancel, fail_read=fail_read)
    gateway = service(repo)
    with pytest.raises(ChatGatewayError) as exc:
        await gateway.stream(_ConnectedRequest(), BODY, request_size_bytes=80)
    assert exc.value.code == "admission_unavailable"
    assert repo.submissions == 1
    assert repo.keys[0] and len(repo.list_jobs()) == int(commit)
    assert repo.reads <= 3 and repo.cancels <= 3
    assert gateway._unresolved_admissions == expected_unknown
    assert gateway._admissions_in_flight == 0
    if commit and not fail_cancel and not fail_read:
        assert repo.list_jobs()[0].status is JobStatus.CANCELLED


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_cancel,expected_unknown", [(False, 0), (True, 1)])
async def test_cancelled_lost_ack_holds_permit_until_reconciled(fail_cancel, expected_unknown):
    repo = LostAckRepository(block=True, fail_cancel=fail_cancel)
    gateway = service(repo)
    task = asyncio.create_task(gateway.stream(_ConnectedRequest(), BODY, request_size_bytes=80))
    try:
        assert await asyncio.to_thread(repo.entered.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert gateway._admissions_in_flight == 1
        with pytest.raises(ChatGatewayError) as exc:
            await gateway.stream(_ConnectedRequest(), BODY, request_size_bytes=80)
        assert exc.value.code == "admission_overloaded"
    finally:
        repo.release.set()
    await _wait_for(lambda: not gateway._pending_admission_cleanup)
    assert gateway._admissions_in_flight == 0
    assert gateway._unresolved_admissions == expected_unknown
    assert repo.submissions == 1
    assert len(repo.list_jobs()) == 1
    assert repo.list_jobs()[0].status is (JobStatus.QUEUED if fail_cancel else JobStatus.CANCELLED)


@pytest.mark.asyncio
async def test_cancel_acknowledging_other_terminal_is_not_success():
    class TerminalRepo(LostAckRepository):
        def cancel_job(self, job_id, **kwargs):
            record = self.get_job(job_id)
            from dataclasses import replace
            return replace(record, status=JobStatus.FAILED)

    repo = TerminalRepo()
    gateway = service(repo)
    with pytest.raises(ChatGatewayError):
        await gateway.stream(_ConnectedRequest(), BODY, request_size_bytes=80)
    assert gateway._unresolved_admissions == 1


@pytest.mark.asyncio
async def test_transient_readback_and_lost_cancel_ack_are_rechecked():
    class TransientRepo(LostAckRepository):
        def get_job_by_idempotency_key(self, key, **kwargs):
            if self.reads == 0:
                self.reads += 1
                raise StorageUnavailable("temporary read failure")
            return super().get_job_by_idempotency_key(key, **kwargs)

        def cancel_job(self, job_id, **kwargs):
            if self.cancels == 0:
                self.cancels += 1
                InMemoryControlRepository.cancel_job(self, job_id, **kwargs)
                raise StorageUnavailable("cancel committed; acknowledgement lost")
            return super().cancel_job(job_id, **kwargs)

    repo = TransientRepo()
    gateway = service(repo)
    with pytest.raises(ChatGatewayError):
        await gateway.stream(_ConnectedRequest(), BODY, request_size_bytes=80)
    assert repo.submissions == 1
    assert repo.reads == 2 and repo.cancels == 2
    assert repo.list_jobs()[0].status is JobStatus.CANCELLED
    assert gateway._unresolved_admissions == 0


@pytest.mark.asyncio
async def test_cancelled_lost_ack_without_visible_commit_records_unknown():
    repo = LostAckRepository(block=True, commit=False)
    gateway = service(repo)
    task = asyncio.create_task(gateway.stream(_ConnectedRequest(), BODY, request_size_bytes=80))
    try:
        assert await asyncio.to_thread(repo.entered.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert gateway._admissions_in_flight == 1
    finally:
        repo.release.set()
    await _wait_for(lambda: not gateway._pending_admission_cleanup)
    assert repo.reads == 3 and not repo.list_jobs()
    # The server could still COMMIT after an ambiguous acknowledgement.
    assert gateway._unresolved_admissions == 1
    assert gateway._admissions_in_flight == 0

@pytest.mark.asyncio
async def test_lost_ack_primary_read_miss_can_precede_late_server_commit():
    """Negative control: UNKNOWN is not proof of an absent future commit."""

    class DeferredServerCommit(LostAckRepository):
        def __init__(self):
            super().__init__(commit=False)
            self.allow_server_commit = threading.Event()
            self.commit_finished = threading.Event()
            self.server_thread = None

        def submit_job(self, submission, **kwargs):
            self.submissions += 1
            self.keys.append(submission.idempotency_key)

            def commit_after_client_thread_dies():
                try:
                    if self.allow_server_commit.wait(3):
                        InMemoryControlRepository.submit_job(self, submission, **kwargs)
                finally:
                    self.commit_finished.set()

            self.server_thread = threading.Thread(
                target=commit_after_client_thread_dies, daemon=True
            )
            self.server_thread.start()
            raise StorageUnavailable("ack lost while server still deciding COMMIT")

    repo = DeferredServerCommit()
    gateway = service(repo)
    try:
        with pytest.raises(ChatGatewayError) as exc:
            await gateway.stream(_ConnectedRequest(), BODY, request_size_bytes=80)
        assert exc.value.code == "admission_unavailable"
        assert repo.reads >= 1
        assert repo.list_jobs() == []
        # A missing row in a fresh read is not proof the server cannot commit.
        assert gateway._unresolved_admissions == 1
    finally:
        repo.allow_server_commit.set()
        assert await asyncio.to_thread(repo.commit_finished.wait, 3)
        if repo.server_thread is not None:
            await asyncio.to_thread(repo.server_thread.join, 3)

    assert len(repo.list_jobs()) == 1
    assert repo.list_jobs()[0].status is JobStatus.QUEUED
    assert repo.submissions == 1
    # This is the accepted best-effort limit, not a durable claim fence.
    InMemoryControlRepository.cancel_job(repo, repo.list_jobs()[0].job_id)


@pytest.mark.asyncio
async def test_actual_asgi_disconnect_during_admission_cancels_late_job():
    repo = LostAckRepository(block=True)
    gateway = service(repo)
    messages = asyncio.Queue()
    messages.put_nowait({"type": "http.request", "body": b"{}", "more_body": False})
    request = Request({"type": "http", "method": "POST", "path": "/v1/chat/completions"}, messages.get)
    assert await request.body() == b"{}"
    task = asyncio.create_task(gateway.stream(request, BODY, request_size_bytes=80))
    try:
        assert await asyncio.to_thread(repo.entered.wait, 2)
        messages.put_nowait({"type": "http.disconnect"})
        with pytest.raises(ChatGatewayError) as exc:
            await asyncio.wait_for(task, 2)
        assert exc.value.code == "client_disconnected"
        assert gateway._admissions_in_flight == 1
        assert messages.empty()
    finally:
        repo.release.set()
    await _wait_for(lambda: not gateway._pending_admission_cleanup)
    assert gateway._admissions_in_flight == 0
    assert gateway._unresolved_admissions == 0
    assert repo.list_jobs()[0].status is JobStatus.CANCELLED


@pytest.mark.asyncio
async def test_repeated_request_cancellation_does_not_duplicate_cleanup_or_permit():
    repo = LostAckRepository(block=True)
    gateway = service(repo)
    task = asyncio.create_task(gateway.stream(_ConnectedRequest(), BODY, request_size_bytes=80))
    try:
        assert await asyncio.to_thread(repo.entered.wait, 2)
        task.cancel()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert gateway._admissions_in_flight == 1
        assert len(gateway._pending_admission_cleanup) == 1
    finally:
        repo.release.set()
    await _wait_for(lambda: not gateway._pending_admission_cleanup)
    assert gateway._admissions_in_flight == 0
    assert repo.list_jobs()[0].status is JobStatus.CANCELLED


@pytest.mark.asyncio
async def test_cancel_during_readback_transfers_slot_to_existing_reconciliation():
    class SlowReadback(LostAckRepository):
        def __init__(self):
            super().__init__()
            self.read_started = threading.Event()
            self.read_release = threading.Event()

        def get_job_by_idempotency_key(self, key, **kwargs):
            self.read_started.set()
            if not self.read_release.wait(3):
                raise RuntimeError("test readback not released")
            return super().get_job_by_idempotency_key(key, **kwargs)

    repo = SlowReadback()
    gateway = service(repo)
    task = asyncio.create_task(gateway.stream(_ConnectedRequest(), BODY, request_size_bytes=80))
    try:
        assert await asyncio.to_thread(repo.read_started.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert gateway._admissions_in_flight == 1
        assert len(gateway._pending_admission_cleanup) == 1
        with pytest.raises(ChatGatewayError) as exc:
            await gateway.stream(_ConnectedRequest(), BODY, request_size_bytes=80)
        assert exc.value.code == "admission_overloaded"
    finally:
        repo.read_release.set()
    await _wait_for(lambda: not gateway._pending_admission_cleanup)
    assert gateway._admissions_in_flight == 0
    assert repo.list_jobs()[0].status is JobStatus.CANCELLED


@pytest.mark.asyncio
async def test_unexpected_post_commit_exception_is_reconciled_as_503():
    class UnexpectedAck(LostAckRepository):
        def submit_job(self, submission, **kwargs):
            self.submissions += 1
            self.keys.append(submission.idempotency_key)
            InMemoryControlRepository.submit_job(self, submission, **kwargs)
            raise RuntimeError("unexpected post-commit failure")

    repo = UnexpectedAck()
    gateway = service(repo)
    with pytest.raises(ChatGatewayError) as exc:
        await gateway.stream(_ConnectedRequest(), BODY, request_size_bytes=80)
    assert exc.value.status_code == 503
    assert repo.list_jobs()[0].status is JobStatus.CANCELLED
    assert gateway._admissions_in_flight == 0
