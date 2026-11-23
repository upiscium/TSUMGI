import asyncio
import threading

import pytest

from tsumgi.runtime.providers.parakeet_managed import ParakeetManagedRuntime


class Config:
    pass


class BlockingChild:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def close(self):
        self.entered.set()
        self.release.wait(3)


async def test_cancelled_stop_retains_same_cleanup_task_for_retry():
    child = BlockingChild()
    runtime = ParakeetManagedRuntime(config=Config())
    runtime._child = child
    stop = asyncio.create_task(runtime.stop())
    assert await asyncio.to_thread(child.entered.wait, 2)
    cleanup = runtime._cleanup
    stop.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stop
    assert runtime._cleanup is cleanup and not cleanup.done()

    child.release.set()
    await runtime.stop()
    assert cleanup.done() and runtime._cleanup is None


async def test_timed_out_stop_retains_cleanup_until_confirmed_retry(monkeypatch):
    child = BlockingChild()
    runtime = ParakeetManagedRuntime(config=Config())
    runtime._child = child
    monkeypatch.setattr(runtime, "STOP_TIMEOUT", 0.02)
    with pytest.raises(RuntimeError, match="shutdown is unconfirmed"):
        await runtime.stop()
    cleanup = runtime._cleanup
    assert cleanup is not None and not cleanup.done()

    child.release.set()
    await asyncio.wait_for(cleanup, 2)
    await runtime.stop()
    assert runtime._cleanup is None and runtime._child is None
