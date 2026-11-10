"""TSUMGI #232 R17 Spica-only isolated Control + Managed Serving12K acceptance.

Experimental Worker registers only with isolated localhost Control, not shared
production. New lab-only Serving identity; always
restore unchanged original 8K Worker. An independent systemd timer invokes
--recover if this command disappears. No shared Control catalog changes.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib
import json
import os
import re
import signal
import socket
import stat
import subprocess
import sys
import time
import tomllib
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

SOURCE = Path("/root/tsumgi-bonsai12k-native-canary-20261109-r2")
# Do not import/execute site-owned helper code before verifying owner, mode
# and the exact prepare-receipt digest. Import only inside baseline_files().
native = None

ROOT = Path("/root/tsumgi-bonsai12k-serving-r17-run")
OPERATOR = Path("/root/tsumgi-r17-operator.py")
STAGE = Path("/etc/astrumweaver/bonsai-serving-r17")
EXPERIMENT_UNIT = "tsumgi-bonsai12k-spica-r17-worker.service"
WATCHDOG_TIMER = "tsumgi-bonsai12k-spica-r17-recovery.timer"
WORKER_UNIT = "astrumweaver-worker.service"
ORIGINAL_WORKER = Path("/etc/astrumweaver/bonsai-worker.toml")
ORIGINAL_RUNTIME = Path("/etc/astrumweaver/bonsai-runtime-deployment.json")
STAGED_RUNTIME = STAGE / "runtime12k.json"
STAGED_WORKER = STAGE / "lab-worker.toml"
FLAG = ROOT / "rollback_verified.disarmed"
ARMED = ROOT / "watchdog_armed.json"
WATCHDOG_TRIGGERED = ROOT / "watchdog_execution_started"
RECOVERY_LOCK = ROOT / "recovery.lock"
RESULT = ROOT / "result.json"
GPU = "GPU-f78bcab4-245f-b3ff-5570-ed629b239331"
# Read-only baselines from root-owned Spica R6 helper files. Must match
# BEFORE importing any R6 helper code, including the first --prepare run.
PINNED_R6_HELPER_SHA256 = {
    "controller.py": "d6ab074fb8ea7924a7ecc27bf8c4c4d0372264e4a1eb055d3361586ff9aea4fc",
    "bonsai_12k_rollback_preflight.py": "1d4dc5c9a26d6e3a4f95fa4239c3097a385cf10a39076e4570b97c4769cd0d4f",
    "bonsai_12k_spica_fixtures.py": "a91285c86cbf354f4440aad20ec920242a7375e8742e92e2f786b530f553d475",
}
PINNED_ORIGINAL_HASHES = {
    str(ORIGINAL_WORKER): "16f12f70e52e5de84280ee44123b8a07e76282df8ada516f26f68b16199c6f6d",
    str(ORIGINAL_RUNTIME): "f0c20685800548151197c13b9e9418f29f62e4739fffb72c66545bb3379a4623",
    "/etc/astrumweaver/bonsai-serving-deployment.json": "6cad463e2e07c41850db29aeb6afc53e24b54932190472ecf41040049f84d01b",
}
PYTHON = "/usr/bin/python3"
WORKER_BIN = "/nix/var/nix/profiles/tsumgi-v012-ga-installer/bin/astrumweaver-worker"
LAB_CONTROL_UNIT = "tsumgi-bonsai12k-spica-r17-control.service"
LAB_WORKER = "bonsai-spica-r17-lab"
LAB_PROFILE = "bonsai2-27b-12k-lab"
LAB_CONTROL = "http://127.0.0.1:19090"
LAB_NATIVE = "http://127.0.0.1:18085"
STAGED_SERVING = STAGE / "serving12k-lab.json"
STAGED_CATALOG = STAGE / "catalog12k-lab.json"
STAGED_LAB_CONFIG = STAGE / "bonsai_12k_r17_lab_config.py"
STAGED_LAB_CONTROL = STAGE / "bonsai_12k_r17_lab_control.py"

class Blocked(RuntimeError):
    pass


def note(event: str, **data) -> None:
    print(json.dumps({"event": event, **data}, sort_keys=True), flush=True)


def check(value, code: str) -> None:
    if not value:
        raise Blocked(code)


def run(argv: list[str], timeout: int = 30, *, tolerate: bool = False):
    completed = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    if not tolerate and completed.returncode:
        raise Blocked("command_failed:" + Path(argv[0]).name + ":" + str(completed.returncode))
    return completed


def hash_of(path: Path) -> str:
    with path.open("rb") as stream:
        digest = hashlib.sha256()
        for part in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(part)
    return digest.hexdigest()


def baseline_files() -> None:
    check(socket.gethostname() == "spica" and os.geteuid() == 0, "wrong-host-or-user")
    check(Path(__file__).resolve(strict=True) == OPERATOR,
          "not-the-pinned-operator-script")
    check(not ROOT.is_symlink() and not STAGE.is_symlink(), "unsafe-stage-symlink")
    for source in (OPERATOR, SOURCE / "controller.py",
                   SOURCE / "bonsai_12k_rollback_preflight.py",
                   SOURCE / "bonsai_12k_spica_fixtures.py"):
        info = source.lstat()
        check(stat.S_ISREG(info.st_mode) and info.st_uid == 0
              and not (stat.S_IMODE(info.st_mode) & 0o022),
              "root-helper-not-protected")
    for parent in (SOURCE, OPERATOR.parent):
        info = parent.lstat()
        check(stat.S_ISDIR(info.st_mode) and info.st_uid == 0
              and not (stat.S_IMODE(info.st_mode) & 0o022),
              "root-helper-directory-not-protected")
    for name, pinned_hash in PINNED_R6_HELPER_SHA256.items():
        check(hash_of(SOURCE / name) == pinned_hash,
              "r6-helper-source-sha-drift-before-import")
    receipt = ROOT / "prepare-receipt.json"
    if receipt.is_file():
        audit = json.loads(receipt.read_text())
        require_code = {str(path) for path in (
            OPERATOR, SOURCE / "controller.py",
            SOURCE / "bonsai_12k_rollback_preflight.py",
            SOURCE / "bonsai_12k_spica_fixtures.py",
        )}
        check(set(audit.get("pinned_root_code_sha256", {})) == require_code,
              "pinned-root-helper-list-drift")
        for path, expected in audit["pinned_root_code_sha256"].items():
            check(hash_of(Path(path)) == expected, "pinned-root-helper-drift")
    global native
    if native is None:
        sys.path.insert(0, str(SOURCE))
        native = importlib.import_module("controller")
    from bonsai_12k_rollback_preflight import check_host
    check(check_host("spica")["status"] == "HOST_LOCAL_BACKUP_BYTES_MATCH_NO_LIVE_ADMISSION",
          "protected-8k-snapshot-drift")
    for location, expected in PINNED_ORIGINAL_HASHES.items():
        check(hash_of(Path(location)) == expected, "original-8k-byte-drift")
    check(run(["sha256sum", "--check", "--status",
               "/etc/astrumweaver/bonsai-model.sha256"], timeout=95).returncode == 0,
          "model-not-pinned")


def _owned_write(path: Path, raw: bytes, *, group: int, mode: int = 0o640) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        os.fchown(fd, 0, group)
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(fd)
    finally:
        os.close(fd)


def _lab_secret() -> str:
    env = (STAGE / "control.env").read_text().splitlines()
    tokens = dict(line.split("=", 1) for line in env if "=" in line)
    token = tokens.get("R17_LAB_CLIENT_TOKEN")
    check(type(token) is str and len(token) >= 24,
          "missing-private-lab-client-token")
    return token


def _lab_query(endpoint: str) -> dict:
    headers = {"Authorization": "Bearer " + _lab_secret()}
    request = urllib.request.Request(LAB_CONTROL + endpoint, headers=headers)
    with urllib.request.urlopen(request, timeout=4) as response:
        check(response.status == 200, "unexpected-lab-http")
        return json.load(response)


def prepare() -> None:
    baseline_files()
    check(native.worker_owns_original_8k(), "original-worker-not-ready")
    check(STAGE.is_dir() and not ROOT.exists(), "isolated-stage-not-prepared")
    files = (
        STAGED_RUNTIME, STAGED_WORKER, STAGED_SERVING, STAGED_CATALOG,
        STAGED_LAB_CONFIG, STAGED_LAB_CONTROL,
        STAGE / "control.env", STAGE / "worker.env",
    )
    for file in files:
        info = file.lstat()
        check(stat.S_ISREG(info.st_mode) and info.st_uid == 0
              and not (stat.S_IMODE(info.st_mode) & 0o022),
              "unprotected-isolated-lab-stage")
    ROOT.mkdir(mode=0o700)
    os.chmod(ROOT, 0o700)
    audit = {
        "schema": "tsumgi-r17-spica-isolated-review-only-preparation",
        "old_hashes": PINNED_ORIGINAL_HASHES,
        "staged_hashes": {str(f): hash_of(f) for f in files},
        "pinned_root_code_sha256": {
            str(path): hash_of(path) for path in (
                OPERATOR, SOURCE / "controller.py",
                SOURCE / "bonsai_12k_rollback_preflight.py",
                SOURCE / "bonsai_12k_spica_fixtures.py",
            )
        },
        "production_promotion_authorized": False,
        "mandatory_8k_rollback": True,
    }
    _owned_write(ROOT / "prepare-receipt.json",
                 (json.dumps(audit, sort_keys=True, indent=2) + "\n").encode(),
                 group=0, mode=0o600)
    note("R17_STAGE_PREPARED_NO_PRODUCTION_CHANGE",
         files=len(files), control_isolated=True)



def verify_lab_control_process() -> None:
    # The live unit/port owner must be the reviewed localhost Control code,
    # not merely a process impersonating its /__lab/status response.
    pidtext = run(["systemctl", "show", "-P", "MainPID",
                   LAB_CONTROL_UNIT], timeout=6).stdout.strip()
    check(pidtext.isdecimal() and int(pidtext) > 0,
          "lab-control-not-running-with-pid")
    pid = int(pidtext)
    unit = run(["systemctl", "show", "-P", "User",
                LAB_CONTROL_UNIT], timeout=6).stdout.strip()
    check(unit == "astrumweaver", "lab-control-unit-user-mismatch")
    envfiles = run(["systemctl", "show", "-P", "EnvironmentFiles",
                    LAB_CONTROL_UNIT], timeout=6).stdout.strip()
    check(envfiles == str(STAGE / "control.env") + " (ignore_errors=no)",
          "lab-control-environment-file-mismatch")
    command_line = (Path("/proc") / str(pid) / "cmdline").read_bytes()
    check(command_line.endswith(b"\0"), "lab-control-exec-argv-unterminated")
    argv = command_line.split(b"\0")[:-1]
    check(argv == [
        b"/nix/var/nix/profiles/tsumgi-v012-ga-installer/bin/python3",
        str(STAGED_LAB_CONTROL).encode(),
    ], "lab-control-actual-exec-argv-mismatch")
    cg = (Path("/proc") / str(pid) / "cgroup").read_text()
    check(LAB_CONTROL_UNIT in cg, "lab-control-cgroup-owner-mismatch")
    declared_env = dict(line.split("=", 1) for line in
                        (STAGE / "control.env").read_text().splitlines()
                        if "=" in line)
    actual_env = (Path("/proc") / str(pid) / "environ").read_bytes().split(b"\0")
    for key in ("R17_LAB_CLIENT_TOKEN", "R17_LAB_WORKER_TOKEN"):
        check(type(declared_env.get(key)) is str
              and (key + "=" + declared_env[key]).encode() in actual_env,
              "lab-control-actual-bearer-env-mismatch")
    ports = run(["ss", "-H", "-lntp", "sport", "=", ":19090"],
                timeout=8).stdout.splitlines()
    check(len(ports) == 1
          and "127.0.0.1:19090" in ports[0]
          and "0.0.0.0:19090" not in ports[0]
          and "[::]:19090" not in ports[0]
          and re.search(r"pid=(\d+)", ports[0]) is not None
          and int(re.search(r"pid=(\d+)", ports[0]).group(1)) == pid,
          "lab-control-listener-not-owned-by-expected-pid")


def parse_control_runtime_max_usec(value: str) -> int:
    """Fail closed for missing, infinite, compound or unexpected unit limits."""
    if type(value) is not str:
        raise Blocked("lab-control-runtime-limit-invalid")
    match = re.fullmatch(r"([1-9][0-9]*)(us|ms|s|min|h)", value.strip())
    check(match is not None, "lab-control-runtime-limit-invalid")
    count, unit = match.groups()
    scale = {"us": 1, "ms": 1000, "s": 1000000,
             "min": 60000000, "h": 3600000000}[unit]
    maximum = int(count) * scale
    check(0 < maximum <= 3600 * 1000000,
          "lab-control-runtime-limit-out-of-bounds")
    return maximum


def control_lifetime_remaining_usec(
    *, runtime_max_usec: int, active_since_usec: int,
    now_usec: int,
) -> int:
    """Pure monotonic-clock test of the actual isolated unit deadline."""
    check(type(runtime_max_usec) is int and 0 < runtime_max_usec <= 3600 * 1000000
          and type(active_since_usec) is int and active_since_usec > 0
          and type(now_usec) is int and now_usec >= active_since_usec,
          "lab-control-lifetime-clock-invalid")
    return runtime_max_usec - (now_usec - active_since_usec)


def control_stage_minimum_seconds(stage: str) -> int:
    """Reserve full timeout for sequential requests in a single stage."""
    floors = {
        "gateway_small_chat": 300,
        "gateway_stream": 300,
        "gateway_10k_long": 300,
        # Tool Call and follow-up Tool Result each have an independent
        # 210s HTTP timeout; a single 240s budget is insufficient.
        "gateway_tool_result": 520,
    }
    check(type(stage) is str and stage in floors,
          "lab-control-unreviewed-stage")
    return floors[stage]


def stage_wall_clock_limit_seconds(stage: str) -> int:
    """Real wall-time limit, shorter than the Control TTL stage floor."""
    return control_stage_minimum_seconds(stage) - 40


@contextmanager
def bounded_stage_walltime(stage: str):
    """Interrupt stalled HTTP streaming; always reach the 8K recovery finally.

    urllib timeouts are per socket operation, not a whole SSE stream SLA.
    This SIGALRM covers the entire multi-request stage on the operator main
    thread. An independent systemd recovery timer remains authoritative if
    the foreground operator dies or cannot service the signal.
    """
    limit = stage_wall_clock_limit_seconds(stage)
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    check(previous_timer == (0.0, 0.0), "unexpected-operator-alarm-already-armed")

    def walltime_expired(_signum, _frame):
        raise Blocked("lab-stage-walltime-expired")

    try:
        signal.signal(signal.SIGALRM, walltime_expired)
        signal.setitimer(signal.ITIMER_REAL, limit)
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


def verify_lab_control_lifetime(*, minimum_seconds: int) -> None:
    """Reject expiring or unbounded Control before any physical Worker stop.

    The watchdog protects 8K recovery, but it cannot keep a separate
    Control unit alive. Require a 20-minute bounded Control lifetime
    with >=1000 seconds remaining at the physical preflight; a per-stage
    check then rejects a Control about to expire mid-request.
    """
    check(type(minimum_seconds) is int and 240 <= minimum_seconds <= 1000,
          "lab-control-minimum-ttl-invalid")
    unit = LAB_CONTROL_UNIT
    max_raw = run(["systemctl", "show", "-P", "RuntimeMaxUSec", unit],
                  timeout=6).stdout.strip()
    runtime_us = parse_control_runtime_max_usec(max_raw)
    check(runtime_us == 1200 * 1000000,
          "lab-control-runtime-cap-not-exactly-20min")
    started = run(["systemctl", "show", "-P",
                   "ActiveEnterTimestampMonotonic", unit],
                  timeout=6).stdout.strip()
    check(started.isdecimal() and int(started) > 0,
          "lab-control-activation-timestamp-invalid")
    now_us = time.monotonic_ns() // 1000
    remaining = control_lifetime_remaining_usec(
        runtime_max_usec=runtime_us,
        active_since_usec=int(started),
        now_usec=now_us,
    )
    check(remaining >= minimum_seconds * 1000000,
          "lab-control-insufficient-remaining-lifetime")
    check(native.is_active(unit), "lab-control-no-longer-active")
    note("R17_CONTROL_LIFETIME_GATE_PASS",
         remaining_seconds=remaining // 1000000,
         required_seconds=minimum_seconds)


def preflight() -> None:
    baseline_files()
    check(ROOT.is_dir() and STAGE.is_dir(),
          "lab-stage-or-recovery-root-missing")
    audit = json.loads((ROOT / "prepare-receipt.json").read_text())
    check(audit.get("production_promotion_authorized") is False
          and audit.get("mandatory_8k_rollback") is True,
          "lab-stage-permission-drift")
    for f, expected in audit["staged_hashes"].items():
        check(hash_of(Path(f)) == expected, "lab-source-or-env-changed")
    check(not FLAG.exists() and not ARMED.exists() and not RESULT.exists(),
          "experiment-already-finished-or-armed")
    cfg = tomllib.loads(STAGED_WORKER.read_text())
    runtime = json.loads(STAGED_RUNTIME.read_text())
    serving = json.loads(STAGED_SERVING.read_text())
    catalog = json.loads(STAGED_CATALOG.read_text())
    check(cfg["worker"]["id"] == LAB_WORKER
          and cfg["worker"]["control_url"] == LAB_CONTROL
          and cfg["worker"]["gpu_uuids"] == [GPU]
          and cfg["worker"]["capabilities"] == ["llm.chat", "text.generate"]
          and cfg["worker"]["health_port"] == 19100
          and cfg["runtime"]["manifest"] == str(STAGED_RUNTIME)
          and cfg["serving"]["manifest"] == str(STAGED_SERVING),
          "lab-worker-would-break-isolation")
    check(runtime["provider_config"]["context_size"] == 12288
          and runtime["provider_config"]["base_url"] == LAB_NATIVE
          and runtime["setup_intent"]["configuration"]["context_size"] == 12288
          and runtime["setup_intent"]["configuration"]["base_url"] == LAB_NATIVE
          and runtime["setup_intent"]["configuration"]["gpu_uuids"] == [GPU],
          "lab-runtime-context-gpu-or-port-drift")
    check(serving["contracts"][0]["contract"]["limits"] == {
        "input_tokens": 10240, "output_tokens": 2048,
        "total_tokens": 12288, "request_bytes": 65536,
    }, "lab-serving-limit-drift")
    check(catalog["profiles"][0]["profile"]["profile_id"] == LAB_PROFILE
          and len(catalog["profiles"]) == 1
          and catalog["profiles"][0]["deployment"] == serving["deployment"]
          and catalog["profiles"][0]["contract"] ==
              serving["contracts"][0]["contract"],
          "lab-serving-catalog-identity-drift")
    check(native.worker_owns_original_8k(), "original-8k-not-ready")
    check(native.free_mib() >= 3000, "insufficient-original-idle-free-vram")
    check(not native.is_active(EXPERIMENT_UNIT), "lab-worker-already-active")
    check(native.is_active(LAB_CONTROL_UNIT), "isolated-control-not-active")
    verify_lab_control_process()
    verify_lab_control_lifetime(minimum_seconds=1000)
    snapshot = _lab_query("/__lab/status")
    check(snapshot.get("status") ==
          "R17_ISOLATED_INMEMORY_LAB_NOT_PRODUCTION"
          and snapshot.get("lab_model_id") == LAB_PROFILE
          and snapshot.get("production_eligible") is False
          and snapshot.get("workers") == []
          and snapshot.get("jobs") == [],
          "lab-control-not-empty-or-incorrect")
    note("R17_PREFLIGHT_PASS_NO_SERVICE_CHANGE", ctx=12288,
         lab_worker=LAB_WORKER, isolated_control=True)


def ready(port: int, endpoint: str, *, expected: int = 200) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{endpoint}", timeout=4) as response:
        check(response.status == expected, "unexpected-readiness-http")
        return json.load(response)


def wait_managed_12k(monitor) -> None:
    deadline = time.monotonic() + 165
    last = "waiting"
    while time.monotonic() < deadline:
        monitor.assert_healthy()
        if not native.is_active(EXPERIMENT_UNIT):
            raise Blocked("experimental-worker-exited")
        try:
            health = ready(19100, "/ready")
            slot = ready(18085, "/slots")
            if health.get("ready") is True and health.get("active_job_id") is None:
                check(health.get("worker_id") == LAB_WORKER, "unrecognized-canary-worker")
                if len(slot) == 1 and slot[0].get("n_ctx") == 12288:
                    snapshot = _lab_query("/__lab/status")
                    workers = snapshot.get("workers")
                    declared = json.loads(STAGED_SERVING.read_text())
                    if (type(workers) is list and len(workers) == 1
                            and workers[0].get("worker_id") == LAB_WORKER
                            and workers[0].get("state") == "online"
                            and workers[0].get("has_serving") is True
                            and workers[0].get("deployment_revision") ==
                                declared["contracts"][0]["contract"]["deployment_revision"]
                            and type(workers[0].get("runtime_instance_epoch")) is str):
                        note("R17_MANAGED_12K_READY_LAB_SERVING", native_ctx=12288,
                             lab_epoch=workers[0]["runtime_instance_epoch"],
                             isolated_deployment=workers[0]["deployment_revision"],
                             free_mib=native.free_mib())
                        return
            last = "ready-or-context-mismatch"
        except Exception as exc:  # noqa: BLE001 - untrusted readiness during rollback
            last = type(exc).__name__
        time.sleep(0.7)
    raise Blocked("managed12k-not-ready:" + last)


def arm_watchdog() -> None:
    check(not ARMED.exists(), "watchdog-already-armed")
    argv = ["systemd-run", "--on-active=780s",
            "--unit=tsumgi-bonsai12k-spica-r17-recovery",
            "--description=TSUMGI Spica12K R14 independent 8K rollback",
            "--property=TimeoutStartSec=1800s", PYTHON, str(OPERATOR), "--recover"]
    run(argv, timeout=20)
    check(run(["systemctl", "is-active", "--quiet", WATCHDOG_TIMER],
              tolerate=True).returncode == 0, "rollback-watchdog-not-active")
    _owned_write(ARMED, b'{"rollback_timer_armed":true}\n', group=0, mode=0o600)
    note("INDEPENDENT_8K_RECOVERY_ARMED", timer=WATCHDOG_TIMER)


def start_managed_unit() -> None:
    argv = [
        "systemd-run", "--unit=tsumgi-bonsai12k-spica-r17-worker",
        "--description=TSUMGI Spica 12K isolated-Control lab Serving/Job canary",
        "--uid=astrumweaver", "--gid=astrumweaver",
        "--property=SupplementaryGroups=astrumweaver-config",
        "--property=EnvironmentFile=/etc/astrumweaver/bonsai-serving-r17/worker.env",
        "--setenv=LD_LIBRARY_PATH=/etc/astrumweaver/runtime/nvidia-driver",
        "--property=KillMode=control-group",
        "--property=TimeoutStopSec=85s", "--property=RuntimeMaxSec=1080s",
        "--property=Restart=no", "--property=NoNewPrivileges=yes",
        "--property=PrivateTmp=yes", "--property=ProtectHome=yes",
        "--property=ProtectSystem=strict",
        "--property=RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
        "--property=StateDirectory=astrumweaver",
        WORKER_BIN, "--config", str(STAGED_WORKER),
    ]
    run(argv, timeout=35)
    note("TRANSIENT_MANAGED_12K_START_REQUESTED")


def disarm_timer_after_verified_8k() -> None:
    run(["systemctl", "stop", WATCHDOG_TIMER], timeout=15, tolerate=True)
    check(run(["systemctl", "is-active", "--quiet", WATCHDOG_TIMER],
              timeout=10, tolerate=True).returncode != 0,
          "watchdog-timer-still-active-after-8k-recovery")


def _recover_locked() -> None:
    baseline_files()
    native.CANARY = EXPERIMENT_UNIT
    if FLAG.exists():
        check(native.worker_owns_original_8k(), "disarmed-but-original-8k-not-ready")
        disarm_timer_after_verified_8k()
        note("RECOVERY_ALREADY_COMPLETE_AND_TIMER_DISARMED")
        return
    if not native.is_active(EXPERIMENT_UNIT) and native.worker_owns_original_8k():
        note("RECOVERY_ORIGINAL_8K_ALREADY_READY")
    else:
        # Stop only the dedicated transient service, even if it is FAILED.
        load = run(["systemctl", "show", "-P", "LoadState", EXPERIMENT_UNIT],
                   timeout=10, tolerate=True).stdout.strip()
        if load not in ("", "not-found"):
            note("RECOVERY_STOPPING_ONLY_OWNED_TRANSIENT_UNIT")
            run(["systemctl", "stop", EXPERIMENT_UNIT], timeout=135, tolerate=True)
        # A failed/inactive transient could still own native descendants.
        native.wait_clear(timeout=115)
    if not native.is_active(WORKER_UNIT):
        run(["systemctl", "reset-failed", WORKER_UNIT], timeout=15, tolerate=True)
        run(["systemctl", "start", WORKER_UNIT], timeout=105)
    native.wait_for_8k(timeout=165)
    check(native.worker_owns_original_8k(), "original-worker-ownership-not-proven")
    note("ORIGINAL_8K_RESTORED", ctx=8192, original_files_untouched=True)
    disarm_timer_after_verified_8k()
    fd = os.open(FLAG, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, b"original-worker-8k-verified\n")
        os.fsync(fd)
    finally:
        os.close(fd)


def recover(*, from_watchdog: bool = False) -> None:
    # Write the watchdog intervention marker BEFORE waiting for the recovery
    # lock. Otherwise the foreground may finish while the timer waits and
    # incorrectly report that a concurrently triggered watchdog never fired.
    if from_watchdog and not WATCHDOG_TRIGGERED.exists():
        try:
            marker = os.open(WATCHDOG_TRIGGERED,
                             os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            pass
        else:
            try:
                os.write(marker, b"independent-rollback-timer-triggered\n")
                os.fsync(marker)
            finally:
                os.close(marker)
    fd = os.open(RECOVERY_LOCK, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        _recover_locked()
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)



def _lab_post(data: dict, *, timeout: int = 210):
    raw = json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    check(len(raw) <= 65536, "lab-chat-request-byte-limit")
    req = urllib.request.Request(
        LAB_CONTROL + "/v1/chat/completions",
        data=raw, method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + _lab_secret(),
        },
    )
    return urllib.request.urlopen(req, timeout=timeout)


def validate_terminal_lab_claim(
    job: dict, worker: dict, *, expected_contract_revision: str,
    expected_deployment_revision: str,
) -> None:
    """Review the *terminal* Job, not its transient lease owner field.

    InMemoryControlRepository.complete_job clears assigned_worker_id on
    SUCCEEDED. Actual claim identity survives in claimed deployment/contract/
    epoch fields, while the lab Repository admits exactly one Worker ID.
    A terminal Job still carrying an assigned owner is a contract violation.
    """
    check(type(job) is dict and type(worker) is dict
          and type(expected_contract_revision) is str
          and re.fullmatch(r"sha256:[0-9a-f]{64}",
                           expected_contract_revision) is not None
          and type(expected_deployment_revision) is str
          and re.fullmatch(r"sha256:[0-9a-f]{64}",
                           expected_deployment_revision) is not None,
          "lab-terminal-claim-shape-invalid")
    check(worker.get("worker_id") == LAB_WORKER
          and worker.get("state") == "online"
          and worker.get("has_serving") is True
          and type(worker.get("deployment_revision")) is str
          and re.fullmatch(r"sha256:[0-9a-f]{64}",
                           worker["deployment_revision"]) is not None
          and worker["deployment_revision"] == expected_deployment_revision
          and type(worker.get("runtime_instance_epoch")) is str,
          "lab-terminal-worker-identity-invalid")
    try:
        epoch = UUID(worker["runtime_instance_epoch"])
    except (ValueError, AttributeError) as exc:
        raise Blocked("lab-terminal-worker-identity-invalid") from exc
    check(str(epoch) == worker["runtime_instance_epoch"] and epoch.int != 0,
          "lab-terminal-worker-identity-invalid")
    check(job.get("status") == "succeeded"
          and job.get("assigned_worker_id") is None
          and job.get("serving_bound") is True
          and job.get("claimed_deployment_revision")
              == worker["deployment_revision"]
          and job.get("claimed_runtime_instance_epoch")
              == worker["runtime_instance_epoch"]
          and job.get("claimed_serving_contract_revision")
              == expected_contract_revision,
          "lab-terminal-claim-or-epoch-not-verified")


def _assert_lab_job_succeeded(*, required_count: int = 1) -> None:
    snapshot = _lab_query("/__lab/status")
    workers, jobs = snapshot["workers"], snapshot["jobs"]
    check(len(workers) == 1 and len(jobs) >= required_count,
          "lab-worker-or-job-count-invalid")
    expected = json.loads(STAGED_SERVING.read_text())
    validate_terminal_lab_claim(
        jobs[-1], workers[0],
        expected_contract_revision=expected["contracts"][0]["serving_contract_revision"],
        expected_deployment_revision=expected["contracts"][0]["contract"]["deployment_revision"],
    )


def _lab_chat(payload: dict, monitor, *, timeout: int = 210) -> None:
    monitor.assert_healthy()
    with _lab_post(payload, timeout=timeout) as response:
        check(response.status == 200, "lab-chat-status-invalid")
        result = json.load(response)
    message = result.get("choices", [{}])[0].get("message", {})
    check(type(message) is dict and type(message.get("content")) is str
          and bool(message["content"].strip()), "lab-chat-empty-response")
    _assert_lab_job_succeeded()
    monitor.assert_healthy()
    note("R17_GATEWAY_CHAT_PASS", chars=len(message["content"]))


def _lab_sse(payload: dict, monitor) -> list[dict]:
    monitor.assert_healthy()
    check(payload.get("stream") is True, "lab-sse-stream-required")
    chunks: list[dict] = []
    total_bytes = 0
    done = False
    with _lab_post(payload, timeout=210) as response:
        check(response.status == 200, "lab-sse-http-non200")
        for line in response:
            total_bytes += len(line)
            check(total_bytes <= 1048576, "lab-sse-too-large")
            row = line.strip()
            if not row.startswith(b"data: "):
                continue
            data = row[6:]
            if data == b"[DONE]":
                done = True
                break
            frame = json.loads(data)
            check(type(frame) is dict and type(frame.get("choices")) is list,
                  "lab-sse-frame-invalid")
            chunks.append(frame)
            monitor.assert_healthy()
    check(done and chunks, "lab-sse-not-terminal")
    _assert_lab_job_succeeded()
    monitor.assert_healthy()
    return chunks


def _lab_plain(monitor) -> None:
    _lab_chat({
        "model": LAB_PROFILE,
        "messages": [{"role": "user",
                      "content": "Answer with one short test sentence."}],
        "max_tokens": 32,
    }, monitor)


def _lab_stream(monitor) -> None:
    frames = _lab_sse({
        "model": LAB_PROFILE, "stream": True,
        "messages": [{"role": "user", "content": "Say hello briefly."}],
        "max_tokens": 64, "stream_options": {"include_usage": True},
    }, monitor)
    check(any(type(c.get("delta")) is dict
              and type(c["delta"].get("content")) is str
              and c["delta"]["content"]
              for frame in frames for c in frame.get("choices", [])),
          "lab-stream-no-content")
    note("R17_GATEWAY_STREAM_PASS", frames=len(frames))


def _lab_long(monitor) -> None:
    # Actually route the historical 10,231-token synthetic prompt through
    # the isolated Gateway -> real Control Job -> Managed Worker -> Native.
    from bonsai_12k_spica_fixtures import make_request

    payload = make_request("long", model=LAB_PROFILE)
    _lab_chat(payload, monitor, timeout=210)
    note("R17_GATEWAY_LONG_PASS", expected_native_input_tokens=10231)


def _lab_tool(monitor) -> None:
    from bonsai_12k_spica_fixtures import make_request
    small = {
        "role": "user",
        "content": "Call synthetic_echo with text equal to R6, then summarize the result.",
    }
    body = {
        "model": LAB_PROFILE, "messages": [small],
        "tools": make_request("tool")["tools"],
        "tool_choice": "auto", "stream": True,
        "max_tokens": 128, "stream_options": {"include_usage": True},
    }
    frames = _lab_sse(body, monitor)
    call_id, tool_name = None, None
    fragments: list[str] = []
    finishes: set[str] = set()
    for frame in frames:
        for choice in frame.get("choices", []):
            check(type(choice) is dict and choice.get("index") == 0,
                  "lab-tool-choice-index-invalid")
            if choice.get("finish_reason") is not None:
                finishes.add(choice["finish_reason"])
            delta = choice.get("delta") or {}
            check(type(delta) is dict, "lab-tool-delta-invalid")
            for call in delta.get("tool_calls", []):
                check(type(call) is dict and call.get("index") == 0,
                      "lab-tool-stream-index-invalid")
                if call.get("id") is not None:
                    check(type(call["id"]) is str and
                          (call_id is None or call["id"] == call_id),
                          "lab-tool-call-id-conflict")
                    call_id = call["id"]
                fn = call.get("function") or {}
                check(type(fn) is dict, "lab-tool-function-invalid")
                if fn.get("name") is not None:
                    check(type(fn["name"]) is str and
                          (tool_name is None or fn["name"] == tool_name),
                          "lab-tool-name-conflict")
                    tool_name = fn["name"]
                if fn.get("arguments") is not None:
                    check(type(fn["arguments"]) is str,
                          "lab-tool-argument-fragment-invalid")
                    fragments.append(fn["arguments"])
    arguments = "".join(fragments)
    check(finishes == {"tool_calls"} and tool_name == "synthetic_echo"
          and type(call_id) is str and 0 < len(call_id) <= 128
          and 0 < len(arguments) <= 2048,
          "lab-tool-incomplete-or-nonterminal")
    parsed = json.loads(arguments)
    check(type(parsed) is dict and set(parsed) == {"text"}
          and parsed["text"] == "R6", "lab-tool-argument-meaning-invalid")
    follow = {
        "model": LAB_PROFILE, "messages": [
            small,
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": call_id, "type": "function",
                "function": {"name": tool_name, "arguments": arguments},
            }]},
            {"role": "tool", "tool_call_id": call_id,
             "content": "synthetic_echo result: R6"},
        ], "stream": True, "tool_choice": "none", "max_tokens": 128,
        "stream_options": {"include_usage": True},
    }
    done = _lab_sse(follow, monitor)
    reasons, contents = set(), []
    for frame in done:
        for choice in frame.get("choices", []):
            if choice.get("finish_reason") is not None:
                reasons.add(choice["finish_reason"])
            delta = choice.get("delta") or {}
            check(type(delta) is dict and not delta.get("tool_calls"),
                  "lab-tool-result-extra-tool")
            if type(delta.get("content")) is str:
                contents.append(delta["content"])
    check(reasons == {"stop"} and "".join(contents).strip(),
          "lab-tool-result-no-final-content")
    note("R17_GATEWAY_TOOL_RESULT_PASS", correlated_id=True,
         tool_call_terminal=True, result_terminal=True)


def execute() -> int:
    # This marker is a deliberate operator action, not a GitHub Issue grant.
    check(os.environ.get("TSUMGI_R17_OPERATOR_APPROVED")
          == "SPICA_R17_ISOLATED_MANAGED12K_JOB_CANARY_8K_RESTORE",
          "explicit-r17-execution-marker-required")
    preflight()
    native.PORT = 18085
    native.CANARY = EXPERIMENT_UNIT
    native.OUT = ROOT
    monitor = native.NVMLMonitor()
    stages = []
    failed = None
    stats = None
    restored = False
    lab_summary = None
    arm_watchdog()
    timer_armed_at = time.monotonic()
    try:
        note("STOPPING_ONLY_SPICA_ORIGINAL_WORKER")
        run(["systemctl", "stop", WORKER_UNIT], timeout=130)
        check(not native.is_active(WORKER_UNIT), "original-worker-not-stopped")
        native.wait_clear(timeout=90)
        check(native.free_mib() >= 10000, "original-gpu-residency-not-released")
        monitor.begin()
        monitor.assert_healthy()
        start_managed_unit()
        wait_managed_12k(monitor)
        for stage, method in (
            ("gateway_small_chat", lambda: _lab_plain(monitor)),
            ("gateway_stream", lambda: _lab_stream(monitor)),
            ("gateway_10k_long", lambda: _lab_long(monitor)),
            ("gateway_tool_result", lambda: _lab_tool(monitor)),
        ):
            monitor.assert_healthy()
            verify_lab_control_lifetime(
                minimum_seconds=control_stage_minimum_seconds(stage),
            )
            check(not WATCHDOG_TRIGGERED.exists() and
                  time.monotonic() - timer_armed_at <= 450,
                  "canary-time-budget-or-independent-rollback-triggered")
            note("R17_TEST_BEGIN", name=stage)
            with bounded_stage_walltime(stage):
                method()
            monitor.assert_healthy()
            check(not WATCHDOG_TRIGGERED.exists() and
                  time.monotonic() - timer_armed_at <= 680,
                  "canary-expired-or-independent-rollback-triggered")
            stages.append(stage)
            note("R17_TEST_PASSED", name=stage)
        lab_summary = _lab_query("/__lab/status")
        check(lab_summary.get("production_eligible") is False
              and type(lab_summary.get("workers")) is list
              and len(lab_summary["workers"]) == 1
              and type(lab_summary.get("jobs")) is list
              and len(lab_summary["jobs"]) >= 5,
              "lab-final-claim-or-job-state-invalid")
        expected = json.loads(STAGED_SERVING.read_text())
        contract = expected["contracts"][0]["serving_contract_revision"]
        deployment = expected["contracts"][0]["contract"]["deployment_revision"]
        for completed in lab_summary["jobs"]:
            validate_terminal_lab_claim(
                completed, lab_summary["workers"][0],
                expected_contract_revision=contract,
                expected_deployment_revision=deployment,
            )
        note("R17_LAB_JOB_CLAIMS_ACCEPTED", jobs=len(lab_summary["jobs"]),
             terminal_assignment_released=True)
    except Exception as exc:  # noqa: BLE001 - ensure mandatory recovery on any failure
        failed = type(exc).__name__ + ":" + str(exc)[:170]
        note("R17_ABORT", reason=failed, completed=stages)
    finally:
        # The monitor itself may fail while ending an aborted run. Never let
        # its exception bypass the immediate 8K recovery path.
        try:
            stats = monitor.finish()
        except BaseException as exc:  # noqa: BLE001 - hard safety boundary
            stats = {
                "safety_abort": True, "min_sampled_free_mib": None,
                "max_sample_gap_ms": None,
                "monitor_finish_failed": type(exc).__name__,
            }
            note("R17_MONITOR_FINISH_FAILED", reason=type(exc).__name__)
            if failed is None:
                failed = "monitor-finish-failed"
        finally:
            try:
                recover()
                restored = True
            except BaseException as exc:  # noqa: BLE001 - hard safety boundary
                note("R17_RECOVERY_BLOCKED", reason=type(exc).__name__ + ":" + str(exc)[:170])
                if failed is None:
                    failed = "recovery-blocked"
    passed = (
        failed is None and restored and not WATCHDOG_TRIGGERED.exists()
        and stages == [
            "gateway_small_chat", "gateway_stream",
            "gateway_10k_long", "gateway_tool_result",
        ]
        and lab_summary is not None
        and not stats["safety_abort"]
        and stats["min_sampled_free_mib"] is not None
        and stats["min_sampled_free_mib"] >= 2048
        and stats["max_sample_gap_ms"] is not None
        and stats["max_sample_gap_ms"] <= 250
    )
    record = {
        "schema_version": "tsumgi-r17-spica-isolated-managed12k-bounded-canary-v1",
        "status": "ISOLATED_LAB_MANAGED_JOB_AND_GATEWAY_SMOKE_8K_RESTORED" if passed
                  else "BLOCKED_OR_FAILED_RESTORATION_REQUIRES_ATTENTION",
        "reported_tests": stages,
        "vram": stats,
        "restored_original_8k": restored,
        "lab_only_12k_serving_advertised": bool(lab_summary),
        "lab_only_job_claims": (
            [] if lab_summary is None else lab_summary["jobs"]
        ),
        "lab_only_worker_epoch": (
            None if lab_summary is None else
            lab_summary["workers"][0]["runtime_instance_epoch"]
        ),
        "cancellation_e2e_qualified": False,
        "independent_provenance_attested": False,
        "production_control_catalog_modified": False,
        "native_request_bodies_independently_attested": False,
        "production_12k_serving_revision_issued": False,
        "permanent_12k_promotion": False,
        "error": failed,
    }
    _owned_write(RESULT, (json.dumps(record, indent=2, sort_keys=True)+"\n").encode(),
                 group=0, mode=0o600)
    note("R17_FINAL", status=record["status"], restored8k=restored,
         sampled_min_free_mib=stats["min_sampled_free_mib"])
    return 0 if passed else 3


def main() -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--prepare", action="store_true")
    group.add_argument("--preflight", action="store_true")
    group.add_argument("--execute", action="store_true")
    group.add_argument("--recover", action="store_true")
    arg = parser.parse_args()
    try:
        if arg.prepare:
            prepare()
        elif arg.preflight:
            preflight()
        elif arg.recover:
            recover(from_watchdog=True)
        else:
            return execute()
        return 0
    except Exception as exc:  # noqa: BLE001 - fail closed with a bounded diagnostic
        note("R17_BLOCKED", reason=type(exc).__name__ + ":" + str(exc)[:170])
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
