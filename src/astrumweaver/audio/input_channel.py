"""Bounded, process-local pre-admission audio input (Issue #174).

This deliberately does not expose a Worker download API or imply that a
filesystem write and a PostgreSQL admission are an atomic transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace
from threading import RLock
import secrets

from ..control.models import JobSubmission, JobStatus
from ..control.repository import (
    ConflictError, DeadlineExceededError, NoCompatibleDeployment,
    NotFoundError, OverloadedError, StorageUnavailable,
)
from .artifact_spool import ArtifactSpool


@dataclass(frozen=True)
class _Input:
    artifact_id: str
    scope: str
    receipt: dict
    job_id: str | None = None


class AudioInputChannel:
    """Single-Control staged WAV owner; caller supplies an exclusive spool."""

    def __init__(self, repository, spool: ArtifactSpool, *, max_bytes: int):
        if type(max_bytes) is not int or max_bytes < 12:
            raise ValueError("invalid maximum audio input bytes")
        if not isinstance(spool, ArtifactSpool):
            raise TypeError("expected an ArtifactSpool")
        if spool._max_total_bytes is None or spool._max_artifacts is None:
            raise ValueError("finite audio input aggregate quotas required")
        self.repository = repository
        self.spool = spool
        self.max_bytes = max_bytes
        self._lock = RLock()
        self._inputs: dict[str, _Input] = {}

    def reap_expired(self) -> int:
        """Reclaim verified TTL-expired files and their in-memory receipts."""
        with self._lock:
            count = self.spool.reap_expired()
            for ident in tuple(self._inputs):
                if ident not in self.spool._records:
                    del self._inputs[ident]
            return count

    def stage(self, data: bytes) -> dict:
        """Receive verified bounded WAV bytes under an unguessable local ID."""
        with self._lock:
            self.reap_expired()
            if (type(data) is not bytes or not 12 <= len(data) <= self.max_bytes
                    or data[:4] != b"RIFF" or data[8:12] != b"WAVE"):
                raise ValueError("bounded RIFF/WAVE input required")
            scope = "input-stage:" + secrets.token_hex(16)
            receipt = self.spool.stage(data, scope=scope)
            aid = receipt["artifact_id"]
            self._inputs[aid] = _Input(aid, scope, receipt)
            return {
                "uri": "tsumgi-audio-input:" + aid,
                "media_type": "audio/wav",
                "digest": "sha256:" + receipt["sha256"],
                "size_bytes": receipt["size"],
            }

    def submit(self, submission: JobSubmission, *, artifact_uri: str):
        """Bind a staged input to a Job; delete it if admission fails.

        The per-process lock serializes use of this input receipt. An unusual
        post-admission local filesystem failure must not roll back the DB Job:
        the caller must handle it explicitly, not claim atomicity.
        """
        if (submission.capability != "audio.transcribe"
                or submission.serving is None or submission.deadline_at is None
                or not submission.idempotency_key):
            raise ConflictError("audio input binding rejected")
        if type(artifact_uri) is not str or not artifact_uri.startswith("tsumgi-audio-input:"):
            raise ConflictError("audio input binding rejected")
        ident = artifact_uri[len("tsumgi-audio-input:"):]
        with self._lock:
            staged = self._inputs.get(ident)
            if staged is None:
                raise ConflictError("audio input binding rejected")
            try:
                self.spool.read(ident, scope=staged.scope)
                if "audio_input" in submission.payload:
                    raise ConflictError("audio input binding rejected")
                payload = dict(submission.payload)
                payload["audio_input"] = {
                    "uri": artifact_uri,
                    "media_type": "audio/wav",
                    "digest": "sha256:" + staged.receipt["sha256"],
                    "size_bytes": staged.receipt["size"],
                }
                recorded = self.repository.submit_job(
                    replace(submission, payload=payload)
                )
                if staged.job_id is not None and recorded.job_id != staged.job_id:
                    raise ConflictError("audio input binding rejected")
            except StorageUnavailable:
                # A lost COMMIT acknowledgement is not proof of rollback.
                # Preserve input for the same idempotency key to reconcile.
                raise
            except (ConflictError, DeadlineExceededError, NoCompatibleDeployment,
                    OverloadedError, ValueError, TypeError, KeyError):
                # The staged input is not usable after an admission failure.
                # If verified unlink fails, retain the record for later cleanup.
                try:
                    self.spool.delete(ident, scope=staged.scope)
                except (OSError, ValueError):
                    pass
                else:
                    del self._inputs[ident]
                raise
            self._inputs[ident] = replace(staged, job_id=recorded.job_id)
            return recorded

    def read_for_worker(
        self, job_id: str, *, worker_id: str, attempt: int,
        lease_token: str, runtime_instance_epoch: str,
    ) -> bytes:
        """Only the currently fenced Worker attempt may retrieve bound input."""
        with self._lock:
            self.repository._authorize_audio_artifact_attempt(
                job_id, worker_id=worker_id, attempt=attempt,
                lease_token=lease_token,
                runtime_instance_epoch=runtime_instance_epoch,
            )
            job = self.repository.get_job(job_id)
            binding = job.payload.get("audio_input")
            if not isinstance(binding, dict):
                raise NotFoundError("audio input unavailable")
            uri = binding.get("uri")
            if type(uri) is not str or not uri.startswith("tsumgi-audio-input:"):
                raise NotFoundError("audio input unavailable")
            ident = uri.removeprefix("tsumgi-audio-input:")
            staged = self._inputs.get(ident)
            if (staged is None or staged.job_id != job_id
                    or binding.get("size_bytes") != staged.receipt["size"]
                    or binding.get("digest") != "sha256:" + staged.receipt["sha256"]
                    or binding.get("media_type") != "audio/wav"):
                raise NotFoundError("audio input unavailable")
            content = self.spool.read(ident, scope=staged.scope)
            # Do not reuse an authorization granted before disk I/O.
            self.repository._authorize_audio_artifact_attempt(
                job_id, worker_id=worker_id, attempt=attempt,
                lease_token=lease_token,
                runtime_instance_epoch=runtime_instance_epoch,
            )
            return content

    def discard(self, artifact_uri: str) -> None:
        ident = artifact_uri.removeprefix("tsumgi-audio-input:")
        with self._lock:
            staged = self._inputs.get(ident)
            if staged is None or staged.job_id is not None:
                raise NotFoundError("staged audio input unavailable")
            self.spool.delete(ident, scope=staged.scope)
            del self._inputs[ident]
