"""B1b0: fake-manager dispatch only; no real systemd or cgroup operations."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from astrumweaver.runtime.native_cgroup_contract import (
    NativeLaunchAllowlist,
    NativeLaunchRequest,
)
from astrumweaver.runtime.native_cgroup_ledger import (
    NativeLedgerPhase,
    NativeRunReservationLedger,
)
from astrumweaver.runtime.native_systemd_port import (
    NativeBrokerDispatchCoordinator,
    NativeManagerPortError,
    NativeStartPendingReceipt,
    ReviewedNativeCommand,
    StartTransientUnitPlan,
)

REV = "sha256:" + "a" * 64
EXE = "/nix/store/" + "0" * 32 + "-llama/bin/llama-server"
MODEL = "/models/fixture-not-an-actual-model.gguf"
GPU = "GPU-00000000-0000-0000-0000-000000000001"
EPOCH = "99999999-9999-4999-8999-999999999999"
UID = 1000


def request(*, run_id: str | None = None) -> NativeLaunchRequest:
    return NativeLaunchRequest(
        run_id=str(uuid4()) if run_id is None else run_id,
        worker_id="sirius-test",
        worker_epoch=EPOCH,
        deployment_revision=REV,
        executable=EXE,
        model_ref=MODEL,
        gpu_uuids=(GPU,),
    )


def reviewed(req: NativeLaunchRequest) -> ReviewedNativeCommand:
    return ReviewedNativeCommand(
        allowlist=NativeLaunchAllowlist(
            worker_id=req.worker_id,
            deployment_revision=req.deployment_revision,
            executable=req.executable,
            model_ref=req.model_ref,
            gpu_uuids=req.gpu_uuids,
        ),
        argv=(req.executable, "--model", req.model_ref),
        worker_uid=UID,
    )


def ledger(path: Path) -> NativeRunReservationLedger:
    return NativeRunReservationLedger(path / "native-reservations.sqlite3")


class FakeManager:
    def __init__(
        self,
        *,
        outcome: object = "/org/freedesktop/systemd1/job/123",
        wait_for_release: bool = False,
    ) -> None:
        self.outcome = outcome
        self.calls: list[StartTransientUnitPlan] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        if not wait_for_release:
            self.release.set()

    async def start_transient_unit(self, plan: StartTransientUnitPlan) -> str:
        self.calls.append(plan)
        self.entered.set()
        await self.release.wait()
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def test_fixed_service_plan_has_no_untrusted_unit_or_override_properties():
    req = request()
    plan = StartTransientUnitPlan.for_request(
        req, reviewed(req), trusted_peer_uid=UID,
    )
    assert plan.unit_name == req.unit_name
    assert plan.mode == "fail"
    assert plan.auxiliary_units == ()
    props = dict(plan.properties)
    assert len(props) == len(plan.properties)
    assert props["ExecStart"] == (EXE, (EXE, "--model", MODEL), False)
    assert props["Type"] == "exec"
    assert props["User"] == "astrumweaver"
    assert props["Group"] == "astrumweaver"
    assert props["Slice"] == "system.slice"
    assert props["Restart"] == "no"
    assert props["KillMode"] == "control-group"
    for name in (
        "NoNewPrivileges", "ProtectControlGroups", "ProtectHome",
        "PrivateTmp", "ProtectKernelTunables", "ProtectKernelModules",
    ):
        assert props[name] is True
    assert props["ProtectSystem"] == "strict"
    assert props["DevicePolicy"] == "closed"
    assert set(props).isdisjoint({
        "Delegate", "RootDirectory", "RootImage", "Environment",
        "DeviceAllow", "CapabilityBoundingSet",
    })


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("argv", (EXE, "--model", "/models/different.gguf")),
        ("argv", ("/usr/bin/bash", "--model", MODEL)),
        ("argv", (EXE, "--model", MODEL, MODEL)),
        ("argv", (EXE, "--model", MODEL + "\n")),
        ("argv", (EXE, "--model", MODEL, "x" * 8500)),
        ("argv", (EXE, "--model", MODEL, "\0")),
        ("argv", (EXE,)),
        ("worker_uid", 0),
        ("worker_uid", True),
    ],
)
def test_reviewed_command_rejects_malformed_or_unreviewed_exec(field, value):
    req = request()
    with pytest.raises(NativeManagerPortError):
        replace(reviewed(req), **{field: value})


@pytest.mark.asyncio
async def test_wrong_uid_or_policy_rejected_before_sqlite_reservation_or_pid1(tmp_path):
    req = request()
    manager = FakeManager()
    instance = NativeBrokerDispatchCoordinator(ledger(tmp_path), manager)
    with pytest.raises(NativeManagerPortError, match="not allowed"):
        await instance.dispatch_once(
            req, reviewed(req), trusted_peer_uid=UID + 1,
        )
    bad_policy = replace(
        reviewed(req),
        allowlist=replace(reviewed(req).allowlist, gpu_uuids=("GPU-OTHER",)),
    )
    with pytest.raises(NativeManagerPortError, match="not allowed"):
        await instance.dispatch_once(req, bad_policy, trusted_peer_uid=UID)
    assert manager.calls == []
    assert ledger(tmp_path).list_reservations() == ()


@pytest.mark.asyncio
async def test_queued_manager_job_is_not_start_or_cgroup_ready(tmp_path):
    req = request()
    manager = FakeManager()
    store = ledger(tmp_path)
    instance = NativeBrokerDispatchCoordinator(store, manager)
    receipt = await instance.dispatch_once(
        req, reviewed(req), trusted_peer_uid=UID,
    )
    assert isinstance(receipt, NativeStartPendingReceipt)
    assert receipt.unit_name == req.unit_name
    assert receipt.manager_job_path == "/org/freedesktop/systemd1/job/123"
    assert receipt.phase is NativeLedgerPhase.UNKNOWN_FENCED
    assert store.list_reservations()[0].phase is NativeLedgerPhase.UNKNOWN_FENCED
    assert len(manager.calls) == 1
    with pytest.raises(NativeManagerPortError, match="already reserved"):
        await instance.dispatch_once(req, reviewed(req), trusted_peer_uid=UID)
    assert len(manager.calls) == 1  # no duplicate systemd start


@pytest.mark.asyncio
async def test_lost_manager_ack_fences_run_and_refuses_other_gpu_owner(tmp_path):
    req = request()
    manager = FakeManager(outcome=ConnectionError("synthetic lost manager ACK"))
    store = ledger(tmp_path)
    coordinator = NativeBrokerDispatchCoordinator(store, manager)
    with pytest.raises(NativeManagerPortError, match="unconfirmed"):
        await coordinator.dispatch_once(
            req, reviewed(req), trusted_peer_uid=UID,
        )
    assert len(manager.calls) == 1
    assert ledger(tmp_path).list_reservations()[0].phase is NativeLedgerPhase.UNKNOWN_FENCED
    with pytest.raises(NativeManagerPortError, match="reservation denied"):
        await coordinator.dispatch_once(
            request(), reviewed(req), trusted_peer_uid=UID,
        )
    # The second attempt is intentionally invalid for policy; confirm
    # the same GPU collision with a correct newly reviewed policy too.
    other = request()
    with pytest.raises(NativeManagerPortError, match="reservation denied"):
        await coordinator.dispatch_once(
            other, reviewed(other), trusted_peer_uid=UID,
        )
    assert len(manager.calls) == 1


@pytest.mark.asyncio
async def test_cancel_after_durable_start_intent_never_reissues_manager_call(tmp_path):
    req = request()
    manager = FakeManager(wait_for_release=True)
    store = ledger(tmp_path)
    coordinator = NativeBrokerDispatchCoordinator(store, manager)
    task = asyncio.create_task(
        coordinator.dispatch_once(req, reviewed(req), trusted_peer_uid=UID)
    )
    try:
        await asyncio.wait_for(manager.entered.wait(), timeout=2)
        assert len(manager.calls) == 1
        assert store.list_reservations()[0].phase is NativeLedgerPhase.UNKNOWN_FENCED
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(NativeManagerPortError, match="already reserved"):
            await coordinator.dispatch_once(
                req, reviewed(req), trusted_peer_uid=UID,
            )
        assert len(manager.calls) == 1
    finally:
        manager.release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    [None, "", "failed", "/org/freedesktop/systemd1/job/0", "/org/freedesktop/systemd1/unit/12"],
)
async def test_untrusted_job_reply_remains_fenced(tmp_path, outcome):
    req = request()
    manager = FakeManager(outcome=outcome)
    store = ledger(tmp_path)
    with pytest.raises(NativeManagerPortError, match="untrusted job identity"):
        await NativeBrokerDispatchCoordinator(store, manager).dispatch_once(
            req, reviewed(req), trusted_peer_uid=UID,
        )
    assert len(manager.calls) == 1
    assert store.list_reservations()[0].phase is NativeLedgerPhase.UNKNOWN_FENCED


@pytest.mark.asyncio
async def test_8_concurrent_brokers_do_not_double_submit_same_gpu(tmp_path):
    reqs = [request() for _ in range(8)]
    manager = FakeManager()
    stores = [ledger(tmp_path) for _ in range(8)]
    brokers = [NativeBrokerDispatchCoordinator(s, manager) for s in stores]

    async def attempt(i):
        try:
            return await brokers[i].dispatch_once(
                reqs[i], reviewed(reqs[i]), trusted_peer_uid=UID,
            )
        except NativeManagerPortError:
            return None

    results = await asyncio.gather(*(attempt(i) for i in range(len(brokers))))
    assert sum(isinstance(x, NativeStartPendingReceipt) for x in results) == 1
    assert len(manager.calls) == 1
    assert len(ledger(tmp_path).list_reservations()) == 1
    assert ledger(tmp_path).list_reservations()[0].phase is NativeLedgerPhase.UNKNOWN_FENCED


def test_not_a_real_manager_transport_or_process_interface():
    import inspect

    import astrumweaver.runtime.native_systemd_port as module
    source = inspect.getsource(module)
    assert "subprocess." not in source
    assert "systemctl " not in source
    assert "busctl " not in source
    assert "os.kill" not in source
    assert "dbus_next" not in source


def test_offline_dbus_start_transient_encoder_matches_manager_api_shape():
    from astrumweaver.runtime.native_systemd_port import (
        SystemdStartTransientCallShape,
    )

    req = request()
    call = SystemdStartTransientCallShape.for_request(
        req, reviewed(req), trusted_peer_uid=UID,
    )
    assert call.destination == "org.freedesktop.systemd1"
    assert call.object_path == "/org/freedesktop/systemd1"
    assert call.interface == "org.freedesktop.systemd1.Manager"
    assert call.method == "StartTransientUnit"
    assert call.signature == "ssa(sv)a(sa(sv))"
    assert len(call.args) == 4
    name, mode, properties, auxiliary = call.args
    assert name == req.unit_name
    assert mode == "fail"
    assert auxiliary == ()
    props = dict(properties)
    assert len(props) == len(properties)
    assert props["User"].signature == "s"
    assert props["User"].value == "astrumweaver"
    assert props["ProtectControlGroups"].signature == "b"
    assert props["ProtectControlGroups"].value is True
    assert props["ExecStart"].signature == "a(sasb)"
    assert props["ExecStart"].value == ((EXE, (EXE, "--model", MODEL), False),)
    assert "Delegate" not in props
    assert "Environment" not in props
    assert "DeviceAllow" not in props


def test_dbus_encoder_rejects_forged_peer_identity_before_message_assembly():
    from astrumweaver.runtime.native_systemd_port import (
        SystemdStartTransientCallShape,
    )

    req = request()
    with pytest.raises(NativeManagerPortError, match="not allowed"):
        SystemdStartTransientCallShape.for_request(
            req, reviewed(req), trusted_peer_uid=UID + 1,
        )


@pytest.mark.asyncio
async def test_ledger_prelaunch_fence_error_prevents_any_manager_operation(
    tmp_path, monkeypatch,
):
    from astrumweaver.runtime.native_cgroup_ledger import NativeLedgerError

    req = request()
    store = ledger(tmp_path)
    manager = FakeManager()

    def fail_before_manager_call(*_args, **_kwargs):
        raise NativeLedgerError("simulated ledger write failure")

    monkeypatch.setattr(store, "mark_start_intent", fail_before_manager_call)
    with pytest.raises(NativeManagerPortError, match="unable to fence"):
        await NativeBrokerDispatchCoordinator(store, manager).dispatch_once(
            req, reviewed(req), trusted_peer_uid=UID,
        )
    assert manager.calls == []
    inventory = ledger(tmp_path).list_reservations()
    assert len(inventory) == 1
    assert inventory[0].phase is NativeLedgerPhase.RESERVED
    # Conservative broker restart reconciliation will fence this old
    # reservation, not free the GPU on the assumption no manager ran.
    assert ledger(tmp_path).fence_on_broker_restart() == 1
    assert ledger(tmp_path).list_reservations()[0].phase is NativeLedgerPhase.UNKNOWN_FENCED


@pytest.mark.asyncio
async def test_corrupted_ledger_refuses_manager_call_before_dispatch(tmp_path):
    import sqlite3
    store = ledger(tmp_path)
    initial = request()
    store.reserve(initial, reviewed(initial).allowlist, trusted_peer_uid=UID)
    with sqlite3.connect(tmp_path / "native-reservations.sqlite3") as db:
        db.execute("DELETE FROM gpu_owners WHERE run_id=?", (initial.run_id,))

    manager = FakeManager()
    other = request()
    with pytest.raises(NativeManagerPortError, match="reservation denied"):
        await NativeBrokerDispatchCoordinator(store, manager).dispatch_once(
            other, reviewed(other), trusted_peer_uid=UID,
        )
    assert manager.calls == []


@pytest.mark.asyncio
async def test_broker_manager_port_never_exposes_stop_or_gpu_release(tmp_path):
    manager = FakeManager()
    instance = NativeBrokerDispatchCoordinator(ledger(tmp_path), manager)
    assert not hasattr(instance, "release")
    assert not hasattr(instance, "systemctl")
    assert not hasattr(instance, "kill_run")
    assert not hasattr(instance, "gpu_reset")


class FakeDbusTransport:
    def __init__(self, reply="/org/freedesktop/systemd1/job/123"):
        self.calls = []
        self.reply = reply

    async def call_manager(self, call):
        self.calls.append(call)
        if isinstance(self.reply, BaseException):
            raise self.reply
        return self.reply


@pytest.mark.asyncio
async def test_offline_dbus_adapter_reaches_exact_call_shape_after_durable_fence(
    tmp_path,
):
    from astrumweaver.runtime.native_systemd_port import (
        ShapedSystemdManagerPort,
    )

    req = request()
    transport = FakeDbusTransport()
    store = ledger(tmp_path)
    manager = ShapedSystemdManagerPort(
        req, reviewed(req), trusted_peer_uid=UID, transport=transport,
    )
    result = await NativeBrokerDispatchCoordinator(
        store, manager,
    ).dispatch_once(req, reviewed(req), trusted_peer_uid=UID)
    assert result.phase is NativeLedgerPhase.UNKNOWN_FENCED
    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call.method == "StartTransientUnit"
    assert call.args[0] == req.unit_name
    assert call.args[1] == "fail"
    assert call.args[3] == ()
    assert store.list_reservations()[0].phase is NativeLedgerPhase.UNKNOWN_FENCED


@pytest.mark.asyncio
async def test_offline_manager_adapter_refuses_modified_plan_before_transport_call():
    from astrumweaver.runtime.native_systemd_port import (
        ShapedSystemdManagerPort,
    )

    req = request()
    transport = FakeDbusTransport()
    manager = ShapedSystemdManagerPort(
        req, reviewed(req), trusted_peer_uid=UID, transport=transport,
    )
    approved = StartTransientUnitPlan.for_request(
        req, reviewed(req), trusted_peer_uid=UID,
    )
    with pytest.raises(NativeManagerPortError, match="differs from approved"):
        await manager.start_transient_unit(replace(approved, mode="replace"))
    with pytest.raises(NativeManagerPortError, match="differs from approved"):
        await manager.start_transient_unit(
            replace(approved, properties=approved.properties + (("Delegate", True),))
        )
    with pytest.raises(NativeManagerPortError, match="differs from approved"):
        await manager.start_transient_unit(
            replace(approved, unit_name="arbitrary.service")
        )
    assert transport.calls == []


@pytest.mark.asyncio
async def test_offline_dbus_lost_ack_cannot_retry_manager_start(tmp_path):
    from astrumweaver.runtime.native_systemd_port import (
        ShapedSystemdManagerPort,
    )

    req = request()
    transport = FakeDbusTransport(ConnectionError("synthetic lost manager reply"))
    store = ledger(tmp_path)
    manager = ShapedSystemdManagerPort(
        req, reviewed(req), trusted_peer_uid=UID, transport=transport,
    )
    coordinator = NativeBrokerDispatchCoordinator(store, manager)
    with pytest.raises(NativeManagerPortError, match="unconfirmed"):
        await coordinator.dispatch_once(req, reviewed(req), trusted_peer_uid=UID)
    assert len(transport.calls) == 1
    assert store.list_reservations()[0].phase is NativeLedgerPhase.UNKNOWN_FENCED
    with pytest.raises(NativeManagerPortError, match="already reserved"):
        await coordinator.dispatch_once(req, reviewed(req), trusted_peer_uid=UID)
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_manager_no_reply_is_bounded_and_gpu_remains_fenced(tmp_path):
    """Without a bound, a hanging PID1 adapter stalls dispatch forever."""
    req = request()
    manager = FakeManager(wait_for_release=True)
    store = ledger(tmp_path)
    broker = NativeBrokerDispatchCoordinator(
        store, manager, manager_ack_timeout_seconds=0.03,
    )
    try:
        with pytest.raises(NativeManagerPortError, match="reply unconfirmed"):
            await asyncio.wait_for(
                broker.dispatch_once(req, reviewed(req), trusted_peer_uid=UID),
                timeout=2.0,
            )
        assert len(manager.calls) == 1
        assert store.list_reservations()[0].phase is NativeLedgerPhase.UNKNOWN_FENCED
        with pytest.raises(NativeManagerPortError, match="already reserved"):
            await broker.dispatch_once(req, reviewed(req), trusted_peer_uid=UID)
        assert len(manager.calls) == 1
    finally:
        manager.release.set()


@pytest.mark.parametrize("invalid", [0, -1, True, False, float("nan"), float("inf"), None, "1"])
def test_manager_ack_timeout_rejects_unbounded_or_invalid_values(tmp_path, invalid):
    manager = FakeManager()
    with pytest.raises(ValueError, match="manager_ack_timeout_seconds"):
        NativeBrokerDispatchCoordinator(
            ledger(tmp_path), manager, manager_ack_timeout_seconds=invalid,
        )


@pytest.mark.asyncio
async def test_manager_ignoring_cancellation_cannot_extend_ack_wait(tmp_path):
    """wait_for(task) alone may exceed its deadline if adapter ignores cancel."""

    class CancellationResistantManager(FakeManager):
        async def start_transient_unit(self, plan):
            self.calls.append(plan)
            self.entered.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                # Simulate in-flight bus transaction completing after client
                # cancellation, without assuming PID1 did not start a unit.
                await self.release.wait()
            return "/org/freedesktop/systemd1/job/431"

    req = request()
    manager = CancellationResistantManager(wait_for_release=True)
    store = ledger(tmp_path)
    broker = NativeBrokerDispatchCoordinator(
        store, manager, manager_ack_timeout_seconds=0.02,
    )
    dispatch = asyncio.create_task(
        broker.dispatch_once(req, reviewed(req), trusted_peer_uid=UID)
    )
    try:
        await asyncio.wait_for(manager.entered.wait(), 2)
        await asyncio.sleep(0.1)
        assert dispatch.done(), "manager suppressed cancellation and hung ACK"
        with pytest.raises(NativeManagerPortError, match="reply unconfirmed"):
            await dispatch
        assert store.list_reservations()[0].phase is NativeLedgerPhase.UNKNOWN_FENCED
        with pytest.raises(NativeManagerPortError, match="already reserved"):
            await broker.dispatch_once(req, reviewed(req), trusted_peer_uid=UID)
        assert len(manager.calls) == 1
    finally:
        manager.release.set()
        if not dispatch.done():
            await asyncio.wait_for(dispatch, timeout=2)


@pytest.mark.asyncio
async def test_late_manager_ack_does_not_auto_unfence_after_timeout(tmp_path):
    req = request()
    manager = FakeManager(wait_for_release=True)
    store = ledger(tmp_path)
    broker = NativeBrokerDispatchCoordinator(
        store, manager, manager_ack_timeout_seconds=0.02,
    )
    with pytest.raises(NativeManagerPortError, match="reply unconfirmed"):
        await broker.dispatch_once(req, reviewed(req), trusted_peer_uid=UID)
    assert len(broker._pending_manager_calls) == 1
    assert store.list_reservations()[0].phase is NativeLedgerPhase.UNKNOWN_FENCED
    manager.release.set()
    await asyncio.wait_for(
        asyncio.gather(*tuple(broker._pending_manager_calls)),
        timeout=2.0,
    )
    # A callback may still be scheduled after Task completion.
    await asyncio.sleep(0)
    assert not broker._pending_manager_calls
    assert store.list_reservations()[0].phase is NativeLedgerPhase.UNKNOWN_FENCED
    with pytest.raises(NativeManagerPortError, match="already reserved"):
        await broker.dispatch_once(req, reviewed(req), trusted_peer_uid=UID)
    assert len(manager.calls) == 1


@pytest.mark.asyncio
async def test_cancelled_caller_cannot_cancel_unconfirmed_manager_operation(tmp_path):
    req = request()
    manager = FakeManager(wait_for_release=True)
    store = ledger(tmp_path)
    broker = NativeBrokerDispatchCoordinator(
        store, manager, manager_ack_timeout_seconds=0.5,
    )
    caller = asyncio.create_task(
        broker.dispatch_once(req, reviewed(req), trusted_peer_uid=UID)
    )
    try:
        await asyncio.wait_for(manager.entered.wait(), timeout=2)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert len(manager.calls) == 1
        assert len(broker._pending_manager_calls) == 1
        assert store.list_reservations()[0].phase is NativeLedgerPhase.UNKNOWN_FENCED
    finally:
        manager.release.set()
        await asyncio.wait_for(
            asyncio.gather(*tuple(broker._pending_manager_calls)), timeout=2,
        )
    await asyncio.sleep(0)
    assert not broker._pending_manager_calls
    assert store.list_reservations()[0].phase is NativeLedgerPhase.UNKNOWN_FENCED


@pytest.mark.asyncio
async def test_pending_manager_limit_denies_new_gpu_before_reservation(tmp_path):
    """A lost Manager reply must not permit unbounded pending operations."""
    first = request()
    second = replace(
        request(),
        gpu_uuids=("GPU-00000000-0000-0000-0000-000000000002",),
    )
    manager = FakeManager(wait_for_release=True)
    store = ledger(tmp_path)
    broker = NativeBrokerDispatchCoordinator(
        store,
        manager,
        manager_ack_timeout_seconds=0.02,
        max_pending_manager_calls=1,
    )
    try:
        with pytest.raises(NativeManagerPortError, match="reply unconfirmed"):
            await broker.dispatch_once(first, reviewed(first), trusted_peer_uid=UID)
        assert len(broker._pending_manager_calls) == 1
        with pytest.raises(NativeManagerPortError, match="pending manager"):
            await broker.dispatch_once(
                second, reviewed(second), trusted_peer_uid=UID,
            )
        assert len(manager.calls) == 1
        rows = store.list_reservations()
        assert len(rows) == 1
        assert rows[0].run_id == first.run_id
        assert rows[0].phase is NativeLedgerPhase.UNKNOWN_FENCED
    finally:
        manager.release.set()
        await asyncio.wait_for(
            asyncio.gather(*tuple(broker._pending_manager_calls)), timeout=2,
        )
        await asyncio.sleep(0)

    assert not broker._pending_manager_calls
    # The other GPU may be reserved after its *separate* pending slot frees,
    # but the first run must remain fenced forever without trusted release.
    second_receipt = await broker.dispatch_once(
        second, reviewed(second), trusted_peer_uid=UID,
    )
    assert second_receipt.phase is NativeLedgerPhase.UNKNOWN_FENCED
    assert len(manager.calls) == 2
    by_run = {row.run_id: row for row in store.list_reservations()}
    assert by_run[first.run_id].phase is NativeLedgerPhase.UNKNOWN_FENCED
    assert by_run[second.run_id].phase is NativeLedgerPhase.UNKNOWN_FENCED


@pytest.mark.parametrize("invalid", [0, -1, True, False, 17, None, "3", 1.5])
def test_pending_manager_limit_cannot_be_unbounded_or_non_integer(tmp_path, invalid):
    with pytest.raises(ValueError, match="max_pending_manager_calls"):
        NativeBrokerDispatchCoordinator(
            ledger(tmp_path),
            FakeManager(),
            max_pending_manager_calls=invalid,
        )
