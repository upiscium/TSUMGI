"""Small process-local, private temporary-file spool (TSUMGI-AUDIO-FC-001/r0).

Scopes are trusted caller-supplied authorization labels, not credentials or paths.
Metadata and authorization live only in this instance; this is not durable storage.
The caller owns the lifetime and eventual cleanup of the spool root.
"""

from contextlib import contextmanager
import errno
import hashlib
import math
import os
import re
import secrets
import stat
import time


class ArtifactSpool:
    """Store bounded byte artifacts in a private temporary directory.

    ``max_bytes`` bounds each artifact. Optional aggregate quotas account for
    successful creates until verified deletion, including expired artifacts.
    Callers must serialize access and explicitly delete or reap artifacts.
    Expired artifacts remain deletable by their authorized scope. Same-effective-
    UID actors are trusted; this is not protection against their concurrent edits.
    """

    def __init__(self, root, *, max_bytes, ttl_seconds, clock=time.monotonic,
                 max_total_bytes=None, max_artifacts=None):
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        for name, value in (("max_total_bytes", max_total_bytes),
                            ("max_artifacts", max_artifacts)):
            if value is not None and (type(value) is not int or value <= 0):
                raise ValueError(f"{name} must be a positive built-in integer or None")
        if (isinstance(ttl_seconds, bool)
                or not isinstance(ttl_seconds, (int, float))
                or ttl_seconds <= 0):
            raise ValueError("ttl_seconds must be positive and finite")
        try:
            finite = math.isfinite(ttl_seconds)
        except OverflowError:
            finite = False
        if not finite or not callable(clock):
            raise ValueError("invalid expiry configuration")
        try:
            root = os.fspath(root)
            if not isinstance(root, str) or not root or "\0" in root:
                raise ValueError("invalid spool root")
            if ".." in root.split(os.sep):
                raise ValueError("root traversal is forbidden")
            self._root = os.path.abspath(root)
        except TypeError as exc:
            raise ValueError("invalid spool root") from exc
        self._max_bytes = max_bytes
        self._max_total_bytes = max_total_bytes
        self._max_artifacts = max_artifacts
        self._total_bytes = 0
        self._ttl = ttl_seconds
        self._clock = clock
        self._records = {}
        self._now()
        try:
            root_fd = self._open_root()
            created_name = None
            try:
                self._root_identity = self._identity(os.fstat(root_fd))
                # Random temporary names, with a bounded exclusive-creation retry.
                for _ in range(16):
                    name = "artifact-spool-" + secrets.token_hex(16)
                    try:
                        os.mkdir(name, 0o700, dir_fd=root_fd)
                        created_name = name
                        break
                    except FileExistsError:
                        continue
                else:
                    raise ValueError("cannot allocate private spool")
                self._name = name
                directory_fd = os.open(name, self._directory_flags(), dir_fd=root_fd)
                try:
                    os.fchmod(directory_fd, 0o700)
                    self._directory_identity = self._identity(os.fstat(directory_fd))
                finally:
                    os.close(directory_fd)
            except BaseException as exc:
                if created_name is not None:
                    # Only reclaim this constructor's own still-empty directory.
                    # rmdir never follows a replaced symlink or deletes contents.
                    try:
                        os.rmdir(created_name, dir_fd=root_fd)
                    except OSError as cleanup_error:
                        exc.add_note(f"private spool initialization cleanup failed: {cleanup_error!s}")
                raise
            finally:
                os.close(root_fd)
        except OSError as exc:
            # Unsafe path topology is a configuration rejection. Disk full,
            # physical I/O and other operational storage failures remain
            # OSError so the future Gateway does not classify them as 4xx.
            if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.ENOENT):
                raise ValueError("unsafe or missing spool root") from exc
            raise

    @staticmethod
    def _directory_flags():
        return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC

    @staticmethod
    def _identity(info):
        return info.st_dev, info.st_ino

    @staticmethod
    def _check_trusted_ancestor(info, path):
        """Refuse untrusted or group/world-writable path components.

        Only root-owned sticky *system temporary directories* may be
        writable ancestors, never arbitrary writable or user-owned paths.
        Every descendant is checked independently.
        """
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.geteuid()):
            raise ValueError("untrusted spool ancestor")
        if stat.S_IMODE(info.st_mode) & 0o022:
            if not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX
                    and path in ("/tmp", "/var/tmp", "/dev/shm")):
                raise ValueError("untrusted writable spool ancestor")

    def _open_root(self):
        # Walk components through directory descriptors and validate every
        # opened inode, not just its textual path, to reject symlink/rename
        # redirection and untrusted writable ancestry.
        fd = os.open(os.sep, self._directory_flags())
        try:
            current = os.sep
            self._check_trusted_ancestor(os.fstat(fd), current)
            for component in self._root.split(os.sep):
                if component:
                    child = os.open(component, self._directory_flags(), dir_fd=fd)
                    try:
                        current = os.path.join(current, component)
                        self._check_trusted_ancestor(os.fstat(child), current)
                    except BaseException:
                        os.close(child)
                        raise
                    os.close(fd)
                    fd = child
            final = os.fstat(fd)
            if (final.st_uid != os.geteuid()
                    or stat.S_IMODE(final.st_mode) != 0o700):
                raise ValueError("private spool root must be owned by Control with mode 0700")
            return fd
        except BaseException:
            os.close(fd)
            raise

    @contextmanager
    def _directory(self):
        # Do not catch storage failures raised by the body after yield:
        # a write failure is not a client-supplied validation error.
        fd = None
        try:
            root_fd = self._open_root()
            try:
                if self._identity(os.fstat(root_fd)) != self._root_identity:
                    raise ValueError("spool root changed")
                fd = os.open(self._name, self._directory_flags(), dir_fd=root_fd)
            finally:
                os.close(root_fd)
            info = os.fstat(fd)
            if (self._identity(info) != self._directory_identity
                    or stat.S_IMODE(info.st_mode) != 0o700
                    or info.st_uid != os.geteuid()):
                raise ValueError("private spool changed")
        except BaseException:
            if fd is not None:
                os.close(fd)
            raise
        try:
            yield fd
        finally:
            os.close(fd)

    @staticmethod
    def _scope(scope):
        if not isinstance(scope, str) or not scope:
            raise ValueError("an explicit nonempty scope is required")

    def _now(self):
        try:
            value = self._clock()
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("invalid clock value")
            if not math.isfinite(value):
                raise ValueError("invalid clock value")
            return value
        except (TypeError, OverflowError) as exc:
            raise ValueError("invalid clock value") from exc

    def _record(self, artifact_id, scope):
        self._scope(scope)
        if not isinstance(artifact_id, str) or not re.fullmatch(r"[0-9a-f]{64}", artifact_id):
            raise ValueError("invalid artifact ID")
        record = self._records.get(artifact_id)
        if record is None or record[0] != scope:
            raise ValueError("unknown or unauthorized artifact")
        return record

    def stage(self, data: bytes, *, scope: str):
        """Publish bytes and return only opaque ID, SHA-256, and byte size."""
        self._scope(scope)
        # Require the exact built-in bytes type. A subclass can override
        # __len__ independently of memoryview's actual buffer size, defeating
        # the hard per-artifact byte ceiling and corrupting receipt metadata.
        if type(data) is not bytes or len(data) > self._max_bytes:
            raise ValueError("artifact must be exact bytes within max_bytes")
        if (self._max_total_bytes is not None
                and self._total_bytes + len(data) > self._max_total_bytes):
            raise ValueError("aggregate artifact byte quota exceeded")
        if (self._max_artifacts is not None
                and len(self._records) >= self._max_artifacts):
            raise ValueError("artifact count quota exceeded")
        digest = hashlib.sha256(data).hexdigest()
        with self._directory() as directory_fd:
            for _ in range(16):
                artifact_id = secrets.token_hex(32)
                if artifact_id in self._records:
                    continue
                try:
                    fd = os.open(artifact_id, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                 | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory_fd)
                    break
                except FileExistsError:
                    continue
            else:
                raise ValueError("cannot allocate artifact")
            identity = None
            try:
                try:
                    identity = self._identity(os.fstat(fd))
                    os.fchmod(fd, 0o600)
                    remaining = memoryview(data)
                    while remaining:
                        written = os.write(fd, remaining)
                        if written <= 0:
                            raise OSError(errno.EIO, "incomplete artifact write")
                        remaining = remaining[written:]
                    identity = self._identity(os.fstat(fd))
                finally:
                    os.close(fd)
                created = self._now()
                self._records[artifact_id] = (scope, created, digest, len(data), identity)
            except BaseException as exc:
                # Failed creates never consume quota or produce receipts. If
                # cleanup fails, retain the original storage error and leave
                # any orphan to the caller-owned root lifecycle, not the reaper.
                try:
                    info = os.stat(artifact_id, dir_fd=directory_fd,
                                   follow_symlinks=False)
                    if (identity is None or not stat.S_ISREG(info.st_mode)
                            or info.st_nlink != 1 or info.st_uid != os.geteuid()
                            or self._identity(info) != identity):
                        raise ValueError("unsafe failed-artifact cleanup")
                    self._unlink_verified(artifact_id, directory_fd)
                except (OSError, ValueError) as cleanup_error:
                    exc.add_note(f"artifact cleanup failed: {cleanup_error!s}")
                raise
            self._total_bytes += len(data)
        return {"artifact_id": artifact_id, "sha256": digest, "size": len(data)}

    def _check_file(self, info, record):
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
                or self._identity(info) != record[4] or info.st_size != record[3]):
            raise ValueError("artifact integrity violation")

    def read(self, artifact_id, *, scope):
        """Return verified bytes; expiry is inclusive at the TTL boundary."""
        record = self._record(artifact_id, scope)
        if self._now() - record[1] >= self._ttl:
            raise ValueError("artifact expired")
        with self._directory() as directory_fd:
            try:
                fd = os.open(artifact_id, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                             | os.O_CLOEXEC, dir_fd=directory_fd)
            except OSError as exc:
                # A replaced path/symlink is an invalid artifact reference.
                # Genuine read/storage failures remain OSError (not HTTP 4xx).
                if exc.errno in (errno.ELOOP, errno.ENOENT, errno.ENOTDIR):
                    raise ValueError("unsafe or missing artifact") from exc
                raise
            try:
                self._check_file(os.fstat(fd), record)
                with os.fdopen(fd, "rb", closefd=False) as source:
                    data = source.read(record[3] + 1)
                self._check_file(os.fstat(fd), record)
                self._check_file(os.stat(artifact_id, dir_fd=directory_fd,
                                         follow_symlinks=False), record)
            finally:
                os.close(fd)
        if len(data) != record[3] or hashlib.sha256(data).hexdigest() != record[2]:
            raise ValueError("artifact integrity violation")
        if self._now() - record[1] >= self._ttl:
            raise ValueError("artifact expired")
        return data

    def delete(self, artifact_id, *, scope):
        """Safely reclaim the originally recorded inode, even when corrupted.

        The caller must supply the exact trusted scope. A changed size/digest
        must never prevent authorized cleanup, but symlink/replaced inodes
        cannot be mistaken for the recorded file.
        """
        record = self._record(artifact_id, scope)
        with self._directory() as directory_fd:
            self._remove_record(artifact_id, record, directory_fd)

    @staticmethod
    def _unlink_verified(artifact_id, directory_fd):
        os.unlink(artifact_id, dir_fd=directory_fd)
        try:
            os.stat(artifact_id, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        raise ValueError("artifact removal not verified")

    def _remove_record(self, artifact_id, record, directory_fd):
        info = os.stat(artifact_id, dir_fd=directory_fd, follow_symlinks=False)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.geteuid()
                or self._identity(info) != record[4]):
            raise ValueError("unsafe artifact reclamation")
        self._unlink_verified(artifact_id, directory_fd)
        del self._records[artifact_id]
        self._total_bytes -= record[3]

    def reap_expired(self):
        """Make one bounded pass over recorded entries; return removals only.

        Expiry is inclusive, evaluated once for this call. Never scan the disk
        for unrecorded files. Unsafe, missing, or failed removals retain their
        records and quota charges; other expired entries can still be reclaimed.
        Root/directory validation failures propagate rather than redirect cleanup.
        """
        now = self._now()
        removed = 0
        with self._directory() as directory_fd:
            for artifact_id, record in tuple(self._records.items()):
                if now - record[1] < self._ttl:
                    continue
                try:
                    self._remove_record(artifact_id, record, directory_fd)
                except (OSError, ValueError):
                    continue
                removed += 1
        return removed
