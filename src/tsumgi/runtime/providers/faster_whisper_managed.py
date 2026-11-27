"""Single-use supervised Faster-Whisper process (mirrors parakeet_managed.py). No native imports in the Worker."""
from __future__ import annotations

import asyncio
from types import MappingProxyType
from uuid import uuid4

from ...execution import JobExecutionError, ResidencyReport
from ..contracts import RuntimeHealth, RuntimeHealthState
from .faster_whisper import FasterWhisperExecutor
from .faster_whisper_process import FasterWhisperProcess


class _ReadyBoundExecutor(FasterWhisperExecutor):
    """A cached executor must not outlive its runtime generation or input pins."""

    def __init__(self, child, runtime):
        super().__init__(child)
        self._runtime = runtime

    async def execute_audio(self, job, media):
        generation = self._runtime._generation
        if not (await self._runtime.health()).ready:
            raise JobExecutionError("runtime_unavailable", "Faster-Whisper runtime unavailable", retryable=False)
        result = await super().execute_audio(job, media)
        if generation != self._runtime._generation or not (await self._runtime.health()).ready:
            raise JobExecutionError("runtime_unavailable", "Faster-Whisper runtime unavailable", retryable=False)
        return result


class FasterWhisperManagedRuntime:
    provider_id = "faster-whisper-ja"
    BOOT_TIMEOUT = 180.0
    STOP_TIMEOUT = 6.0

    def __init__(self, *, config):
        self._config = config
        self._state = RuntimeHealthState.STOPPED
        self._consumed = False
        self._released = False
        self._load: asyncio.Task | None = None
        self._cleanup: asyncio.Task | None = None
        self._child: FasterWhisperProcess | None = None
        self._executor: FasterWhisperExecutor | None = None
        self._file_ids = None
        self._generation = 0
        self._stop_lock = asyncio.Lock()

    async def start(self) -> None:
        if self._consumed or self._released or self._state is not RuntimeHealthState.STOPPED:
            raise RuntimeError("Faster-Whisper runtime cannot be restarted")
        self._consumed = True
        self._state = RuntimeHealthState.STARTING
        self._generation += 1
        generation = self._generation
        try:
            async with asyncio.timeout(self.BOOT_TIMEOUT):
                ids = await asyncio.to_thread(self._config.identities)
                await asyncio.to_thread(self._config.preflight)
                if (ids != await asyncio.to_thread(self._config.identities)
                        or generation != self._generation or self._state is not RuntimeHealthState.STARTING):
                    raise ValueError()
                cfg = self._config
                child = self._child = FasterWhisperProcess(
                    cfg.interpreter_path, cfg.model_dir, cfg.gpu_uuid,
                    private_home=cfg.private_home, base_python_root=cfg.base_python_root,
                    interpreter_sha256=cfg.interpreter_sha256,
                    epoch=str(uuid4()),
                )
                self._load = asyncio.create_task(asyncio.to_thread(child.start))
                self._load.add_done_callback(FasterWhisperExecutor._observe)
                # Keep the load handle if startup is cancelled: cancelling a
                # to_thread await does not stop its process-spawn thread.
                await asyncio.shield(self._load)
                if (generation != self._generation or self._state is not RuntimeHealthState.STARTING
                        or self._released or not child.alive
                        or ids != await asyncio.to_thread(self._config.identities)):
                    raise ValueError()
                self._file_ids = ids
                self._executor = _ReadyBoundExecutor(child, self)
                self._executor.serving_features = MappingProxyType(
                    {"audio.transcribe": frozenset({"ja", "json"})})
                self._state = RuntimeHealthState.READY
        except BaseException as error:
            self._state = RuntimeHealthState.FAILED
            try:
                await self.stop()
            except Exception:
                pass  # Admission stays revoked; retained handles allow cleanup.
            if not isinstance(error, Exception):
                raise
            raise RuntimeError("Faster-Whisper runtime startup failed") from None

    async def health(self) -> RuntimeHealth:
        if self._state is RuntimeHealthState.READY:
            try:
                if (self._child is None or not self._child.alive
                        or self._file_ids != await asyncio.to_thread(self._config.identities)):
                    self._state = RuntimeHealthState.FAILED
            except (OSError, RuntimeError, ValueError):
                self._state = RuntimeHealthState.FAILED
        return RuntimeHealth(
            state=self._state, ready=self._state is RuntimeHealthState.READY,
            detail="Faster-Whisper runtime unavailable" if self._state is RuntimeHealthState.FAILED else None,
        )

    async def stop(self) -> None:
        self._generation += 1
        self._state = RuntimeHealthState.FAILED  # Revoke before any await.
        self._executor = None
        async with self._stop_lock:
            try:
                async with asyncio.timeout(self.STOP_TIMEOUT):
                    if self._child is not None:
                        # A timed-out caller leaves the in-flight close task in
                        # place. Only a completed failure permits a new close
                        # attempt; never discard uncertain cleanup.
                        retry_failed_close = (
                            self._cleanup is not None
                            and self._cleanup.done()
                            and not self._cleanup.cancelled()
                            and self._cleanup.exception() is not None
                        )
                        if self._cleanup is None or retry_failed_close:
                            self._cleanup = asyncio.create_task(
                                asyncio.to_thread(self._child.close))
                            self._cleanup.add_done_callback(FasterWhisperExecutor._observe)
                        await asyncio.shield(self._cleanup)
                    if self._load is not None:
                        await asyncio.gather(asyncio.shield(self._load), return_exceptions=True)
            except Exception:
                raise RuntimeError("Faster-Whisper shutdown is unconfirmed") from None
            self._child = None
            self._load = None
            self._cleanup = None
            self._state = RuntimeHealthState.STOPPED

    async def release(self) -> None:
        if not self._released:
            await self.stop()
            self._released = True

    def executor(self):
        if self._released or self._state is not RuntimeHealthState.READY or self._executor is None:
            raise RuntimeError("Faster-Whisper runtime is not ready")
        return self._executor

    async def residency(self) -> ResidencyReport:
        return ResidencyReport()
