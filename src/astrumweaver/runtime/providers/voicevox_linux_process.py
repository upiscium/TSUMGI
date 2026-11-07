"""#215 B6c: Linux-owned VOICEVOX child process, source only.

Uses Linux /proc for exact PID start-time, UID, executable inode and socket-FD
correlation; no cgroup/pidfd/process sandbox, model-load attestation, logs
certificate, external process kill, RuntimeProvider registration or deployment.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import os
import re
import signal
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from .voicevox_managed import VoicevoxOwnedRuntimeSpec, VoicevoxProcessObservation

_PIN = re.compile(r"sha256:[0-9a-f]{64}\Z")
_MAX_FILE = 16 * 1024 * 1024 * 1024


class VoicevoxOwnedProcessError(RuntimeError):
    """Fixed redacted errors. Never include paths, PID, argv or native output."""

    def __init__(self) -> None:
        super().__init__("VOICEVOX owned process unavailable")


def _fail() -> None:
    raise VoicevoxOwnedProcessError()


def _strict_path(value: object) -> bool:
    if (
        type(value) is not str
        or not os.path.isabs(value)
        or value == "/"
        or os.path.normpath(value) != value
        or "\x00" in value
    ):
        return False
    try:
        return len(value.encode("utf-8")) <= 4096
    except UnicodeError:
        return False


def _fixed_argv_is_reviewable(
    fixed_args: tuple[str, ...],
    auxiliary_path: str | None,
) -> bool:
    """No silent shell/module/inline-script or unpinned path-based code load.

    The controller cannot attest dynamically loaded code via Python -m/-c,
    and cannot hash an arbitrary path in fixed_args unless the operator
    provided the one explicit pinned auxiliary file.
    """
    for arg in fixed_args:
        if arg in ("-c", "-m", "-e", "--module", "--eval", "--command"):
            return False
        if (
            "/" in arg or "\\" in arg or arg.lower().endswith((".py", ".pyc", ".sh"))
        ) and (auxiliary_path is None or arg != auxiliary_path):
            return False
    return True


@dataclass(frozen=True, slots=True, repr=False)
class VoicevoxLinuxLaunchSpec:
    """Operator-pinned fixed argv, never modified by the current Job payload."""

    executable_path: str
    fixed_args: tuple[str, ...]
    auxiliary_path: str | None = None
    auxiliary_sha256: str | None = None
    stop_timeout_seconds: float = 2.0

    def __post_init__(self) -> None:
        if (
            not _strict_path(self.executable_path)
            or type(self.fixed_args) is not tuple
            or len(self.fixed_args) > 24
            or any(
                type(arg) is not str
                or not arg
                or len(arg.encode("utf-8")) > 4096
                or any(ord(ch) < 32 or ord(ch) == 127 for ch in arg)
                for arg in self.fixed_args
            )
            or (self.auxiliary_path is None) != (self.auxiliary_sha256 is None)
            or not _fixed_argv_is_reviewable(self.fixed_args, self.auxiliary_path)
            or (
                self.auxiliary_path is not None
                and (
                    not _strict_path(self.auxiliary_path)
                    or type(self.auxiliary_sha256) is not str
                    or not _PIN.fullmatch(self.auxiliary_sha256)
                    or self.auxiliary_path not in self.fixed_args
                )
            )
            or type(self.stop_timeout_seconds) not in (int, float)
            or not math.isfinite(self.stop_timeout_seconds)
            or not 0 < self.stop_timeout_seconds <= 60
        ):
            raise ValueError("VOICEVOX owned process launch configuration is invalid")


def _verified_file(path: str, expected: str) -> tuple[int, int, int, int]:
    """Point-in-time digest plus inode identity; each component rejects symlinks."""
    if (
        not _strict_path(path)
        or type(expected) is not str
        or not _PIN.fullmatch(expected)
    ):
        _fail()
    current = "/"
    try:
        for part in Path(path).parts[1:]:
            current = os.path.join(current, part)
            if stat.S_ISLNK(os.lstat(current).st_mode):
                _fail()
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= _MAX_FILE:
                _fail()
            digest = hashlib.sha256()
            total = 0
            while chunk := os.read(fd, 1024 * 1024):
                total += len(chunk)
                if total > _MAX_FILE:
                    _fail()
                digest.update(chunk)
            after = os.fstat(fd)
            identity = (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
            )
            if (
                identity
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                or total != before.st_size
                or "sha256:" + digest.hexdigest() != expected
            ):
                _fail()
            return identity
        finally:
            os.close(fd)
    except (OSError, UnicodeError, ValueError, TypeError):
        _fail()


def _proc_uid(pid: int) -> int:
    raw = Path(f"/proc/{pid}/status").read_text("ascii")
    for line in raw.splitlines():
        if line.startswith("Uid:"):
            columns = line.split()
            if len(columns) != 5:
                _fail()
            ids = [int(x) for x in columns[1:]]
            if any(x != ids[0] for x in ids):
                _fail()
            return ids[0]
    _fail()


def _proc_generation(pid: int) -> tuple[int, str]:
    # Linux /proc/<pid>/stat's field 2 (comm) may contain spaces or ')'.
    raw = Path(f"/proc/{pid}/stat").read_text("ascii")
    right = raw.rfind(")")
    if right < 0:
        _fail()
    fields = raw[right + 2 :].split()
    if len(fields) <= 19:
        _fail()
    return int(fields[19]), fields[0]  # field 22 starttime, field 3 state


def _proc_fixed_command(pid: int, argv: tuple[str, ...]) -> bool:
    """Check full child argv, because exec of same pinned binary retains PID.

    /proc/cmdline is process-writable (not an authenticated attestation).
    Detects unreviewed re-exec/argv drift; refuses READY rather than adopting.
    """
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as source:
            raw = source.read(65537)
        if not raw or len(raw) > 65536 or raw[-1:] != b"\0":
            return False
        tokens = raw[:-1].split(b"\0")
        return tuple(v.decode("utf-8") for v in tokens) == argv
    except (OSError, ValueError, UnicodeError):
        return False


def _listener_sockets(port: int) -> tuple[tuple[str, str, int], ...]:
    """Return ALL IPv4/IPv6 listeners on the target port, not just loopback.

    A process may bind separate 127.0.0.1 and non-loopback IPv4 sockets on
    the same port. Ignoring the latter would silently qualify an engine also
    exposed on the network. Reject all ambiguity conservatively.
    """
    result: list[tuple[str, str, int]] = []
    try:
        for kind in ("tcp", "tcp6"):
            lines = Path(f"/proc/net/{kind}").read_text("ascii").splitlines()[1:]
            for line in lines:
                columns = line.split()
                if len(columns) < 10 or columns[3] != "0A":
                    continue
                address, hexport = columns[1].split(":")
                if int(hexport, 16) != port:
                    continue
                # Preserve every listening interface on this port. The
                # caller allows exactly one 127.0.0.1 IPv4 listener; all
                # other addresses and multiple listeners deny readiness.
                result.append((kind, address, int(columns[9])))
    except (OSError, ValueError, IndexError):
        _fail()
    return tuple(result)


def _process_socket_inodes(pid: int) -> frozenset[int]:
    inodes = set()
    try:
        for entry in Path(f"/proc/{pid}/fd").iterdir():
            link = os.readlink(entry)
            if link.startswith("socket:[") and link.endswith("]"):
                inodes.add(int(link[8:-1]))
    except (OSError, ValueError):
        _fail()
    return frozenset(inodes)


def _owned_group_generation(pid: int, uid: int, generation: int) -> bool:
    """Signal authority for an already created direct child, not READY proof.

    A child can exec into a different image without changing PID, birth tick,
    UID or process group. Reject that image for readiness, but still reap the
    exact child generation whose handle was returned by our own spawn.
    """
    try:
        start, state = _proc_generation(pid)
        return (
            start == generation
            and state not in ("Z", "X")
            and _proc_uid(pid) == uid
            and os.getpgid(pid) == pid
        )
    except (OSError, ValueError, VoicevoxOwnedProcessError):
        return False


def _proc_identity(
    pid: int, identity: tuple[int, int, int, int], uid: int, generation: int
) -> bool:
    try:
        info = os.stat(f"/proc/{pid}/exe")
        observed = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        return observed == identity and _owned_group_generation(pid, uid, generation)
    except (OSError, ValueError, VoicevoxOwnedProcessError):
        return False


class LinuxVoicevoxOwnedProcessControl:
    """Exactly one observed child, not arbitrary existing PID adoption.

    All Linux /proc readers and hash preflight run off the Worker event loop.
    The controller only starts the immutable fixed argv. Its endpoint_owner
    returns 0 for an occupied foreign/ambiguous endpoint (never a usable PID).
    """

    def __init__(
        self,
        *,
        launch_spec: VoicevoxLinuxLaunchSpec,
        config: VoicevoxOwnedRuntimeSpec,
    ) -> None:
        if (
            sys.platform != "linux"
            or type(launch_spec) is not VoicevoxLinuxLaunchSpec
            or type(config) is not VoicevoxOwnedRuntimeSpec
            or launch_spec.executable_path != config.executable_path
            or launch_spec.stop_timeout_seconds > config.stop_timeout_seconds
        ):
            raise ValueError("Linux VOICEVOX process controller unavailable")
        self._launch_spec = launch_spec
        self._config = config
        self._argv = (launch_spec.executable_path, *launch_spec.fixed_args)
        self._proc: asyncio.subprocess.Process | None = None
        self._nonce: str | None = None
        self._identity: tuple[int, int, int, int] | None = None
        self._model_identity: tuple[int, int, int, int] | None = None
        self._aux_identity: tuple[int, int, int, int] | None = None
        self._start_ticks: int | None = None
        self._lock = asyncio.Lock()
        # Once a spawn is cancelled, never admit another child from this
        # controller. A late subprocess result is reaped by the same loop.
        self._aborted = False
        self._aborted_nonce: str | None = None
        self._pending_spawn_cleanup: asyncio.Task[None] | None = None
        self._pending_verification: asyncio.Task | None = None
        self._pending_reap: asyncio.Task[int] | None = None
        self._verified_nonce: str | None = None
        self._stopping = False

    @staticmethod
    def _port(cfg: VoicevoxOwnedRuntimeSpec) -> int:
        # VoicevoxOwnedRuntimeSpec has already verified exact loopback URL.
        return int(cfg.endpoint.rsplit(":", 1)[1])

    def _read_owner(self, port: int) -> int | None:
        listeners = _listener_sockets(port)
        if not listeners:
            return None
        if (
            len(listeners) != 1
            or listeners[0][0] != "tcp"
            or listeners[0][1] != "0100007F"
        ):
            # A wildcard listener (0.0.0.0) is publicly exposed even if it
            # belongs to our exact child. Refuse it; do not relabel as local.
            return 0
        proc = self._proc
        if (
            proc is None
            or self._config is None
            or proc.returncode is not None
            or self._identity is None
            or self._start_ticks is None
            or not _proc_identity(
                proc.pid,
                self._identity,
                self._config.owner_uid,
                self._start_ticks,
            )
        ):
            return 0
        if not _proc_fixed_command(proc.pid, self._argv):
            return 0
        inode = listeners[0][2]
        if inode <= 0 or inode not in _process_socket_inodes(proc.pid):
            return 0
        return proc.pid

    async def endpoint_owner(self) -> int | None:
        if self._aborted or self._stopping:
            _fail()
        # Config is frozen at construction: ManagedRuntime preflight calls
        # endpoint_owner BEFORE launch. Never admit a second/foreign listener.
        cfg = self._config
        try:
            return await asyncio.to_thread(self._read_owner, self._port(cfg))
        except Exception:  # noqa: BLE001 - no path/hostname/error leak
            raise VoicevoxOwnedProcessError() from None

    async def inspect(self) -> VoicevoxProcessObservation | None:
        if self._aborted or self._stopping:
            _fail()
        cfg = self._config
        proc = self._proc
        if cfg is None or proc is None:
            return None
        try:
            ok = await asyncio.to_thread(self._inspect_identity, cfg, proc.pid)
            if not ok:
                _fail()
            owner = await self.endpoint_owner()
            if owner is None:
                return None
            if owner != proc.pid:
                _fail()
            return VoicevoxProcessObservation(
                pid=proc.pid,
                uid=cfg.owner_uid,
                endpoint_owner_pid=proc.pid,
                executable_sha256=cfg.adapter.engine_artifact_sha256,
                model_artifact_sha256=cfg.adapter.model_artifact_sha256,
                endpoint=cfg.endpoint,
                worker_resource_id=cfg.worker_resource_id,
                deployment_revision=cfg.serving.deployment.revision,
                launch_nonce=self._nonce,
            )
        except Exception:  # noqa: BLE001 - fixed error only
            raise VoicevoxOwnedProcessError() from None

    def _inspect_identity(self, cfg: VoicevoxOwnedRuntimeSpec, pid: int) -> bool:
        if (
            self._identity is None
            or self._model_identity is None
            or self._start_ticks is None
            or not _proc_identity(pid, self._identity, cfg.owner_uid, self._start_ticks)
            or not _proc_fixed_command(pid, self._argv)
        ):
            return False
        model = cfg.model_artifact_path
        try:
            meta = os.stat(model, follow_symlinks=False)
            observed = (meta.st_dev, meta.st_ino, meta.st_size, meta.st_mtime_ns)
            if observed != self._model_identity:
                return False
            if self._aux_identity is not None:
                aux = self._launch_spec.auxiliary_path
                if aux is None:
                    return False
                stat_aux = os.stat(aux, follow_symlinks=False)
                if (
                    stat_aux.st_dev,
                    stat_aux.st_ino,
                    stat_aux.st_size,
                    stat_aux.st_mtime_ns,
                ) != self._aux_identity:
                    return False
            return True
        except OSError:
            return False

    async def launch(self, *, config: VoicevoxOwnedRuntimeSpec, nonce: str) -> None:
        if (
            type(config) is not VoicevoxOwnedRuntimeSpec
            or type(nonce) is not str
            or len(nonce) != 36
            or not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", nonce)
        ):
            _fail()
        async with self._lock:
            if self._aborted or self._proc is not None or self._nonce is not None:
                _fail()
            self._verified_nonce = None
            if (
                config != self._config
                or config.executable_path != self._launch_spec.executable_path
                or config.owner_uid != os.geteuid()
                or os.getegid() != os.getgid()
            ):
                _fail()
            port = self._port(config)
            try:
                artifacts = await asyncio.to_thread(
                    self._check_before_launch, config, port
                )
                exe_identity, model_identity, aux_identity = artifacts
                spawn = asyncio.create_task(
                    asyncio.create_subprocess_exec(
                        config.executable_path,
                        *self._launch_spec.fixed_args,
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                        env={"PATH": "/usr/bin:/bin", "PYTHONNOUSERSITE": "1"},
                        cwd="/",
                        close_fds=True,
                        start_new_session=True,
                    )
                )
                try:
                    proc = await asyncio.shield(spawn)
                except asyncio.CancelledError:
                    # Shield preserves a potentially already-started child.
                    # Never wait without a deadline for an unresponsive
                    # subprocess creator while holding launch's lock. The
                    # known spawn task is handed to a terminal late reaper.
                    self._aborted = True
                    self._aborted_nonce = nonce
                    self._pending_spawn_cleanup = asyncio.create_task(
                        self._reap_delayed_spawn(spawn, port)
                    )
                    raise
                self._proc = proc
                self._nonce = nonce
                self._identity = exe_identity
                self._model_identity = model_identity
                self._aux_identity = aux_identity
                self._start_ticks, _state = await asyncio.to_thread(
                    _proc_generation, proc.pid
                )
                if not await asyncio.to_thread(
                    _proc_identity,
                    proc.pid,
                    exe_identity,
                    config.owner_uid,
                    self._start_ticks,
                ):
                    _fail()
            except BaseException as error:
                if self._proc is not None:
                    await self._reap_newborn()
                if not isinstance(error, Exception):
                    raise
                raise VoicevoxOwnedProcessError() from None

    async def _reap_delayed_spawn(
        self,
        spawn: asyncio.Task[asyncio.subprocess.Process],
        port: int,
    ) -> None:
        """Terminal late-spawn cleanup after the waiting launcher was cancelled.

        This does not release admission again. If subprocess creation stalls
        forever, the controller remains FAILED/aborted; stop_owned returns an
        explicit cleanup-uncertain error on its deadline, not READY.
        """
        try:
            try:
                proc = await asyncio.shield(spawn)
            except Exception:  # noqa: BLE001 - spawn effects are not provable
                # A failed await is not proof that subprocess creation had
                # zero side effects. With no owned handle, cleanup is unknown:
                # never confirm stop or permit another launch.
                _fail()
            self._proc = proc
            self._nonce = self._aborted_nonce
            await self._reap_newborn()
        except BaseException as error:
            if not isinstance(error, Exception):
                raise
            raise VoicevoxOwnedProcessError() from None

    def _check_before_launch(self, cfg: VoicevoxOwnedRuntimeSpec, port: int):
        if _listener_sockets(port):
            _fail()  # Never adopt/kill a foreign listener.
        exe = _verified_file(cfg.executable_path, cfg.adapter.engine_artifact_sha256)
        model = _verified_file(
            cfg.model_artifact_path, cfg.adapter.model_artifact_sha256
        )
        aux = None
        if self._launch_spec.auxiliary_path is not None:
            aux = _verified_file(
                self._launch_spec.auxiliary_path,
                self._launch_spec.auxiliary_sha256,
            )
        return exe, model, aux

    async def _reap_newborn(self) -> None:
        """Spawn-failure cleanup before a stable generation can be observed.

        We own the newly created asyncio subprocess handle; never target a
        foreign PID or a separately discovered listener during this path.
        """
        proc = self._proc
        if proc is None:
            return
        try:
            async with asyncio.timeout(self._launch_spec.stop_timeout_seconds):
                if proc.returncode is None:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                await self._wait_reaped(proc)
                if await self._verify(_listener_sockets, self._port(self._config)):
                    _fail()
        except BaseException as error:
            self._aborted = True
            if not isinstance(error, Exception):
                raise
            raise VoicevoxOwnedProcessError() from None
        self._clear_owned()

    def _clear_owned(self) -> None:
        self._verified_nonce = self._nonce
        self._proc = None
        self._nonce = None
        self._identity = None
        self._model_identity = None
        self._aux_identity = None
        self._start_ticks = None
        self._pending_reap = None

    async def _finish_verification(self) -> None:
        pending = self._pending_verification
        if pending is not None:
            try:
                await asyncio.shield(pending)
            finally:
                if pending.done():
                    self._pending_verification = None

    async def _verify(self, function, *args):
        # Cancelling to_thread does NOT stop its OS thread. Retain the task,
        # bound every await by the caller's cleanup deadline and reconcile it
        # before creating another inspection. An old port result is never reused
        # as fresh proof; reconciliation always performs a new post-reap scan.
        await self._finish_verification()
        pending = asyncio.create_task(asyncio.to_thread(function, *args))
        self._pending_verification = pending
        pending.add_done_callback(self._observe_verification)
        try:
            return await asyncio.shield(pending)
        finally:
            if pending.done():
                self._pending_verification = None

    @staticmethod
    def _observe_verification(task: asyncio.Task) -> None:
        # Retain the result for reconciliation without an unhandled-task log
        # containing arbitrary inspection exceptions if no retry ever occurs.
        if not task.cancelled():
            task.exception()

    async def _wait_reaped(self, proc: asyncio.subprocess.Process) -> None:
        if self._pending_reap is None:
            self._pending_reap = asyncio.create_task(proc.wait())
        await asyncio.shield(self._pending_reap)

    async def stop_owned(self, *, nonce: str) -> None:
        if (
            type(nonce) is not str
            or len(nonce) != 36
            or not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", nonce)
        ):
            _fail()
        if nonce not in (self._nonce, self._verified_nonce, self._aborted_nonce):
            _fail()  # A foreign request has no authority over this instance.
        deadline = (
            asyncio.get_running_loop().time() + self._launch_spec.stop_timeout_seconds
        )
        self._stopping = True
        try:
            # The ONLY whole process-cleanup deadline: lock, prior inspection,
            # TERM grace, conditional KILL, reap and fresh listener proof.
            async with asyncio.timeout_at(deadline):
                async with self._lock:
                    if self._pending_spawn_cleanup is not None:
                        if nonce != self._aborted_nonce:
                            _fail()
                        pending = self._pending_spawn_cleanup
                        if not pending.done():
                            # Wait for the task's completion, not its result.
                            # Failure may leave a returned owned handle that
                            # must be reconciled with a fresh port scan.
                            # asyncio.wait does not cancel this owned task if
                            # our bounded stop attempt is interrupted.
                            await asyncio.wait((pending,))
                        if not pending.done():
                            _fail()
                        if pending.cancelled() or pending.exception() is not None:
                            if self._proc is None or self._nonce != nonce:
                                _fail()  # Unknown spawn effects: fail closed.
                            # Retry with the *same* owned handle and a fresh
                            # bounded post-reap listener proof, not the stale
                            # exception from the completed cleanup task.
                            await self._reap_newborn()
                        self._pending_spawn_cleanup = None
                        return
                    if self._proc is None:
                        if nonce == self._verified_nonce:
                            return
                        _fail()
                    if nonce != self._nonce:
                        _fail()
                    await self._finish_verification()
                    await self._stop_once(deadline)
        except BaseException as error:
            self._aborted = True
            if not isinstance(error, Exception):
                raise
            raise VoicevoxOwnedProcessError() from None
        finally:
            self._stopping = False

    async def _signal_owned(self, proc: asyncio.subprocess.Process, sig: int) -> None:
        if proc.returncode is not None:
            return
        safe = (
            self._identity is not None
            and self._start_ticks is not None
            and await self._verify(
                _owned_group_generation,
                proc.pid,
                self._config.owner_uid,
                self._start_ticks,
            )
        )
        # The direct child can exit while /proc is being inspected. A false
        # generation check then needs reap + port proof, not a foreign signal.
        if proc.returncode is not None:
            return
        if not safe:
            _fail()
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            pass

    async def _stop_once(self, deadline: float) -> None:
        proc = self._proc
        if proc is None:
            return
        if proc.returncode is None:
            # Image/argv drift denies READY but does not remove authority to
            # reap the exact owned PID/UID/birth-generation/process group.
            await self._signal_owned(proc, signal.SIGTERM)
            if self._pending_reap is None:
                self._pending_reap = asyncio.create_task(proc.wait())
            remaining = max(0.0, deadline - asyncio.get_running_loop().time())
            _, pending = await asyncio.wait(
                (self._pending_reap,), timeout=remaining / 2
            )
            if pending:
                await self._signal_owned(proc, signal.SIGKILL)
        await self._wait_reaped(proc)
        # A direct child returncode is not cleanup proof. Escaped descendants
        # or a foreign rebind must keep ownership/nonce and fail closed.
        if await self._verify(_listener_sockets, self._port(self._config)):
            _fail()
        if asyncio.get_running_loop().time() >= deadline:
            _fail()
        self._clear_owned()
