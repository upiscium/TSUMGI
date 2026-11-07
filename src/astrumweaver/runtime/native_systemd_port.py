"""Offline PID1 StartTransientUnit port and dispatch coordinator (#240 B1b0).

This module has NO concrete D-Bus transport, no systemd-run invocation, and
no Worker integration. Its injected manager is for isolated fake tests. A
future *privileged, authenticated* broker must implement that transport and
attest unit/cgroup identity before ever declaring readiness or releasing GPUs.

The manager's start JOB reply is *not* native process readiness, cgroup
placement, process exit, or an authorization to retry a lost launch.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from dataclasses import dataclass
from typing import Protocol

from .native_cgroup_contract import NativeLaunchAllowlist, NativeLaunchRequest
from .native_cgroup_ledger import (
    NativeLedgerError,
    NativeLedgerPhase,
    NativeRunReservationLedger,
)

_log = logging.getLogger(__name__)
_JOB_OBJECT = re.compile(r"/org/freedesktop/systemd1/job/[1-9][0-9]*\Z")
_MAX_ARG_COUNT = 32
_MAX_ARG_BYTES = 8192


class NativeManagerPortError(RuntimeError):
    """No pre-exec placement/manager acknowledgement can be certified."""


@dataclass(frozen=True, slots=True)
class ReviewedNativeCommand:
    """Exact administrator-reviewed exec vector; never from client JSON.

    The Broker MUST load this from independently authorized immutable
    configuration. The type is not a security capability by itself.
    """

    allowlist: NativeLaunchAllowlist
    argv: tuple[str, ...]
    worker_uid: int

    def __post_init__(self) -> None:
        if not isinstance(self.allowlist, NativeLaunchAllowlist):
            raise NativeManagerPortError("missing immutable native allowlist")
        if type(self.worker_uid) is not int or self.worker_uid <= 0:
            raise NativeManagerPortError("native worker UID must be unprivileged")
        if type(self.argv) is not tuple or not 2 <= len(self.argv) <= _MAX_ARG_COUNT:
            raise NativeManagerPortError("native command argument count is invalid")
        if any(
            type(a) is not str or not a or "\x00" in a or "\n" in a or "\r" in a
            or len(a.encode("utf-8")) > 4096
            for a in self.argv
        ):
            raise NativeManagerPortError("invalid reviewed native executable arguments")
        if sum(len(a.encode("utf-8")) + 1 for a in self.argv) > _MAX_ARG_BYTES:
            raise NativeManagerPortError("native argv is not bounded")
        if self.argv[0] != self.allowlist.executable:
            raise NativeManagerPortError("native executable does not match policy")
        if self.argv.count(self.allowlist.model_ref) != 1:
            raise NativeManagerPortError("native model ref must occur exactly once")

    def check(self, request: NativeLaunchRequest, *, trusted_peer_uid: int) -> None:
        # The future IPC transport MUST derive trusted_peer_uid from SO_PEERCRED
        # and must not accept it from a client-provided JSON field.
        if (
            not isinstance(request, NativeLaunchRequest)
            or not self.allowlist.permits(request)
            or type(trusted_peer_uid) is not int
            or trusted_peer_uid != self.worker_uid
        ):
            raise NativeManagerPortError("native run is not allowed by broker policy")


@dataclass(frozen=True, slots=True)
class StartTransientUnitPlan:
    """Exact fixed-property plan; NOT a D-Bus call and NOT deployable yet."""

    unit_name: str
    mode: str
    properties: tuple[tuple[str, object], ...]
    auxiliary_units: tuple[()] = ()

    @classmethod
    def for_request(
        cls,
        request: NativeLaunchRequest,
        policy: ReviewedNativeCommand,
        *,
        trusted_peer_uid: int,
    ) -> StartTransientUnitPlan:
        policy.check(request, trusted_peer_uid=trusted_peer_uid)
        # Deliberately no arbitrary property dict, shell command, root user,
        # Delegate, privileged capabilities, or caller-chosen unit.
        #
        # Phase B1b0 DOES NOT solve GPU device policy, network/UDS access,
        # model runtime arguments, or systemd version compatibility. This
        # plan must be independently reviewed before a real transport exists.
        props: tuple[tuple[str, object], ...] = (
            ("Description", f"TSUMGI native run {request.run_id}"),
            ("Type", "exec"),
            ("Slice", "system.slice"),
            ("User", "astrumweaver"),
            ("Group", "astrumweaver"),
            ("ExecStart", (request.executable, policy.argv, False)),
            ("Restart", "no"),
            ("KillMode", "control-group"),
            ("NoNewPrivileges", True),
            ("ProtectControlGroups", True),
            ("ProtectSystem", "strict"),
            ("ProtectHome", True),
            ("PrivateTmp", True),
            ("ProtectKernelTunables", True),
            ("ProtectKernelModules", True),
            ("DevicePolicy", "closed"),
        )
        return cls(
            unit_name=request.unit_name,
            mode="fail",  # never replace an already-running native unit
            properties=props,
            auxiliary_units=(),
        )


class NativeSystemdManagerPort(Protocol):
    """A future trusted manager adapter; no real implementation is shipped."""

    async def start_transient_unit(self, plan: StartTransientUnitPlan) -> str:
        """Queue one unit start and return a Job object path (not readiness)."""
        ...


@dataclass(frozen=True, slots=True)
class NativeStartPendingReceipt:
    """A job-path acknowledgement, explicitly NOT serving/ownership release."""

    run_id: str
    unit_name: str
    manager_job_path: str
    phase: NativeLedgerPhase = NativeLedgerPhase.UNKNOWN_FENCED


class NativeBrokerDispatchCoordinator:
    """Source-only dispatch sequencing, injected fake manager interface.

    Production must NOT use this as a complete Broker: it neither authenticates
    callers nor verifies PID1/cgroup snapshots or safely releases reservations.
    """

    def __init__(
        self,
        ledger: NativeRunReservationLedger,
        manager: NativeSystemdManagerPort,
        *,
        manager_ack_timeout_seconds: float = 30.0,
    ) -> None:
        if not isinstance(ledger, NativeRunReservationLedger):
            raise NativeManagerPortError("native ledger required")
        if not callable(getattr(manager, "start_transient_unit", None)):
            raise NativeManagerPortError("native manager port required")
        if (
            type(manager_ack_timeout_seconds) not in {float, int}
            or not math.isfinite(manager_ack_timeout_seconds)
            or not 0 < manager_ack_timeout_seconds <= 30.0
        ):
            raise ValueError(
                "manager_ack_timeout_seconds must be finite, positive and <= 30"
            )
        self._ledger = ledger
        self._manager = manager
        self._manager_ack_timeout_seconds = manager_ack_timeout_seconds
        # Retain unresolved Manager calls until their tasks finish. A
        # timeout/cancel can never prove PID1 did not create the service.
        # The persistent ledger keeps that run UNKNOWN_FENCED regardless.
        self._pending_manager_calls: set[asyncio.Task[str]] = set()

    def _finish_manager_call(self, task: asyncio.Task[str]) -> None:
        self._pending_manager_calls.discard(task)
        # Harvest a late error so the event loop doesn't warn about an
        # unhandled task exception. NEVER turn a late job path into READY
        # or free the durably reserved GPU; B1b1 needs trusted reconciliation.
        try:
            task.result()
        except asyncio.CancelledError:
            _log.warning("native manager operation cancelled; ownership stays fenced")
        except Exception as exc:  # noqa: BLE001 - harvest all late task failures
            # Keep untrusted error text out of logs; no claim of GPU release.
            _log.warning(
                "native manager operation ended with %s; ownership stays fenced",
                type(exc).__name__,
            )

    async def dispatch_once(
        self,
        request: NativeLaunchRequest,
        policy: ReviewedNativeCommand,
        *,
        trusted_peer_uid: int,
    ) -> NativeStartPendingReceipt:
        if not isinstance(policy, ReviewedNativeCommand):
            raise NativeManagerPortError("admin-reviewed native command required")
        # Perform all local shape/authorization checks *before* reserving GPUs.
        plan = StartTransientUnitPlan.for_request(
            request, policy, trusted_peer_uid=trusted_peer_uid,
        )
        try:
            reservation = self._ledger.reserve(
                request, policy.allowlist, trusted_peer_uid=trusted_peer_uid,
            )
        except NativeLedgerError as exc:
            raise NativeManagerPortError("native launch reservation denied") from exc

        # An exact idempotent replay is NOT a second launch authorization.
        if not reservation.newly_reserved:
            raise NativeManagerPortError(
                "native run is already reserved; manager reconciliation required"
            )

        try:
            # This MUST commit durably BEFORE the first manager invocation.
            self._ledger.mark_start_intent(
                request.run_id, trusted_peer_uid=trusted_peer_uid,
            )
        except NativeLedgerError as exc:
            raise NativeManagerPortError(
                "unable to fence native launch before manager operation"
            ) from exc

        # Even a successful job path does not prove the service began or that
        # the cgroup contains the native process. Unknown manager outcomes,
        # cancellations or malformed replies leave the durable ledger
        # UNKNOWN_FENCED and forbid retry. No cleanup or auto-unfencing here.
        #
        # asyncio.wait_for(coroutine) is NOT reliably bounded if the
        # coroutine suppresses cancellation. Keep the manager operation in
        # a separate owned task and shield it: only the CALLER's reply wait
        # expires. The underlying D-Bus request may still complete later,
        # so the persistent run's GPU fence must NOT be cleared.
        operation = asyncio.create_task(self._manager.start_transient_unit(plan))
        self._pending_manager_calls.add(operation)
        operation.add_done_callback(self._finish_manager_call)
        try:
            job_path = await asyncio.wait_for(
                asyncio.shield(operation),
                timeout=self._manager_ack_timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise NativeManagerPortError(
                "native manager reply unconfirmed; reconciliation required"
            ) from exc
        if type(job_path) is not str or _JOB_OBJECT.fullmatch(job_path) is None:
            raise NativeManagerPortError(
                "native manager returned untrusted job identity"
            )
        return NativeStartPendingReceipt(
            run_id=request.run_id,
            unit_name=plan.unit_name,
            manager_job_path=job_path,
        )


@dataclass(frozen=True, slots=True)
class DbusVariantShape:
    """Structural D-Bus variant for offline inspection; NOT a marshalled value."""

    signature: str
    value: object


@dataclass(frozen=True, slots=True)
class SystemdStartTransientCallShape:
    """Exact PID1 Manager call shape; this class does NOT transmit messages."""

    destination: str
    object_path: str
    interface: str
    method: str
    signature: str
    args: tuple[object, ...]

    @classmethod
    def for_request(
        cls,
        request: NativeLaunchRequest,
        policy: ReviewedNativeCommand,
        *,
        trusted_peer_uid: int,
    ) -> SystemdStartTransientCallShape:
        plan = StartTransientUnitPlan.for_request(
            request, policy, trusted_peer_uid=trusted_peer_uid,
        )
        string_props = {
            "Description", "Type", "Slice", "User", "Group", "Restart",
            "KillMode", "ProtectSystem", "DevicePolicy",
        }
        bool_props = {
            "NoNewPrivileges", "ProtectControlGroups", "ProtectHome",
            "PrivateTmp", "ProtectKernelTunables", "ProtectKernelModules",
        }
        # Validate the exact property allowlist even in the offline encoder.
        props: list[tuple[str, DbusVariantShape]] = []
        seen: set[str] = set()
        for name, raw in plan.properties:
            if name in seen:
                raise NativeManagerPortError("duplicate systemd property")
            seen.add(name)
            if name in string_props and type(raw) is str:
                props.append((name, DbusVariantShape("s", raw)))
            elif name in bool_props and type(raw) is bool and raw is True:
                props.append((name, DbusVariantShape("b", raw)))
            elif name == "ExecStart":
                if (
                    type(raw) is not tuple or len(raw) != 3
                    or raw[0] != request.executable
                    or raw[1] != policy.argv
                    or raw[2] is not False
                ):
                    raise NativeManagerPortError("invalid ExecStart manager property")
                props.append((
                    name,
                    DbusVariantShape("a(sasb)", ((raw[0], raw[1], False),)),
                ))
            else:
                raise NativeManagerPortError("unexpected systemd property")
        if seen != string_props | bool_props | {"ExecStart"}:
            raise NativeManagerPortError("incomplete systemd isolation properties")
        return cls(
            destination="org.freedesktop.systemd1",
            object_path="/org/freedesktop/systemd1",
            interface="org.freedesktop.systemd1.Manager",
            method="StartTransientUnit",
            # s name, s mode, a(sv) properties, a(sa(sv)) aux.
            signature="ssa(sv)a(sa(sv))",
            args=(plan.unit_name, plan.mode, tuple(props), ()),
        )


class NativeOfflineDbusTransport(Protocol):
    """Offline test transport accepting one validated manager method shape."""

    async def call_manager(self, call: SystemdStartTransientCallShape) -> object:
        ...


class ShapedSystemdManagerPort:
    """Strict adapter through a *provided* transport, not a live system bus.

    The trusted Broker must separately load its immutable policy and peer UID
    from privileged sources. Passing a fake transport here cannot establish
    a real PID1 manager identity or permit GPU service activation.
    """

    def __init__(
        self,
        request: NativeLaunchRequest,
        policy: ReviewedNativeCommand,
        *,
        trusted_peer_uid: int,
        transport: NativeOfflineDbusTransport,
    ) -> None:
        if not callable(getattr(transport, "call_manager", None)):
            raise NativeManagerPortError("offline manager call transport is missing")
        self._expected_plan = StartTransientUnitPlan.for_request(
            request, policy, trusted_peer_uid=trusted_peer_uid,
        )
        self._call = SystemdStartTransientCallShape.for_request(
            request, policy, trusted_peer_uid=trusted_peer_uid,
        )
        self._transport = transport

    async def start_transient_unit(self, plan: StartTransientUnitPlan) -> str:
        # Do not let a compromised coordinator substitute mode, argv, UIDs,
        # systemd properties or the unit name after policy approval.
        if type(plan) is not StartTransientUnitPlan or plan != self._expected_plan:
            raise NativeManagerPortError("manager call differs from approved plan")
        reply = await self._transport.call_manager(self._call)
        # The coordinator separately rejects invalid/missing Job paths.
        return reply
