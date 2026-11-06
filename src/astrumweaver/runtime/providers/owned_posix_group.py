"""Bounded, process-identity-pinned signalling for a managed POSIX session.

This is not a cgroup containment boundary. Each target is opened as a stable
/proc/<pid> directory fd and signalled with pidfd_send_signal; numeric killpg
is deliberately *not* used. The owner must be captured while its newly
spawned session leader is still available. Linux Workers only.
"""
from __future__ import annotations

import os
from pathlib import Path
import signal
from dataclasses import dataclass

_MAX_PROC_BYTES = 8192
_MAX_MEMBERS = 128
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW


def _read_at(dirfd: int, filename: str) -> bytes:
    fd = os.open(filename, _FILE_FLAGS, dir_fd=dirfd)
    try:
        raw = os.read(fd, _MAX_PROC_BYTES + 1)
    finally:
        os.close(fd)
    if not raw or len(raw) > _MAX_PROC_BYTES:
        raise RuntimeError("owned process identity file is unavailable")
    return raw


def _stat_fields(raw: bytes) -> tuple[str, int, int, int]:
    # /proc/[pid]/stat's comm may contain spaces and ')' characters.
    # Find the last ')' before the remaining fixed-index numeric fields.
    end = raw.rfind(b") ")
    if end < 1:
        raise RuntimeError("invalid owned process stat record")
    try:
        cols = raw[end + 2 :].split()
        if len(cols) < 20:
            raise ValueError("short stat record")
        state = cols[0].decode("ascii", "strict")
        return state, int(cols[2]), int(cols[3]), int(cols[19])
    except (ValueError, IndexError, UnicodeDecodeError) as exc:
        raise RuntimeError("invalid owned process stat record") from exc


@dataclass
class OwnedPosixGroup:
    """Anchored original session, only used for this one controller instance."""

    pgid: int
    start_ticks: int
    cgroup: bytes
    leader_fd: int

    @classmethod
    def capture(cls, pid: int) -> "OwnedPosixGroup":
        if os.name != "posix" or not hasattr(signal, "pidfd_send_signal"):
            raise RuntimeError("stable Linux process identifiers unavailable")
        if type(pid) is not int or pid <= 1:
            raise RuntimeError("invalid group leader identifier")
        fd = os.open(f"/proc/{pid}", _DIR_FLAGS)
        try:
            state, group, session, ticks = _stat_fields(_read_at(fd, "stat"))
            if group != pid or session != pid or ticks <= 0 or state == "Z":
                raise RuntimeError("spawn did not create an owned process session")
            cgroup = _read_at(fd, "cgroup")
            return cls(pid, ticks, cgroup, fd)
        except BaseException:
            os.close(fd)
            raise

    def close(self) -> None:
        if self.leader_fd >= 0:
            os.close(self.leader_fd)
            self.leader_fd = -1

    def _members(self) -> list[int]:
        if self.leader_fd < 0:
            raise RuntimeError("owned group identity has been released")
        fds: list[int] = []
        try:
            for name in os.listdir("/proc"):
                if not name.isdigit():
                    continue
                try:
                    fd = os.open(f"/proc/{name}", _DIR_FLAGS)
                except (FileNotFoundError, ProcessLookupError):
                    continue
                except PermissionError as exc:
                    # We cannot prove an unreadable PID is outside the
                    # managed group. Fail closed rather than report quiet.
                    raise RuntimeError(
                        "unable to inspect all processes for owned group"
                    ) from exc
                try:
                    state, group, session, ticks = _stat_fields(
                        _read_at(fd, "stat")
                    )
                    # PIDFD targets remain stable even after PID recycling.
                    # Require the exact inherited session, generation and
                    # cgroup; refuse a newly reused numeric group leader.
                    if int(name) == self.pgid and ticks != self.start_ticks:
                        raise RuntimeError("owned group leader PID was reused")
                    if (
                        group != self.pgid
                        or session != self.pgid
                        or state in {"Z", "X", "x"}
                    ):
                        continue
                    if ticks < self.start_ticks:
                        raise RuntimeError("process predates owned session")
                    if _read_at(fd, "cgroup") != self.cgroup:
                        raise RuntimeError("owned group crossed cgroup boundary")
                    fds.append(fd)
                    fd = -1
                    if len(fds) > _MAX_MEMBERS:
                        raise RuntimeError("owned group has too many members")
                except (FileNotFoundError, ProcessLookupError):
                    continue
                finally:
                    if fd >= 0:
                        os.close(fd)
            return fds
        except BaseException:
            for fd in fds:
                os.close(fd)
            raise

    def has_live_members(self) -> bool:
        fds = self._members()
        try:
            return bool(fds)
        finally:
            for fd in fds:
                os.close(fd)

    def signal_members(self, sig: signal.Signals) -> None:
        fds = self._members()
        try:
            for fd in fds:
                try:
                    # A stable pidfd reference, NOT kill(<numeric pid>) or
                    # killpg(<numeric PGID>). ESRCH means this process exited.
                    signal.pidfd_send_signal(fd, sig, None, 0)
                except ProcessLookupError:
                    continue
        finally:
            for fd in fds:
                os.close(fd)
