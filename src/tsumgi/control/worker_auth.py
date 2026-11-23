"""Static per-worker bearer credentials for the Worker Control API."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Iterable, Mapping
from types import MappingProxyType

_SCHEMA_VERSION = "worker-credentials-v1"
_MAX_WORKERS = 1024
_MAX_FILE_SIZE = 1024 * 1024
_WORKER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z", re.ASCII)
_TOKEN = re.compile(r"[A-Za-z0-9._~+/-]+=*\Z", re.ASCII)
_ERROR = "invalid worker credentials configuration"


def _valid_token(value: object) -> bool:
    return (
        type(value) is str
        and 32 <= len(value) <= 512
        and _TOKEN.fullmatch(value) is not None
    )


def _pairs_no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(_ERROR)
        result[key] = value
    return result


class WorkerCredentials:
    """An immutable token-digest registry; the supplied mapping is copied."""

    __slots__ = ("_token_workers",)

    def __init__(self, mapping: Mapping[str, str]) -> None:
        try:
            if not isinstance(mapping, Mapping) or not 1 <= len(mapping) <= _MAX_WORKERS:
                raise ValueError
            digests: dict[bytes, str] = {}
            for worker_id, token in mapping.items():
                if (
                    type(worker_id) is not str
                    or _WORKER_ID.fullmatch(worker_id) is None
                    or not _valid_token(token)
                ):
                    raise ValueError
                digest = hashlib.sha256(token.encode("ascii")).digest()
                if digest in digests:
                    raise ValueError
                digests[digest] = worker_id
            object.__setattr__(self, "_token_workers", MappingProxyType(digests))
        except Exception:
            raise ValueError(_ERROR) from None

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("WorkerCredentials is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("WorkerCredentials is immutable")

    def __repr__(self) -> str:
        return f"WorkerCredentials(count={len(self._token_workers)})"

    def authenticate(self, supplied: str | None) -> str | None:
        if not _valid_token(supplied):
            return None
        try:
            return self._token_workers.get(hashlib.sha256(supplied.encode("ascii")).digest())
        except Exception:
            return None

    def reject_token_collisions(self, tokens: Iterable[str | None]) -> None:
        try:
            own = self._token_workers
            for token in tokens:
                if type(token) is str and token:
                    if hashlib.sha256(token.encode("utf-8")).digest() in own:
                        raise ValueError(_ERROR)
        except ValueError:
            raise ValueError(_ERROR) from None
        except Exception:
            raise ValueError(_ERROR) from None


def load_worker_credentials(path: str) -> WorkerCredentials:
    """Load a bounded, private, regular JSON credential file exactly once."""
    fd: int | None = None
    try:
        if type(path) is not str or not path or not os.path.isabs(path):
            raise ValueError
        if ".." in path.split(os.sep):
            raise ValueError
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | os.O_NOFOLLOW
        fd = os.open(path, flags)
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid not in (0, os.geteuid())
            or stat.S_IMODE(info.st_mode) not in (0o400, 0o600)
            or info.st_size > _MAX_FILE_SIZE
        ):
            raise ValueError
        chunks: list[bytes] = []
        remaining = _MAX_FILE_SIZE + 1
        while remaining:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > _MAX_FILE_SIZE:
            raise ValueError
        text = raw.decode("utf-8", errors="strict")
        document = json.loads(text, object_pairs_hook=_pairs_no_duplicates)
        if (
            type(document) is not dict
            or set(document) != {"schema_version", "workers"}
            or document["schema_version"] != _SCHEMA_VERSION
            or type(document["workers"]) is not dict
        ):
            raise ValueError
        return WorkerCredentials(document["workers"])
    except Exception:
        raise ValueError(_ERROR) from None
    finally:
        if fd is not None:
            os.close(fd)


__all__ = ["WorkerCredentials", "load_worker_credentials"]
