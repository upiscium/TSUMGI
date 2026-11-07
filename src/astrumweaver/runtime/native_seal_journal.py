"""#240 R5a — source-only, non-authoritative durable seal audit.

Uses the SAME local SQLite transaction database as the existing B1a GPU
reservation ledger, so synthetic seal stages cannot race a reservation
update inside the journal. It does NOT contact PID1, inspect real cgroups,
authenticate its caller, seal a Linux subtree, or release a GPU.

A recorded claim, including a complete and hash-chained sequence, is NOT a
trusted cleanup receipt. A future separately reviewed privileged adapter
must supply kernel/PID1 evidence and irrevocable launch exclusion.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any

from .native_cgroup_contract import (
    NativeLaunchRequest,
    NativeRunUnitIdentity,
)
from .native_cgroup_ledger import (
    NativeLedgerPhase,
    NativeRunReservationLedger,
)
from .native_nested_cgroup import (
    OfflineCgroupObjectKey,
    OfflineNestedCgroupSamples,
    validate_offline_nested_empty_candidate,
)
from .native_systemd_port import (
    NativeManagerPortError,
    ReviewedNativeCommand,
)

_VERSION = "1"
_MAX_CONTEXT = 8192
_MAX_DETAILS = 8192
_ZERO_HASH = "0" * 64


class NativeSealJournalError(RuntimeError):
    """Inconsistent, ambiguous or out-of-order native seal audit claim."""


class NativeSealStage(StrEnum):
    LAUNCH_DISABLED = "launch-disabled"
    NATIVE_EMPTY_OBSERVED = "native-empty-observed"
    NATIVE_REMOVED = "native-removed"
    PARENT_STOPPED = "parent-stopped"


_STAGES = tuple(NativeSealStage)


def _canonical(data: object) -> str:
    try:
        result = json.dumps(
            data, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        )
    except (TypeError, ValueError) as exc:
        raise NativeSealJournalError("unsupported seal claim") from exc
    if len(result.encode("utf-8")) > _MAX_DETAILS:
        raise NativeSealJournalError("seal claim is too large")
    return result


def _sha(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class OfflineNativeSealContext:
    """Immutable offline synthetic run/generation/cgroup key binding."""

    request: NativeLaunchRequest
    initial_unit: NativeRunUnitIdentity
    native_key: OfflineCgroupObjectKey
    events_key: OfflineCgroupObjectKey

    def __post_init__(self) -> None:
        req, unit = self.request, self.initial_unit
        if (
            not isinstance(req, NativeLaunchRequest)
            or not isinstance(unit, NativeRunUnitIdentity)
            or not isinstance(self.native_key, OfflineCgroupObjectKey)
            or not isinstance(self.events_key, OfflineCgroupObjectKey)
            or unit.run_id != req.run_id
            or unit.unit_name != req.unit_name
            or unit.cgroup_path != req.cgroup_path
            or unit.worker_epoch != req.worker_epoch
            or unit.deployment_revision != req.deployment_revision
        ):
            raise NativeSealJournalError("seal context run or generation mismatch")

    @property
    def run_id(self) -> str:
        return self.request.run_id

    def serialization(self) -> str:
        return _canonical(asdict(self))


@dataclass(frozen=True, slots=True)
class OfflineSealAuditRecord:
    """Stage persisted in SQLite, explicitly NOT a GPU-release token."""

    run_id: str
    ordinal: int
    stage: NativeSealStage
    chain_digest: str


class OfflineNativeSealJournal:
    """Append-only, same-database staged *claims* without any release API.

    A caller must supply an existing B1a reservation in UNKNOWN_FENCED state.
    This deliberately calls the package-private B1a transaction connection:
    a future broker design must audit the privileged trust boundary before
    exposing any such interface to untrusted processes.
    """

    def __init__(self, reservations: NativeRunReservationLedger) -> None:
        if not isinstance(reservations, NativeRunReservationLedger):
            raise NativeSealJournalError("B1a native reservation ledger is required")
        self._reservations = reservations
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return self._reservations._connect()  # internal-only; not public IPC

    def _initialize(self) -> None:
        with closing(self._connect()) as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                self._reservations._assert_consistent_owners(db)
                db.execute(
                    "CREATE TABLE IF NOT EXISTS seal_schema("
                    "key TEXT PRIMARY KEY, version TEXT NOT NULL)"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS seal_runs("
                    "run_id TEXT PRIMARY KEY REFERENCES native_runs(run_id),"
                    "context_json TEXT NOT NULL,"
                    "context_sha TEXT NOT NULL)"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS seal_events("
                    "run_id TEXT NOT NULL REFERENCES seal_runs(run_id),"
                    "ordinal INTEGER NOT NULL,"
                    "stage TEXT NOT NULL,"
                    "details_json TEXT NOT NULL,"
                    "previous_sha TEXT NOT NULL,"
                    "chain_sha TEXT NOT NULL,"
                    "PRIMARY KEY(run_id,ordinal), UNIQUE(run_id,stage))"
                )
                existing = db.execute(
                    "SELECT version FROM seal_schema WHERE key='format'"
                ).fetchone()
                if existing is None:
                    if (
                        db.execute("SELECT count(*) FROM seal_runs").fetchone()[0]
                        or db.execute("SELECT count(*) FROM seal_events").fetchone()[0]
                    ):
                        raise NativeSealJournalError(
                            "native seal schema missing with existing audit rows"
                        )
                    db.execute(
                        "INSERT INTO seal_schema(key,version) VALUES('format',?)",
                        (_VERSION,),
                    )
                elif existing != (_VERSION,):
                    raise NativeSealJournalError("unsupported native seal audit schema")
                self._check_integrity(db)
                db.commit()
            except (NativeSealJournalError, sqlite3.DatabaseError) as exc:
                db.rollback()
                if isinstance(exc, NativeSealJournalError):
                    raise
                raise NativeSealJournalError("native seal schema unavailable") from exc

    def _check_integrity(self, db: sqlite3.Connection) -> None:
        """Detect corruption, missing/gapped transitions and broken chain.

        The digest chain detects accidental/unreviewed mutations only.
        It is NOT a signature or a defense against a privileged attacker
        who can rewrite the database and recompute hashes.
        """
        if db.execute(
            "SELECT version FROM seal_schema WHERE key='format'"
        ).fetchone() != (_VERSION,):
            raise NativeSealJournalError("native seal schema changed")
        runs = db.execute(
            "SELECT run_id,context_json,context_sha FROM seal_runs ORDER BY run_id"
        ).fetchall()
        if len(runs) > 256:
            raise NativeSealJournalError("native seal run limit exceeded")
        for run_id, serialized, digest in runs:
            if (
                type(serialized) is not str
                or len(serialized.encode("utf-8")) > _MAX_CONTEXT
                or _sha(serialized) != digest
            ):
                raise NativeSealJournalError("native seal context digest mismatch")
            try:
                ctx = json.loads(serialized)
                if ctx["request"]["run_id"] != run_id or _canonical(ctx) != serialized:
                    raise NativeSealJournalError("native seal context mismatch")
            except (KeyError, ValueError, TypeError) as exc:
                raise NativeSealJournalError("native seal context corrupted") from exc

            manifest = db.execute(
                "SELECT request_json,phase FROM native_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if (
                manifest is None
                or manifest[0] != _canonical(ctx["request"])
                or manifest[1] != NativeLedgerPhase.UNKNOWN_FENCED.value
            ):
                raise NativeSealJournalError(
                    "native seal claim detached from fenced GPU ownership"
                )
            events = db.execute(
                "SELECT ordinal,stage,details_json,previous_sha,chain_sha "
                "FROM seal_events WHERE run_id=? ORDER BY ordinal", (run_id,),
            ).fetchall()
            if not events or len(events) > len(_STAGES):
                raise NativeSealJournalError("native seal event chain is incomplete")
            previous = _ZERO_HASH
            for ix, (ordinal, stage, details, prev_digest, chained) in enumerate(events):
                if (
                    ordinal != ix
                    or stage != _STAGES[ix].value
                    or type(details) is not str
                    or len(details.encode("utf-8")) > _MAX_DETAILS
                    or prev_digest != previous
                ):
                    raise NativeSealJournalError("native seal stage/order mismatch")
                try:
                    if _canonical(json.loads(details)) != details:
                        raise NativeSealJournalError("native seal details changed")
                except (TypeError, ValueError) as exc:
                    raise NativeSealJournalError("native seal details unreadable") from exc
                calculated = _sha(
                    _canonical({
                        "run_id": run_id,
                        "context_sha": digest,
                        "ordinal": ix,
                        "stage": stage,
                        "details": json.loads(details),
                        "previous_sha": previous,
                    })
                )
                if chained != calculated:
                    raise NativeSealJournalError("native seal chain digest mismatch")
                previous = chained
        stray = db.execute(
            "SELECT count(*) FROM seal_events e LEFT JOIN seal_runs r "
            "ON r.run_id=e.run_id WHERE r.run_id IS NULL"
        ).fetchone()[0]
        if stray:
            raise NativeSealJournalError("orphaned native seal events")

    def _assert_owned(self, db: sqlite3.Connection, ctx: OfflineNativeSealContext) -> None:
        self._reservations._assert_consistent_owners(db)
        row = db.execute(
            "SELECT request_json,phase FROM native_runs WHERE run_id=?",
            (ctx.run_id,),
        ).fetchone()
        if (
            row is None
            or row != (
                self._reservations._serialized_request(ctx.request),
                NativeLedgerPhase.UNKNOWN_FENCED.value,
            )
        ):
            raise NativeSealJournalError("native seal run is not durably fenced")

    def _append(
        self,
        ctx: OfflineNativeSealContext,
        stage: NativeSealStage,
        detail: dict[str, Any],
    ) -> OfflineSealAuditRecord:
        if not isinstance(ctx, OfflineNativeSealContext) or stage not in _STAGES:
            raise NativeSealJournalError("invalid native seal stage or context")
        context = ctx.serialization()
        details = _canonical(detail)
        if len(context.encode("utf-8")) > _MAX_CONTEXT:
            raise NativeSealJournalError("native seal context exceeds bound")
        with closing(self._connect()) as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                self._assert_owned(db, ctx)
                self._check_integrity(db)
                row = db.execute(
                    "SELECT context_json,context_sha FROM seal_runs WHERE run_id=?",
                    (ctx.run_id,),
                ).fetchone()
                if row is None:
                    if stage is not NativeSealStage.LAUNCH_DISABLED:
                        raise NativeSealJournalError(
                            "seal must begin with durable launch exclusion"
                        )
                    db.execute(
                        "INSERT INTO seal_runs(run_id,context_json,context_sha) "
                        "VALUES(?,?,?)",
                        (ctx.run_id,context,_sha(context)),
                    )
                    ordinal, previous = 0, _ZERO_HASH
                else:
                    if row != (context,_sha(context)):
                        raise NativeSealJournalError(
                            "native seal context or InvocationID reused"
                        )
                    audit = db.execute(
                        "SELECT ordinal,stage,chain_sha FROM seal_events "
                        "WHERE run_id=? ORDER BY ordinal DESC LIMIT 1",
                        (ctx.run_id,),
                    ).fetchone()
                    ordinal, previous = audit[0]+1, audit[2]
                    if ordinal >= len(_STAGES) or stage is not _STAGES[ordinal]:
                        raise NativeSealJournalError(
                            "duplicate, skipped or re-ordered seal transition"
                        )
                digest = _sha(
                    _canonical({
                        "run_id": ctx.run_id,
                        "context_sha": _sha(context),
                        "ordinal": ordinal,
                        "stage": stage.value,
                        "details": detail,
                        "previous_sha": previous,
                    })
                )
                db.execute(
                    "INSERT INTO seal_events("
                    "run_id,ordinal,stage,details_json,previous_sha,chain_sha"
                    ") VALUES(?,?,?,?,?,?)",
                    (ctx.run_id,ordinal,stage.value,details,previous,digest),
                )
                db.commit()
                return OfflineSealAuditRecord(
                    run_id=ctx.run_id,ordinal=ordinal,stage=stage,chain_digest=digest,
                )
            except (NativeSealJournalError, sqlite3.DatabaseError) as exc:
                db.rollback()
                if isinstance(exc, NativeSealJournalError):
                    raise
                raise NativeSealJournalError("native seal stage could not commit") from exc

    def record_launch_disabled(
        self,
        ctx: OfflineNativeSealContext,
        *,
        claimed_global_dispatch_disabled: bool,
    ) -> OfflineSealAuditRecord:
        """Record claimed broker-wide launch disable, not enforce it here."""
        if claimed_global_dispatch_disabled is not True:
            raise NativeSealJournalError("broker launch dispatch must first be disabled")
        return self._append(
            ctx,NativeSealStage.LAUNCH_DISABLED,{"launch_disabled":True},
        )

    def record_native_empty(
        self,
        ctx: OfflineNativeSealContext,
        policy: ReviewedNativeCommand,
        samples: OfflineNestedCgroupSamples,
    ) -> OfflineSealAuditRecord:
        """Append a syntactically plausible observation, not kernel evidence."""
        if (
            not isinstance(ctx, OfflineNativeSealContext)
            or not isinstance(samples, OfflineNestedCgroupSamples)
            or samples.native_key_before != ctx.native_key
            or samples.events_key_before != ctx.events_key
        ):
            raise NativeSealJournalError("native seal cgroup identity changed")
        try:
            validate_offline_nested_empty_candidate(
                ctx.request,policy,samples,initial_unit=ctx.initial_unit,
            )
        except NativeManagerPortError as exc:
            raise NativeSealJournalError("native seal empty evidence inconsistent") from exc
        return self._append(
            ctx,NativeSealStage.NATIVE_EMPTY_OBSERVED,
            {
                "native": samples.native_path,
                "events_live": samples.native_events_live.decode("ascii"),
                "events_empty": samples.native_events_empty.decode("ascii"),
                "wrapper_reaped": samples.wrapper_reaped,
                "unit_invocation": ctx.initial_unit.manager_invocation_id,
            },
        )

    def record_subgroup_removed(
        self,
        ctx: OfflineNativeSealContext,
        *,
        claimed_rmdir_succeeded: bool,
        claimed_old_fd_reentry_denied: bool,
        claimed_supervisor_still_alive: bool,
    ) -> OfflineSealAuditRecord:
        """Do not infer removal from a missing path or a StopUnit reply."""
        if (
            claimed_rmdir_succeeded is not True
            or claimed_old_fd_reentry_denied is not True
            or claimed_supervisor_still_alive is not True
        ):
            raise NativeSealJournalError("native subtree not proven sealed")
        return self._append(
            ctx,NativeSealStage.NATIVE_REMOVED,
            {"rmdir":True,"fd_reentry_denied":True,"monitor_alive":True},
        )

    def record_parent_stopped(
        self,
        ctx: OfflineNativeSealContext,
        *,
        reported_invocation: str,
        claimed_inactive: bool,
        claimed_supervisor_reaped: bool,
    ) -> OfflineSealAuditRecord:
        if (
            not isinstance(ctx, OfflineNativeSealContext)
            or reported_invocation != ctx.initial_unit.manager_invocation_id
            or claimed_inactive is not True
            or claimed_supervisor_reaped is not True
        ):
            raise NativeSealJournalError("parent terminal generation unverified")
        return self._append(
            ctx,NativeSealStage.PARENT_STOPPED,
            {"invocation":reported_invocation,"inactive":True,"reaped":True},
        )

    def audit(self, run_id: str) -> tuple[OfflineSealAuditRecord, ...]:
        """Read staged claims for diagnostics. Never changes a GPU owner."""
        if type(run_id) is not str:
            raise NativeSealJournalError("invalid native seal run ID")
        with closing(self._connect()) as db:
            try:
                db.execute("BEGIN")
                self._reservations._assert_consistent_owners(db)
                self._check_integrity(db)
                rows = db.execute(
                    "SELECT ordinal,stage,chain_sha FROM seal_events "
                    "WHERE run_id=? ORDER BY ordinal",(run_id,),
                ).fetchall()
                return tuple(OfflineSealAuditRecord(
                    run_id=run_id,ordinal=i,stage=NativeSealStage(s),chain_digest=h,
                ) for i,s,h in rows)
            except sqlite3.DatabaseError as exc:
                raise NativeSealJournalError("native seal audit unavailable") from exc
