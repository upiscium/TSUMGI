"""PostgreSQL implementation of TSUMGI durable control state."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from hashlib import sha256
from contextlib import contextmanager
from datetime import datetime, timedelta
from math import ceil
import atexit
import os
import threading
import weakref
from typing import Any
from uuid import uuid4

from ..contracts import AcceleratorDevice, ResourceShape, WorkerSpec
from ..execution import JobResult
from ..scheduling import worker_matches
from ..serving import (
    ServingJobBinding,
    WorkerServingAdvertisement,
    worker_serving_accepts_job,
)
from .migrate import _require_utf8_database, required_migration_names
from .audio_epoch import (
    AudioEpochFence,
    fence_matches,
    validate_fence_arguments,
    validate_fence_lookup,
    worker_matches_fence,
)
from .audio_prepare import (
    AudioEpochPreparation,
    AudioEpochPreparationRelease,
    AudioPreparationQualification,
    preparation_matches,
    validate_prepare_arguments,
    validate_prepare_lookup,
    validate_release_arguments,
    worker_spec_revision,
)
from .models import (
    JobEventRecord,
    JobRecord,
    JobStatus,
    JobSubmission,
    WorkerHeartbeat,
    WorkerRecord,
    WorkerRegistration,
    WorkerState,
    utc_now,
)
from .repository import (
    ConflictError,
    DeadlineExceededError,
    EventBufferFull,
    NoCompatibleDeployment,
    NotFoundError,
    OverloadedError,
    RepositoryError,
    StorageUnavailable,
    OFFLINE_REASON_KEY,
    RECOVERABLE_OFFLINE_REASON,
    _assert_audio_artifact_attempt,
    _assert_worker_epoch,
    _aware,
    _event_size,
    _failure_payload,
    _lease_expiry,
    _lease_seconds_for,
    _submission_matches,
    _validate_audio_attempt_arguments,
)
from .serde import (
    job_result_from_dict,
    job_result_to_dict,
    requirements_from_dict,
    requirements_to_dict,
)

try:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb
except ImportError:  # pragma: no cover - optional dependency path
    psycopg = None  # type: ignore[assignment]
    dict_row = None  # type: ignore[assignment]
    Jsonb = None  # type: ignore[assignment,misc]

try:
    from psycopg_pool import ConnectionPool
except ImportError:  # pragma: no cover - falls back to one connection per call
    ConnectionPool = None  # type: ignore[assignment,misc]


def _json(value: Any) -> Any:
    return Jsonb(value) if Jsonb is not None else value


def _json_optional(value: Any) -> Any:
    """Persist absent optional JSON as SQL NULL, not JSONB null."""
    return None if value is None else _json(value)


def _close_pool(ref: "weakref.ref[Any]") -> None:
    pool = ref()
    if pool is not None:
        pool.close()


# Separate advisory namespaces prevent Worker identity locks and GPU locks
# from aliasing. Stable digest collisions only serialize unrelated identities.
_WORKER_OWNERSHIP_LOCK = 0x41535701
_GPU_OWNERSHIP_LOCK = 0x41535702


def _ownership_lock_key(identity: str) -> int:
    return int.from_bytes(sha256(identity.encode("utf-8")).digest()[:4], "big", signed=True)


# Only the explicitly bounded Gateway submission uses these limits. Existing
# non-chat Control operations retain their reviewed transaction semantics.
_ADMISSION_CONNECT_CAP_SECONDS = 5
_ADMISSION_LOCK_CAP_MS = 2_000
_ADMISSION_STATEMENT_CAP_MS = 5_000
_ADMISSION_TRANSACTION_CAP_MS = 10_000
_ADMISSION_CLOCK_SKEW_SECONDS = 2.0
_ADMISSION_MIN_POSTGRES_VERSION = 170000


def _bounded_admission_deadline(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("admission deadline must be timezone-aware")
    return _aware(value)


def _admission_remaining_ms(deadline: datetime) -> int:
    remaining = (deadline - utc_now()).total_seconds()
    if remaining <= 0:
        raise DeadlineExceededError("chat admission deadline expired")
    return max(1, ceil(remaining * 1000))


def _pg_timeout_settings(connection: Any, names: tuple[str, ...]) -> dict[str, int]:
    """Read effective operator timeouts in milliseconds, without changing them."""
    rows = connection.execute(
        """SELECT name, setting::bigint AS value_ms, unit
        FROM pg_settings WHERE name = ANY(%s)""",
        (list(names),),
    ).fetchall()
    values: dict[str, int] = {}
    for row in rows:
        if row["unit"] != "ms" or int(row["value_ms"]) < 0:
            raise StorageUnavailable("unsupported PostgreSQL timeout setting")
        values[row["name"]] = int(row["value_ms"])
    if set(values) != set(names):
        raise StorageUnavailable("PostgreSQL timeout settings are incomplete")
    return values


def _stricter_timeout_ms(operator_ms: int, requested_ms: int) -> int:
    """Zero means unlimited. Never lengthen a positive operator setting."""
    return requested_ms if operator_ms == 0 else min(operator_ms, requested_ms)


class PostgresControlRepository:
    """Transactional PostgreSQL control-plane repository."""

    def check_storage(self) -> None:
        try:
            required = required_migration_names()
        except RuntimeError as exc:
            raise StorageUnavailable(
                "postgresql migration metadata is unavailable"
            ) from exc

        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT
                    to_regclass('public.workers') AS workers,
                    to_regclass('public.jobs') AS jobs,
                    to_regclass('public.job_events') AS job_events,
                    to_regclass('public.schema_migrations') AS schema_migrations
                """
            ).fetchone()
            if (
                not row
                or row.get("workers") is None
                or row.get("jobs") is None
                or row.get("job_events") is None
                or row.get("schema_migrations") is None
            ):
                raise StorageUnavailable("postgresql schema is unavailable")

            applied_rows = connection.execute(
                "SELECT name FROM schema_migrations"
            ).fetchall()

        applied = {str(item["name"]) for item in applied_rows}
        missing = [name for name in required if name not in applied]
        if missing:
            raise StorageUnavailable(
                "postgresql schema migrations are incomplete: "
                + ", ".join(missing)
            )


    def __init__(
        self,
        database_url: str,
        *,
        worker_ttl_seconds: int = 60,
        lease_seconds: int = 300,
        event_max_count: int = 4096,
        event_max_payload_bytes: int = 65536,
        event_max_total_bytes: int = 4 * 1024 * 1024,
        event_max_read: int = 512,
        pool_min_size: int = 1,
        pool_max_size: int = 10,
        pool_timeout_seconds: float = 2.0,
        audio_lease_seconds: int | None = None,
    ) -> None:
        if audio_lease_seconds is not None and (
            type(audio_lease_seconds) is not int or audio_lease_seconds < 1
        ):
            raise ValueError("audio_lease_seconds must be a positive integer")
        self.audio_lease_seconds = audio_lease_seconds
        if not database_url:
            raise ValueError("database_url is required")
        if type(pool_min_size) is not int or pool_min_size < 0:
            raise ValueError("pool_min_size must be a non-negative integer")
        if type(pool_max_size) is not int or pool_max_size < max(1, pool_min_size):
            raise ValueError("pool_max_size must be a positive integer >= pool_min_size")
        if (
            isinstance(pool_timeout_seconds, bool)
            or not isinstance(pool_timeout_seconds, (int, float))
            or not 0 < pool_timeout_seconds < float("inf")
        ):
            raise ValueError("pool_timeout_seconds must be finite and positive")
        for value, name in (
            (worker_ttl_seconds, "worker_ttl_seconds"),
            (lease_seconds, "lease_seconds"),
            (event_max_count, "event_max_count"),
            (event_max_payload_bytes, "event_max_payload_bytes"),
            (event_max_total_bytes, "event_max_total_bytes"),
            (event_max_read, "event_max_read"),
        ):
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        self.database_url = database_url
        self.worker_ttl_seconds = worker_ttl_seconds
        self.lease_seconds = lease_seconds
        self.event_max_count = event_max_count
        self.event_max_payload_bytes = event_max_payload_bytes
        self.event_max_total_bytes = event_max_total_bytes
        self.event_max_read = event_max_read
        # Connecting costs ~40-50 ms per call in production; every claim,
        # event, heartbeat and Gateway poll paid it. Calls without an
        # admission deadline borrow a pooled connection instead. Bounded
        # admissions keep a fresh connection with their own connect timeout.
        # The short wait keeps an unreachable database a fast
        # StorageUnavailable, well inside the Worker client's 10 s timeout.
        self.pool_min_size = pool_min_size
        self.pool_max_size = pool_max_size
        self.pool_timeout_seconds = float(pool_timeout_seconds)
        self._pool: Any = None
        self._pool_closed = False
        self._pool_lock = threading.Lock()

    def _get_pool(self) -> Any:
        if ConnectionPool is None:
            return None
        with self._pool_lock:
            if self._pool_closed:
                # After close(), late calls (for example a maintenance thread
                # still finishing) use a fresh connection, not a new pool.
                return None
            if self._pool is None:
                pool = ConnectionPool(
                    self.database_url,
                    kwargs={"row_factory": dict_row},
                    min_size=self.pool_min_size,
                    max_size=self.pool_max_size,
                    timeout=self.pool_timeout_seconds,
                    # Hand out only live connections: a restarted server or a
                    # dropped TCP session is replaced, not returned.
                    check=ConnectionPool.check_connection,
                    open=False,
                    name="tsumgi-control",
                )
                pool.open(wait=False)
                # Close before interpreter finalization, where the pool's
                # own finalizer can no longer join its worker threads.
                atexit.register(_close_pool, weakref.ref(pool))
                self._pool = pool
            return self._pool

    def close(self) -> None:
        """Close pooled connections. Later calls connect per call."""
        with self._pool_lock:
            pool, self._pool = self._pool, None
            self._pool_closed = True
        if pool is not None:
            pool.close()

    @contextmanager
    def _connection(
        self, *, admission_deadline_at: datetime | None = None
    ) -> Iterator[Any]:
        if psycopg is None:
            raise StorageUnavailable(
                "PostgreSQL support requires the tsumgi postgres dependency"
            )
        deadline = _bounded_admission_deadline(admission_deadline_at)
        conninfo = self.database_url
        if deadline is not None:
            # Preserve existing libpq options. Do not append -c GUC flags
            # that could loosen a stricter operator statement/lock timeout.
            remaining = _admission_remaining_ms(deadline)
            try:
                options = psycopg.conninfo.conninfo_to_dict(conninfo)
                existing_timeout = options.get("connect_timeout")
                if existing_timeout is None:
                    # libpq precedence is explicit conninfo > service > env.
                    # conninfo_to_dict() does not resolve service files. Never
                    # promote an application cap above an unknown service cap:
                    # require an explicit timeout instead of guessing (service
                    # can also be selected by PGSERVICE).
                    if options.get("service") or os.environ.get("PGSERVICE"):
                        raise StorageUnavailable(
                            "bounded PostgreSQL admission requires an explicit "
                            "connect_timeout when a service is selected"
                        )
                    existing_timeout = os.environ.get("PGCONNECT_TIMEOUT")
                connect_timeout = min(
                    _ADMISSION_CONNECT_CAP_SECONDS,
                    max(1, ceil(remaining / 1000)),
                )
                if existing_timeout is not None:
                    operator_timeout = int(existing_timeout)
                    if operator_timeout > 0:
                        connect_timeout = min(connect_timeout, operator_timeout)
                conninfo = psycopg.conninfo.make_conninfo(
                    conninfo, connect_timeout=connect_timeout,
                )
            except (TypeError, ValueError) as exc:
                raise StorageUnavailable(
                    "bounded PostgreSQL admission configuration is invalid"
                ) from exc
        pool = self._get_pool() if deadline is None else None
        try:
            if pool is not None:
                connection = pool.getconn(timeout=self.pool_timeout_seconds)
            else:
                connection = psycopg.connect(conninfo, row_factory=dict_row)
        except Exception as exc:
            if deadline is not None and utc_now() >= deadline:
                raise DeadlineExceededError("chat admission connection deadline expired") from exc
            raise StorageUnavailable("postgresql is unavailable") from exc
        ready = False
        try:
            if deadline is not None:
                # PostgreSQL 17 is needed for the transaction-wide timeout.
                # Bootstrap on read-only pg_settings metadata before the UTF8
                # probe, then tighten the effective role/DSN/session settings
                # without ever relaxing a positive operator timeout. A
                # network blackhole on this bootstrap query remains outside
                # the hard server-side bound, as documented in the ADR.
                if connection.info.server_version < _ADMISSION_MIN_POSTGRES_VERSION:
                    raise StorageUnavailable(
                        "bounded chat admission requires PostgreSQL 17 or newer"
                    )
                try:
                    remaining = _admission_remaining_ms(deadline)
                    settings = _pg_timeout_settings(
                        connection, ("statement_timeout", "lock_timeout")
                    )
                    connection.execute(
                        """SELECT
                            set_config('statement_timeout', %s, false),
                            set_config('lock_timeout', %s, false)
                        """,
                        (
                            f"{_stricter_timeout_ms(settings['statement_timeout'], min(remaining, _ADMISSION_STATEMENT_CAP_MS))}ms",
                            f"{_stricter_timeout_ms(settings['lock_timeout'], min(remaining, _ADMISSION_LOCK_CAP_MS))}ms",
                        ),
                    )
                    # SET with session scope inside a transaction is undone
                    # by ROLLBACK: COMMIT this session-only GUC setup first.
                    connection.commit()
                except RepositoryError:
                    raise
                except Exception as exc:
                    raise StorageUnavailable(
                        "cannot establish bounded PostgreSQL admission settings"
                    ) from exc

            # Check every Control connection, not only the /ready probe.
            # Otherwise a caller could submit/register despite failed
            # readiness. No application table is touched before this check.
            try:
                _require_utf8_database(connection)
                # With psycopg's default non-autocommit connection, the probe
                # SELECT opens an implicit transaction. Roll it back so the
                # caller's transaction can issue SET TRANSACTION first.
                # This probe performs no application reads/writes.
                connection.rollback()
            except Exception as exc:
                raise StorageUnavailable(
                    "PostgreSQL UTF8 database and client encoding are required "
                    "or could not be verified"
                ) from exc

            if deadline is not None:
                before = utc_now()
                try:
                    row = connection.execute(
                        "SELECT clock_timestamp() AS db_clock"
                    ).fetchone()
                except Exception as exc:
                    if utc_now() >= deadline:
                        raise DeadlineExceededError(
                            "chat admission clock-check deadline expired"
                        ) from exc
                    raise StorageUnavailable(
                        "PostgreSQL admission clock check unavailable"
                    ) from exc
                after = utc_now()
                db_clock = row["db_clock"]
                skew = timedelta(seconds=_ADMISSION_CLOCK_SKEW_SECONDS)
                if not before - skew <= db_clock <= after + skew:
                    raise StorageUnavailable(
                        "PostgreSQL clock skew exceeds admission safety budget"
                    )
                if after >= deadline or db_clock >= deadline:
                    raise DeadlineExceededError("chat admission deadline expired")
                connection.rollback()
            ready = True
            yield connection
        finally:
            if pool is None:
                connection.close()
            elif not ready:
                # A connection that failed its checks never goes back for reuse.
                connection.close()
                pool.putconn(connection)
            else:
                # Same outcome as closing: uncommitted work is discarded.
                # A connection that cannot roll back is returned broken and
                # the pool discards it.
                try:
                    connection.rollback()
                except Exception:
                    pass
                pool.putconn(connection)

    @contextmanager
    def _transaction(
        self, *, admission_deadline_at: datetime | None = None
    ) -> Iterator[Any]:
        deadline = _bounded_admission_deadline(admission_deadline_at)
        with self._connection(admission_deadline_at=deadline) as connection:
            try:
                with connection.transaction():
                    # Ownership predicates must see a fresh snapshot after an
                    # advisory-lock wait, independent of the database default.
                    connection.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
                    if deadline is not None:
                        remaining = _admission_remaining_ms(deadline)
                        settings = _pg_timeout_settings(
                            connection,
                            ("lock_timeout", "statement_timeout", "transaction_timeout"),
                        )
                        connection.execute(
                            """SELECT
                                set_config('lock_timeout', %s, true),
                                set_config('statement_timeout', %s, true),
                                set_config('transaction_timeout', %s, true)
                            """,
                            (
                                f"{_stricter_timeout_ms(settings['lock_timeout'], min(remaining, _ADMISSION_LOCK_CAP_MS))}ms",
                                f"{_stricter_timeout_ms(settings['statement_timeout'], min(remaining, _ADMISSION_STATEMENT_CAP_MS))}ms",
                                f"{_stricter_timeout_ms(settings['transaction_timeout'], min(remaining, _ADMISSION_TRANSACTION_CAP_MS))}ms",
                            ),
                        )
                    yield connection
                    if deadline is not None:
                        # Fresh DB clock is essential: request-start time and
                        # now()/transaction_timestamp() are stale after waits.
                        # This must run *inside* the transaction before COMMIT.
                        if utc_now() >= deadline:
                            raise DeadlineExceededError("chat admission deadline expired")
                        db_clock = connection.execute(
                            "SELECT clock_timestamp() AS db_clock"
                        ).fetchone()["db_clock"]
                        if db_clock >= deadline or utc_now() >= deadline:
                            raise DeadlineExceededError("chat admission deadline expired")
            except RepositoryError:
                raise
            except Exception as exc:
                # Loss of COMMIT acknowledgement is *not* proof that no Job
                # exists. An idempotent durable reconciliation is a later gate.
                raise StorageUnavailable(
                    "postgresql transaction failed or outcome is unconfirmed"
                ) from exc

    @staticmethod
    def _worker(row: dict[str, Any]) -> WorkerRecord:
        spec = WorkerSpec(
            worker_id=row["id"],
            worker_class=row["worker_class"],
            gpu_uuids=tuple(row.get("gpu_uuids") or ()),
            accelerators=tuple(
                AcceleratorDevice(
                    uuid=str(item["uuid"]),
                    memory_mb=int(item["memory_mb"]),
                    compute_capability=item.get("compute_capability"),
                    device_class=item.get("device_class"),
                )
                for item in row.get("accelerators") or ()
            ),
            capabilities=frozenset(row.get("capabilities") or ()),
            labels=row.get("labels") or {},
            resources=ResourceShape(
                gpu_count=int(row.get("gpu_count") or 0),
                total_vram_mb=int(row.get("total_vram_mb") or 0),
                max_single_gpu_vram_mb=int(row.get("max_single_gpu_vram_mb") or 0),
            ),
        )
        return WorkerRecord(
            spec=spec,
            max_concurrency=int(row["max_concurrency"]),
            state=WorkerState(row["state"]),
            active_jobs=int(row.get("active_jobs") or 0),
            registered_at=row["registered_at"],
            last_seen_at=row["last_seen_at"],
            metadata=row.get("metadata") or {},
            serving=(
                None
                if row.get("serving") is None
                else WorkerServingAdvertisement.from_dict(row["serving"])
            ),
        )

    @staticmethod
    def _event(row: dict[str, Any]) -> JobEventRecord:
        return JobEventRecord(
            job_id=str(row["job_id"]),
            attempt=int(row["attempt"]),
            sequence=int(row["sequence"]),
            worker_id=str(row["worker_id"]),
            runtime_instance_epoch=row.get("runtime_instance_epoch"),
            kind=str(row["kind"]),
            payload=row.get("payload") or {},
            created_at=row["created_at"],
        )

    @staticmethod
    def _job(row: dict[str, Any]) -> JobRecord:
        return JobRecord(
            job_id=str(row["id"]),
            capability=row["capability"],
            payload=row.get("payload") or {},
            requirements=requirements_from_dict(row.get("requirements")),
            priority=int(row["priority"]),
            sequence=int(row["sequence"]),
            status=JobStatus(row["status"]),
            attempts=int(row["attempts"]),
            max_attempts=int(row["max_attempts"]),
            idempotency_key=row.get("idempotency_key"),
            assigned_worker_id=row.get("assigned_worker_id"),
            lease_token=row.get("lease_token"),
            result=job_result_from_dict(row.get("result")),
            error=row.get("error"),
            created_at=row["created_at"],
            available_at=row["available_at"],
            started_at=row.get("started_at"),
            finished_at=row.get("finished_at"),
            lease_expires_at=row.get("lease_expires_at"),
            deadline_at=row.get("deadline_at"),
            serving=(
                None
                if row.get("serving") is None
                else ServingJobBinding.from_dict(row["serving"])
            ),
            claimed_deployment_revision=row.get("claimed_deployment_revision"),
            claimed_serving_contract_revision=row.get(
                "claimed_serving_contract_revision"
            ),
            claimed_runtime_instance_epoch=row.get(
                "claimed_runtime_instance_epoch"
            ),
            updated_at=row["updated_at"],
        )

    def _lock_worker_ownership(
        self,
        connection: Any,
        worker_id: str,
        requested_gpu_uuids: tuple[str, ...] = (),
    ) -> dict[str, Any] | None:
        """Serialize acquisition, including a Worker/GPU with no row yet.

        Enrollment/state transitions take identity -> sorted GPU lock keys ->
        own Worker row. They never lock peer Worker rows or job rows. Job
        transactions retain job -> Worker order and do not take advisory locks.
        Release-only updates need no advisory lock: they can remove, but cannot
        introduce, an owner. Reacquisition must pass through this method.
        """
        connection.execute(
            "SELECT pg_advisory_xact_lock(%s::integer, %s::integer)",
            (_WORKER_OWNERSHIP_LOCK, _ownership_lock_key(worker_id)),
        )
        current = connection.execute(
            "SELECT gpu_uuids FROM workers WHERE id = %s", (worker_id,)
        ).fetchone()
        # The identity lock prevents concurrent enrollment from changing this
        # topology while GPU locks are acquired. Include old and desired GPUs
        # so topology replacement and resource handoff use one protocol.
        gpu_uuids = set(requested_gpu_uuids)
        if current is not None:
            gpu_uuids.update(current.get("gpu_uuids") or ())
        for key in sorted({_ownership_lock_key(uuid) for uuid in gpu_uuids}):
            connection.execute(
                "SELECT pg_advisory_xact_lock(%s::integer, %s::integer)",
                (_GPU_OWNERSHIP_LOCK, key),
            )
        # Liveness and active-job counts may have changed during a lock wait.
        # Read/lock the current row only after acquiring the entire GPU set.
        return connection.execute(
            "SELECT * FROM workers WHERE id = %s FOR UPDATE", (worker_id,)
        ).fetchone()

    @staticmethod
    def _has_active_audio_preparation(
        connection: Any, worker_id: str, now: datetime
    ) -> bool:
        # Active: not released, not expired at `now`, qualification not revoked.
        return connection.execute(
            """SELECT 1 FROM audio_epoch_preparations AS p
               WHERE p.worker_id = %s AND p.expires_at > %s
                 AND NOT EXISTS (SELECT 1 FROM audio_epoch_preparation_releases AS l
                                 WHERE l.worker_id = p.worker_id
                                   AND l.runtime_instance_epoch = p.runtime_instance_epoch)
                 AND NOT EXISTS (SELECT 1 FROM audio_preparation_qualification_revocations AS r
                                 WHERE r.qualification_revision = p.qualification_revision)
               LIMIT 1""",
            (worker_id, now),
        ).fetchone() is not None

    @staticmethod
    def _assert_audio_epoch_not_fenced(
        connection: Any, worker_id: str, runtime_instance_epoch: str | None,
        now: datetime | None = None,
    ) -> None:
        if PostgresControlRepository._has_active_audio_preparation(
            connection, worker_id, _aware(now)
        ):
            raise ConflictError("audio epoch is not active")
        if runtime_instance_epoch is None:
            return
        row = connection.execute(
            """SELECT 1 FROM audio_epoch_fences
               WHERE worker_id = %s AND runtime_instance_epoch = %s""",
            (worker_id, runtime_instance_epoch),
        ).fetchone()
        if row is not None:
            raise ConflictError("audio epoch is fenced")

    def store_audio_preparation_qualification(
        self, qualification: AudioPreparationQualification
    ) -> AudioPreparationQualification:
        try:
            validated = AudioPreparationQualification.from_dict(qualification.to_dict())
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("invalid audio preparation qualification") from exc
        with self._transaction() as connection:
            worker = connection.execute(
                "SELECT id FROM workers WHERE id = %s FOR UPDATE",
                (validated.worker_id,),
            ).fetchone()
            if worker is None:
                raise NotFoundError("worker not found")
            connection.execute(
                """INSERT INTO audio_preparation_qualifications
                       (qualification_revision, worker_id, body)
                   VALUES (%s, %s, %s) ON CONFLICT DO NOTHING""",
                (validated.qualification_revision, validated.worker_id,
                 _json(validated.to_dict())),
            )
            row = connection.execute(
                """SELECT qualification_revision, worker_id, body
                   FROM audio_preparation_qualifications
                   WHERE qualification_revision = %s""",
                (validated.qualification_revision,),
            ).fetchone()
            try:
                stored = AudioPreparationQualification.from_dict(row["body"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ConflictError("audio preparation qualification conflict") from exc
            if (row["worker_id"] != validated.worker_id
                    or row["qualification_revision"] != validated.qualification_revision
                    or stored != validated):
                raise ConflictError("audio preparation qualification conflict")
            return stored

    def revoke_audio_preparation_qualification(
        self, qualification_revision: str, *, now: datetime | None = None
    ) -> None:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT worker_id FROM audio_preparation_qualifications WHERE qualification_revision = %s",
                (qualification_revision,),
            ).fetchone()
            if row is None:
                raise NotFoundError("audio preparation qualification not found")
            worker_id = row["worker_id"]
            worker = connection.execute(
                "SELECT id FROM workers WHERE id = %s FOR UPDATE", (worker_id,)
            ).fetchone()
            if worker is None:
                raise NotFoundError("audio preparation qualification not found")
            exists = connection.execute(
                "SELECT 1 FROM audio_preparation_qualifications WHERE qualification_revision = %s AND worker_id = %s",
                (qualification_revision, worker_id),
            ).fetchone()
            if exists is None:
                raise NotFoundError("audio preparation qualification not found")
            timestamp = _aware(now) if now is not None else connection.execute(
                "SELECT clock_timestamp() AS ts"
            ).fetchone()["ts"]
            connection.execute(
                """INSERT INTO audio_preparation_qualification_revocations
                       (qualification_revision, revoked_at)
                   VALUES (%s, %s) ON CONFLICT DO NOTHING""",
                (qualification_revision, timestamp),
            )

    def prepare_audio_epoch(
        self, worker_id: str, runtime_instance_epoch: str, *,
        qualification_revision: str, expected_intent_revision: int,
        prepare_request_id: str, ttl_seconds: int = 60,
        now: datetime | None = None,
    ) -> AudioEpochPreparation:
        validate_prepare_arguments(
            worker_id, runtime_instance_epoch, qualification_revision,
            expected_intent_revision, prepare_request_id, ttl_seconds,
        )
        with self._transaction() as connection:
            worker_row = connection.execute(
                "SELECT * FROM workers WHERE id = %s FOR UPDATE", (worker_id,)
            ).fetchone()
            if worker_row is None:
                raise NotFoundError("worker not found")
            timestamp = _aware(now) if now is not None else connection.execute(
                "SELECT clock_timestamp() AS ts"
            ).fetchone()["ts"]
            qualification_row = connection.execute(
                """SELECT qualification_revision, worker_id, body,
                          NOT EXISTS (SELECT 1 FROM audio_preparation_qualification_revocations r
                                      WHERE r.qualification_revision = q.qualification_revision) AS active
                   FROM audio_preparation_qualifications q
                   WHERE qualification_revision = %s""",
                (qualification_revision,),
            ).fetchone()
            try:
                qualification = AudioPreparationQualification.from_dict(
                    qualification_row["body"]
                )
            except (TypeError, ValueError, KeyError) as exc:
                raise ConflictError("audio epoch preparation rejected") from exc
            if (qualification_row["worker_id"] != worker_id
                    or qualification.qualification_revision != qualification_revision
                    or not qualification.reviewed
                    or not qualification_row["active"]
                    or qualification.issued_at > timestamp
                    or qualification.expires_at <= timestamp):
                raise ConflictError("audio epoch preparation rejected")
            worker = self._worker(worker_row)
            if (not worker_matches_fence(
                    worker, worker.serving.runtime_instance.epoch if worker.serving else "",
                    qualification.deployment_revision,
                    qualification.serving_contract_revision)
                    or worker_spec_revision(worker) != qualification.worker_spec_revision):
                raise ConflictError("audio epoch preparation rejected")
            if worker.state is not WorkerState.OFFLINE or worker.active_jobs:
                raise ConflictError("audio epoch preparation rejected")
            running = connection.execute(
                "SELECT 1 FROM jobs WHERE status = 'running' AND assigned_worker_id = %s LIMIT 1",
                (worker_id,),
            ).fetchone()
            if running is not None:
                raise ConflictError("audio epoch preparation rejected")
            if worker.serving is not None and runtime_instance_epoch == worker.serving.runtime_instance.epoch:
                raise ConflictError("audio epoch preparation rejected")
            claimed = connection.execute(
                """SELECT 1 FROM jobs WHERE assigned_worker_id = %s
                   AND claimed_runtime_instance_epoch = %s LIMIT 1""",
                (worker_id, runtime_instance_epoch),
            ).fetchone()
            if claimed is not None:
                raise ConflictError("audio epoch preparation rejected")
            fenced = connection.execute(
                """SELECT 1 FROM audio_epoch_fences
                   WHERE worker_id = %s AND runtime_instance_epoch = %s""",
                (worker_id, runtime_instance_epoch),
            ).fetchone()
            if fenced is not None:
                raise ConflictError("audio epoch preparation rejected")
            existing = connection.execute(
                """SELECT * FROM audio_epoch_preparations
                   WHERE worker_id = %s AND runtime_instance_epoch = %s""",
                (worker_id, runtime_instance_epoch),
            ).fetchone()
            if existing is not None:
                record = AudioEpochPreparation(**existing)
                latest = connection.execute(
                    "SELECT max(intent_revision) AS revision FROM audio_epoch_preparations WHERE worker_id = %s",
                    (worker_id,),
                ).fetchone()["revision"] or 0
                released = connection.execute(
                    """SELECT 1 FROM audio_epoch_preparation_releases
                       WHERE worker_id = %s AND runtime_instance_epoch = %s""",
                    (worker_id, runtime_instance_epoch),
                ).fetchone()
                if (preparation_matches(record, qualification_revision,
                        expected_intent_revision, prepare_request_id)
                        and released is None
                        and record.expires_at > timestamp and latest == record.intent_revision):
                    return record
                raise ConflictError("audio epoch preparation rejected")
            latest = connection.execute(
                "SELECT max(intent_revision) AS revision FROM audio_epoch_preparations WHERE worker_id = %s",
                (worker_id,),
            ).fetchone()["revision"] or 0
            if expected_intent_revision != latest:
                raise ConflictError("audio epoch preparation rejected")
            # The request id is unique across all Workers: answer 409 here, the same
            # as the memory implementation, instead of a 503 from the constraint.
            if connection.execute(
                "SELECT 1 FROM audio_epoch_preparations WHERE prepare_request_id = %s",
                (prepare_request_id,),
            ).fetchone() is not None:
                raise ConflictError("audio epoch preparation rejected")
            if self._has_active_audio_preparation(connection, worker_id, timestamp):
                raise ConflictError("audio epoch preparation rejected")
            expires = min(qualification.expires_at, timestamp + timedelta(seconds=ttl_seconds))
            try:
                connection.execute(
                """INSERT INTO audio_epoch_preparations
                   (worker_id, runtime_instance_epoch, qualification_revision,
                    deployment_revision, serving_contract_revision, prepare_request_id,
                    expected_intent_revision, intent_revision, prepared_at, expires_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (worker_id, runtime_instance_epoch, qualification_revision,
                 qualification.deployment_revision, qualification.serving_contract_revision,
                 prepare_request_id, expected_intent_revision,
                 expected_intent_revision + 1, timestamp, expires),
                )
            except psycopg.errors.UniqueViolation as exc:  # a concurrent identical request
                raise ConflictError("audio epoch preparation rejected") from exc
            return AudioEpochPreparation(
                worker_id, runtime_instance_epoch, qualification_revision,
                qualification.deployment_revision, qualification.serving_contract_revision,
                prepare_request_id, expected_intent_revision,
                expected_intent_revision + 1, timestamp, expires,
            )

    def get_audio_epoch_preparation(
        self, worker_id: str, runtime_instance_epoch: str
    ) -> AudioEpochPreparation:
        validate_prepare_lookup(worker_id, runtime_instance_epoch)
        with self._connection() as connection:
            row = connection.execute(
                """SELECT * FROM audio_epoch_preparations
                   WHERE worker_id = %s AND runtime_instance_epoch = %s""",
                (worker_id, runtime_instance_epoch),
            ).fetchone()
        if row is None:
            raise NotFoundError("audio epoch preparation not found")
        return AudioEpochPreparation(**row)

    def release_audio_epoch_preparation(
        self, worker_id: str, runtime_instance_epoch: str, *,
        release_request_id: str, now: datetime | None = None,
    ) -> AudioEpochPreparationRelease:
        validate_release_arguments(worker_id, runtime_instance_epoch, release_request_id)
        timestamp = _aware(now)
        with self._transaction() as connection:
            # Same lock as prepare/fence: serialize per Worker.
            worker_row = connection.execute(
                "SELECT id FROM workers WHERE id = %s FOR UPDATE", (worker_id,)
            ).fetchone()
            if worker_row is None or connection.execute(
                """SELECT 1 FROM audio_epoch_preparations
                   WHERE worker_id = %s AND runtime_instance_epoch = %s""",
                (worker_id, runtime_instance_epoch),
            ).fetchone() is None:
                raise NotFoundError("audio epoch preparation not found")
            existing = connection.execute(
                """SELECT * FROM audio_epoch_preparation_releases
                   WHERE worker_id = %s AND runtime_instance_epoch = %s""",
                (worker_id, runtime_instance_epoch),
            ).fetchone()
            if existing is not None:
                record = AudioEpochPreparationRelease(**existing)
                if record.release_request_id == release_request_id:
                    return record
                raise ConflictError("audio epoch preparation release conflict")
            if connection.execute(
                "SELECT 1 FROM audio_epoch_preparation_releases WHERE release_request_id = %s",
                (release_request_id,),
            ).fetchone() is not None:
                raise ConflictError("audio epoch preparation release conflict")
            try:
                connection.execute(
                    """INSERT INTO audio_epoch_preparation_releases
                       (worker_id, runtime_instance_epoch, release_request_id, released_at)
                       VALUES (%s, %s, %s, %s)""",
                    (worker_id, runtime_instance_epoch, release_request_id, timestamp),
                )
            except psycopg.errors.UniqueViolation as exc:
                raise ConflictError("audio epoch preparation release conflict") from exc
            return AudioEpochPreparationRelease(
                worker_id, runtime_instance_epoch, release_request_id, timestamp,
            )

    def get_audio_epoch_preparation_release(
        self, worker_id: str, runtime_instance_epoch: str
    ) -> AudioEpochPreparationRelease:
        validate_prepare_lookup(worker_id, runtime_instance_epoch)
        with self._connection() as connection:
            row = connection.execute(
                """SELECT * FROM audio_epoch_preparation_releases
                   WHERE worker_id = %s AND runtime_instance_epoch = %s""",
                (worker_id, runtime_instance_epoch),
            ).fetchone()
        if row is None:
            raise NotFoundError("audio epoch preparation release not found")
        return AudioEpochPreparationRelease(**row)

    def fence_audio_epoch(
        self,
        worker_id: str,
        runtime_instance_epoch: str,
        *,
        expected_deployment_revision: str,
        expected_serving_contract_revision: str,
        fence_request_id: str,
        now: datetime | None = None,
    ) -> AudioEpochFence:
        validate_fence_arguments(
            worker_id, runtime_instance_epoch, expected_deployment_revision,
            expected_serving_contract_revision, fence_request_id,
        )
        timestamp = _aware(now)
        with self._transaction() as connection:
            worker_row = connection.execute(
                "SELECT * FROM workers WHERE id = %s FOR UPDATE", (worker_id,)
            ).fetchone()
            if worker_row is None:
                raise NotFoundError("worker not found")

            existing = connection.execute(
                """SELECT * FROM audio_epoch_fences
                   WHERE worker_id = %s AND runtime_instance_epoch = %s""",
                (worker_id, runtime_instance_epoch),
            ).fetchone()
            if existing is not None:
                record = AudioEpochFence(**existing)
                if fence_matches(
                    record,
                    expected_deployment_revision=expected_deployment_revision,
                    expected_serving_contract_revision=expected_serving_contract_revision,
                    fence_request_id=fence_request_id,
                ):
                    return record
                raise ConflictError("audio epoch fence conflict")

            worker = self._worker(worker_row)
            if not worker_matches_fence(
                worker, runtime_instance_epoch, expected_deployment_revision,
                expected_serving_contract_revision,
            ):
                raise ConflictError("audio epoch fence conflict")
            connection.execute(
                """INSERT INTO audio_epoch_fences (
                       worker_id, runtime_instance_epoch, deployment_revision,
                       serving_contract_revision, fence_request_id, fenced_at
                   ) VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT DO NOTHING""",
                (
                    worker_id, runtime_instance_epoch, expected_deployment_revision,
                    expected_serving_contract_revision, fence_request_id, timestamp,
                ),
            )
            row = connection.execute(
                """SELECT * FROM audio_epoch_fences
                   WHERE worker_id = %s AND runtime_instance_epoch = %s""",
                (worker_id, runtime_instance_epoch),
            ).fetchone()
            if row is None:
                raise ConflictError("audio epoch fence conflict")
            record = AudioEpochFence(**row)
            if not fence_matches(
                record,
                expected_deployment_revision=expected_deployment_revision,
                expected_serving_contract_revision=expected_serving_contract_revision,
                fence_request_id=fence_request_id,
            ):
                raise ConflictError("audio epoch fence conflict")
            return record

    def get_audio_epoch_fence(
        self, worker_id: str, runtime_instance_epoch: str
    ) -> AudioEpochFence:
        validate_fence_lookup(worker_id, runtime_instance_epoch)
        with self._connection() as connection:
            row = connection.execute(
                """SELECT * FROM audio_epoch_fences
                   WHERE worker_id = %s AND runtime_instance_epoch = %s""",
                (worker_id, runtime_instance_epoch),
            ).fetchone()
        if row is None:
            raise NotFoundError("audio epoch fence not found")
        return AudioEpochFence(**row)

    def _gpu_overlap(
        self,
        connection: Any,
        registration: WorkerRegistration,
    ) -> str | None:
        gpu_uuids = list(registration.spec.gpu_uuids)
        if not gpu_uuids:
            return None
        row = connection.execute(
            """
            SELECT id
            FROM workers
            WHERE id <> %s
              AND (state <> 'offline' OR active_jobs > 0)
              AND EXISTS (
                  SELECT 1
                  FROM jsonb_array_elements_text(%s::jsonb) AS requested(uuid)
                  WHERE gpu_uuids ? requested.uuid
              )
            ORDER BY id
            LIMIT 1
            """,
            (registration.spec.worker_id, _json(gpu_uuids)),
        ).fetchone()
        return None if row is None else str(row["id"])

    def register_worker(
        self, registration: WorkerRegistration, *, recovery: bool = False,
        now: datetime | None = None
    ) -> WorkerRecord:
        timestamp = _aware(now)
        if OFFLINE_REASON_KEY in registration.metadata:
            raise ConflictError("Worker cannot supply Control-owned offline reason")
        spec = registration.spec
        with self._transaction() as connection:
            existing = self._lock_worker_ownership(
                connection, spec.worker_id, spec.gpu_uuids
            )
            proposed_epoch = (
                None if registration.serving is None
                else registration.serving.runtime_instance.epoch
            )
            if self._has_active_audio_preparation(connection, spec.worker_id, timestamp):
                raise ConflictError("audio epoch is not active")
            if proposed_epoch is None:
                history = connection.execute(
                    "SELECT 1 FROM audio_epoch_fences WHERE worker_id = %s LIMIT 1",
                    (spec.worker_id,),
                ).fetchone()
                if history is not None:
                    raise ConflictError("audio epoch is fenced")
            else:
                self._assert_audio_epoch_not_fenced(
                    connection, spec.worker_id, proposed_epoch, timestamp
                )
            if existing is not None:
                current = self._worker(existing)
                if recovery:
                    if (current.state is not WorkerState.OFFLINE
                        or current.metadata.get(OFFLINE_REASON_KEY) != RECOVERABLE_OFFLINE_REASON):
                        raise ConflictError("Worker is not eligible for explicit recovery")
                    if (current.spec != registration.spec
                        or current.max_concurrency != registration.max_concurrency
                        or current.serving != registration.serving):
                        raise ConflictError("recovery Worker identity or runtime epoch differs")
                    if current.active_jobs:
                        raise ConflictError("recovery forbidden while Worker leases are active")
                if current.active_jobs > 0:
                    if registration.spec != current.spec:
                        raise ConflictError(
                            "worker resource/topology cannot change while jobs are active"
                        )
                    if registration.max_concurrency < current.active_jobs:
                        raise ConflictError(
                            "max_concurrency cannot be lower than active job count"
                        )
                    if registration.serving != current.serving:
                        raise ConflictError(
                            "worker serving deployment cannot change while jobs are active"
                        )

            if recovery:
                if existing is None:
                    raise ConflictError("Worker recovery requires an existing registration")
                active_leases = connection.execute(
                    """SELECT count(*)::integer AS count FROM jobs
                       WHERE status = 'running' AND assigned_worker_id = %s""",
                    (spec.worker_id,),
                ).fetchone()["count"]
                if active_leases:
                    raise ConflictError("recovery forbidden while durable leases are active")
            conflicting = self._gpu_overlap(connection, registration)
            if conflicting:
                raise ConflictError(
                    f"GPU identity overlaps with worker {conflicting}"
                )

            registered_at = existing["registered_at"] if existing else timestamp
            active_jobs = connection.execute(
                """
                SELECT count(*)::integer AS count
                FROM jobs
                WHERE status = 'running' AND assigned_worker_id = %s
                """,
                (spec.worker_id,),
            ).fetchone()["count"]

            row = connection.execute(
                """
                INSERT INTO workers (
                    id, worker_class, capabilities, labels, gpu_uuids,
                    accelerators, gpu_count, total_vram_mb, max_single_gpu_vram_mb,
                    max_concurrency, state, metadata, serving, registered_at,
                    last_seen_at, active_jobs, updated_at
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, 'online', %s, %s, %s,
                    %s, %s, %s
                )
                ON CONFLICT (id) DO UPDATE SET
                    worker_class = EXCLUDED.worker_class,
                    capabilities = EXCLUDED.capabilities,
                    labels = EXCLUDED.labels,
                    gpu_uuids = EXCLUDED.gpu_uuids,
                    accelerators = EXCLUDED.accelerators,
                    gpu_count = EXCLUDED.gpu_count,
                    total_vram_mb = EXCLUDED.total_vram_mb,
                    max_single_gpu_vram_mb = EXCLUDED.max_single_gpu_vram_mb,
                    max_concurrency = EXCLUDED.max_concurrency,
                    state = 'online',
                    metadata = EXCLUDED.metadata,
                    serving = EXCLUDED.serving,
                    last_seen_at = EXCLUDED.last_seen_at,
                    active_jobs = EXCLUDED.active_jobs,
                    updated_at = EXCLUDED.updated_at
                RETURNING *
                """,
                (
                    spec.worker_id,
                    spec.worker_class,
                    _json(sorted(spec.capabilities)),
                    _json(dict(spec.labels)),
                    _json(list(spec.gpu_uuids)),
                    _json(
                        [
                            {
                                "uuid": device.uuid,
                                "memory_mb": device.memory_mb,
                                "compute_capability": device.compute_capability,
                                "device_class": device.device_class,
                            }
                            for device in spec.accelerators
                        ]
                    ),
                    spec.resources.gpu_count,
                    spec.resources.total_vram_mb,
                    spec.resources.max_single_gpu_vram_mb,
                    registration.max_concurrency,
                    _json(dict(registration.metadata)),
                    _json_optional(
                        None
                        if registration.serving is None
                        else registration.serving.to_dict()
                    ),
                    registered_at,
                    timestamp,
                    active_jobs,
                    timestamp,
                ),
            ).fetchone()
            return self._worker(row)

    def get_worker(self, worker_id: str) -> WorkerRecord:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM workers WHERE id = %s", (worker_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"worker not found: {worker_id}")
        return self._worker(row)

    def list_workers(self) -> list[WorkerRecord]:
        """Every registered Worker (any state), ordered by id."""
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM workers ORDER BY id").fetchall()
        return [self._worker(row) for row in rows]

    def set_worker_state(
        self,
        worker_id: str,
        state: WorkerState,
        *,
        runtime_instance_epoch: str | None = None,
        now: datetime | None = None,
    ) -> WorkerRecord:
        timestamp = _aware(now)
        with self._transaction() as connection:
            current_row = self._lock_worker_ownership(connection, worker_id)
            if current_row is None:
                raise NotFoundError(f"worker not found: {worker_id}")
            current = self._worker(current_row)
            _assert_worker_epoch(current, runtime_instance_epoch)
            self._assert_audio_epoch_not_fenced(
                connection, worker_id, runtime_instance_epoch, timestamp
            )
            if state is not WorkerState.OFFLINE:
                conflict = self._gpu_overlap(
                    connection,
                    WorkerRegistration(
                        spec=current.spec,
                        max_concurrency=current.max_concurrency,
                        metadata=current.metadata,
                        serving=current.serving,
                    ),
                )
                if conflict:
                    raise ConflictError(
                        f"GPU identity overlaps with worker {conflict}"
                    )
            metadata = dict(current.metadata)
            if state is WorkerState.OFFLINE:
                metadata[OFFLINE_REASON_KEY] = "operator"
            else:
                metadata.pop(OFFLINE_REASON_KEY, None)
            row = connection.execute(
                """
                UPDATE workers
                SET state = %s, last_seen_at = %s, metadata = %s, updated_at = %s
                WHERE id = %s
                RETURNING *
                """,
                (state.value, timestamp, _json(metadata), timestamp, worker_id),
            ).fetchone()
            return self._worker(row)

    def heartbeat_worker(
        self,
        worker_id: str,
        heartbeat: WorkerHeartbeat | None = None,
        *,
        now: datetime | None = None,
    ) -> WorkerRecord:
        timestamp = _aware(now)
        heartbeat = heartbeat or WorkerHeartbeat()
        if OFFLINE_REASON_KEY in heartbeat.metadata:
            raise ConflictError("Worker cannot override Control-owned offline reason")
        with self._transaction() as connection:
            # Keep the global lock order job -> worker whenever a job is
            # involved. Claim/completion/recovery use the same order.
            if heartbeat.active_job_id is not None or heartbeat.lease_token is not None:
                if not heartbeat.active_job_id or not heartbeat.lease_token:
                    raise ConflictError(
                        "active_job_id and lease_token must be supplied together"
                    )
                job_row = connection.execute(
                    "SELECT * FROM jobs WHERE id::text = %s FOR UPDATE",
                    (heartbeat.active_job_id,),
                ).fetchone()
                if job_row is None:
                    raise NotFoundError(
                        f"job not found: {heartbeat.active_job_id}"
                    )
                self._assert_running_row(
                    job_row,
                    worker_id=worker_id,
                    lease_token=heartbeat.lease_token,
                    runtime_instance_epoch=heartbeat.runtime_instance_epoch,
                    now=timestamp,
                )
                connection.execute(
                    """
                    UPDATE jobs
                    SET lease_expires_at = %s, updated_at = %s
                    WHERE id = %s
                    """,
                    (
                        _lease_expiry(
                            timestamp,
                            _lease_seconds_for(
                                self.lease_seconds, self.audio_lease_seconds,
                                job_row.get("capability"),
                            ),
                            job_row.get("deadline_at"),
                        ),
                        timestamp,
                        job_row["id"],
                    ),
                )

            worker_row = connection.execute(
                "SELECT * FROM workers WHERE id = %s FOR UPDATE", (worker_id,)
            ).fetchone()
            if worker_row is None:
                raise NotFoundError(f"worker not found: {worker_id}")
            current = self._worker(worker_row)
            _assert_worker_epoch(current, heartbeat.runtime_instance_epoch)
            self._assert_audio_epoch_not_fenced(
                connection, worker_id, heartbeat.runtime_instance_epoch, timestamp
            )
            if current.state is WorkerState.OFFLINE and heartbeat.state in {
                WorkerState.ONLINE, WorkerState.DRAINING,
            }:
                raise ConflictError("offline worker must explicitly reacquire GPU ownership")

            active_jobs = connection.execute(
                """
                SELECT count(*)::integer AS count
                FROM jobs
                WHERE status = 'running' AND assigned_worker_id = %s
                """,
                (worker_id,),
            ).fetchone()["count"]
            state = (heartbeat.state or current.state).value
            metadata = {**dict(current.metadata), **dict(heartbeat.metadata)}
            row = connection.execute(
                """
                UPDATE workers
                SET state = %s, last_seen_at = %s, active_jobs = %s,
                    metadata = %s, updated_at = %s
                WHERE id = %s
                RETURNING *
                """,
                (
                    state,
                    timestamp,
                    active_jobs,
                    _json(metadata),
                    timestamp,
                    worker_id,
                ),
            ).fetchone()
            return self._worker(row)

    def expire_stale_workers(
        self, *, now: datetime | None = None
    ) -> list[WorkerRecord]:
        timestamp = _aware(now)
        cutoff = timestamp - timedelta(seconds=self.worker_ttl_seconds)
        with self._transaction() as connection:
            rows = connection.execute(
                """
                UPDATE workers
                SET state = 'offline',
                    metadata = metadata || jsonb_build_object(
                        %s::text,
                        CASE WHEN state = 'online' THEN %s::text ELSE 'draining_expired' END
                    ),
                    updated_at = %s
                WHERE state <> 'offline' AND last_seen_at < %s
                RETURNING *
                """,
                (OFFLINE_REASON_KEY, RECOVERABLE_OFFLINE_REASON, timestamp, cutoff),
            ).fetchall()
        return [self._worker(row) for row in rows]

    def expire_deadline_jobs(
        self, *, now: datetime | None = None
    ) -> list[JobRecord]:
        timestamp = _aware(now)
        expired: list[JobRecord] = []
        while True:
            with self._transaction() as connection:
                row = connection.execute(
                    """
                    SELECT *
                    FROM jobs
                    WHERE status IN ('queued', 'running')
                      AND deadline_at IS NOT NULL
                      AND deadline_at <= %s
                    ORDER BY sequence ASC
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                    """,
                    (timestamp,),
                ).fetchone()
                if row is None:
                    return expired
                was_running = row["status"] == JobStatus.RUNNING.value
                updated = connection.execute(
                    """
                    UPDATE jobs
                    SET status = 'failed',
                        error = %s,
                        assigned_worker_id = NULL,
                        lease_token = NULL,
                        lease_expires_at = NULL,
                        finished_at = %s,
                        updated_at = %s
                    WHERE id = %s
                    RETURNING *
                    """,
                    (
                        _json(
                            {
                                "type": "deadline_expired",
                                "message": "job deadline expired",
                                "retryable": False,
                            }
                        ),
                        timestamp,
                        timestamp,
                        row["id"],
                    ),
                ).fetchone()
                if was_running:
                    self._decrement_worker(
                        connection, row.get("assigned_worker_id"), timestamp
                    )
                expired.append(self._job(updated))

    def submit_job(
        self, submission: JobSubmission, *, now: datetime | None = None,
        admission_deadline_at: datetime | None = None,
    ) -> JobRecord:
        deadline = _bounded_admission_deadline(admission_deadline_at)
        if (
            deadline is not None
            and submission.deadline_at is not None
            and deadline > _aware(submission.deadline_at)
        ):
            raise ValueError("admission deadline cannot exceed serving Job deadline")
        timestamp = _aware(now)
        available_at = (
            _aware(submission.available_at)
            if submission.available_at is not None
            else timestamp
        )
        job_id = str(uuid4())
        requirements = requirements_to_dict(submission.requirements)

        with self._transaction(admission_deadline_at=deadline) as connection:
            # Resolve durable idempotency before transient admission state.
            if submission.idempotency_key:
                existing = connection.execute(
                    """
                    SELECT * FROM jobs
                    WHERE idempotency_key = %s
                    FOR UPDATE
                    """,
                    (submission.idempotency_key,),
                ).fetchone()
                if existing is not None:
                    record = self._job(existing)
                    if not _submission_matches(record, submission):
                        raise ConflictError(
                            "idempotency key already belongs to a different job request"
                        )
                    return record

            if (
                submission.deadline_at is not None
                and _aware(submission.deadline_at) <= timestamp
            ):
                raise DeadlineExceededError("job deadline has expired")

            if submission.serving is not None:
                cutoff = timestamp - timedelta(seconds=self.worker_ttl_seconds)
                candidate_rows = connection.execute(
                    """
                    SELECT w.* FROM workers AS w
                    WHERE w.state = 'online'
                      AND w.last_seen_at >= %s
                      AND w.capabilities ? %s
                      AND NOT EXISTS (
                          SELECT 1 FROM audio_epoch_fences AS f
                          WHERE f.worker_id = w.id
                            AND f.runtime_instance_epoch = w.serving #>> '{runtime_instance,epoch}'
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM audio_epoch_preparations AS p
                          WHERE p.worker_id = w.id
                            AND p.expires_at > %s
                            AND NOT EXISTS (
                                SELECT 1 FROM audio_epoch_preparation_releases AS l
                                WHERE l.worker_id = p.worker_id
                                  AND l.runtime_instance_epoch = p.runtime_instance_epoch
                            )
                            AND NOT EXISTS (
                                SELECT 1 FROM audio_preparation_qualification_revocations AS r
                                WHERE r.qualification_revision = p.qualification_revision
                            )
                      )
                    """,
                    (cutoff, submission.capability, timestamp),
                ).fetchall()
                compatible = [
                    worker
                    for worker in (self._worker(row) for row in candidate_rows)
                    if worker_matches(worker.spec, submission.requirements)
                    and worker_serving_accepts_job(
                        worker.serving, submission.capability, submission.serving
                    )
                ]
                if not compatible:
                    raise NoCompatibleDeployment(
                        "no compatible serving deployment is available"
                    )
                if all(
                    worker.active_jobs >= worker.max_concurrency
                    for worker in compatible
                ):
                    raise OverloadedError(
                        "all compatible serving deployments are at capacity"
                    )

            row = connection.execute(
                """
                INSERT INTO jobs (
                    id, capability, payload, requirements, priority,
                    attempts, max_attempts, idempotency_key,
                    created_at, available_at, deadline_at, serving, updated_at
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    0, %s, %s,
                    %s, %s, %s, %s, %s
                )
                ON CONFLICT (idempotency_key) DO NOTHING
                RETURNING *
                """,
                (
                    job_id,
                    submission.capability,
                    _json(dict(submission.payload)),
                    _json(requirements),
                    submission.priority,
                    submission.max_attempts,
                    submission.idempotency_key,
                    timestamp,
                    available_at,
                    None if submission.deadline_at is None else _aware(submission.deadline_at),
                    _json_optional(
                        None
                        if submission.serving is None
                        else submission.serving.to_dict()
                    ),
                    timestamp,
                ),
            ).fetchone()
            if row is not None:
                return self._job(row)

            existing = connection.execute(
                """
                SELECT * FROM jobs
                WHERE idempotency_key = %s
                FOR UPDATE
                """,
                (submission.idempotency_key,),
            ).fetchone()
            if existing is None:
                raise StorageUnavailable("idempotent job insert did not persist")
            record = self._job(existing)
            if not _submission_matches(record, submission):
                raise ConflictError(
                    "idempotency key already belongs to a different job request"
                )
            return record

    def get_job(self, job_id: str) -> JobRecord:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE id::text = %s", (job_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"job not found: {job_id}")
        return self._job(row)

    def _authorize_audio_artifact_attempt(
        self, job_id: str, *, worker_id: str, attempt: int, lease_token: str,
        runtime_instance_epoch: str,
    ) -> None:
        """Call-time check, not a grant or per-Worker Bearer authentication.

        Future binary transfer/publication must revalidate current authority.
        """
        _validate_audio_attempt_arguments(
            job_id, worker_id, attempt, lease_token, runtime_instance_epoch
        )
        with self._transaction() as connection:
            # Preserve the global Job -> Worker lock order. Neither lookup
            # mutates state, and the clock is sampled only after lock waits.
            job_row = connection.execute(
                "SELECT * FROM jobs WHERE id::text = %s FOR UPDATE", (job_id,)
            ).fetchone()
            if job_row is None:
                raise ConflictError("audio artifact authority rejected")
            worker_row = connection.execute(
                "SELECT * FROM workers WHERE id = %s FOR UPDATE", (worker_id,)
            ).fetchone()
            if worker_row is None:
                raise ConflictError("audio artifact authority rejected")
            try:
                self._assert_audio_epoch_not_fenced(
                    connection, worker_id, runtime_instance_epoch
                )
                job = self._job(job_row)
                worker = self._worker(worker_row)
            except (AttributeError, KeyError, TypeError, ValueError, ConflictError):
                raise ConflictError("audio artifact authority rejected") from None
            _assert_audio_artifact_attempt(
                job, worker, job_id=job_id, worker_id=worker_id, attempt=attempt,
                lease_token=lease_token, runtime_instance_epoch=runtime_instance_epoch,
                now=utc_now(),
            )

    def get_job_by_idempotency_key(
        self, key: str, *, admission_deadline_at: datetime | None = None
    ) -> JobRecord | None:
        # Fresh primary snapshot after the original submission thread has
        # stopped. A missing row is not inferred from a failed connection.
        try:
            with self._connection(admission_deadline_at=admission_deadline_at) as connection:
                row = connection.execute(
                    "SELECT * FROM jobs WHERE idempotency_key = %s", (key,)
                ).fetchone()
        except RepositoryError:
            raise
        except Exception as exc:
            raise StorageUnavailable("idempotent job readback unavailable") from exc
        return None if row is None else self._job(row)

    def append_job_event(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_token: str,
        runtime_instance_epoch: str | None,
        kind: str,
        payload: Mapping[str, object],
        now: datetime | None = None,
    ) -> JobEventRecord:
        timestamp = _aware(now)
        normalized_kind = str(kind).strip()
        if not normalized_kind:
            raise ValueError("event kind must not be blank")
        size = _event_size(normalized_kind, payload)
        if size > self.event_max_payload_bytes:
            raise EventBufferFull("job event payload exceeds the configured limit")

        with self._transaction() as connection:
            job_row = connection.execute(
                "SELECT * FROM jobs WHERE id::text = %s FOR UPDATE",
                (job_id,),
            ).fetchone()
            if job_row is None:
                raise NotFoundError(f"job not found: {job_id}")
            self._assert_running_row(
                job_row,
                worker_id=worker_id,
                lease_token=lease_token,
                runtime_instance_epoch=runtime_instance_epoch,
                now=timestamp,
            )
            worker_row = connection.execute(
                "SELECT * FROM workers WHERE id = %s FOR UPDATE", (worker_id,)
            ).fetchone()
            if worker_row is None:
                raise ConflictError("audio artifact authority rejected")
            self._assert_audio_epoch_not_fenced(
                connection, worker_id, runtime_instance_epoch, timestamp
            )
            claimed_epoch = job_row.get("claimed_runtime_instance_epoch")
            if runtime_instance_epoch != claimed_epoch:
                raise ConflictError(f"job {job_id} runtime instance is stale")

            aggregate = connection.execute(
                """
                SELECT
                    count(*)::integer AS event_count,
                    COALESCE(sum(payload_bytes), 0)::bigint AS total_bytes,
                    COALESCE(max(sequence), 0)::bigint AS last_sequence
                FROM job_events
                WHERE job_id = %s
                """,
                (job_row["id"],),
            ).fetchone()
            if int(aggregate["event_count"]) >= self.event_max_count:
                raise EventBufferFull("job event count exceeds the configured limit")
            if int(aggregate["total_bytes"]) + size > self.event_max_total_bytes:
                raise EventBufferFull("job event buffer exceeds the configured byte limit")
            sequence = int(aggregate["last_sequence"]) + 1

            row = connection.execute(
                """
                INSERT INTO job_events (
                    job_id, sequence, attempt, worker_id,
                    runtime_instance_epoch, kind, payload,
                    payload_bytes, created_at
                ) VALUES (
                    %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s
                )
                RETURNING *
                """,
                (
                    job_row["id"],
                    sequence,
                    int(job_row["attempts"]),
                    worker_id,
                    claimed_epoch,
                    normalized_kind,
                    _json(dict(payload)),
                    size,
                    timestamp,
                ),
            ).fetchone()
            return self._event(row)

    def list_job_events(
        self,
        job_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 128,
    ) -> list[JobEventRecord]:
        if type(after_sequence) is not int or after_sequence < 0:
            raise ValueError("after_sequence must be a non-negative integer")
        if type(limit) is not int or not 1 <= limit <= self.event_max_read:
            raise ValueError("event read limit is outside the configured bound")
        with self._connection() as connection:
            exists = connection.execute(
                "SELECT 1 FROM jobs WHERE id::text = %s",
                (job_id,),
            ).fetchone()
            if exists is None:
                raise NotFoundError(f"job not found: {job_id}")
            rows = connection.execute(
                """
                SELECT *
                FROM job_events
                WHERE job_id::text = %s
                  AND sequence > %s
                ORDER BY sequence ASC
                LIMIT %s
                """,
                (job_id, after_sequence, limit),
            ).fetchall()
        return [self._event(row) for row in rows]

    def claim_next_job(
        self,
        worker_id: str,
        *,
        runtime_instance_epoch: str | None = None,
        now: datetime | None = None,
    ) -> JobRecord | None:
        timestamp = _aware(now)
        cutoff = timestamp - timedelta(seconds=self.worker_ttl_seconds)
        with self._transaction() as connection:
            worker_row = connection.execute(
                "SELECT * FROM workers WHERE id = %s", (worker_id,)
            ).fetchone()
            if worker_row is None:
                raise NotFoundError(f"worker not found: {worker_id}")
            worker = self._worker(worker_row)
            _assert_worker_epoch(worker, runtime_instance_epoch)
            self._assert_audio_epoch_not_fenced(
                connection, worker_id, runtime_instance_epoch, timestamp
            )
            if worker.last_seen_at < cutoff:
                connection.execute(
                    """
                    UPDATE workers
                    SET state = 'offline',
                        metadata = metadata || jsonb_build_object(
                            %s::text,
                            CASE WHEN state = 'online'
                                 THEN %s::text ELSE 'draining_expired' END
                        ),
                        updated_at = %s
                    WHERE id = %s AND state <> 'offline' AND last_seen_at < %s
                    """,
                    (
                        OFFLINE_REASON_KEY, RECOVERABLE_OFFLINE_REASON,
                        timestamp, worker_id, cutoff,
                    ),
                )
                return None
            if worker.state is not WorkerState.ONLINE:
                return None

            row = connection.execute(
                """
                SELECT j.*
                FROM jobs AS j
                WHERE j.status = 'queued'
                  AND j.available_at <= %s
                  AND (j.deadline_at IS NULL OR j.deadline_at > %s)
                  AND j.attempts < j.max_attempts
                  AND %s::jsonb ? j.capability
                  AND (
                      j.requirements->>'worker_class' IS NULL
                      OR j.requirements->>'worker_class' = %s
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM jsonb_array_elements_text(
                          COALESCE(j.requirements->'required_capabilities', '[]'::jsonb)
                      ) AS required(capability)
                      WHERE NOT (%s::jsonb ? required.capability)
                  )
                  AND COALESCE(j.requirements->'required_labels', '{}'::jsonb) <@ %s::jsonb
                  AND NOT EXISTS (
                      SELECT 1
                      FROM jsonb_array_elements_text(
                          COALESCE(j.requirements->'required_gpu_uuids', '[]'::jsonb)
                      ) AS required(uuid)
                      WHERE NOT (%s::jsonb ? required.uuid)
                  )
                  AND %s >= COALESCE(
                      (j.requirements->>'min_gpu_count')::integer, 0
                  )
                  AND %s >= COALESCE(
                      (j.requirements->>'min_total_vram_mb')::integer, 0
                  )
                  AND %s >= COALESCE(
                      (j.requirements->>'min_single_gpu_vram_mb')::integer, 0
                  )
                  AND (
                      (
                          j.serving IS NULL
                          AND NOT EXISTS (
                              SELECT 1
                              FROM jsonb_array_elements(
                                  COALESCE(%s::jsonb->'contracts', '[]'::jsonb)
                              ) AS covered(item)
                              WHERE covered.item->'contract'->>'capability'
                                    = j.capability
                          )
                      )
                      OR (
                          %s::jsonb IS NOT NULL
                          AND %s::jsonb->>'deployment_revision'
                              = j.serving->>'deployment_revision'
                          AND EXISTS (
                              SELECT 1
                              FROM jsonb_array_elements(
                                  COALESCE(%s::jsonb->'contracts', '[]'::jsonb)
                              ) AS advertised(item)
                              WHERE advertised.item->>'serving_contract_revision'
                                    = j.serving->>'serving_contract_revision'
                                AND advertised.item->'contract'->>'capability'
                                    = j.serving->>'capability'
                                AND advertised.item->'contract'->>'operation_schema'
                                    = j.serving->>'operation_schema'
                          )
                      )
                  )
                  AND (
                      SELECT w.active_jobs < w.max_concurrency
                      FROM workers AS w
                      WHERE w.id = %s
                  )
                ORDER BY j.priority DESC, j.sequence ASC
                FOR UPDATE SKIP LOCKED
                LIMIT 1
                """,
                (
                    timestamp,
                    timestamp,
                    _json(sorted(worker.spec.capabilities)),
                    worker.spec.worker_class,
                    _json(sorted(worker.spec.capabilities)),
                    _json(dict(worker.spec.labels)),
                    _json(list(worker.spec.gpu_uuids)),
                    worker.spec.resources.gpu_count,
                    worker.spec.resources.total_vram_mb,
                    worker.spec.resources.max_single_gpu_vram_mb,
                    _json_optional(
                        None if worker.serving is None else worker.serving.to_dict()
                    ),
                    _json_optional(
                        None if worker.serving is None else worker.serving.to_dict()
                    ),
                    _json_optional(
                        None if worker.serving is None else worker.serving.to_dict()
                    ),
                    _json_optional(
                        None if worker.serving is None else worker.serving.to_dict()
                    ),
                    worker_id,
                ),
            ).fetchone()
            if row is None:
                return None

            locked_worker_row = connection.execute(
                "SELECT * FROM workers WHERE id = %s FOR UPDATE", (worker_id,)
            ).fetchone()
            if locked_worker_row is None:
                raise NotFoundError(f"worker not found: {worker_id}")
            locked_worker = self._worker(locked_worker_row)
            _assert_worker_epoch(locked_worker, runtime_instance_epoch)
            self._assert_audio_epoch_not_fenced(
                connection, worker_id, runtime_instance_epoch, timestamp
            )
            if (
                locked_worker.last_seen_at < cutoff
                or locked_worker.state is not WorkerState.ONLINE
                or locked_worker.active_jobs >= locked_worker.max_concurrency
            ):
                return None

            selected = self._job(row)
            if (
                selected.capability not in locked_worker.spec.capabilities
                or not worker_matches(locked_worker.spec, selected.requirements)
                or not worker_serving_accepts_job(locked_worker.serving, selected.capability, selected.serving)
            ):
                return None

            lease_token = str(uuid4())
            claimed_deployment_revision = (
                None
                if selected.serving is None
                else locked_worker.serving.deployment_revision
            )
            claimed_serving_contract_revision = (
                None
                if selected.serving is None
                else selected.serving.serving_contract_revision
            )
            claimed_runtime_instance_epoch = (
                None
                if selected.serving is None
                else locked_worker.serving.runtime_instance.epoch
            )
            claimed_row = connection.execute(
                """
                UPDATE jobs
                SET status = 'running',
                    attempts = attempts + 1,
                    assigned_worker_id = %s,
                    lease_token = %s,
                    lease_expires_at = %s,
                    claimed_deployment_revision = %s,
                    claimed_serving_contract_revision = %s,
                    claimed_runtime_instance_epoch = %s,
                    started_at = %s,
                    finished_at = NULL,
                    error = NULL,
                    updated_at = %s
                WHERE id = %s
                RETURNING *
                """,
                (
                    worker_id,
                    lease_token,
                    _lease_expiry(
                        timestamp,
                        _lease_seconds_for(
                            self.lease_seconds, self.audio_lease_seconds,
                            selected.capability,
                        ),
                        selected.deadline_at,
                    ),
                    claimed_deployment_revision,
                    claimed_serving_contract_revision,
                    claimed_runtime_instance_epoch,
                    timestamp,
                    timestamp,
                    row["id"],
                ),
            ).fetchone()
            connection.execute(
                """
                UPDATE workers
                SET active_jobs = active_jobs + 1, updated_at = %s
                WHERE id = %s
                """,
                (timestamp, worker_id),
            )
            return self._job(claimed_row)

    @staticmethod
    def _assert_running_row(
        row: dict[str, Any],
        *,
        worker_id: str,
        lease_token: str,
        runtime_instance_epoch: str | None,
        now: datetime,
    ) -> None:
        if row["status"] != JobStatus.RUNNING.value:
            raise ConflictError(f"job {row['id']} is not running")
        if row.get("assigned_worker_id") != worker_id:
            raise ConflictError(f"job {row['id']} is assigned to another worker")
        if not lease_token or row.get("lease_token") != lease_token:
            raise ConflictError(f"job {row['id']} lease token is stale")
        expires_at = row.get("lease_expires_at")
        if expires_at is None or expires_at <= now:
            raise ConflictError(f"job {row['id']} lease has expired")
        deadline_at = row.get("deadline_at")
        if deadline_at is not None and deadline_at <= now:
            raise ConflictError(f"job {row['id']} deadline has expired")
        claimed_epoch = row.get("claimed_runtime_instance_epoch")
        if claimed_epoch is not None and runtime_instance_epoch != claimed_epoch:
            raise ConflictError(f"job {row['id']} runtime instance is stale")

    @staticmethod
    def _decrement_worker(
        connection: Any, worker_id: str | None, timestamp: datetime
    ) -> None:
        if worker_id is None:
            return
        connection.execute(
            """
            UPDATE workers
            SET active_jobs = GREATEST(active_jobs - 1, 0), updated_at = %s
            WHERE id = %s
            """,
            (timestamp, worker_id),
        )

    def complete_job(
        self,
        job_id: str,
        result: JobResult,
        *,
        worker_id: str,
        lease_token: str,
        runtime_instance_epoch: str | None = None,
        now: datetime | None = None,
    ) -> JobRecord:
        if not isinstance(result, JobResult):
            raise TypeError("result must be JobResult")
        timestamp = _aware(now)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE id::text = %s FOR UPDATE", (job_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"job not found: {job_id}")
            self._assert_running_row(
                row,
                worker_id=worker_id,
                lease_token=lease_token,
                runtime_instance_epoch=runtime_instance_epoch,
                now=timestamp,
            )
            worker_row = connection.execute(
                "SELECT * FROM workers WHERE id = %s FOR UPDATE", (worker_id,)
            ).fetchone()
            if worker_row is None:
                raise ConflictError(f"job {job_id} is assigned to another worker")
            self._assert_audio_epoch_not_fenced(
                connection, worker_id, runtime_instance_epoch, timestamp
            )
            updated = connection.execute(
                """
                UPDATE jobs
                SET status = 'succeeded',
                    result = %s,
                    error = NULL,
                    assigned_worker_id = NULL,
                    lease_token = NULL,
                    lease_expires_at = NULL,
                    finished_at = %s,
                    updated_at = %s
                WHERE id = %s
                RETURNING *
                """,
                (
                    _json(job_result_to_dict(result)),
                    timestamp,
                    timestamp,
                    row["id"],
                ),
            ).fetchone()
            self._decrement_worker(connection, worker_id, timestamp)
            return self._job(updated)

    def fail_job(
        self,
        job_id: str,
        error: str | dict[str, object],
        *,
        retryable: bool,
        worker_id: str,
        lease_token: str,
        runtime_instance_epoch: str | None = None,
        now: datetime | None = None,
    ) -> JobRecord:
        timestamp = _aware(now)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE id::text = %s FOR UPDATE", (job_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"job not found: {job_id}")
            self._assert_running_row(
                row,
                worker_id=worker_id,
                lease_token=lease_token,
                runtime_instance_epoch=runtime_instance_epoch,
                now=timestamp,
            )
            worker_row = connection.execute(
                "SELECT * FROM workers WHERE id = %s FOR UPDATE", (worker_id,)
            ).fetchone()
            if worker_row is None:
                raise ConflictError(f"job {job_id} is assigned to another worker")
            self._assert_audio_epoch_not_fenced(
                connection, worker_id, runtime_instance_epoch, timestamp
            )
            should_retry = (
                retryable
                and int(row["attempts"]) < int(row["max_attempts"])
                and (row.get("deadline_at") is None or row["deadline_at"] > timestamp)
            )
            status = JobStatus.QUEUED if should_retry else JobStatus.FAILED
            updated = connection.execute(
                """
                UPDATE jobs
                SET status = %s,
                    error = %s,
                    assigned_worker_id = NULL,
                    lease_token = NULL,
                    lease_expires_at = NULL,
                    started_at = CASE WHEN %s THEN NULL ELSE started_at END,
                    finished_at = CASE WHEN %s THEN NULL ELSE %s END,
                    available_at = CASE WHEN %s THEN %s ELSE available_at END,
                    claimed_deployment_revision = CASE
                        WHEN %s THEN NULL ELSE claimed_deployment_revision END,
                    claimed_serving_contract_revision = CASE
                        WHEN %s THEN NULL ELSE claimed_serving_contract_revision END,
                    claimed_runtime_instance_epoch = CASE
                        WHEN %s THEN NULL ELSE claimed_runtime_instance_epoch END,
                    updated_at = %s
                WHERE id = %s
                RETURNING *
                """,
                (
                    status.value,
                    _json(_failure_payload(error, retryable=should_retry)),
                    should_retry,
                    should_retry,
                    timestamp,
                    should_retry,
                    timestamp,
                    should_retry,
                    should_retry,
                    should_retry,
                    timestamp,
                    row["id"],
                ),
            ).fetchone()
            self._decrement_worker(connection, worker_id, timestamp)
            return self._job(updated)

    def cancel_job(
        self, job_id: str, *, now: datetime | None = None,
        admission_deadline_at: datetime | None = None,
    ) -> JobRecord:
        timestamp = _aware(now)
        with self._transaction(admission_deadline_at=admission_deadline_at) as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE id::text = %s FOR UPDATE", (job_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"job not found: {job_id}")
            job = self._job(row)
            if job.status in {
                JobStatus.SUCCEEDED,
                JobStatus.FAILED,
                JobStatus.CANCELLED,
            }:
                return job
            updated = connection.execute(
                """
                UPDATE jobs
                SET status = 'cancelled',
                    assigned_worker_id = NULL,
                    lease_token = NULL,
                    lease_expires_at = NULL,
                    finished_at = %s,
                    updated_at = %s
                WHERE id = %s
                RETURNING *
                """,
                (timestamp, timestamp, row["id"]),
            ).fetchone()
            if job.status is JobStatus.RUNNING:
                self._decrement_worker(
                    connection, job.assigned_worker_id, timestamp
                )
            return self._job(updated)

    def recover_expired_jobs(
        self, *, now: datetime | None = None
    ) -> list[JobRecord]:
        timestamp = _aware(now)
        recovered: list[JobRecord] = []
        while True:
            with self._transaction() as connection:
                row = connection.execute(
                    """
                    SELECT *
                    FROM jobs
                    WHERE status = 'running'
                      AND lease_expires_at <= %s
                    ORDER BY sequence ASC
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                    """,
                    (timestamp,),
                ).fetchone()
                if row is None:
                    return recovered

                deadline_expired = (
                    row.get("deadline_at") is not None
                    and row["deadline_at"] <= timestamp
                )
                should_retry = (
                    not deadline_expired
                    and int(row["attempts"]) < int(row["max_attempts"])
                )
                status = JobStatus.QUEUED if should_retry else JobStatus.FAILED
                updated = connection.execute(
                    """
                    UPDATE jobs
                    SET status = %s,
                        error = %s,
                        assigned_worker_id = NULL,
                        lease_token = NULL,
                        lease_expires_at = NULL,
                        started_at = CASE WHEN %s THEN NULL ELSE started_at END,
                        finished_at = CASE WHEN %s THEN NULL ELSE %s END,
                        available_at = CASE WHEN %s THEN %s ELSE available_at END,
                        claimed_deployment_revision = CASE
                            WHEN %s THEN NULL ELSE claimed_deployment_revision END,
                        claimed_serving_contract_revision = CASE
                            WHEN %s THEN NULL ELSE claimed_serving_contract_revision END,
                        claimed_runtime_instance_epoch = CASE
                            WHEN %s THEN NULL ELSE claimed_runtime_instance_epoch END,
                        updated_at = %s
                    WHERE id = %s
                    RETURNING *
                    """,
                    (
                        status.value,
                        _json(
                            (
                                {
                                    "type": "deadline_expired",
                                    "message": "job deadline expired",
                                    "retryable": False,
                                }
                                if deadline_expired
                                else {
                                    "message": "worker lease expired",
                                    "retryable": should_retry,
                                }
                            )
                        ),
                        should_retry,
                        should_retry,
                        timestamp,
                        should_retry,
                        timestamp,
                        should_retry,
                        should_retry,
                        should_retry,
                        timestamp,
                        row["id"],
                    ),
                ).fetchone()
                self._decrement_worker(
                    connection, row.get("assigned_worker_id"), timestamp
                )
                recovered.append(self._job(updated))


__all__ = ["PostgresControlRepository"]
