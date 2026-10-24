"""Opt-in, Control-local WAV output staging and terminal visibility.

This is a process-local bridge for Wave2C. A successful durable Job result
is the only publication decision; this store is not a durable object store.
Do not treat its call-time authority check as a reusable capability.
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock

from ..control.models import JobStatus
from ..control.repository import ConflictError, NotFoundError
from ..execution import ArtifactRef, JobResult
from .artifact_spool import ArtifactSpool


_ERROR = "audio output authority rejected"
_AUDIO_CAPABILITIES = frozenset(("audio.synthesize", "audio.transcribe"))


@dataclass(frozen=True)
class _StagedOutput:
    job_id: str
    worker_id: str
    attempt: int
    epoch: str
    scope: str
    artifact_id: str
    reference: ArtifactRef


class AudioOutputChannel:
    """Local, serialized owner of staged output receipts.

    The caller must supply the Control repository used by the API. All spool
    methods are serialized. No bytes appear in durable Job JSON.
    """

    def __init__(self, repository, spool: ArtifactSpool, *, max_bytes: int):
        if type(max_bytes) is not int or max_bytes < 12:
            raise ValueError("max_bytes must be an integer >= 12")
        if not isinstance(spool, ArtifactSpool):
            raise TypeError("spool must be ArtifactSpool")
        if not callable(getattr(repository, "_authorize_audio_artifact_attempt", None)):
            raise ValueError("audio artifact attempt authority is required")
        self.repository = repository
        self.max_bytes = max_bytes
        self._spool = spool
        self._lock = RLock()
        self._staged: dict[str, _StagedOutput] = {}

    def authorize(
        self, job_id: str, *, worker_id: str, attempt: int,
        lease_token: str, runtime_instance_epoch: str,
    ) -> None:
        self.repository._authorize_audio_artifact_attempt(
            job_id, worker_id=worker_id, attempt=attempt,
            lease_token=lease_token,
            runtime_instance_epoch=runtime_instance_epoch,
        )

    @staticmethod
    def _scope(job_id: str, worker_id: str, attempt: int, epoch: str) -> str:
        # An authorization label used ONLY by this trusted Control service.
        return f"audio-output:{job_id}:{worker_id}:{attempt}:{epoch}"

    def stage(
        self, job_id: str, *, worker_id: str, attempt: int,
        lease_token: str, runtime_instance_epoch: str, data: bytes,
    ) -> ArtifactRef:
        with self._lock:
            self.authorize(
                job_id, worker_id=worker_id, attempt=attempt,
                lease_token=lease_token,
                runtime_instance_epoch=runtime_instance_epoch,
            )
            if (type(data) is not bytes or not 12 <= len(data) <= self.max_bytes
                    or data[:4] != b"RIFF" or data[8:12] != b"WAVE"):
                raise ValueError("audio output must be a bounded RIFF/WAVE file")
            scope = self._scope(job_id, worker_id, attempt, runtime_instance_epoch)
            receipt = self._spool.stage(data, scope=scope)
            artifact_id = receipt["artifact_id"]
            reference = ArtifactRef(
                uri=f"tsumgi-audio:{artifact_id}",
                media_type="audio/wav",
                digest=f"sha256:{receipt['sha256']}",
                size_bytes=receipt["size"],
            )
            try:
                # The lease can expire while disk I/O is in progress.
                self.authorize(
                    job_id, worker_id=worker_id, attempt=attempt,
                    lease_token=lease_token,
                    runtime_instance_epoch=runtime_instance_epoch,
                )
            except BaseException:
                self._spool.delete(artifact_id, scope=scope)
                raise
            self._staged[artifact_id] = _StagedOutput(
                job_id, worker_id, attempt, runtime_instance_epoch,
                scope, artifact_id, reference,
            )
            return reference

    def complete_job(
        self, job_id: str, result: JobResult, *, worker_id: str,
        lease_token: str, runtime_instance_epoch: str | None,
    ):
        """Require exact staged output before durable terminal success.

        Repository.complete_job itself rechecks the lease under its existing
        lock/transaction. This is not a cross-DB/filesystem transaction.
        """
        with self._lock:
            current = self.repository.get_job(job_id)
            if current.capability in _AUDIO_CAPABILITIES and result.artifacts:
                if len(result.artifacts) != 1:
                    raise ConflictError(_ERROR)
                ref = result.artifacts[0]
                if not ref.uri.startswith("tsumgi-audio:"):
                    raise ConflictError(_ERROR)
                artifact_id = ref.uri[len("tsumgi-audio:"):]
                staged = self._staged.get(artifact_id)
                if (staged is None or staged.job_id != job_id
                        or staged.worker_id != worker_id
                        or staged.attempt != current.attempts
                        or staged.epoch != runtime_instance_epoch
                        or staged.reference != ref):
                    raise ConflictError(_ERROR)
                self.authorize(
                    job_id, worker_id=worker_id, attempt=current.attempts,
                    lease_token=lease_token,
                    runtime_instance_epoch=runtime_instance_epoch,
                )
                try:
                    self._spool.read(artifact_id, scope=staged.scope)
                except ValueError as exc:
                    raise ConflictError(_ERROR) from exc

            return self.repository.complete_job(
                job_id, result, worker_id=worker_id,
                lease_token=lease_token,
                runtime_instance_epoch=runtime_instance_epoch,
            )

    def read_published(self, job_id: str, artifact_id: str) -> bytes:
        """Only expose an intact output referenced by a SUCCEEDED Job."""
        with self._lock:
            staged = self._staged.get(artifact_id)
            if staged is None or staged.job_id != job_id:
                raise NotFoundError("audio output unavailable")
            record = self.repository.get_job(job_id)
            if (record.status is not JobStatus.SUCCEEDED
                    or record.result is None
                    or staged.reference not in record.result.artifacts):
                raise NotFoundError("audio output unavailable")
            try:
                return self._spool.read(artifact_id, scope=staged.scope)
            except ValueError as exc:
                raise NotFoundError("audio output unavailable") from exc
