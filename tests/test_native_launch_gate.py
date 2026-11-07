"""#240 R5b: persistent same-transaction GPU-dispatch exclusion only.

All files are disposable local SQLite; no PID1, systemd, GPU, model or
physical cgroup operations. A gate is NOT a trusted launch authenticator.
"""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from astrumweaver.runtime.native_cgroup_contract import (
    NativeLaunchAllowlist,
    NativeLaunchRequest,
    NativeRunUnitIdentity,
)
from astrumweaver.runtime.native_cgroup_ledger import (
    NativeLedgerError,
    NativeLedgerPhase,
    NativeRunReservationLedger,
)
from astrumweaver.runtime.native_nested_cgroup import OfflineCgroupObjectKey
from astrumweaver.runtime.native_seal_journal import (
    NativeSealJournalError,
    NativeSealStage,
    OfflineNativeSealContext,
    OfflineNativeSealJournal,
)

REV = "sha256:" + "a" * 64
BIN = "/nix/store/" + "0" * 32 + "-llama/bin/llama-server"
MODEL = "/models/offline-not-real.gguf"
EPOCH = "99999999-9999-4999-8999-999999999999"
GPU_A = "GPU-00000000-0000-0000-0000-000000000001"
GPU_B = "GPU-00000000-0000-0000-0000-000000000002"
GPU_C = "GPU-00000000-0000-0000-0000-000000000003"


def request(gpus=(GPU_A,)):
    return NativeLaunchRequest(
        run_id=str(uuid4()), worker_id="sirius-test", worker_epoch=EPOCH,
        deployment_revision=REV, executable=BIN, model_ref=MODEL,
        gpu_uuids=gpus,
    )


def allow(req):
    return NativeLaunchAllowlist(
        worker_id=req.worker_id, deployment_revision=req.deployment_revision,
        executable=req.executable, model_ref=req.model_ref,
        gpu_uuids=req.gpu_uuids,
    )


def ready(tmp_path, gpus=(GPU_A,)):
    db = tmp_path / "native-owned.sqlite3"
    req = request(gpus)
    ledger = NativeRunReservationLedger(db)
    assert ledger.reserve(req, allow(req), trusted_peer_uid=1000).newly_reserved
    ledger.mark_start_intent(req.run_id, trusted_peer_uid=1000)
    unit = NativeRunUnitIdentity(
        run_id=req.run_id, worker_epoch=req.worker_epoch,
        deployment_revision=req.deployment_revision,
        unit_name=req.unit_name, cgroup_path=req.cgroup_path,
        manager_invocation_id="f" * 32,
    )
    ctx = OfflineNativeSealContext(
        request=req, initial_unit=unit,
        native_key=OfflineCgroupObjectKey(21, 123456),
        events_key=OfflineCgroupObjectKey(21, 765432),
    )
    journal = OfflineNativeSealJournal(ledger)
    return db, ledger, journal, req, ctx


def test_launch_disabled_audit_atomically_closes_gpu_dispatch(tmp_path):
    db, ledger, journal, req, ctx = ready(tmp_path)
    result = journal.record_launch_disabled(
        ctx, claimed_global_dispatch_disabled=True,
    )
    assert result.stage is NativeSealStage.LAUNCH_DISABLED
    assert ledger.list_launch_fences() == ((GPU_A, req.run_id),)
    assert ledger.list_reservations()[0].phase is NativeLedgerPhase.UNKNOWN_FENCED
    reopened = NativeRunReservationLedger(db)
    assert reopened.list_launch_fences() == ((GPU_A, req.run_id),)
    assert len(OfflineNativeSealJournal(reopened).audit(req.run_id)) == 1
    other = request()
    with pytest.raises(NativeLedgerError, match="dispatch persistently fenced"):
        reopened.reserve(other, allow(other), trusted_peer_uid=1000)
    assert len(reopened.list_reservations()) == 1
    assert not hasattr(reopened, "release")
    assert not hasattr(journal, "release")


def test_same_run_retry_is_observation_never_a_second_launch(tmp_path):
    _, ledger, journal, req, ctx = ready(tmp_path)
    journal.record_launch_disabled(ctx, claimed_global_dispatch_disabled=True)
    old = ledger.reserve(req, allow(req), trusted_peer_uid=1000)
    assert old.newly_reserved is False
    assert old.phase is NativeLedgerPhase.UNKNOWN_FENCED
    with pytest.raises(NativeLedgerError, match="already dispatched"):
        ledger.mark_start_intent(req.run_id, trusted_peer_uid=1000)


def test_all_gpus_of_one_run_become_fenced_in_same_transaction(tmp_path):
    _, ledger, journal, req, ctx = ready(tmp_path, (GPU_A, GPU_B))
    journal.record_launch_disabled(ctx, claimed_global_dispatch_disabled=True)
    assert ledger.list_launch_fences() == (
        (GPU_A, req.run_id), (GPU_B, req.run_id),
    )
    for gpus in ((GPU_A,), (GPU_B,), (GPU_A, GPU_C), (GPU_C, GPU_B)):
        contender = request(gpus)
        with pytest.raises(NativeLedgerError, match="dispatch persistently fenced"):
            ledger.reserve(contender, allow(contender), trusted_peer_uid=1000)
    free = request((GPU_C,))
    assert ledger.reserve(free, allow(free), trusted_peer_uid=1000).newly_reserved
    assert len(ledger.list_reservations()) == 2


def test_corrupt_partial_gpu_dispatch_fence_is_never_repaired(tmp_path):
    db, ledger, journal, _, ctx = ready(tmp_path, (GPU_A, GPU_B))
    journal.record_launch_disabled(ctx, claimed_global_dispatch_disabled=True)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "DELETE FROM native_gpu_dispatch_fences WHERE gpu_uuid=?",
            (GPU_B,),
        )
    with pytest.raises(NativeLedgerError, match="fence is incomplete"):
        ledger.list_launch_fences()
    with pytest.raises(NativeLedgerError, match="fence is incomplete"):
        NativeRunReservationLedger(db)
    contender = request((GPU_C,))
    with pytest.raises(NativeLedgerError):
        ledger.reserve(contender, allow(contender), trusted_peer_uid=1000)


def test_complete_deletion_of_fence_rows_cannot_detach_r5_audit(tmp_path):
    db, ledger, journal, req, ctx = ready(tmp_path)
    journal.record_launch_disabled(ctx, claimed_global_dispatch_disabled=True)
    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM native_gpu_dispatch_fences WHERE run_id=?", (req.run_id,))
    with pytest.raises(NativeLedgerError, match="audit detached"):
        ledger.list_launch_fences()
    with pytest.raises(NativeLedgerError, match="audit detached"):
        ledger.reserve(request(), allow(req), trusted_peer_uid=1000)


def test_missing_dispatch_gate_table_on_version2_reopen_is_not_recreated(tmp_path):
    db, _, _, _, _ = ready(tmp_path)
    # v2 enforces table-presence on restart, even with no fence rows yet.
    with sqlite3.connect(db) as conn:
        conn.execute("DROP TABLE native_gpu_dispatch_fences")
    with pytest.raises(NativeLedgerError, match="gate table missing"):
        NativeRunReservationLedger(db)
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE "
            "name='native_gpu_dispatch_fences'"
        ).fetchone()[0] == 0


def test_unqualified_old_version1_with_live_run_never_silently_upgrades(tmp_path):
    db, _, _, _, _ = ready(tmp_path)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE ledger_schema SET version='1' WHERE key='format'")
    with pytest.raises(NativeLedgerError, match="v1 native ledger has unqualified"):
        NativeRunReservationLedger(db)


def test_empty_version1_can_upgrade_to_v2_with_no_reserved_gpus(tmp_path):
    db = tmp_path / "fresh.sqlite3"
    NativeRunReservationLedger(db)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE ledger_schema SET version='1' WHERE key='format'")
    upgraded = NativeRunReservationLedger(db)
    assert upgraded.list_launch_fences() == ()
    assert upgraded.list_reservations() == ()
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT version FROM ledger_schema WHERE key='format'"
        ).fetchone() == ("2",)


def test_failed_audit_insert_rolls_back_gpu_dispatch_fence_in_same_tx(tmp_path):
    db, ledger, journal, req, ctx = ready(tmp_path)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TRIGGER simulated_seal_insert_failure "
            "BEFORE INSERT ON seal_events "
            "BEGIN SELECT RAISE(ABORT,'synthetic fail'); END"
        )
    with pytest.raises(NativeSealJournalError, match="could not commit"):
        journal.record_launch_disabled(ctx, claimed_global_dispatch_disabled=True)
    assert ledger.list_launch_fences() == ()
    assert journal.audit(req.run_id) == ()
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM seal_runs").fetchone()[0] == 0
        conn.execute("DROP TRIGGER simulated_seal_insert_failure")
    journal.record_launch_disabled(ctx, claimed_global_dispatch_disabled=True)
    assert ledger.list_launch_fences() == ((GPU_A, req.run_id),)
    assert len(journal.audit(req.run_id)) == 1


def test_duplicate_r5_launch_disabled_calls_do_not_insert_second_fence(tmp_path):
    _, ledger, journal, req, ctx = ready(tmp_path)
    journal.record_launch_disabled(ctx, claimed_global_dispatch_disabled=True)
    with pytest.raises(NativeSealJournalError, match="duplicate|skipped|re-ordered"):
        journal.record_launch_disabled(ctx, claimed_global_dispatch_disabled=True)
    assert ledger.list_launch_fences() == ((GPU_A, req.run_id),)
    assert len(journal.audit(req.run_id)) == 1


def test_two_journal_handles_racing_on_same_run_commit_only_one(tmp_path):
    db, ledger, first, req, ctx = ready(tmp_path)
    second = OfflineNativeSealJournal(NativeRunReservationLedger(db))

    def write_once(i):
        j = first if i % 2 else second
        try:
            return j.record_launch_disabled(
                ctx, claimed_global_dispatch_disabled=True,
            )
        except (NativeSealJournalError, NativeLedgerError):
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        out = tuple(pool.map(write_once, range(8)))
    assert sum(item is not None for item in out) == 1
    assert ledger.list_launch_fences() == ((GPU_A, req.run_id),)
    assert len(first.audit(req.run_id)) == 1


@pytest.mark.parametrize("claimed", [False, 0, None, "yes", 1])
def test_untrusted_false_or_truthy_launch_disabled_claim_does_not_write_gate(
    tmp_path, claimed,
):
    _, ledger, journal, _, ctx = ready(tmp_path)
    with pytest.raises(NativeSealJournalError, match="first be disabled"):
        journal.record_launch_disabled(
            ctx, claimed_global_dispatch_disabled=claimed,
        )
    assert ledger.list_launch_fences() == ()
    assert journal.audit(ctx.run_id) == ()


def test_fenced_gpu_stays_fenced_after_broker_restart(tmp_path):
    db, ledger, journal, req, ctx = ready(tmp_path)
    journal.record_launch_disabled(ctx, claimed_global_dispatch_disabled=True)
    assert ledger.fence_on_broker_restart() == 0
    reopened = NativeRunReservationLedger(db)
    assert reopened.list_launch_fences() == ((GPU_A, req.run_id),)
    with pytest.raises(NativeLedgerError, match="dispatch persistently fenced"):
        other = request()
        reopened.reserve(other, allow(other), trusted_peer_uid=1000)
    assert reopened.list_reservations()[0].phase is NativeLedgerPhase.UNKNOWN_FENCED


def test_failed_second_gpu_fence_insert_rolls_back_every_fence_and_audit(tmp_path):
    db, ledger, journal, req, ctx = ready(tmp_path, (GPU_A, GPU_B))
    # The 2nd per-GPU insert fails after the first inserted into an
    # uncommitted SQLite transaction. Neither the first fence nor its
    # corresponding audit event may survive.
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TRIGGER fail_second_gpu_fence "
            "BEFORE INSERT ON native_gpu_dispatch_fences "
            f"WHEN NEW.gpu_uuid='{GPU_B}' "
            "BEGIN SELECT RAISE(ABORT,'synthetic second GPU fail'); END"
        )
    with pytest.raises(NativeSealJournalError, match="could not commit"):
        journal.record_launch_disabled(ctx, claimed_global_dispatch_disabled=True)
    assert ledger.list_launch_fences() == ()
    assert journal.audit(req.run_id) == ()
    assert ledger.list_reservations()[0].phase is NativeLedgerPhase.UNKNOWN_FENCED
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM seal_runs").fetchone()[0] == 0
        conn.execute("DROP TRIGGER fail_second_gpu_fence")
    journal.record_launch_disabled(ctx, claimed_global_dispatch_disabled=True)
    assert ledger.list_launch_fences() == (
        (GPU_A, req.run_id), (GPU_B, req.run_id)
    )
    assert len(journal.audit(req.run_id)) == 1


def test_corrupted_original_gpu_owner_cannot_be_reclaimed_after_close(tmp_path):
    db, ledger, journal, req, ctx = ready(tmp_path)
    journal.record_launch_disabled(ctx, claimed_global_dispatch_disabled=True)
    # Simulate a later accidental future release implementation deleting
    # GPU ownership without deleting the R5b sticky fence. The mismatch
    # must trigger a hard refusal instead of treating GPU as free.
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            "DELETE FROM gpu_owners WHERE run_id=?", (req.run_id,)
        )
    with pytest.raises(NativeLedgerError, match="ownership index"):
        ledger.list_launch_fences()
    with pytest.raises(NativeLedgerError):
        ledger.reserve(request((GPU_B,)), allow(request((GPU_B,))), trusted_peer_uid=1000)


@pytest.mark.asyncio
async def test_offline_broker_port_cannot_dispatch_after_gate_close(tmp_path):
    from astrumweaver.runtime.native_systemd_port import (
        NativeBrokerDispatchCoordinator,
        NativeManagerPortError,
        ReviewedNativeCommand,
    )

    class NeverCalledManager:
        def __init__(self):
            self.calls = []

        async def start_transient_unit(self, plan):
            self.calls.append(plan)
            raise AssertionError("manager must not be called")

    db, ledger, journal, _, ctx = ready(tmp_path)
    journal.record_launch_disabled(ctx, claimed_global_dispatch_disabled=True)
    new_run = request()
    reviewer = ReviewedNativeCommand(
        allowlist=allow(new_run),
        argv=(BIN, "--model", MODEL),
        worker_uid=1000,
    )
    manager = NeverCalledManager()
    another_ledger_instance = NativeRunReservationLedger(db)
    broker = NativeBrokerDispatchCoordinator(another_ledger_instance, manager)
    with pytest.raises(NativeManagerPortError, match="reservation denied"):
        await broker.dispatch_once(
            new_run, reviewer, trusted_peer_uid=1000,
        )
    assert manager.calls == []
    assert ledger.list_launch_fences() == ((GPU_A, ctx.run_id),)


def test_broker_crash_immediately_after_gate_commit_retains_gpu_barrier(tmp_path):
    import json
    import os
    import subprocess
    import sys
    from dataclasses import asdict
    from pathlib import Path

    db, ledger, _, req, ctx = ready(tmp_path)
    program = (
        "import json,os,sys;"
        "from pathlib import Path;"
        "from astrumweaver.runtime.native_cgroup_ledger import NativeRunReservationLedger;"
        "from astrumweaver.runtime.native_cgroup_contract import NativeLaunchRequest,NativeRunUnitIdentity;"
        "from astrumweaver.runtime.native_nested_cgroup import OfflineCgroupObjectKey;"
        "from astrumweaver.runtime.native_seal_journal import OfflineNativeSealContext,OfflineNativeSealJournal;"
        "q=json.loads(sys.argv[2]);q['gpu_uuids']=tuple(q['gpu_uuids']);"
        "r=NativeLaunchRequest(**q);"
        "u=NativeRunUnitIdentity(**json.loads(sys.argv[3]));"
        "ctx=OfflineNativeSealContext(r,u,OfflineCgroupObjectKey(21,123456),OfflineCgroupObjectKey(21,765432));"
        "j=OfflineNativeSealJournal(NativeRunReservationLedger(Path(sys.argv[1])));"
        "j.record_launch_disabled(ctx,claimed_global_dispatch_disabled=True);"
        "os._exit(17)"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = (
        str(Path(__file__).resolve().parents[1] / "src")
        + os.pathsep + env.get("PYTHONPATH", "")
    )
    proc = subprocess.run(
        [sys.executable, "-c", program, str(db),
         json.dumps(asdict(req)), json.dumps(asdict(ctx.initial_unit))],
        env=env, stdin=subprocess.DEVNULL, capture_output=True,
        text=True, check=False, timeout=10,
    )
    assert proc.returncode == 17, proc.stderr
    fresh = NativeRunReservationLedger(db)
    assert fresh.list_launch_fences() == ((GPU_A, req.run_id),)
    assert len(OfflineNativeSealJournal(fresh).audit(req.run_id)) == 1
    another = request()
    with pytest.raises(NativeLedgerError, match="dispatch persistently fenced"):
        fresh.reserve(another, allow(another), trusted_peer_uid=1000)
    assert ledger.list_reservations()[0].phase is NativeLedgerPhase.UNKNOWN_FENCED
