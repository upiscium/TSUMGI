"""Source-only native-run identity and release contract for TSUMGI #240.

NO broker, D-Bus, systemd or cgroup interaction exists in this module. The
objects below encode *claimed* authorization and *reported* observations,
not proof of a trusted monitor, secure IPC or kernel containment. They are
not wired into Worker startup or production Serving admission.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from uuid import UUID

_SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ID = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?\Z")
_GPU = re.compile(r"GPU-[A-Za-z0-9-]{1,90}\Z")
_UNIT_INVOCATION = re.compile(r"[0-9a-f]{32}\Z")
_STORE_BINARY = re.compile(
    r"/nix/store/[a-z0-9]{32}-[A-Za-z0-9+._-]+/"
    r"(?:[A-Za-z0-9+._-]+/)*[A-Za-z0-9+._-]+\Z"
)
_MAX_EVENTS_BYTES = 4096
_EVENT_NAME = re.compile(r"[a-z][a-z0-9_]*\Z")


class NativeRunContractError(ValueError):
    """Structural or state-transition violation. No side effects are taken."""


class NativeRunPhase(StrEnum):
    RESERVED = "reserved"
    STARTING = "starting"
    LIVE = "live"
    DRAINING = "draining"
    TERM_ACKED = "term-acked"
    KILL_ACKED = "kill-acked"
    EMPTY_VERIFIED = "empty-verified"
    RELEASED = "released"
    UNKNOWN_FENCED = "unknown-fenced"


def _uuid4(value: str, field: str) -> str:
    if type(value) is not str:
        raise NativeRunContractError(f"{field} must be canonical UUIDv4")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as exc:
        raise NativeRunContractError(
            f"{field} must be canonical UUIDv4"
        ) from exc
    if parsed.version != 4 or str(parsed) != value:
        raise NativeRunContractError(f"{field} must be canonical UUIDv4")
    return value


def _sha(value: str, field: str) -> str:
    if type(value) is not str or _SHA.fullmatch(value) is None:
        raise NativeRunContractError(f"{field} must be canonical SHA256")
    return value


def _absolute_path(value: str, field: str) -> str:
    if (
        type(value) is not str
        or not value.startswith("/")
        or "//" in value
        or "\\" in value
        or any(part in {".", ".."} for part in value.split("/"))
        or any(ord(ch) < 33 or ord(ch) > 126 for ch in value)
        or str(PurePosixPath(value)) != value
    ):
        raise NativeRunContractError(f"{field} must be a canonical absolute path")
    return value


@dataclass(frozen=True, slots=True)
class NativeLaunchRequest:
    """Pure data request; not authenticated by shape validation alone."""

    run_id: str
    worker_id: str
    worker_epoch: str
    deployment_revision: str
    executable: str
    model_ref: str
    gpu_uuids: tuple[str, ...]

    def __post_init__(self) -> None:
        _uuid4(self.run_id, "run_id")
        _uuid4(self.worker_epoch, "worker_epoch")
        if type(self.worker_id) is not str or _ID.fullmatch(self.worker_id) is None:
            raise NativeRunContractError("invalid worker_id")
        _sha(self.deployment_revision, "deployment_revision")
        if (
            type(self.executable) is not str
            or _STORE_BINARY.fullmatch(self.executable) is None
            or any(part in {".", ".."} for part in self.executable.split("/"))
            or str(PurePosixPath(self.executable)) != self.executable
        ):
            raise NativeRunContractError("executable must be a Nix store binary")
        _absolute_path(self.model_ref, "model_ref")
        if type(self.gpu_uuids) is not tuple or not self.gpu_uuids:
            raise NativeRunContractError("gpu_uuids must be a nonempty tuple")
        if any(
            type(value) is not str or _GPU.fullmatch(value) is None
            for value in self.gpu_uuids
        ) or len(set(self.gpu_uuids)) != len(self.gpu_uuids):
            raise NativeRunContractError("invalid or duplicate gpu_uuids")

    @property
    def unit_name(self) -> str:
        # Fixed, non-user-supplied systemd service name. A broker must also
        # reserve it durably and reject collisions/old outstanding runs.
        return f"tsumgi-native-{UUID(self.run_id).hex}.service"

    @property
    def cgroup_path(self) -> str:
        # Phase B0 assumes system.slice. A different slice is not auto-trusted.
        return f"/system.slice/{self.unit_name}"


@dataclass(frozen=True, slots=True)
class NativeLaunchAllowlist:
    """Administrator-reviewed exact binding; the broker must authenticate it."""

    worker_id: str
    deployment_revision: str
    executable: str
    model_ref: str
    gpu_uuids: tuple[str, ...]

    def permits(self, request: NativeLaunchRequest) -> bool:
        return isinstance(request, NativeLaunchRequest) and (
            request.worker_id == self.worker_id
            and request.deployment_revision == self.deployment_revision
            and request.executable == self.executable
            and request.model_ref == self.model_ref
            and request.gpu_uuids == self.gpu_uuids
        )


@dataclass(frozen=True, slots=True)
class NativeRunUnitIdentity:
    """Claimed manager identity; only a trusted PID1 adapter may populate it."""

    run_id: str
    worker_epoch: str
    deployment_revision: str
    unit_name: str
    cgroup_path: str
    manager_invocation_id: str

    def __post_init__(self) -> None:
        _uuid4(self.run_id, "run_id")
        _uuid4(self.worker_epoch, "worker_epoch")
        _sha(self.deployment_revision, "deployment_revision")
        if (
            type(self.unit_name) is not str
            or self.unit_name != f"tsumgi-native-{UUID(self.run_id).hex}.service"
        ):
            raise NativeRunContractError("unexpected native unit identity")
        if (
            type(self.cgroup_path) is not str
            or self.cgroup_path != f"/system.slice/{self.unit_name}"
        ):
            raise NativeRunContractError("unexpected native cgroup identity")
        if (
            type(self.manager_invocation_id) is not str
            or _UNIT_INVOCATION.fullmatch(self.manager_invocation_id) is None
            or self.manager_invocation_id == "0" * 32
        ):
            raise NativeRunContractError("invalid systemd InvocationID")


def parse_cgroup_events(raw: bytes) -> dict[str, int]:
    """Parse a bounded cgroup.events snapshot, not a kernel attestation."""
    if type(raw) is not bytes or not raw or len(raw) > _MAX_EVENTS_BYTES:
        raise NativeRunContractError("invalid bounded cgroup.events")
    try:
        lines = raw.decode("ascii").splitlines()
        parsed: dict[str, int] = {}
        for line in lines:
            key, value = line.split(" ")
            if (
                _EVENT_NAME.fullmatch(key) is None
                or key in parsed
                or value not in {"0", "1"}
            ):
                raise ValueError("unexpected event field")
            parsed[key] = int(value)
        if "populated" not in parsed:
            raise ValueError("missing populated")
        return parsed
    except (UnicodeError, ValueError) as exc:
        raise NativeRunContractError(
            "malformed or missing cgroup.events populated indicator"
        ) from exc


@dataclass(frozen=True, slots=True)
class NativeStopObservation:
    """Reported state supplied by a *future trusted* manager/cgroup reader."""

    identity: NativeRunUnitIdentity
    cgroup_events: bytes
    manager_inactive: bool
    wrapper_reaped: bool

    def __post_init__(self) -> None:
        if not isinstance(self.identity, NativeRunUnitIdentity):
            raise NativeRunContractError("missing manager identity")
        if type(self.manager_inactive) is not bool or type(self.wrapper_reaped) is not bool:
            raise NativeRunContractError("invalid stop acknowledgement")
        parse_cgroup_events(self.cgroup_events)


class NativeRunTransitionContract:
    """In-memory state machine; NOT a durable or concurrency-safe run ledger."""

    def __init__(
        self, request: NativeLaunchRequest, policy: NativeLaunchAllowlist,
    ) -> None:
        if not isinstance(request, NativeLaunchRequest) or not isinstance(
            policy, NativeLaunchAllowlist
        ) or not policy.permits(request):
            raise NativeRunContractError("native run is not allowlisted")
        self.request = request
        self.phase = NativeRunPhase.RESERVED
        self.identity: NativeRunUnitIdentity | None = None

    def _require(self, *allowed: NativeRunPhase) -> None:
        if self.phase not in allowed:
            raise NativeRunContractError(
                f"illegal native-run transition from {self.phase.value}"
            )

    def _match(self, identity: NativeRunUnitIdentity) -> None:
        if (
            not isinstance(identity, NativeRunUnitIdentity)
            or identity.run_id != self.request.run_id
            or identity.worker_epoch != self.request.worker_epoch
            or identity.deployment_revision != self.request.deployment_revision
            or identity.unit_name != self.request.unit_name
            or identity.cgroup_path != self.request.cgroup_path
        ):
            self.phase = NativeRunPhase.UNKNOWN_FENCED
            raise NativeRunContractError("native run manager identity mismatch")
        if self.identity is not None and identity != self.identity:
            self.phase = NativeRunPhase.UNKNOWN_FENCED
            raise NativeRunContractError("native run manager invocation changed")

    def record_starting(self, identity: NativeRunUnitIdentity) -> None:
        self._require(NativeRunPhase.RESERVED)
        self._match(identity)
        self.identity = identity
        self.phase = NativeRunPhase.STARTING

    def record_ready(self, identity: NativeRunUnitIdentity) -> None:
        self._require(NativeRunPhase.STARTING)
        self._match(identity)
        self.phase = NativeRunPhase.LIVE

    def record_stop_requested(self) -> None:
        self._require(NativeRunPhase.STARTING, NativeRunPhase.LIVE)
        self.phase = NativeRunPhase.DRAINING

    def record_term_ack(self) -> None:
        self._require(NativeRunPhase.DRAINING)
        self.phase = NativeRunPhase.TERM_ACKED

    def record_kill_ack(self) -> None:
        self._require(NativeRunPhase.TERM_ACKED)
        self.phase = NativeRunPhase.KILL_ACKED

    def record_empty(self, observation: NativeStopObservation) -> None:
        self._require(NativeRunPhase.TERM_ACKED, NativeRunPhase.KILL_ACKED)
        if not isinstance(observation, NativeStopObservation):
            raise NativeRunContractError("missing trusted stop observation")
        self._match(observation.identity)
        if (
            parse_cgroup_events(observation.cgroup_events)["populated"] != 0
            or not observation.manager_inactive
            or not observation.wrapper_reaped
        ):
            raise NativeRunContractError("native cgroup not verified empty")
        self.phase = NativeRunPhase.EMPTY_VERIFIED

    def record_release(self) -> None:
        self._require(NativeRunPhase.EMPTY_VERIFIED)
        self.phase = NativeRunPhase.RELEASED

    def fence_unknown(self) -> None:
        if self.phase is not NativeRunPhase.RELEASED:
            self.phase = NativeRunPhase.UNKNOWN_FENCED
