"""TSUMGI #240 B1b0: OFFLINE nested cgroup observation contract.

No systemd, D-Bus, /sys/fs/cgroup, subprocess or Worker operations are
performed here. These fields are *claims*, not trusted PID1/kernel evidence.
In particular successful structural validation MUST NOT release a B1a GPU
reservation or authorize a new native process.
"""

from __future__ import annotations

from dataclasses import dataclass

from .native_cgroup_contract import (
    NativeLaunchRequest,
    NativeRunUnitIdentity,
    parse_cgroup_events,
)
from .native_systemd_port import NativeManagerPortError, ReviewedNativeCommand


@dataclass(frozen=True, slots=True)
class OfflineCgroupObjectKey:
    """Claimed st_dev/st_ino from one held cgroup or events descriptor."""

    device: int
    inode: int

    def __post_init__(self) -> None:
        if (
            type(self.device) is not int
            or type(self.inode) is not int
            or self.device <= 0
            or self.inode <= 0
        ):
            raise NativeManagerPortError("invalid cgroup object identity")


@dataclass(frozen=True, slots=True)
class OfflineNestedCgroupSamples:
    """Synthetic, untrusted monitor/native subgroup observations.

    The real supervisor MUST create both subgroups under a uniquely
    authorized run Unit. It must read live and terminal events through
    generation-bound descriptors *before* stopping the parent Unit.
    """

    unit: NativeRunUnitIdentity
    monitor_path: str
    native_path: str
    native_key_before: OfflineCgroupObjectKey
    native_key_after: OfflineCgroupObjectKey
    events_key_before: OfflineCgroupObjectKey
    events_key_after: OfflineCgroupObjectKey
    native_events_live: bytes
    native_events_empty: bytes
    parent_events_while_monitor_alive: bytes
    native_effective_uid: int
    wrapper_reaped: bool

    def __post_init__(self) -> None:
        if (
            not isinstance(self.unit, NativeRunUnitIdentity)
            or type(self.monitor_path) is not str
            or type(self.native_path) is not str
            or any(
                not isinstance(value, OfflineCgroupObjectKey)
                for value in (
                    self.native_key_before, self.native_key_after,
                    self.events_key_before, self.events_key_after,
                )
            )
            or type(self.native_effective_uid) is not int
            or self.native_effective_uid <= 0
            or type(self.wrapper_reaped) is not bool
        ):
            raise NativeManagerPortError("malformed offline native cgroup sample")
        # Do not substitute a missing read or a deleted cgroup with an
        # inferred 'populated 0'. Parsing itself rejects empty/malformed IO.
        parse_cgroup_events(self.native_events_live)
        parse_cgroup_events(self.native_events_empty)
        parse_cgroup_events(self.parent_events_while_monitor_alive)


def validate_offline_nested_empty_candidate(
    request: NativeLaunchRequest,
    policy: ReviewedNativeCommand,
    samples: OfflineNestedCgroupSamples,
    *,
    initial_unit: NativeRunUnitIdentity,
) -> None:
    """Only validate synthetic shape. Return NO release or readiness grant.

    The two-level candidate keeps monitor alive while 'native' becomes
    empty, then a separately reviewed supervisor would seal/delete the
    native subtree and stop the parent service. B1a has deliberately no
    release API until trusted PID1+kernel attestation and crash recovery.
    """
    if (
        not isinstance(request, NativeLaunchRequest)
        or not isinstance(policy, ReviewedNativeCommand)
        or not isinstance(samples, OfflineNestedCgroupSamples)
        or not policy.allowlist.permits(request)
    ):
        raise NativeManagerPortError("offline native cgroup policy mismatch")
    # A fresh stopped/restarted Unit with the same name may have a *different*
    # InvocationID. Tie all stop samples to the original start generation,
    # not merely to the immutable run ID and cgroup path strings.
    if (
        not isinstance(initial_unit, NativeRunUnitIdentity)
        or samples.unit != initial_unit
    ):
        raise NativeManagerPortError("offline parent Unit generation or invocation changed")
    if (
        samples.unit.run_id != request.run_id
        or samples.unit.worker_epoch != request.worker_epoch
        or samples.unit.deployment_revision != request.deployment_revision
        or samples.unit.unit_name != request.unit_name
        or samples.unit.cgroup_path != request.cgroup_path
    ):
        raise NativeManagerPortError("offline parent Unit generation mismatch")
    if (
        samples.monitor_path != request.cgroup_path + "/monitor"
        or samples.native_path != request.cgroup_path + "/native"
        or samples.monitor_path == samples.native_path
    ):
        raise NativeManagerPortError("offline monitor/native cgroup scope mismatch")
    if (
        samples.native_key_before != samples.native_key_after
        or samples.events_key_before != samples.events_key_after
    ):
        raise NativeManagerPortError("offline cgroup generation changed during stop")
    if samples.native_effective_uid != policy.worker_uid:
        raise NativeManagerPortError("offline native process identity is not least-privileged")
    if not samples.wrapper_reaped:
        raise NativeManagerPortError("native wrapper has not been reaped")
    if (
        parse_cgroup_events(samples.native_events_live)["populated"] != 1
        or parse_cgroup_events(samples.native_events_empty)["populated"] != 0
        or parse_cgroup_events(
            samples.parent_events_while_monitor_alive
        )["populated"] != 1
    ):
        raise NativeManagerPortError(
            "native cgroup did not become empty while monitor was still alive"
        )
    # No state transition and no GPU release: producer of 'samples' may be
    # malicious or stale. A future trusted manager must also prove atomic
    # pre-exec placement, no cgroup escapes/repopulation, and durable stop.
