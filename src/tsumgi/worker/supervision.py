"""Fail-closed, single-probe health authority for one owned RuntimeProvider."""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time

from ..runtime.contracts import ManagedRuntime, RuntimeHealth, RuntimeHealthState
from ..runtime.lifecycle import RuntimeLifecycleManager

_LOG = logging.getLogger(__name__)

# A provider's fixed failure codes carry the provider's own name as a prefix, for example
# `voicevox_probe_failed`. Only such a code is logged, so that a latched failure can be
# told apart from another one; any other detail (free text can embed paths or
# credentials) is never retained or logged.
_CODE_TAIL = re.compile(r"[a-z0-9][a-z0-9_]{0,47}\Z")


class RuntimeUnavailable(RuntimeError):
    """Local attempt must be abandoned; durable Control fencing still applies."""


class RuntimeHealthSupervisor:
    """A failure is latched for this Worker invocation; only restart recovers it.

    The fixed daemon policy is one probe per second with a five-second total
    async budget. Constructor overrides are for embedding/tests, not hidden
    environment overrides. No raw provider detail/metadata is retained here; a
    provider's own fixed failure code is logged when it has the provider's prefix.
    """

    def __init__(
        self,
        runtime: ManagedRuntime,
        *,
        interval_seconds: float = 1.0,
        timeout_seconds: float = 5.0,
        lifecycle: RuntimeLifecycleManager | None = None,
        shutdown_timeout_seconds: float = 60.0,
    ) -> None:
        if any(
            isinstance(value, bool) or not math.isfinite(value) or value <= 0
            for value in (interval_seconds, timeout_seconds, shutdown_timeout_seconds)
        ):
            raise ValueError("runtime health timings must be finite and positive")
        if lifecycle is not None and lifecycle.runtime is not runtime:
            raise ValueError("runtime supervisor lifecycle must own the same runtime")
        self.runtime = runtime
        self.lifecycle = lifecycle or RuntimeLifecycleManager(runtime)
        self.shutdown_timeout_seconds = shutdown_timeout_seconds
        self.interval_seconds = interval_seconds
        self.timeout_seconds = timeout_seconds
        self.initialized = asyncio.Event()
        self.failed = asyncio.Event()
        self._valid_until = 0.0
        self._reason: str | None = None
        self._probe_lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    @property
    def available(self) -> bool:
        # Expiry closes admission even if the supervising coroutine is delayed.
        if self._valid_until and time.monotonic() >= self._valid_until:
            self._fail("health_stale")
        return not self._closed and not self.failed.is_set() and bool(self._valid_until)

    @property
    def state(self) -> str:
        if self._closed:
            return "stopped"
        if self.available:
            return "ready"
        return "failed" if self.failed.is_set() else "starting"

    @property
    def failure_reason(self) -> str | None:
        return self._reason

    def _provider_code(self, detail: object) -> str | None:
        provider = getattr(self.runtime, "provider_id", None)
        if not isinstance(provider, str) or not isinstance(detail, str):
            return None
        prefix = provider.replace("-", "_") + "_"
        if detail.startswith(prefix) and _CODE_TAIL.fullmatch(detail[len(prefix):]):
            return detail
        return None

    def _fail(self, reason: str, provider_code: str | None = None) -> None:
        if self.failed.is_set() or self._closed:
            return
        self._valid_until = 0.0
        self._reason = reason
        self.failed.set()
        if provider_code is None:
            _LOG.error("Runtime health lost (%s); restart required, claims paused", reason)
        else:
            _LOG.error(
                "Runtime health lost (%s, provider reported %s); restart required, claims paused",
                reason, provider_code,
            )

    async def check(self) -> bool:
        """Also used after executor errors; probes never overlap.

        The budget includes waiting for an already in-flight probe. A late
        success cannot clear a failure/expired health lease or a closed monitor.
        """
        try:
            async with asyncio.timeout(self.timeout_seconds):
                async with self._probe_lock:
                    if self._closed or self.failed.is_set():
                        return False
                    if self._valid_until and not self.available:
                        return False
                    started = time.monotonic()
                    health = await self.runtime.health()
                    if self._closed or self.failed.is_set():
                        return False
                    if self._valid_until and not self.available:
                        return False
                    if time.monotonic() - started >= self.timeout_seconds:
                        self._fail("health_timeout")
                    elif not isinstance(health, RuntimeHealth):
                        self._fail("health_invalid")
                    elif health.state is not RuntimeHealthState.READY or health.ready is not True:
                        self._fail("health_not_ready", self._provider_code(health.detail))
                    else:
                        self._valid_until = started + self.interval_seconds + self.timeout_seconds
        except TimeoutError:
            self._fail("health_timeout")
        except Exception:
            # Third-party health errors may embed credentials or private paths.
            self._fail("health_exception")
        return self.available

    async def start(self) -> None:
        if self._closed or self._task is not None:
            raise RuntimeError("runtime supervisor is single-use")
        self._task = asyncio.create_task(self._run(), name="tsumgi-runtime-supervisor")
        await self.initialized.wait()

    async def _run(self) -> None:
        try:
            while not self.failed.is_set():
                await self.check()
                self.initialized.set()
                if not self.failed.is_set():
                    await asyncio.sleep(self.interval_seconds)
        except asyncio.CancelledError:
            raise
        finally:
            # Unexpected cancellation/exit must never leave fresh readiness.
            if not self._closed:
                self._fail("monitor_stopped")
            self.initialized.set()

    async def close(self) -> None:
        self._closed = True
        self._valid_until = 0.0
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def shutdown_owned(self) -> None:
        """Caller joins execution before this, and releases Control afterwards."""
        await self.lifecycle.shutdown_owned(timeout_seconds=self.shutdown_timeout_seconds)
