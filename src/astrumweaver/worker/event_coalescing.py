"""Coalesce streamed chat chunks while one Job event publish is in flight.

Every Job event is one fenced Control RPC. Publishing one chunk per token
caps delivered throughput at one token per round trip, far below decode
speed. The sink below publishes the first event at once and merges the
text or tool-argument deltas that arrive meanwhile into the next event.
Merged chunks stay ordinary OpenAI chat chunks with longer deltas.

Only plainly mergeable chunks merge: one choice with the same index, no
finish_reason, logprobs or usage, equal top-level metadata, and either text
deltas on both sides or argument fragments of the same tool call. Anything
else (role changes, a new tool call, a finish chunk) starts a new event.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from astrumweaver.execution import JobEvent

CHAT_CHUNK_KIND = "chat.completion.chunk"

# Merged deltas stay far below the Control per-event payload limit (64 KiB).
DEFAULT_MAX_MERGED_BYTES = 16 * 1024
# Unmergeable events queue up to this many before emit() waits for a publish.
DEFAULT_MAX_PENDING_EVENTS = 64

_TOP_LEVEL_KEYS = frozenset({"id", "object", "created", "model", "system_fingerprint", "choices"})
_CHOICE_KEYS = frozenset({"index", "delta", "finish_reason", "logprobs"})
_TEXT_KEYS = ("content", "reasoning_content")


def _single_choice(chunk: Mapping[str, Any]) -> Mapping[str, Any] | None:
    if not set(chunk) <= _TOP_LEVEL_KEYS:
        return None
    choices = chunk.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        return None
    choice = choices[0]
    if not isinstance(choice, Mapping) or not set(choice) <= _CHOICE_KEYS:
        return None
    if choice.get("finish_reason") is not None or choice.get("logprobs") is not None:
        return None
    if not isinstance(choice.get("delta"), Mapping):
        return None
    return choice


def _text_delta(delta: Mapping[str, Any], *, allow_role: bool) -> bool:
    allowed = {"role", *_TEXT_KEYS} if allow_role else set(_TEXT_KEYS)
    if not delta or not set(delta) <= allowed:
        return False
    # llama.cpp opens with {"role": "assistant", "content": null}.
    return all(delta.get(key) is None or isinstance(delta[key], str) for key in _TEXT_KEYS)


def _text_keys(delta: Mapping[str, Any]) -> set[str]:
    return {key for key in _TEXT_KEYS if delta.get(key) is not None}


def _tool_fragment(delta: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The single tool call of a delta that carries only tool_calls."""
    if set(delta) != {"tool_calls"}:
        return None
    calls = delta["tool_calls"]
    if not isinstance(calls, list) or len(calls) != 1 or not isinstance(calls[0], Mapping):
        return None
    call = calls[0]
    if not set(call) <= {"index", "id", "type", "function"}:
        return None
    function = call.get("function")
    if not isinstance(function, Mapping) or not set(function) <= {"name", "arguments"}:
        return None
    if not isinstance(function.get("arguments", ""), str):
        return None
    return call


def merge_chat_chunks(
    first: Mapping[str, Any], second: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Return one chunk equivalent to `first` then `second`, or None."""
    left = _single_choice(first)
    right = _single_choice(second)
    if left is None or right is None:
        return None
    if left.get("index", 0) != right.get("index", 0):
        return None
    if {k: v for k, v in first.items() if k != "choices"} != {
        k: v for k, v in second.items() if k != "choices"
    }:
        return None
    left_delta, right_delta = left["delta"], right["delta"]

    if (
        _text_delta(left_delta, allow_role=True)
        and _text_delta(right_delta, allow_role=False)
        # Only extend channels the left delta already has: content then
        # reasoning merged into one delta would lose their order.
        and _text_keys(right_delta) <= (_text_keys(left_delta) or _text_keys(right_delta))
    ):
        delta: dict[str, Any] = dict(left_delta)
        for key in _TEXT_KEYS:
            if right_delta.get(key) is not None:
                delta[key] = (delta.get(key) or "") + right_delta[key]
    else:
        left_call = _tool_fragment(left_delta)
        right_call = _tool_fragment(right_delta)
        if left_call is None or right_call is None:
            return None
        # The continuation must be a bare argument fragment of the same call.
        if right_call.get("index") != left_call.get("index") or set(right_call) != {
            "index", "function"
        } or set(right_call["function"]) != {"arguments"}:
            return None
        function = dict(left_call["function"])
        function["arguments"] = str(function.get("arguments", "")) + right_call["function"]["arguments"]
        call = dict(left_call)
        call["function"] = function
        delta = {"tool_calls": [call]}

    choice = dict(left)
    choice["delta"] = delta
    merged = dict(first)
    merged["choices"] = [choice]
    return merged


def _event_bytes(event: JobEvent) -> int:
    return len(json.dumps(dict(event.payload), separators=(",", ":"), ensure_ascii=False).encode())


class CoalescingJobEventSink:
    """A JobEventSink that keeps at most one publish in flight.

    `emit` queues the event and returns. A single background task publishes
    events in order. A publish failure is raised from the next `emit` and
    from `flush`, as the same exception the direct publish would have raised.
    """

    def __init__(
        self,
        publish: Callable[[JobEvent], Awaitable[None]],
        *,
        max_merged_bytes: int = DEFAULT_MAX_MERGED_BYTES,
        max_pending_events: int = DEFAULT_MAX_PENDING_EVENTS,
    ) -> None:
        self._publish = publish
        self._max_merged_bytes = max_merged_bytes
        self._max_pending_events = max_pending_events
        self._pending: deque[JobEvent] = deque()
        self._task: asyncio.Task[None] | None = None
        self._error: BaseException | None = None
        self._progress = asyncio.Event()

    async def emit(self, event: JobEvent) -> None:
        if not isinstance(event, JobEvent):
            raise TypeError("streaming executor must emit JobEvent values")
        self._raise_error()
        if not self._merge_into_tail(event):
            while len(self._pending) >= self._max_pending_events:
                self._progress.clear()
                await self._progress.wait()
                self._raise_error()
            self._pending.append(event)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._drain(), name="astrumweaver-job-events")
            # Let the publisher take this event now, so the first token is
            # not held back until the executor next awaits.
            await asyncio.sleep(0)

    async def flush(self) -> None:
        """Wait until every queued event is published, then raise any failure."""
        while self._task is not None and not self._task.done():
            await asyncio.shield(self._task)
        self._raise_error()

    async def aclose(self) -> None:
        """Stop publishing without waiting; queued events are dropped."""
        self._pending.clear()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task

    def _merge_into_tail(self, event: JobEvent) -> bool:
        if not self._pending or event.kind != CHAT_CHUNK_KIND:
            return False
        tail = self._pending[-1]
        if tail.kind != CHAT_CHUNK_KIND:
            return False
        merged = merge_chat_chunks(tail.payload, event.payload)
        if merged is None:
            return False
        candidate = JobEvent(kind=CHAT_CHUNK_KIND, payload=merged)
        if _event_bytes(candidate) > self._max_merged_bytes:
            return False
        self._pending[-1] = candidate
        return True

    async def _drain(self) -> None:
        while self._pending and self._error is None:
            event = self._pending.popleft()
            try:
                await self._publish(event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # handed to the executor by emit/flush
                self._error = exc
                self._pending.clear()
            finally:
                self._progress.set()

    def _raise_error(self) -> None:
        if self._error is not None:
            raise self._error
