"""#232 R20: isolated Managed12K Gateway real TCP disconnect cancellation probe.

Not a public endpoint, not production admission and not evidence attestation.
Pure claim checks are separately testable. The physical helper is called only
by the separately approved root-owned bounded Spica lab operator, after it
arms its independent 8K restoration watchdog and proves Managed12K Ready.
"""
from __future__ import annotations

import hashlib
import json
import select
import subprocess
import sys
import time
from collections.abc import Callable
from typing import Any
from uuid import UUID

LAB_WORKER = "bonsai-spica-r17-lab"
LAB_PROFILE = "bonsai2-27b-12k-lab"
LAB_URL = "http://127.0.0.1:19090/v1/chat/completions"
_SHA = "sha256:"

# External subprocess receives both its bearer token and its exact body
# through stdin. Neither appears on the process command line or in logs.
_CLIENT = r'''
import hashlib
import json
import sys
import urllib.request
import urllib.error
from urllib.parse import urlsplit

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "lab-redirect-denied", headers, fp)

opener=urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)
setup=json.loads(sys.stdin.buffer.readline())
raw=bytes.fromhex(setup["body_hex"])
req=urllib.request.Request(
    "http://127.0.0.1:19090/v1/chat/completions",
    data=raw, method="POST",
    headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + setup["token"],
    },
)
target=urlsplit(req.full_url)
if (target.scheme, target.hostname, target.port, target.path) != (
        "http", "127.0.0.1", 19090, "/v1/chat/completions"):
    raise RuntimeError("cancel-client-target-must-be-exact-loopback")
with opener.open(req, timeout=120) as response:
    print("HEADERS", response.status, hashlib.sha256(raw).hexdigest(), flush=True)
    for line in response:
        if line.strip() == b"data: [DONE]":
            print("SSE_DONE", flush=True)
            break
'''


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise ValueError(reason)


def make_cancellation_body(make_request: Callable[..., dict[str, Any]]) -> bytes:
    """Use the pinned >10K historical recipe; stream without changing input."""
    payload = make_request("long", model=LAB_PROFILE)
    require(type(payload) is dict and payload.get("model") == LAB_PROFILE
            and payload.get("max_tokens") == 16
            and type(payload.get("messages")) is list
            and len(payload["messages"]) == 1, "unqualified-cancel-request-recipe")
    payload["stream"] = True
    payload["stream_options"] = {"include_usage": True}
    raw = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ensure_ascii=True,
    ).encode("utf-8")
    require(10000 <= len(raw) <= 65536, "cancel-raw-http-body-size-invalid")
    return raw


def _digest(value: Any) -> bool:
    if type(value) is not str or len(value) != len(_SHA) + 64:
        return False
    return value.startswith(_SHA) and all(c in "0123456789abcdef" for c in value[7:])


def verify_cancellation_claim(
    *, before_job: dict[str, Any], after_job: dict[str, Any],
    worker: dict[str, Any], staged_deployment: str,
    staged_contract: str,
) -> dict[str, str]:
    """A real CLAIMED -> CANCELLED transition, no remaining lease.

    The bounded lab has exactly one registered Worker; this checks retained
    claim identity against the approved staged lab configuration, not an
    independently authenticated process or cryptographically fresh epoch.
    """
    require(all(type(x) is dict for x in (before_job, after_job, worker)),
            "cancel-report-shape-invalid")
    require(_digest(staged_deployment) and _digest(staged_contract),
            "cancel-staged-revision-invalid")
    require(worker.get("worker_id") == LAB_WORKER
            and worker.get("state") == "online"
            and worker.get("active_jobs") == 0
            and type(worker.get("active_jobs")) is int
            and worker.get("has_serving") is True
            and worker.get("deployment_revision") == staged_deployment
            and type(worker.get("runtime_instance_epoch")) is str
            and worker["runtime_instance_epoch"],
            "cancel-worker-not-eligible-or-still-busy")
    try:
        epoch = UUID(worker["runtime_instance_epoch"])
    except (ValueError, AttributeError) as exc:
        raise ValueError("cancel-worker-epoch-invalid") from exc
    require(str(epoch) == worker["runtime_instance_epoch"] and epoch.int != 0,
            "cancel-worker-epoch-invalid")
    before_id = before_job.get("job_id")
    require(type(before_id) is str and 0 < len(before_id) <= 128
            and before_job.get("status") == "running"
            and before_job.get("assigned_worker_id") == LAB_WORKER
            and before_job.get("serving_bound") is True
            and before_job.get("claimed_deployment_revision") == staged_deployment
            and before_job.get("claimed_serving_contract_revision") == staged_contract
            and before_job.get("claimed_runtime_instance_epoch")
                == worker["runtime_instance_epoch"],
            "cancel-was-not-claimed-before-client-disconnect")
    require(after_job.get("job_id") == before_id
            and after_job.get("status") == "cancelled"
            and after_job.get("assigned_worker_id") is None
            and after_job.get("serving_bound") is True
            and after_job.get("claimed_deployment_revision") == staged_deployment
            and after_job.get("claimed_serving_contract_revision") == staged_contract
            and after_job.get("claimed_runtime_instance_epoch")
                == worker["runtime_instance_epoch"],
            "cancel-not-durably-terminal-or-claim-changed")
    return {
        "job_id": before_id,
        "deployment_revision": staged_deployment,
        "contract_revision": staged_contract,
        "runtime_instance_epoch": worker["runtime_instance_epoch"],
        "status": "cancelled",
    }


def exercise_bounded_real_disconnect(
    *, make_request: Callable[..., dict[str, Any]],
    read_lab_status: Callable[[], dict[str, Any]],
    read_native_slots: Callable[[], list[dict[str, Any]]],
    read_worker_health: Callable[[], dict[str, Any]],
    assert_vram_healthy: Callable[[], None],
    client_bearer: str,
    staged_deployment: str,
    staged_contract: str,
    record_sender_bytes: Callable[[bytes], None],
) -> dict[str, Any]:
    """Bounded real TCP kill *after* observing a RUNNING/claimed 12K Job.

    Does not modify Control DB directly. Client kill forces a real TCP close;
    then require a durable CANCELLED and Native/Worker quiescence. It does
    not prove the physical process origin, cryptographic signature, or
    cancellation after the FIRST native SSE content chunk (long prefill
    generally blocks before the first chunk).
    """
    require(type(client_bearer) is str and len(client_bearer) >= 24,
            "cancel-client-auth-unqualified")
    raw = make_cancellation_body(make_request)
    digest = hashlib.sha256(raw).hexdigest()
    record_sender_bytes(raw)
    before: dict[str, Any] | None = None
    saw_processing = False
    child = subprocess.Popen(
        [sys.executable, "-c", _CLIENT],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    try:
        assert child.stdin is not None and child.stdout is not None
        message = json.dumps({
            "token": client_bearer, "body_hex": raw.hex(),
        }, separators=(",", ":")).encode() + b"\n"
        child.stdin.write(message)
        child.stdin.flush()
        child.stdin.close()
        readable, _, _ = select.select([child.stdout], [], [], 12.0)
        require(bool(readable), "cancel-client-no-http-headers")
        header = child.stdout.readline().decode("ascii", "replace").strip()
        require(header == "HEADERS 200 " + digest,
                "cancel-client-status-or-sender-body-hash-mismatch")
        start = time.monotonic()
        while time.monotonic() - start < 28:
            assert_vram_healthy()
            require(child.poll() is None, "cancel-client-completed-before-disconnect")
            snapshot = read_lab_status()
            workers = snapshot.get("workers")
            jobs = snapshot.get("jobs")
            require(snapshot.get("production_eligible") is False
                    and type(workers) is list and len(workers) == 1
                    and type(jobs) is list and len(jobs) == 1,
                    "cancel-lab-population-or-isolation-invalid")
            job, worker = jobs[0], workers[0]
            require(type(job) is dict and type(worker) is dict,
                    "cancel-lab-record-shape-invalid")
            require(job.get("status") not in ("succeeded", "failed", "cancelled"),
                    "cancel-job-terminal-before-disconnect")
            if (job.get("status") == "running"
                    and job.get("assigned_worker_id") == LAB_WORKER
                    and job.get("claimed_deployment_revision") == staged_deployment
                    and job.get("claimed_serving_contract_revision") == staged_contract
                    and job.get("claimed_runtime_instance_epoch")
                    == worker.get("runtime_instance_epoch")
                    and worker.get("active_jobs") == 1):
                before = dict(job)
                slots = read_native_slots()
                saw_processing = (type(slots) is list and len(slots) == 1
                                  and slots[0].get("n_ctx") == 12288
                                  and slots[0].get("is_processing") is True)
                if saw_processing:
                    break
            time.sleep(0.10)
        require(before is not None and saw_processing,
                "cancel-missing-claimed-processing-before-disconnect")
        # Actual process kill closes its TCP connection. No HTTP or Control
        # cancellation endpoint shortcut is permitted in this acceptance.
        child.kill()
        require(child.wait(timeout=4) < 0, "cancel-client-was-not-killed")
        deadline = time.monotonic() + 48
        final: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            assert_vram_healthy()
            snapshot = read_lab_status()
            workers, jobs = snapshot.get("workers"), snapshot.get("jobs")
            if (type(workers) is list and len(workers) == 1
                    and type(jobs) is list and len(jobs) == 1
                    and jobs[0].get("status") == "cancelled"
                    and workers[0].get("active_jobs") == 0):
                verified = verify_cancellation_claim(
                    before_job=before, after_job=jobs[0], worker=workers[0],
                    staged_deployment=staged_deployment, staged_contract=staged_contract,
                )
                native_slots = read_native_slots()
                health = read_worker_health()
                if (type(native_slots) is list and len(native_slots) == 1
                        and native_slots[0].get("n_ctx") == 12288
                        and native_slots[0].get("is_processing") is False
                        and type(health) is dict and health.get("ready") is True
                        and health.get("active_job_id") is None):
                    final = verified
                    break
            time.sleep(0.15)
        require(final is not None, "cancel-not-acknowledged-or-native-still-working")
        # A race may mark cancellation then later overwrite it with success:
        # require an additional readback after Native is idle.
        time.sleep(1.5)
        stable = read_lab_status()
        require(type(stable.get("jobs")) is list and len(stable["jobs"]) == 1
                and type(stable.get("workers")) is list
                and len(stable["workers"]) == 1,
                "cancel-late-status-population-invalid")
        verify_cancellation_claim(
            before_job=before, after_job=stable["jobs"][0],
            worker=stable["workers"][0],
            staged_deployment=staged_deployment, staged_contract=staged_contract,
        )
        # The final readback must also observe an actual HTTP 200 Native
        # idle and Worker health. A transient HTTP 503 is UNKNOWN, not idle.
        stable_slots = read_native_slots()
        stable_health = read_worker_health()
        require(type(stable_slots) is list and len(stable_slots) == 1
                and stable_slots[0].get("n_ctx") == 12288
                and stable_slots[0].get("is_processing") is False
                and type(stable_health) is dict
                and stable_health.get("ready") is True
                and stable_health.get("active_job_id") is None,
                "cancel-final-native-or-worker-not-idle")
        assert_vram_healthy()
        return {
            "status": "ISOLATED_LAB_REAL_TCP_DISCONNECT_CANCELLED",
            "actual_client_process_killed": True,
            "job_claimed_and_native_processing_before_disconnect": True,
            "job_cancelled_durable_and_worker_released": True,
            "native_slot_idle_and_worker_ready": True,
            "request_sender_sha256": "sha256:" + digest,
            "request_sender_byte_length": len(raw),
            "job_claimed_deployment_revision": final["deployment_revision"],
            "job_claimed_contract_revision": final["contract_revision"],
            "job_claimed_runtime_epoch": final["runtime_instance_epoch"],
            "actual_request_body_independently_attested": False,
            "complete_cuda_runtime_or_gpu_uuid_authenticated": False,
            "production_serving_qualified": False,
        }
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=4)
        if child.stdout is not None:
            child.stdout.close()
