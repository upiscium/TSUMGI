from __future__ import annotations

import asyncio

import httpx
import pytest

from astrumweaver import JobEvent, JobResult, ResidencyReport, ResourceShape, WorkerSpec
from astrumweaver.control import InMemoryControlRepository, JobStatus, JobSubmission
from astrumweaver.control.api import create_app
from astrumweaver.gateway.chat import validate_chat_stream_chunk
from astrumweaver.worker import ControlClient, WorkerRuntime
from astrumweaver.worker.event_coalescing import (
    CHAT_CHUNK_KIND,
    CoalescingJobEventSink,
    merge_chat_chunks,
)

# Captured from llama.cpp b11430 (sirius), minus the trailing usage chunk.
_META = {
    "created": 1794874197,
    "id": "chatcmpl-3kKQ",
    "model": "qwen3.6-35b-a3b-fast",
    "system_fingerprint": "b11430-8345f3339",
    "object": "chat.completion.chunk",
}


def chunk(delta, finish_reason=None, **extra):
    return {
        "choices": [{"finish_reason": finish_reason, "index": 0, "delta": delta}],
        **_META,
        **extra,
    }


def tool(index, arguments, **head):
    call = {"index": index, "function": {"arguments": arguments}}
    if head:
        call["id"] = head["id"]
        call["type"] = "function"
        call["function"]["name"] = head["name"]
    return chunk({"tool_calls": [call]})


def text_of(chunks):
    return "".join(
        c["choices"][0]["delta"].get("content") or "" for c in chunks if c["choices"]
    )


def test_text_deltas_merge_after_llama_cpp_role_chunk():
    merged = merge_chat_chunks(chunk({"role": "assistant", "content": None}), chunk({"content": "Hel"}))
    merged = merge_chat_chunks(merged, chunk({"content": "lo"}))
    assert merged == chunk({"role": "assistant", "content": "Hello"})
    validate_chat_stream_chunk(merged)


def test_reasoning_and_content_merge_per_key():
    merged = merge_chat_chunks(chunk({"reasoning_content": "a"}), chunk({"reasoning_content": "b"}))
    assert merged["choices"][0]["delta"] == {"reasoning_content": "ab"}


@pytest.mark.parametrize(
    "first, second",
    [
        (chunk({"content": "a"}), chunk({"content": "b"}, finish_reason="stop")),
        (chunk({"content": "a"}, finish_reason="stop"), chunk({"content": "b"})),
        (chunk({"content": "a"}), chunk({"role": "assistant", "content": "b"})),
        (chunk({"content": "a"}), {**chunk({"content": "b"}), "usage": {"total_tokens": 1}}),
        (chunk({"content": "a"}), {**chunk({"content": "b"}), "timings": {}}),
        (chunk({"content": "a"}), {**chunk({"content": "b"}), "id": "other"}),
        (chunk({"content": "a"}), {**_META, "choices": []}),
        (chunk({"content": "a"}), tool(0, "{", id="call_1", name="read")),
        (tool(0, "{", id="call_1", name="read"), chunk({"content": "a"})),
        (tool(0, "{\"a\":", id="call_1", name="read"), tool(1, "{", id="call_2", name="grep")),
        (tool(0, "{\"a\":", id="call_1", name="read"), tool(0, "1}", id="call_1", name="read")),
        (tool(0, "{"), tool(1, "}")),
    ],
)
def test_unmergeable_pairs_stay_separate(first, second):
    assert merge_chat_chunks(first, second) is None


def test_tool_argument_fragments_merge_into_the_opening_call():
    merged = merge_chat_chunks(tool(0, "{\"path\":", id="call_1", name="read"), tool(0, "\"a.py\""))
    merged = merge_chat_chunks(merged, tool(0, "}"))
    call = merged["choices"][0]["delta"]["tool_calls"][0]
    assert call == {
        "index": 0,
        "id": "call_1",
        "type": "function",
        "function": {"arguments": "{\"path\":\"a.py\"}", "name": "read"},
    }
    validate_chat_stream_chunk(merged)


class SlowPublisher:
    def __init__(self, delay=0.01, fail_at=None):
        self.delay = delay
        self.fail_at = fail_at
        self.published: list[JobEvent] = []

    async def __call__(self, event):
        await asyncio.sleep(self.delay)
        if self.fail_at is not None and len(self.published) >= self.fail_at:
            raise RuntimeError("control rejected the event")
        self.published.append(event)


def chat_event(payload):
    return JobEvent(kind=CHAT_CHUNK_KIND, payload=payload)


@pytest.mark.asyncio
async def test_sink_publishes_first_chunk_alone_and_merges_the_rest_in_order():
    publish = SlowPublisher()
    sink = CoalescingJobEventSink(publish)
    tokens = [f"t{i} " for i in range(200)]
    await sink.emit(chat_event(chunk({"role": "assistant", "content": None})))
    for token in tokens:
        await sink.emit(chat_event(chunk({"content": token})))
    await sink.emit(chat_event(chunk({}, finish_reason="stop")))
    await sink.flush()

    payloads = [dict(event.payload) for event in publish.published]
    assert len(payloads) < 10
    assert payloads[0] == chunk({"role": "assistant", "content": None})
    assert text_of(payloads) == "".join(tokens)
    assert payloads[-1]["choices"][0]["finish_reason"] == "stop"
    for payload in payloads:
        validate_chat_stream_chunk(payload)


@pytest.mark.asyncio
async def test_sink_never_merges_other_event_kinds():
    publish = SlowPublisher()
    sink = CoalescingJobEventSink(publish)
    for index in range(5):
        await sink.emit(JobEvent(kind="debug.chunk", payload={"index": index}))
    await sink.flush()
    assert [event.payload["index"] for event in publish.published] == [0, 1, 2, 3, 4]


@pytest.mark.asyncio
async def test_sink_caps_merged_size():
    publish = SlowPublisher()
    sink = CoalescingJobEventSink(publish, max_merged_bytes=600)
    for _ in range(50):
        await sink.emit(chat_event(chunk({"content": "x" * 40})))
    await sink.flush()
    assert len(publish.published) > 3
    assert text_of([e.payload for e in publish.published]) == "x" * 2000


@pytest.mark.asyncio
async def test_sink_bounds_pending_unmergeable_events():
    publish = SlowPublisher(delay=0.005)
    sink = CoalescingJobEventSink(publish, max_pending_events=2)
    for index in range(10):
        await sink.emit(JobEvent(kind="debug.chunk", payload={"index": index}))
        assert len(sink._pending) <= 2
    await sink.flush()
    assert len(publish.published) == 10


@pytest.mark.asyncio
async def test_publish_failure_is_raised_by_next_emit_and_flush():
    publish = SlowPublisher(fail_at=1)
    sink = CoalescingJobEventSink(publish)
    await sink.emit(chat_event(chunk({"content": "a"})))
    await sink.emit(chat_event(chunk({"content": "b"}, finish_reason="stop")))
    with pytest.raises(RuntimeError, match="control rejected"):
        await sink.flush()
    with pytest.raises(RuntimeError, match="control rejected"):
        await sink.emit(chat_event(chunk({"content": "c"})))
    assert len(publish.published) == 1


WORKER_TOKEN = "worker-secret"


class TokenExecutor:
    capabilities = frozenset({"debug.echo"})

    def __init__(self, count):
        self.count = count

    async def execute(self, job):
        raise AssertionError("legacy path")

    async def execute_stream(self, job, sink):
        await sink.emit(chat_event(chunk({"role": "assistant", "content": None})))
        for index in range(self.count):
            await sink.emit(chat_event(chunk({"content": f"{index},"})))
        await sink.emit(chat_event(chunk({}, finish_reason="stop")))
        return JobResult(outputs={"streamed": True})

    async def cancel(self, job_id):
        return None

    async def residency(self):
        return ResidencyReport()


@pytest.mark.asyncio
async def test_worker_coalesces_chat_chunks_before_the_terminal_write():
    repo = InMemoryControlRepository()
    app = create_app(
        repo,
        client_token=None,
        worker_token=WORKER_TOKEN,
        client_auth="none",
        maintenance_interval_seconds=60,
    )
    control = ControlClient("http://control", WORKER_TOKEN, transport=httpx.ASGITransport(app=app))
    spec = WorkerSpec(
        worker_id="stream-worker",
        worker_class="cpu-test",
        resources=ResourceShape(),
        capabilities=frozenset({"debug.echo"}),
    )
    runtime = WorkerRuntime(
        spec=spec,
        max_concurrency=1,
        executor=TokenExecutor(300),
        client=control,
        heartbeat_interval_seconds=1,
    )
    try:
        await runtime.register()
        job = repo.submit_job(JobSubmission(capability="debug.echo", max_attempts=1))
        claimed = await control.claim(spec.worker_id)
        await runtime._execute_claim(claimed)
    finally:
        await control.aclose()

    events = repo.list_job_events(job.job_id, limit=128)
    assert repo.get_job(job.job_id).status is JobStatus.SUCCEEDED
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert len(events) < 302
    payloads = [event.payload for event in events]
    assert text_of(payloads) == "".join(f"{i}," for i in range(300))
    assert payloads[-1]["choices"][0]["finish_reason"] == "stop"
