"""Source-only durable reservation tests. No systemd, model, GPU, or cgroup I/O."""

from __future__ import annotations

import os
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
)
from astrumweaver.runtime.native_cgroup_ledger import (
    NativeLedgerError,
    NativeLedgerPhase,
    NativeRunReservationLedger,
)

REV = "sha256:" + "a" * 64
MODEL = "/models/fake-operator-test.gguf"
BINARY = "/nix/store/" + "0" * 32 + "-llama/bin/llama-server"
GPU0 = "GPU-00000000-0000-0000-0000-000000000001"
GPU1 = "GPU-00000000-0000-0000-0000-000000000002"
WORKER = "sirius-test"
EPOCH = "99999999-9999-4999-8999-999999999999"


def request(
    *,
    gpu_uuids=(GPU0,), run_id=None,
    worker_epoch=EPOCH,
) -> NativeLaunchRequest:
    return NativeLaunchRequest(
        run_id=str(uuid4()) if run_id is None else run_id,
        worker_id=WORKER, worker_epoch=worker_epoch,
        deployment_revision=REV, executable=BINARY,
        model_ref=MODEL, gpu_uuids=gpu_uuids,
    )


def reviewed(request: NativeLaunchRequest) -> NativeLaunchAllowlist:
    return NativeLaunchAllowlist(
        worker_id=request.worker_id,
        deployment_revision=request.deployment_revision,
        executable=request.executable,
        model_ref=request.model_ref,
        gpu_uuids=request.gpu_uuids,
    )


def ledger(tmp_path: Path) -> NativeRunReservationLedger:
    return NativeRunReservationLedger(tmp_path / "native-reservations.sqlite3")


def test_private_database_initialization_and_single_reservation(tmp_path):
    store = ledger(tmp_path)
    one = request()
    reserved = store.reserve(one, reviewed(one), trusted_peer_uid=1000)
    assert reserved.newly_reserved
    assert reserved.phase is NativeLedgerPhase.RESERVED
    assert reserved.unit_name == one.unit_name
    assert reserved.gpu_uuids == (GPU0,)
    assert len(store.list_reservations()) == 1
    assert (tmp_path / "native-reservations.sqlite3").stat().st_mode & 0o777 == 0o600


def test_exact_retry_is_observable_without_permission_to_start_twice(tmp_path):
    store = ledger(tmp_path)
    one = request()
    first = store.reserve(one, reviewed(one), trusted_peer_uid=1000)
    second = store.reserve(one, reviewed(one), trusted_peer_uid=1000)
    assert first.newly_reserved and not second.newly_reserved
    assert first.run_id == second.run_id
    store.mark_start_intent(one.run_id, trusted_peer_uid=1000)
    retry = store.reserve(one, reviewed(one), trusted_peer_uid=1000)
    assert retry.phase is NativeLedgerPhase.UNKNOWN_FENCED
    assert not retry.newly_reserved
    with pytest.raises(NativeLedgerError, match="already dispatched"):
        store.mark_start_intent(one.run_id, trusted_peer_uid=1000)


def test_conflicting_gpu_on_different_runs_denied_across_reopen(tmp_path):
    one = request()
    store = ledger(tmp_path)
    store.reserve(one, reviewed(one), trusted_peer_uid=1000)
    reopened = ledger(tmp_path)
    overlapping = request(run_id=str(uuid4()), worker_epoch=str(uuid4()))
    with pytest.raises(NativeLedgerError, match="already durably reserved"):
        reopened.reserve(overlapping, reviewed(overlapping), trusted_peer_uid=1000)
    assert [row.run_id for row in reopened.list_reservations()] == [one.run_id]


def test_multi_gpu_reservation_is_atomic_if_one_gpu_is_already_owned(tmp_path):
    store = ledger(tmp_path)
    old = request(gpu_uuids=(GPU0,))
    store.reserve(old, reviewed(old), trusted_peer_uid=1000)
    failed = request(gpu_uuids=(GPU1, GPU0))
    with pytest.raises(NativeLedgerError, match="already durably reserved"):
        store.reserve(failed, reviewed(failed), trusted_peer_uid=1000)
    free = request(gpu_uuids=(GPU1,))
    success = store.reserve(free, reviewed(free), trusted_peer_uid=1000)
    assert success.newly_reserved
    assert {x.run_id for x in store.list_reservations()} == {old.run_id, free.run_id}


def test_same_run_id_but_different_epoch_peer_or_model_cannot_be_replayed(tmp_path):
    store = ledger(tmp_path)
    initial = request()
    store.reserve(initial, reviewed(initial), trusted_peer_uid=1000)
    with pytest.raises(NativeLedgerError, match="reused"):
        store.reserve(initial, reviewed(initial), trusted_peer_uid=1001)
    new_epoch = replace(initial, worker_epoch=str(uuid4()))
    with pytest.raises(NativeLedgerError, match="reused"):
        store.reserve(new_epoch, reviewed(new_epoch), trusted_peer_uid=1000)
    tampered = replace(initial, model_ref="/models/foreign.gguf")
    with pytest.raises(NativeLedgerError, match="reused"):
        store.reserve(tampered, reviewed(tampered), trusted_peer_uid=1000)


def test_unapproved_manifest_cannot_reserve_any_gpu(tmp_path):
    store = ledger(tmp_path)
    valid = request()
    false_policy = replace(reviewed(valid), executable=BINARY + ".other")
    with pytest.raises(NativeLedgerError, match="unapproved"):
        store.reserve(valid, false_policy, trusted_peer_uid=1000)
    assert store.list_reservations() == ()


@pytest.mark.parametrize("uid", [True, False, -1, None, "1000", 0.5])
def test_untrusted_uid_shapes_are_rejected(tmp_path, uid):
    store = ledger(tmp_path)
    one = request()
    with pytest.raises(NativeLedgerError, match="peer UID"):
        store.reserve(one, reviewed(one), trusted_peer_uid=uid)
    assert store.list_reservations() == ()


def test_wrong_peer_cannot_dispatch_start_or_mutate_reservation(tmp_path):
    store = ledger(tmp_path)
    one = request()
    store.reserve(one, reviewed(one), trusted_peer_uid=1000)
    with pytest.raises(NativeLedgerError):
        store.mark_start_intent(one.run_id, trusted_peer_uid=1001)
    assert store.list_reservations()[0].phase is NativeLedgerPhase.RESERVED


def test_lost_pid1_launch_ack_is_fenced_across_new_ledger_instance(tmp_path):
    store = ledger(tmp_path)
    one = request()
    store.reserve(one, reviewed(one), trusted_peer_uid=1000)
    # This is the mandatory durable operation BEFORE any future manager call.
    store.mark_start_intent(one.run_id, trusted_peer_uid=1000)
    del store  # Future code could crash/lose its manager response here.
    reopened = ledger(tmp_path)
    record = reopened.list_reservations()[0]
    assert record.phase is NativeLedgerPhase.UNKNOWN_FENCED
    with pytest.raises(NativeLedgerError):
        reopened.mark_start_intent(one.run_id, trusted_peer_uid=1000)
    another = request(worker_epoch=str(uuid4()))
    with pytest.raises(NativeLedgerError):
        reopened.reserve(another, reviewed(another), trusted_peer_uid=1000)


def test_broker_restart_fences_reserved_without_releasing_gpu(tmp_path):
    first = ledger(tmp_path)
    one = request()
    first.reserve(one, reviewed(one), trusted_peer_uid=1000)
    assert first.list_reservations()[0].phase is NativeLedgerPhase.RESERVED
    after_crash = ledger(tmp_path)
    assert after_crash.fence_on_broker_restart() == 1
    assert after_crash.fence_on_broker_restart() == 0
    assert after_crash.list_reservations()[0].phase is NativeLedgerPhase.UNKNOWN_FENCED
    with pytest.raises(NativeLedgerError):
        after_crash.mark_start_intent(one.run_id, trusted_peer_uid=1000)


def test_private_directory_and_database_permissions_are_enforced(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    store = ledger(private)
    assert store.list_reservations() == ()
    private.chmod(0o750)
    with pytest.raises(NativeLedgerError, match="owned 0700"):
        ledger(private)
    private.chmod(0o700)
    database = private / "native-reservations.sqlite3"
    database.chmod(0o644)
    with pytest.raises(NativeLedgerError, match="private 0600"):
        ledger(private)


def test_symlinked_database_is_rejected_without_following_link(tmp_path):
    original = tmp_path / "real.sqlite3"
    original.touch(mode=0o600)
    target = tmp_path / "native-reservations.sqlite3"
    target.symlink_to(original)
    with pytest.raises(NativeLedgerError):
        ledger(tmp_path)
    assert original.stat().st_size == 0


def test_unrecognized_version_fences_all_followup_writes(tmp_path):
    store = ledger(tmp_path)
    one = request()
    store.reserve(one, reviewed(one), trusted_peer_uid=1000)
    import sqlite3
    with sqlite3.connect(tmp_path / "native-reservations.sqlite3") as db:
        db.execute("UPDATE ledger_schema SET version='999' WHERE key='format'")
    with pytest.raises(NativeLedgerError, match="schema version"):
        ledger(tmp_path)


def test_concurrent_reservations_share_single_atomic_gpu_owner(tmp_path):
    a = ledger(tmp_path)
    b = ledger(tmp_path)
    requests = [request() for _ in range(8)]

    def try_reserve(i):
        store = a if i % 2 else b
        item = requests[i]
        try:
            return store.reserve(item, reviewed(item), trusted_peer_uid=1000).run_id
        except NativeLedgerError:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(try_reserve, range(len(requests))))
    assert sum(result is not None for result in results) == 1
    inventory = ledger(tmp_path).list_reservations()
    assert len(inventory) == 1
    assert inventory[0].run_id in results


def test_ledger_never_exposes_auto_release_even_with_fake_empty_report(tmp_path):
    store = ledger(tmp_path)
    one = request()
    store.reserve(one, reviewed(one), trusted_peer_uid=1000)
    store.mark_start_intent(one.run_id, trusted_peer_uid=1000)
    # There is intentionally no 'stop done => release' call. The future
    # verified manager adapter and a new explicit revision must own that gate.
    assert not hasattr(store, "release")
    assert not hasattr(store, "mark_empty")
    assert not hasattr(store, "kill_run")
    assert ledger(tmp_path).list_reservations()[0].phase is NativeLedgerPhase.UNKNOWN_FENCED



def test_missing_gpu_ownership_index_fails_closed_without_auto_repair(tmp_path):
    import sqlite3

    store = ledger(tmp_path)
    item = request()
    store.reserve(item, reviewed(item), trusted_peer_uid=1000)
    with sqlite3.connect(tmp_path / "native-reservations.sqlite3") as db:
        db.execute("DELETE FROM gpu_owners WHERE run_id=?", (item.run_id,))
    with pytest.raises(NativeLedgerError, match="ownership index"):
        ledger(tmp_path)
    # Existing object also refuses creating a second GPU owner.
    other = request()
    with pytest.raises(NativeLedgerError, match="ownership index"):
        store.reserve(other, reviewed(other), trusted_peer_uid=1000)


def test_manually_rewritten_unit_identity_or_epoch_is_fenced(tmp_path):
    import sqlite3

    store = ledger(tmp_path)
    item = request()
    store.reserve(item, reviewed(item), trusted_peer_uid=1000)
    with sqlite3.connect(tmp_path / "native-reservations.sqlite3") as db:
        db.execute(
            "UPDATE native_runs SET worker_epoch=? WHERE run_id=?",
            (str(uuid4()), item.run_id),
        )
    with pytest.raises(NativeLedgerError, match="identity is inconsistent"):
        ledger(tmp_path)



def test_actual_subprocess_crash_preserves_prelaunch_fence_and_gpu_owner(tmp_path):
    """A child exits abruptly after a durable launch intent (no PID1 call)."""
    db_path = tmp_path / "native-reservations.sqlite3"
    item = request()
    script = (
        "from pathlib import Path; import os,sys; "
        "from astrumweaver.runtime.native_cgroup_contract import "
        "NativeLaunchRequest,NativeLaunchAllowlist; "
        "from astrumweaver.runtime.native_cgroup_ledger import "
        "NativeRunReservationLedger; "
        "r=NativeLaunchRequest(run_id=sys.argv[2],worker_id=sys.argv[3],"
        "worker_epoch=sys.argv[4],deployment_revision=sys.argv[5],"
        "executable=sys.argv[6],model_ref=sys.argv[7],"
        "gpu_uuids=(sys.argv[8],)); "
        "a=NativeLaunchAllowlist(r.worker_id,r.deployment_revision,"
        "r.executable,r.model_ref,r.gpu_uuids); "
        "l=NativeRunReservationLedger(Path(sys.argv[1])); "
        "l.reserve(r,a,trusted_peer_uid=1000); "
        "l.mark_start_intent(r.run_id,trusted_peer_uid=1000); "
        "os._exit(17)"
    )
    env = os.environ.copy()
    src_root = str(Path(__file__).resolve().parents[1] / "src")
    env["PYTHONPATH"] = src_root + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [sys.executable, "-c", script, str(db_path), item.run_id,
         item.worker_id, item.worker_epoch, item.deployment_revision,
         item.executable, item.model_ref, item.gpu_uuids[0]],
        env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
        check=False, timeout=8,
    )
    assert result.returncode == 17, result.stderr
    after_restart = ledger(tmp_path)
    assert after_restart.list_reservations()[0].phase is NativeLedgerPhase.UNKNOWN_FENCED
    conflicting = request()
    with pytest.raises(NativeLedgerError, match="already durably reserved"):
        after_restart.reserve(
            conflicting, reviewed(conflicting), trusted_peer_uid=1000,
        )



def test_oversized_manifest_and_unbounded_gpu_list_are_denied_before_sqlite(tmp_path):
    store = ledger(tmp_path)
    overlong = replace(request(), model_ref="/models/" + "a" * 16000)
    with pytest.raises(NativeLedgerError, match="too large"):
        store.reserve(overlong, reviewed(overlong), trusted_peer_uid=1000)
    many = request(gpu_uuids=tuple(f"GPU-{i:036d}" for i in range(17)))
    with pytest.raises(NativeLedgerError, match="exceeds reserved capacity"):
        store.reserve(many, reviewed(many), trusted_peer_uid=1000)
    assert store.list_reservations() == ()
