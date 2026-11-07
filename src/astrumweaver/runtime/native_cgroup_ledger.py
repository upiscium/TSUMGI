"""Phase B1a ONLY: local crash-persistent reservations for native runs (#240).

This source is not imported by the Worker and has NO systemd, D-Bus, GPU or
cgroup operations. Only a future authenticated, privileged broker may rely on
its durable state after independent review. No public method releases GPU
ownership: in particular, a client-provided 'populated=0' is never sufficient
to release a reservation.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from contextlib import closing
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from .native_cgroup_contract import (
    NativeLaunchAllowlist,
    NativeLaunchRequest,
    NativeRunContractError,
)

_SCHEMA_VERSION = "1"
_PRIVATE_DIR_MODE = 0o700
_LEDGER_MODE = 0o600
_MAX_REQUEST_BYTES = 8192
_MAX_GPUS_PER_RUN = 16


class NativeLedgerError(RuntimeError):
    """Unsafe, conflicting, or unavailable native-run reservation."""


class NativeLedgerPhase(StrEnum):
    RESERVED = "reserved"
    UNKNOWN_FENCED = "unknown-fenced"


@dataclass(frozen=True, slots=True)
class NativeReservation:
    run_id: str
    unit_name: str
    worker_id: str
    worker_epoch: str
    phase: NativeLedgerPhase
    gpu_uuids: tuple[str, ...]
    caller_uid: int
    newly_reserved: bool
    # An idempotent replay is NOT permission to launch a second service.


class NativeRunReservationLedger:
    """Transactional, no-release reservation ledger for a future broker.

    The broker (NOT this class) must independently authenticate its IPC peer
    and load administrator-owned immutable allowlists. The caller UID argument
    is an asserted input until that broker exists. Use only an already-created,
    private local state directory; do not put this database on NFS.
    """

    def __init__(self, database_path: Path) -> None:
        if not isinstance(database_path, Path):
            raise NativeLedgerError("ledger path must be pathlib.Path")
        if (
            not database_path.is_absolute()
            or database_path.name in {"", ".", ".."}
            or ".." in database_path.parts
        ):
            raise NativeLedgerError("ledger requires a private canonical absolute path")
        if database_path.suffix != ".sqlite3":
            raise NativeLedgerError("ledger database must use .sqlite3")
        self._path = database_path
        self._verify_storage()
        self._initialize()

    def _verify_storage(self) -> None:
        parent = self._path.parent
        try:
            parent_stat = parent.lstat()
            if (
                not stat.S_ISDIR(parent_stat.st_mode)
                or parent_stat.st_uid != os.geteuid()
                or stat.S_IMODE(parent_stat.st_mode) != _PRIVATE_DIR_MODE
            ):
                raise NativeLedgerError("native ledger directory must be owned 0700")
            # A local broker must create and own this directory. Its containing
            # parent hierarchy is managed by the operator, not untrusted IPC.
            flags = os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
            fd = os.open(self._path, flags | os.O_CREAT, _LEDGER_MODE)
            try:
                file_stat = os.fstat(fd)
                if (
                    not stat.S_ISREG(file_stat.st_mode)
                    or file_stat.st_uid != os.geteuid()
                    or stat.S_IMODE(file_stat.st_mode) != _LEDGER_MODE
                    or file_stat.st_nlink != 1
                ):
                    raise NativeLedgerError("native ledger database must be private 0600")
            finally:
                os.close(fd)
        except OSError as exc:
            raise NativeLedgerError("native ledger state path unavailable") from exc

    def _connect(self) -> sqlite3.Connection:
        self._verify_storage()
        try:
            db = sqlite3.connect(str(self._path), timeout=3.0, isolation_level=None)
            db.execute("PRAGMA busy_timeout=3000")
            db.execute("PRAGMA foreign_keys=ON")
            if db.execute("PRAGMA journal_mode=WAL").fetchone() != ("wal",):
                raise NativeLedgerError("native ledger requires local WAL storage")
            db.execute("PRAGMA synchronous=FULL")
            return db
        except (sqlite3.DatabaseError, NativeLedgerError) as exc:
            db.close()
            raise NativeLedgerError("native ledger storage unavailable") from exc

    @classmethod
    def _assert_consistent_owners(cls, db: sqlite3.Connection) -> None:
        """Refuse an incomplete/replaced GPU index before any new reservation.

        SQLite transactions prevent normal partial commits. This additionally
        detects manual corruption or an unintended future migration that drops
        a GPU owner while leaving a live run. It never auto-repairs ownership.
        """
        try:
            version = db.execute(
                "SELECT version FROM ledger_schema WHERE key='format'"
            ).fetchone()
            if version != (_SCHEMA_VERSION,):
                raise NativeLedgerError("native ledger schema identity unavailable")
            owners: dict[str, str] = {}
            rows = db.execute(
                "SELECT run_id,unit_name,worker_id,worker_epoch,phase,"
                "request_json FROM native_runs"
            ).fetchall()
            if len(rows) > 256:
                raise NativeLedgerError("native ledger has too many active runs")
            for run_id, unit_name, worker_id, epoch, phase, serialized in rows:
                if type(serialized) is not str or len(serialized) > _MAX_REQUEST_BYTES:
                    raise NativeLedgerError("native ledger contains oversized declaration")
                data = json.loads(serialized)
                data["gpu_uuids"] = tuple(data["gpu_uuids"])
                request = NativeLaunchRequest(**data)
                if (
                    request.run_id != run_id
                    or request.unit_name != unit_name
                    or request.worker_id != worker_id
                    or request.worker_epoch != epoch
                    or phase not in (
                        NativeLedgerPhase.RESERVED.value,
                        NativeLedgerPhase.UNKNOWN_FENCED.value,
                    )
                    or len(request.gpu_uuids) > _MAX_GPUS_PER_RUN
                    or serialized != cls._serialized_request(request)
                ):
                    raise NativeLedgerError("native ledger run identity is inconsistent")
                for gpu_uuid in request.gpu_uuids:
                    if gpu_uuid in owners:
                        raise NativeLedgerError("native ledger has duplicate GPU ownership")
                    owners[gpu_uuid] = run_id
            recorded = dict(
                db.execute("SELECT gpu_uuid,run_id FROM gpu_owners").fetchall()
            )
            if owners != recorded:
                raise NativeLedgerError("native GPU ownership index is inconsistent")
        except (sqlite3.DatabaseError, ValueError, TypeError, KeyError) as exc:
            raise NativeLedgerError("native ledger ownership is not trustworthy") from exc

    def _initialize(self) -> None:
        with closing(self._connect()) as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "CREATE TABLE IF NOT EXISTS ledger_schema("
                    "key TEXT PRIMARY KEY, version TEXT NOT NULL)"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS native_runs("
                    "run_id TEXT PRIMARY KEY, "
                    "caller_uid INTEGER NOT NULL CHECK(caller_uid>=0), "
                    "request_json TEXT NOT NULL, "
                    "unit_name TEXT NOT NULL UNIQUE, "
                    "worker_id TEXT NOT NULL, "
                    "worker_epoch TEXT NOT NULL, "
                    "phase TEXT NOT NULL CHECK(phase IN "
                    "('reserved','unknown-fenced')))"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS gpu_owners("
                    "gpu_uuid TEXT PRIMARY KEY, "
                    "run_id TEXT NOT NULL REFERENCES native_runs(run_id))"
                )
                row = db.execute(
                    "SELECT version FROM ledger_schema WHERE key='format'"
                ).fetchone()
                if row is None:
                    # A future broker must never infer that a newly created
                    # schema can replace a missing, already populated ledger.
                    if db.execute("SELECT count(*) FROM native_runs").fetchone()[0]:
                        raise NativeLedgerError("missing ledger schema with existing runs")
                    db.execute(
                        "INSERT INTO ledger_schema(key,version) VALUES('format',?)",
                        (_SCHEMA_VERSION,),
                    )
                elif row[0] != _SCHEMA_VERSION:
                    raise NativeLedgerError("unrecognized native ledger schema version")
                self._assert_consistent_owners(db)
                db.commit()
            except (NativeLedgerError, sqlite3.DatabaseError) as exc:
                db.rollback()
                if isinstance(exc, NativeLedgerError):
                    raise
                raise NativeLedgerError("native ledger schema initialization failed") from exc

    @staticmethod
    def _serialized_request(request: NativeLaunchRequest) -> str:
        # This stable exact comparison avoids relaxing an idempotent retry
        # to a different GPU list, model path, epoch, or artifact revision.
        return json.dumps(
            {
                "run_id": request.run_id,
                "worker_id": request.worker_id,
                "worker_epoch": request.worker_epoch,
                "deployment_revision": request.deployment_revision,
                "executable": request.executable,
                "model_ref": request.model_ref,
                "gpu_uuids": request.gpu_uuids,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )

    @staticmethod
    def _validate_peer(caller_uid: int) -> int:
        if type(caller_uid) is not int or caller_uid < 0:
            raise NativeLedgerError("invalid trusted peer UID")
        return caller_uid

    @staticmethod
    def _reservation(row: tuple[Any, ...], *, newly_reserved: bool) -> NativeReservation:
        (
            run_id, unit_name, worker_id, worker_epoch, phase,
            gpu_uuids, caller_uid,
        ) = row
        return NativeReservation(
            run_id=run_id,
            unit_name=unit_name,
            worker_id=worker_id,
            worker_epoch=worker_epoch,
            phase=NativeLedgerPhase(phase),
            gpu_uuids=tuple(json.loads(gpu_uuids)),
            caller_uid=caller_uid,
            newly_reserved=newly_reserved,
        )

    def reserve(
        self,
        request: NativeLaunchRequest,
        allowlist: NativeLaunchAllowlist,
        *,
        trusted_peer_uid: int,
    ) -> NativeReservation:
        """Atomically reserve ALL GPUs, never contacting PID1.

        A duplicate exact request returns newly_reserved=False, but must NOT
        be used by a future broker to retry StartTransientUnit. That requires
        a separate durable start-intent/reconciliation protocol.
        """
        if (
            not isinstance(request, NativeLaunchRequest)
            or not isinstance(allowlist, NativeLaunchAllowlist)
            or not allowlist.permits(request)
        ):
            raise NativeLedgerError("unapproved native launch request")
        caller = self._validate_peer(trusted_peer_uid)
        if len(request.gpu_uuids) > _MAX_GPUS_PER_RUN:
            raise NativeLedgerError("native GPU set exceeds reserved capacity")
        canonical = self._serialized_request(request)
        if len(canonical) > _MAX_REQUEST_BYTES:
            raise NativeLedgerError("native launch declaration is too large")
        with closing(self._connect()) as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                self._assert_consistent_owners(db)
                row = db.execute(
                    "SELECT caller_uid,request_json FROM native_runs WHERE run_id=?",
                    (request.run_id,),
                ).fetchone()
                if row is not None:
                    if row != (caller, canonical):
                        raise NativeLedgerError("run ID reused for different ownership")
                    record = db.execute(
                        "SELECT run_id,unit_name,worker_id,worker_epoch,"
                        "phase,request_json,caller_uid "
                        "FROM native_runs WHERE run_id=?", (request.run_id,),
                    ).fetchone()
                    db.commit()
                    stored = json.loads(record[5])
                    return self._reservation(
                        (record[0], record[1], record[2], record[3],
                         record[4], json.dumps(stored["gpu_uuids"]), record[6]),
                        newly_reserved=False,
                    )
                db.execute(
                    "INSERT INTO native_runs("
                    "run_id,caller_uid,request_json,unit_name,worker_id,"
                    "worker_epoch,phase) VALUES(?,?,?,?,?,?,?)",
                    (request.run_id, caller, canonical, request.unit_name,
                     request.worker_id, request.worker_epoch,
                     NativeLedgerPhase.RESERVED.value),
                )
                for gpu_uuid in request.gpu_uuids:
                    db.execute(
                        "INSERT INTO gpu_owners(gpu_uuid,run_id) VALUES(?,?)",
                        (gpu_uuid, request.run_id),
                    )
                db.commit()
                return NativeReservation(
                    run_id=request.run_id, unit_name=request.unit_name,
                    worker_id=request.worker_id, worker_epoch=request.worker_epoch,
                    phase=NativeLedgerPhase.RESERVED,
                    gpu_uuids=request.gpu_uuids, caller_uid=caller,
                    newly_reserved=True,
                )
            except NativeLedgerError:
                db.rollback()
                raise
            except sqlite3.IntegrityError as exc:
                db.rollback()
                raise NativeLedgerError(
                    "native GPU or run unit is already durably reserved"
                ) from exc
            except sqlite3.DatabaseError as exc:
                db.rollback()
                raise NativeLedgerError("native reservation write failed") from exc

    def mark_start_intent(self, run_id: str, *, trusted_peer_uid: int) -> None:
        """Durably fence BEFORE any future StartTransientUnit call.

        A lost manager acknowledgement cannot be retried as a new launch.
        B1a has no PID1 adapter or release API; the broker must reconcile.
        """
        caller = self._validate_peer(trusted_peer_uid)
        with closing(self._connect()) as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                self._assert_consistent_owners(db)
                changed = db.execute(
                    "UPDATE native_runs SET phase=? WHERE run_id=?"
                    " AND caller_uid=? AND phase=?",
                    (NativeLedgerPhase.UNKNOWN_FENCED.value, run_id,
                     caller, NativeLedgerPhase.RESERVED.value),
                ).rowcount
                if changed != 1:
                    raise NativeLedgerError("start intent missing, stale, or already dispatched")
                db.commit()
            except (NativeLedgerError, sqlite3.DatabaseError) as exc:
                db.rollback()
                if isinstance(exc, NativeLedgerError):
                    raise
                raise NativeLedgerError("unable to persist native launch intent") from exc

    def fence_on_broker_restart(self) -> int:
        """Fail-closed startup reconciliation: never release any GPU.

        Must be called by the future broker before accepting *new* launches,
        using broker-owned durable authority. This class doesn't start units.
        """
        with closing(self._connect()) as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                self._assert_consistent_owners(db)
                changed = db.execute(
                    "UPDATE native_runs SET phase=? WHERE phase=?",
                    (NativeLedgerPhase.UNKNOWN_FENCED.value,
                     NativeLedgerPhase.RESERVED.value),
                ).rowcount
                db.commit()
                return changed
            except sqlite3.DatabaseError as exc:
                db.rollback()
                raise NativeLedgerError("broker restart reconciliation failed") from exc

    def list_reservations(self) -> tuple[NativeReservation, ...]:
        """Read-only diagnostic inventory; never confirms physical cleanup."""
        with closing(self._connect()) as db:
            try:
                db.execute("BEGIN")
                self._assert_consistent_owners(db)
                rows = db.execute(
                    "SELECT run_id,unit_name,worker_id,worker_epoch,phase,"
                    "request_json,caller_uid FROM native_runs ORDER BY run_id"
                ).fetchall()
                results = []
                for row in rows:
                    gpu_uuids = json.loads(row[5])["gpu_uuids"]
                    results.append(self._reservation(
                        (row[0], row[1], row[2], row[3], row[4],
                         json.dumps(gpu_uuids), row[6]), newly_reserved=False,
                    ))
                return tuple(results)
            except (sqlite3.DatabaseError, ValueError, KeyError, NativeRunContractError) as exc:
                raise NativeLedgerError("unable to read consistent native reservations") from exc
