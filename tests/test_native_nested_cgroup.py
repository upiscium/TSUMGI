"""#240: offline-only nested cgroup stop-candidate checks."""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest

from astrumweaver.runtime.native_cgroup_contract import (
    NativeLaunchAllowlist,
    NativeLaunchRequest,
    NativeRunContractError,
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
    validate_offline_nested_empty_candidate,
)
from astrumweaver.runtime.native_systemd_port import (
    NativeManagerPortError,
    ReviewedNativeCommand,
)

_EPOCH = "99999999-9999-4999-8999-999999999999"
_REV = "sha256:" + "a" * 64
_EXE = "/nix/store/" + "0" * 32 + "-llama/bin/llama-server"
_MODEL = "/models/fake-operator-test.gguf"
_GPUS = ("GPU-00000000-0000-0000-0000-000000000001",)


def fixture():
    req = NativeLaunchRequest(
        run_id=str(uuid4()),
        worker_id="sirius-test",
        worker_epoch=_EPOCH,
        deployment_revision=_REV,
        executable=_EXE,
        model_ref=_MODEL,
        gpu_uuids=_GPUS,
    )
    policy = ReviewedNativeCommand(
        allowlist=NativeLaunchAllowlist(
            worker_id=req.worker_id,
            deployment_revision=req.deployment_revision,
            executable=req.executable,
            model_ref=req.model_ref,
            gpu_uuids=req.gpu_uuids,
        ),
        argv=(_EXE, "--model", _MODEL),
        worker_uid=65534,
    )
    unit = NativeRunUnitIdentity(
        run_id=req.run_id,
        worker_epoch=req.worker_epoch,
        deployment_revision=req.deployment_revision,
        unit_name=req.unit_name,
        cgroup_path=req.cgroup_path,
        manager_invocation_id="a" * 32,
    )
    group_key = OfflineCgroupObjectKey(device=1, inode=345)
    events_key = OfflineCgroupObjectKey(device=1, inode=678)
    sample = OfflineNestedCgroupSamples(
        unit=unit,
        monitor_path=req.cgroup_path + "/monitor",
        native_path=req.cgroup_path + "/native",
        native_key_before=group_key,
        native_key_after=group_key,
        events_key_before=events_key,
        events_key_after=events_key,
        native_events_live=b"populated 1\nfrozen 0\n",
        native_events_empty=b"populated 0\nfrozen 0\n",
        parent_events_while_monitor_alive=b"populated 1\nfrozen 0\n",
        native_effective_uid=65534,
        wrapper_reaped=True,
    )
    return req, policy, sample


def test_nested_candidate_matches_generation_and_live_monitor_without_release():
    req, policy, sample = fixture()
    assert validate_offline_nested_empty_candidate(req, policy, sample, initial_unit=sample.unit) is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("monitor_path", "/system.slice/foreign.service/monitor"),
        ("native_path", "/system.slice/foreign.service/native"),
        ("native_path", "native"),
        ("native_path", "/system.slice/foreign.service/../native"),
        ("native_effective_uid", 0),
        ("native_effective_uid", 1000),
        ("wrapper_reaped", False),
        ("native_events_live", b"populated 0\nfrozen 0\n"),
        ("native_events_empty", b"populated 1\nfrozen 0\n"),
        ("parent_events_while_monitor_alive", b"populated 0\nfrozen 0\n"),
    ],
)
def test_nested_candidate_denies_unsafe_scope_uid_or_lifecycle(field, value):
    req, policy, sample = fixture()
    with pytest.raises(NativeManagerPortError):
        validate_offline_nested_empty_candidate(
            req, policy, replace(sample, **{field: value}),
            initial_unit=sample.unit,
        )


@pytest.mark.parametrize("field", ["native_key_after", "events_key_after"])
def test_group_or_events_generation_change_denied(field):
    req, policy, sample = fixture()
    changed = replace(sample, **{
        field: OfflineCgroupObjectKey(device=2, inode=300)
    })
    with pytest.raises(NativeManagerPortError, match="generation changed"):
        validate_offline_nested_empty_candidate(req, policy, changed, initial_unit=sample.unit)


@pytest.mark.parametrize("field", ["worker_epoch", "deployment_revision"])
def test_forged_parent_unit_generation_denied(field):
    req, policy, sample = fixture()
    value = str(uuid4()) if field == "worker_epoch" else "sha256:" + "b" * 64
    tampered = replace(sample, unit=replace(sample.unit, **{field: value}))
    with pytest.raises(NativeManagerPortError, match="parent Unit generation"):
        validate_offline_nested_empty_candidate(req, policy, tampered, initial_unit=sample.unit)


@pytest.mark.parametrize("bad", [0, -1, True, False, "4", None])
def test_kernel_object_key_requires_real_positive_integer_shape(bad):
    with pytest.raises(NativeManagerPortError, match="invalid cgroup object"):
        OfflineCgroupObjectKey(device=bad, inode=100)
    with pytest.raises(NativeManagerPortError, match="invalid cgroup object"):
        OfflineCgroupObjectKey(device=8, inode=bad)


@pytest.mark.parametrize(
    "bad",
    [b"", b"populated 1\npopulated 0\n", b"frozen 0\n", b"garbage"],
)
def test_missing_or_ambiguous_cgroup_event_is_never_synthesized(bad):
    _, _, sample = fixture()
    with pytest.raises(NativeRunContractError):
        replace(sample, native_events_empty=bad)


def test_claimed_native_uid_and_reap_fields_reject_bool_shortcut():
    _, _, sample = fixture()
    with pytest.raises(NativeManagerPortError):
        replace(sample, native_effective_uid=True)
    with pytest.raises(NativeManagerPortError):
        replace(sample, wrapper_reaped=1)


def test_wrong_admin_model_binding_does_not_authorize_sample():
    req, policy, sample = fixture()
    bad = replace(
        policy,
        allowlist=replace(policy.allowlist, model_ref="/models/foreign.gguf"),
        argv=(_EXE, "--model", "/models/foreign.gguf"),
    )
    with pytest.raises(NativeManagerPortError, match="policy mismatch"):
        validate_offline_nested_empty_candidate(req, bad, sample, initial_unit=sample.unit)


def test_untrusted_synthetic_candidate_never_clears_real_ledger_barrier(tmp_path):
    req, policy, sample = fixture()
    ledger = NativeRunReservationLedger(tmp_path / "native-ledger.sqlite3")
    assert ledger.reserve(req, policy.allowlist, trusted_peer_uid=65534).newly_reserved
    ledger.mark_start_intent(req.run_id, trusted_peer_uid=65534)
    assert validate_offline_nested_empty_candidate(req, policy, sample, initial_unit=sample.unit) is None
    assert not hasattr(ledger, "release")
    assert ledger.list_reservations()[0].phase is NativeLedgerPhase.UNKNOWN_FENCED
    other = replace(req, run_id=str(uuid4()))
    with pytest.raises(NativeLedgerError, match="already durably reserved"):
        ledger.reserve(other, policy.allowlist, trusted_peer_uid=65534)
    assert ledger.list_reservations()[0].phase is NativeLedgerPhase.UNKNOWN_FENCED


def test_restarted_unit_invocation_cannot_reuse_original_empty_observation():
    req, policy, sample = fixture()
    rebound = replace(
        sample, unit=replace(sample.unit, manager_invocation_id="b" * 32),
    )
    with pytest.raises(NativeManagerPortError, match="invocation changed"):
        validate_offline_nested_empty_candidate(
            req, policy, rebound, initial_unit=sample.unit,
        )


def test_absent_original_invocation_reference_never_authorizes_candidate():
    req, policy, sample = fixture()
    with pytest.raises(NativeManagerPortError, match="invocation changed"):
        validate_offline_nested_empty_candidate(
            req, policy, sample, initial_unit=None,
        )
