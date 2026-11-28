"""Free-disk floor for the audio spools (#318 G10).

Spool quotas bound what the spools may hold, but nothing else noticed a full
filesystem. A floor refuses *new* admission with a stable error before a
partial write can fail in an unrelated place. It never deletes anything and is
advisory between the check and the write (another process can still fill the
disk); the spool's own write errors remain the last line of defence.
"""

from __future__ import annotations

from collections.abc import Callable
import shutil

from ..control.repository import StorageUnavailable

_GIB = 1024 * 1024 * 1024


class SpoolDiskLow(StorageUnavailable):
    """Free space on the spool filesystem is below the configured floor."""


def default_min_free_bytes(max_artifact_bytes: int) -> int:
    """max(1 GiB, 2 x the largest artifact the spool accepts)."""
    return max(_GIB, 2 * max_artifact_bytes)


class DiskFloor:
    """``free_space(path) -> int`` is injectable so tests need no real disk."""

    def __init__(
        self, path: str, min_free_bytes: int,
        free_space: Callable[[str], int] | None = None,
    ) -> None:
        if type(path) is not str or not path:
            raise ValueError("invalid spool path")
        if type(min_free_bytes) is not int or min_free_bytes < 0:
            raise ValueError("invalid minimum free bytes")
        if free_space is not None and not callable(free_space):
            raise ValueError("free_space must be callable")
        self._path = path
        self.min_free_bytes = min_free_bytes
        self._free_space = free_space or (lambda p: shutil.disk_usage(p).free)
        self.refusals = 0

    def status(self) -> dict[str, int | bool]:
        """Health/metric view: never raises on a measurement failure."""
        try:
            free = int(self._free_space(self._path))
        except (OSError, ValueError, TypeError):
            return {"free_bytes": -1, "min_free_bytes": self.min_free_bytes,
                    "low": self.min_free_bytes > 0, "refusals": self.refusals}
        return {"free_bytes": free, "min_free_bytes": self.min_free_bytes,
                "low": free < self.min_free_bytes, "refusals": self.refusals}

    def check(self) -> None:
        """Raise SpoolDiskLow when below the floor; 0 disables the check.

        An unmeasurable filesystem is treated as low (fail closed).
        """
        if self.min_free_bytes == 0:
            return
        if self.status()["low"]:
            self.refusals += 1
            raise SpoolDiskLow("audio spool free disk space is below the floor")
