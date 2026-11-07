"""Disposable TSUMGI #240 contract tests. No cgroup files or systemd operations."""

from dataclasses import replace

import pytest

from astrumweaver.runtime.native_cgroup_contract import (
    NativeLaunchAllowlist,
    NativeLaunchRequest,
    NativeRunContractError,
    NativeRunPhase,
    NativeRunTransitionContract,
    NativeRunUnitIdentity,
    NativeStopObservation,
    parse_cgroup_events,
)

RUN_ID = "12345678-1234-4234-8234-123456789abc"
EPOCH = "99999999-9999-4999-8999-999999999999"
REVISION = "sha256:" + "a" * 64
EXE = "/nix/store/" + "0" * 32 + "-llama-bin/bin/llama-server"
MODEL = "/models/artificial-not-actual.gguf"
GPUS = ("GPU-00000000-0000-0000-0000-000000000001",)
UNIT = "tsumgi-native-12345678123442348234123456789abc.service"
CGROUP = "/system.slice/" + UNIT


def request() -> NativeLaunchRequest:
    return NativeLaunchRequest(
        run_id=RUN_ID, worker_id="sirius-worker",
        worker_epoch=EPOCH, deployment_revision=REVISION,
        executable=EXE, model_ref=MODEL, gpu_uuids=GPUS,
    )


def policy() -> NativeLaunchAllowlist:
    return NativeLaunchAllowlist(
        worker_id="sirius-worker",
        deployment_revision=REVISION,
        executable=EXE, model_ref=MODEL, gpu_uuids=GPUS,
    )


def identity() -> NativeRunUnitIdentity:
    return NativeRunUnitIdentity(
        run_id=RUN_ID, worker_epoch=EPOCH,
        deployment_revision=REVISION,
        unit_name=UNIT, cgroup_path=CGROUP,
        manager_invocation_id="f" * 32,
    )


def prepared() -> NativeRunTransitionContract:
    machine = NativeRunTransitionContract(request(), policy())
    machine.record_starting(identity())
    machine.record_ready(identity())
    machine.record_stop_requested()
    machine.record_term_ack()
    return machine


def proof(
    *,
    events: bytes = b"populated 0\nfrozen 0\n",
    manager_inactive: bool = True,
    wrapper_reaped: bool = True,
    unit: NativeRunUnitIdentity | None = None,
) -> NativeStopObservation:
    return NativeStopObservation(
        identity=identity() if unit is None else unit,
        cgroup_events=events,
        manager_inactive=manager_inactive,
        wrapper_reaped=wrapper_reaped,
    )


def test_allowed_run_generates_only_bounded_deterministic_service_identity():
    req = request()
    assert req.unit_name == UNIT
    assert req.cgroup_path == CGROUP
    assert policy().permits(req)
    machine = NativeRunTransitionContract(req, policy())
    assert machine.phase is NativeRunPhase.RESERVED
    assert machine.identity is None


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("worker_id", "another-worker"),
        ("deployment_revision", "sha256:" + "b" * 64),
        ("executable", "/nix/store/" + "1" * 32 + "-other/bin/llama-server"),
        ("model_ref", "/models/other.gguf"),
        ("gpu_uuids", ("GPU-OTHER",)),
    ],
)
def test_non_allowlisted_run_never_becomes_reserved(key, value):
    modified = replace(request(), **{key: value})
    assert not policy().permits(modified)
    with pytest.raises(NativeRunContractError, match="not allowlisted"):
        NativeRunTransitionContract(modified, policy())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("run_id", "12345678-1234-1234-8234-123456789abc"),
        ("run_id", "../bad"),
        ("worker_epoch", "99999999-9999-4999-8999-999999999998Z"),
        ("worker_id", "a/../escape"),
        ("worker_id", "X" * 80),
        ("deployment_revision", "sha256:WRONG"),
        ("executable", "/usr/bin/llama-server"),
        ("executable", "/nix/store/" + "0" * 32 + "-pkg/../../bin/llama-server"),
        ("model_ref", "/models/../secret.gguf"),
        ("model_ref", "relative.gguf"),
        ("model_ref", "/models/new line\n.gguf"),
        ("gpu_uuids", ()),
        ("gpu_uuids", (GPUS[0], GPUS[0])),
        ("gpu_uuids", ("GPU-OK", "bad/uuid")),
    ],
)
def test_native_run_request_rejects_untrusted_paths_or_identity(field, value):
    with pytest.raises(NativeRunContractError):
        replace(request(), **{field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("run_id", "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"),
        ("worker_epoch", "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"),
        ("deployment_revision", "sha256:" + "b" * 64),
        ("unit_name", "arbitrary.service"),
        ("cgroup_path", "/system.slice/other.service"),
        ("manager_invocation_id", "0" * 32),
        ("manager_invocation_id", "unexpected"),
    ],
)
def test_unit_identity_rejects_forged_binding(field, value):
    if field in {"run_id", "worker_epoch", "deployment_revision"}:
        # Structurally valid but belongs to a different run or generation.
        if field == "run_id":
            # A *self-consistent* foreign service name must still be
            # rejected against the originally reserved native run.
            other_unit = "tsumgi-native-" + value.replace("-", "") + ".service"
            wrong = replace(
                identity(), run_id=value, unit_name=other_unit,
                cgroup_path="/system.slice/" + other_unit,
            )
        else:
            wrong = replace(identity(), **{field: value})
        machine = NativeRunTransitionContract(request(), policy())
        with pytest.raises(NativeRunContractError, match="identity mismatch"):
            machine.record_starting(wrong)
        assert machine.phase is NativeRunPhase.UNKNOWN_FENCED
    else:
        with pytest.raises(NativeRunContractError):
            replace(identity(), **{field: value})


@pytest.mark.parametrize(
    "events",
    [
        b"",
        b"frozen 0\n",
        b"populated 0\npopulated 1\n",
        b"populated 2\n",
        b"populated -1\n",
        b"populated 0 1\n",
        b"populated 0\nother bad\n",
        b"populated 0\rwrong\n",
        b"populated 0\n" + b"x" * 4096,
        b"populated 0\n\xff",
    ],
)
def test_ambiguous_or_unbounded_cgroup_events_are_denied(events):
    with pytest.raises(NativeRunContractError):
        parse_cgroup_events(events)


def test_stop_release_requires_recursive_empty_and_reaped_wrapper_and_unit():
    machine = prepared()
    for details in (
        {"events": b"populated 1\nfrozen 0\n"},
        {"manager_inactive": False},
        {"wrapper_reaped": False},
    ):
        with pytest.raises(NativeRunContractError, match="not verified empty"):
            machine.record_empty(proof(**details))
        assert machine.phase is NativeRunPhase.TERM_ACKED
        with pytest.raises(NativeRunContractError):
            machine.record_release()
    machine.record_kill_ack()
    machine.record_empty(proof())
    assert machine.phase is NativeRunPhase.EMPTY_VERIFIED
    machine.record_release()
    assert machine.phase is NativeRunPhase.RELEASED
    with pytest.raises(NativeRunContractError):
        machine.record_starting(identity())


def test_unit_invocation_replacement_fences_all_later_transitions():
    machine = prepared()
    new_invocation = replace(identity(), manager_invocation_id="e" * 32)
    with pytest.raises(NativeRunContractError, match="invocation changed"):
        machine.record_empty(proof(unit=new_invocation))
    assert machine.phase is NativeRunPhase.UNKNOWN_FENCED
    with pytest.raises(NativeRunContractError):
        machine.record_empty(proof())
    with pytest.raises(NativeRunContractError):
        machine.record_release()


def test_phase_order_rejects_ready_release_before_first_manager_ack():
    machine = NativeRunTransitionContract(request(), policy())
    with pytest.raises(NativeRunContractError):
        machine.record_ready(identity())
    with pytest.raises(NativeRunContractError):
        machine.record_release()
    machine.record_starting(identity())
    with pytest.raises(NativeRunContractError):
        machine.record_release()
    machine.record_ready(identity())
    with pytest.raises(NativeRunContractError):
        machine.record_kill_ack()
    machine.record_stop_requested()
    with pytest.raises(NativeRunContractError):
        machine.record_empty(proof())


def test_unknown_run_never_re_admits_until_separate_trusted_recovery():
    machine = prepared()
    machine.fence_unknown()
    assert machine.phase is NativeRunPhase.UNKNOWN_FENCED
    with pytest.raises(NativeRunContractError):
        machine.record_empty(proof())
    with pytest.raises(NativeRunContractError):
        machine.record_release()
    with pytest.raises(NativeRunContractError):
        machine.record_ready(identity())


def test_identity_matches_only_one_exact_run_and_worker_epoch():
    machine = NativeRunTransitionContract(request(), policy())
    first = identity()
    machine.record_starting(first)
    with pytest.raises(NativeRunContractError):
        machine.record_ready(replace(first, worker_epoch="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"))
    assert machine.phase is NativeRunPhase.UNKNOWN_FENCED
