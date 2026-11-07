"""#240 R5a: offline durable terminal claims, NEVER GPU-release authority."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
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
from astrumweaver.runtime.native_nested_cgroup import (
    OfflineCgroupObjectKey,
    OfflineNestedCgroupSamples,
)
from astrumweaver.runtime.native_seal_journal import (
    NativeRunReservationLedger as _ImportedLedgerForConsistency,
)
from astrumweaver.runtime.native_seal_journal import (
    NativeSealJournalError,
    NativeSealStage,
    OfflineNativeSealContext,
    OfflineNativeSealJournal,
)
from astrumweaver.runtime.native_systemd_port import ReviewedNativeCommand

_EPOCH = "99999999-9999-4999-8999-999999999999"
_REVISION = "sha256:" + "a" * 64
_BINARY = "/nix/store/" + "0" * 32 + "-llama/bin/llama-server"
_MODEL = "/models/fake-mock.gguf"
_GPU = "GPU-00000000-0000-0000-0000-000000000001"
_INV = "f" * 32


def fixture(tmp_path: Path):
    assert _ImportedLedgerForConsistency is NativeRunReservationLedger
    request = NativeLaunchRequest(
        run_id=str(uuid4()), worker_id="sirius-test",
        worker_epoch=_EPOCH, deployment_revision=_REVISION,
        executable=_BINARY, model_ref=_MODEL,
        gpu_uuids=(_GPU,),
    )
    policy = ReviewedNativeCommand(
        allowlist=NativeLaunchAllowlist(
            worker_id=request.worker_id,
            deployment_revision=request.deployment_revision,
            executable=request.executable,
            model_ref=request.model_ref,
            gpu_uuids=request.gpu_uuids,
        ),
        argv=(_BINARY, "--model", _MODEL),
        worker_uid=65534,
    )
    unit = NativeRunUnitIdentity(
        run_id=request.run_id, worker_epoch=request.worker_epoch,
        deployment_revision=request.deployment_revision,
        unit_name=request.unit_name,
        cgroup_path=request.cgroup_path,
        manager_invocation_id=_INV,
    )
    dir_key = OfflineCgroupObjectKey(device=21,inode=123456)
    event_key = OfflineCgroupObjectKey(device=21,inode=7891011)
    context = OfflineNativeSealContext(
        request=request, initial_unit=unit,
        native_key=dir_key, events_key=event_key,
    )
    samples = OfflineNestedCgroupSamples(
        unit=unit,
        monitor_path=request.cgroup_path + "/monitor",
        native_path=request.cgroup_path + "/native",
        native_key_before=dir_key, native_key_after=dir_key,
        events_key_before=event_key, events_key_after=event_key,
        native_events_live=b"populated 1\nfrozen 0\n",
        native_events_empty=b"populated 0\nfrozen 0\n",
        parent_events_while_monitor_alive=b"populated 1\nfrozen 0\n",
        native_effective_uid=65534,
        wrapper_reaped=True,
    )
    ledger = NativeRunReservationLedger(tmp_path / "native-ledger.sqlite3")
    assert ledger.reserve(
        request,policy.allowlist,trusted_peer_uid=65534,
    ).newly_reserved
    ledger.mark_start_intent(request.run_id,trusted_peer_uid=65534)
    journal = OfflineNativeSealJournal(ledger)
    return ledger, journal, context, policy, samples


def progress(journal,ctx,policy,samples):
    one=journal.record_launch_disabled(ctx,claimed_global_dispatch_disabled=True)
    two=journal.record_native_empty(ctx,policy,samples)
    three=journal.record_subgroup_removed(
        ctx,claimed_rmdir_succeeded=True,
        claimed_old_fd_reentry_denied=True,
        claimed_supervisor_still_alive=True,
    )
    four=journal.record_parent_stopped(
        ctx,reported_invocation=ctx.initial_unit.manager_invocation_id,
        claimed_inactive=True,claimed_supervisor_reaped=True,
    )
    return (one,two,three,four)


def test_full_offline_sequence_hash_chained_and_gpu_remains_fenced(tmp_path):
    ledger,journal,ctx,policy,samples=fixture(tmp_path)
    out=progress(journal,ctx,policy,samples)
    assert [v.stage for v in out]==list(NativeSealStage)
    assert [v.ordinal for v in out]==[0,1,2,3]
    assert all(len(v.chain_digest)==64 for v in out)
    assert len({v.chain_digest for v in out})==4
    assert journal.audit(ctx.run_id)==out
    assert ledger.list_reservations()[0].phase is NativeLedgerPhase.UNKNOWN_FENCED
    assert not hasattr(journal,"release")
    assert not hasattr(journal,"release_gpu")
    assert not hasattr(journal,"approve_native")
    assert not hasattr(ledger,"release")
    second=replace(ctx.request,run_id=str(uuid4()))
    with pytest.raises(NativeLedgerError,match="already durably reserved"):
        ledger.reserve(second,policy.allowlist,trusted_peer_uid=65534)


@pytest.mark.parametrize("cut", [0,1,2,3])
def test_failed_or_crashed_at_each_phase_preserves_last_committed_stage(tmp_path,cut):
    ledger,journal,ctx,policy,samples=fixture(tmp_path)
    actions=(
        lambda: journal.record_launch_disabled(ctx,claimed_global_dispatch_disabled=True),
        lambda: journal.record_native_empty(ctx,policy,samples),
        lambda: journal.record_subgroup_removed(
            ctx,claimed_rmdir_succeeded=True,claimed_old_fd_reentry_denied=True,
            claimed_supervisor_still_alive=True,
        ),
        lambda: journal.record_parent_stopped(
            ctx,reported_invocation=_INV,
            claimed_inactive=True,claimed_supervisor_reaped=True,
        ),
    )
    for a in actions[:cut+1]:
        a()
    # Fresh journal and ledger instance simulate a broker restart; zero new
    # GPU admission, and no implicit stage advancement or auto-cleanup.
    fresh=OfflineNativeSealJournal(
        NativeRunReservationLedger(tmp_path / "native-ledger.sqlite3")
    )
    audit=fresh.audit(ctx.run_id)
    assert len(audit)==cut+1
    assert audit[-1].stage==list(NativeSealStage)[cut]
    assert ledger.list_reservations()[0].phase is NativeLedgerPhase.UNKNOWN_FENCED
    for action in actions[:cut+1]:
        with pytest.raises(NativeSealJournalError,match="duplicate|skipped|re-ordered"):
            action()


def test_stage_skipping_and_missing_start_fence_refused(tmp_path):
    _,journal,ctx,policy,samples=fixture(tmp_path)
    with pytest.raises(NativeSealJournalError,match="begin"):
        journal.record_native_empty(ctx,policy,samples)
    with pytest.raises(NativeSealJournalError,match="begin"):
        journal.record_subgroup_removed(
            ctx,claimed_rmdir_succeeded=True,
            claimed_old_fd_reentry_denied=True,claimed_supervisor_still_alive=True,
        )
    with pytest.raises(NativeSealJournalError,match="dispatch"):
        journal.record_launch_disabled(ctx,claimed_global_dispatch_disabled=False)
    assert journal.audit(ctx.run_id)==()
    journal.record_launch_disabled(ctx,claimed_global_dispatch_disabled=True)
    with pytest.raises(NativeSealJournalError,match="duplicate|skipped|re-ordered"):
        journal.record_parent_stopped(
            ctx,reported_invocation=_INV,claimed_inactive=True,
            claimed_supervisor_reaped=True,
        )


@pytest.mark.parametrize(
    ("field","value"),
    [
        ("claimed_rmdir_succeeded",False),
        ("claimed_old_fd_reentry_denied",False),
        ("claimed_supervisor_still_alive",False),
        ("claimed_rmdir_succeeded",1),
        ("claimed_old_fd_reentry_denied",None),
    ],
)
def test_subgroup_removal_claim_must_include_every_strict_gate(tmp_path,field,value):
    _,journal,ctx,policy,samples=fixture(tmp_path)
    journal.record_launch_disabled(ctx,claimed_global_dispatch_disabled=True)
    journal.record_native_empty(ctx,policy,samples)
    kwargs={
        "claimed_rmdir_succeeded":True,
        "claimed_old_fd_reentry_denied":True,
        "claimed_supervisor_still_alive":True,
    }
    kwargs[field]=value
    with pytest.raises(NativeSealJournalError,match="not proven sealed"):
        journal.record_subgroup_removed(ctx,**kwargs)
    assert len(journal.audit(ctx.run_id))==2


@pytest.mark.parametrize(
    ("field","value"),
    [
        ("reported_invocation","e"*32),
        ("claimed_inactive",False),
        ("claimed_supervisor_reaped",False),
        ("claimed_supervisor_reaped",1),
    ],
)
def test_parent_terminal_requires_exact_original_generation(tmp_path,field,value):
    _,journal,ctx,policy,samples=fixture(tmp_path)
    journal.record_launch_disabled(ctx,claimed_global_dispatch_disabled=True)
    journal.record_native_empty(ctx,policy,samples)
    journal.record_subgroup_removed(
        ctx,claimed_rmdir_succeeded=True,
        claimed_old_fd_reentry_denied=True,
        claimed_supervisor_still_alive=True,
    )
    kw={
        "reported_invocation":_INV,
        "claimed_inactive":True,
        "claimed_supervisor_reaped":True,
    }
    kw[field]=value
    with pytest.raises(NativeSealJournalError,match="parent terminal"):
        journal.record_parent_stopped(ctx,**kw)
    assert len(journal.audit(ctx.run_id))==3


@pytest.mark.parametrize("where", ["invocation","native_inode","events_inode"])
def test_context_rebind_of_original_Unit_or_cgroup_generation_is_denied(tmp_path,where):
    _,journal,ctx,policy,samples=fixture(tmp_path)
    journal.record_launch_disabled(ctx,claimed_global_dispatch_disabled=True)
    if where=="invocation":
        bad=replace(ctx,initial_unit=replace(ctx.initial_unit,manager_invocation_id="e"*32))
    elif where=="native_inode":
        bad=replace(ctx,native_key=OfflineCgroupObjectKey(device=21,inode=908))
    else:
        bad=replace(ctx,events_key=OfflineCgroupObjectKey(device=21,inode=908))
    with pytest.raises(NativeSealJournalError,match="context|changed|inconsistent"):
        journal.record_native_empty(bad,policy,samples)
    assert len(journal.audit(ctx.run_id))==1


def test_mismatched_empty_sample_cannot_be_committed(tmp_path):
    _,journal,ctx,policy,samples=fixture(tmp_path)
    journal.record_launch_disabled(ctx,claimed_global_dispatch_disabled=True)
    bad=replace(samples,native_events_empty=b"populated 1\nfrozen 0\n")
    with pytest.raises(NativeSealJournalError,match="empty evidence"):
        journal.record_native_empty(ctx,policy,bad)
    assert len(journal.audit(ctx.run_id))==1


def test_no_gpu_reservation_or_wrong_epoch_cannot_start_journal(tmp_path):
    ledger=NativeRunReservationLedger(tmp_path / "native-ledger.sqlite3")
    journal=OfflineNativeSealJournal(ledger)
    isolated = tmp_path / "other"
    isolated.mkdir(mode=0o700)
    _,_,ctx,_,_=fixture(isolated)
    with pytest.raises(NativeSealJournalError,match="not durably fenced"):
        journal.record_launch_disabled(ctx,claimed_global_dispatch_disabled=True)


def test_modified_audit_details_or_missing_phase_are_rejected_on_reopen(tmp_path):
    _,journal,ctx,policy,samples=fixture(tmp_path)
    progress(journal,ctx,policy,samples)
    with sqlite3.connect(tmp_path / "native-ledger.sqlite3") as db:
        db.execute(
            "UPDATE seal_events SET details_json=? WHERE run_id=? AND ordinal=1",
            ('{"forged":true}',ctx.run_id),
        )
    with pytest.raises(NativeSealJournalError,match="chain digest"):
        OfflineNativeSealJournal(
            NativeRunReservationLedger(tmp_path/"native-ledger.sqlite3")
        )


def test_manual_delete_of_middle_receipt_fails_closed(tmp_path):
    _,journal,ctx,policy,samples=fixture(tmp_path)
    progress(journal,ctx,policy,samples)
    with sqlite3.connect(tmp_path / "native-ledger.sqlite3") as db:
        db.execute("DELETE FROM seal_events WHERE run_id=? AND ordinal=1",(ctx.run_id,))
    with pytest.raises(NativeSealJournalError,match="stage/order"):
        journal.audit(ctx.run_id)


def test_missing_gpu_owner_index_blocks_seal_append(tmp_path):
    ledger,journal,ctx,_,_=fixture(tmp_path)
    with sqlite3.connect(tmp_path / "native-ledger.sqlite3") as db:
        db.execute("DELETE FROM gpu_owners WHERE run_id=?",(ctx.run_id,))
    with pytest.raises(NativeLedgerError):
        ledger.list_reservations()
    with pytest.raises(NativeLedgerError):
        journal.record_launch_disabled(ctx,claimed_global_dispatch_disabled=True)


def test_multiple_concurrent_launch_disable_calls_allow_exactly_one(tmp_path):
    _,journal,ctx,_,_=fixture(tmp_path)
    def one(_):
        try:
            return journal.record_launch_disabled(
                ctx,claimed_global_dispatch_disabled=True,
            )
        except NativeSealJournalError:
            return None
    with ThreadPoolExecutor(max_workers=8) as ex:
        results=list(ex.map(one,range(8)))
    assert sum(v is not None for v in results)==1
    assert len(journal.audit(ctx.run_id))==1


def test_process_crash_after_committed_fence_does_not_erase_seal_audit(tmp_path):
    ledger,_,ctx,_,_=fixture(tmp_path)
    script=(
        "import os,sys;"
        "from pathlib import Path;"
        "from astrumweaver.runtime.native_cgroup_ledger import NativeRunReservationLedger;"
        "from astrumweaver.runtime.native_seal_journal import OfflineNativeSealJournal;"
        "from astrumweaver.runtime.native_cgroup_contract import NativeLaunchRequest,NativeRunUnitIdentity;"
        "from astrumweaver.runtime.native_nested_cgroup import OfflineCgroupObjectKey;"
        "from astrumweaver.runtime.native_seal_journal import OfflineNativeSealContext;"
        "rj=__import__('json').loads(sys.argv[2]);"
        "rj['gpu_uuids']=tuple(rj['gpu_uuids']);"
        "r=NativeLaunchRequest(**rj);"
        "u=NativeRunUnitIdentity(**__import__('json').loads(sys.argv[3]));"
        "c=OfflineNativeSealContext(r,u,OfflineCgroupObjectKey(21,123456),OfflineCgroupObjectKey(21,7891011));"
        "j=OfflineNativeSealJournal(NativeRunReservationLedger(Path(sys.argv[1])));"
        "j.record_launch_disabled(c,claimed_global_dispatch_disabled=True);"
        "os._exit(17)"
    )
    import json
    env=os.environ.copy()
    env["PYTHONPATH"]=str(Path(__file__).resolve().parents[1]/"src")+os.pathsep+env.get("PYTHONPATH","")
    result=subprocess.run(
        [sys.executable,"-c",script,str(tmp_path/"native-ledger.sqlite3"),
         json.dumps(asdict_request(ctx.request)),json.dumps(asdict_unit(ctx.initial_unit))],
        env=env,stdin=subprocess.DEVNULL,capture_output=True,text=True,
        timeout=10,check=False,
    )
    assert result.returncode==17,result.stderr
    fresh=OfflineNativeSealJournal(NativeRunReservationLedger(tmp_path/"native-ledger.sqlite3"))
    assert [v.stage for v in fresh.audit(ctx.run_id)]==[NativeSealStage.LAUNCH_DISABLED]
    assert ledger.list_reservations()[0].phase is NativeLedgerPhase.UNKNOWN_FENCED


def asdict_request(req):
    from dataclasses import asdict
    return asdict(req)


def asdict_unit(unit):
    from dataclasses import asdict
    return asdict(unit)
