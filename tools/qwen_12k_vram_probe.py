"""Read-only two-GPU VRAM sampling for an *already authorized* 12K Qwen canary.

No model requests, runtime restarts, files, profiles or GPU settings are changed.
An observed memory margin is never a production/Tool/rollback qualification.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import selectors
import subprocess
import sys
import time
from typing import Callable, Sequence

GPU_UUID = re.compile(r"GPU-[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")
# Sirius's reviewed root-owned executable; do not resolve from caller PATH.
COMMAND = [
    "/usr/bin/nvidia-smi", "--query-gpu=uuid,memory.total,memory.free,memory.used",
    "--format=csv,noheader,nounits",
]
MIN_FREE_MIB = 2048
MAX_OUTPUT_BYTES = 8192
MAX_SAMPLES = 600
MAX_DURATION_MS = 180000


def parse_sample(output: bytes, expected_uuids: Sequence[str]) -> dict[str, int]:
    """Parse exactly the reviewed two GPUs. Never treat unknown GPUs as headroom."""
    if len(expected_uuids) != 2 or len(set(expected_uuids)) != 2:
        raise ValueError("expected-exactly-two-distinct-gpus")
    if any(GPU_UUID.fullmatch(uuid) is None for uuid in expected_uuids):
        raise ValueError("invalid-expected-gpu-uuid")
    if not output or len(output) > MAX_OUTPUT_BYTES:
        raise ValueError("invalid-smi-output-size")
    try:
        decoded = output.decode("ascii", "strict")
    except UnicodeDecodeError as exc:
        raise ValueError("invalid-smi-encoding") from exc
    # nvidia-smi can use LF or CRLF; reject a bare CR and extra blanks.
    normalized = decoded.replace("\r\n", "\n")
    if "\r" in normalized:
        raise ValueError("invalid-smi-line-endings")
    rows = normalized.split("\n")
    if rows[-1] == "":
        rows.pop()
    if len(rows) != 2 or any(not row for row in rows):
        raise ValueError("wrong-gpu-row-count")
    metrics: dict[str, int] = {}
    for row in rows:
        cells = [part.strip(" ") for part in row.split(",")]
        if len(cells) != 4:
            raise ValueError("wrong-smi-fields")
        uuid, total_s, free_s, used_s = cells
        if uuid in metrics or uuid not in expected_uuids:
            raise ValueError("unreviewed-or-duplicate-gpu")
        if any(re.fullmatch(r"[0-9]{1,7}", cell) is None
               for cell in (total_s, free_s, used_s)):
            raise ValueError("invalid-memory-number")
        total, free, used = int(total_s), int(free_s), int(used_s)
        if not (2048 <= total <= 200000 and free <= total and used <= total):
            raise ValueError("memory-out-of-range")
        # NVML's total framebuffer capacity is NOT necessarily used + free.
        # On actual Sirius RTX 3060 devices, 377/378 MiB of driver-reserved
        # memory is excluded from both. Reject negative or implausibly large
        # reservations, without falsely rejecting that verified hardware.
        reserved = total - free - used
        if not 0 <= reserved <= 1024:
            raise ValueError("inconsistent-memory-counts")
        metrics[uuid] = free
    if set(metrics) != set(expected_uuids):
        raise ValueError("missing-reviewed-gpu")
    return metrics


def query_nvidia_smi() -> bytes:
    """Run only one fixed read-only NVML command; bound output and wall time."""
    process = subprocess.Popen(
        COMMAND,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    chunks: list[bytes] = []
    size = 0
    deadline = time.monotonic() + 3
    try:
        if process.stdout is None:
            raise ValueError("smi-output-pipe-missing")
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(COMMAND, 3)
                if not selector.select(remaining):
                    raise subprocess.TimeoutExpired(COMMAND, 3)
                part = os.read(process.stdout.fileno(), 8192)
                if not part:
                    break
                size += len(part)
                if size > MAX_OUTPUT_BYTES:
                    raise ValueError("smi-output-too-large")
                chunks.append(part)
        process.wait(timeout=max(0.01, deadline - time.monotonic()))
        if process.returncode:
            raise ValueError("smi-nonzero-exit")
        return b"".join(chunks)
    except BaseException:
        if process.poll() is None:
            process.kill()
        process.wait()
        raise
    finally:
        if process.stdout is not None:
            process.stdout.close()


def observe(
    expected_uuids: Sequence[str],
    *,
    phase: str,
    samples: int,
    interval_ms: int,
    query: Callable[[], bytes] = query_nvidia_smi,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, object]:
    if phase not in ("idle", "prefill"):
        raise ValueError("unknown-observation-phase")
    if not 5 <= samples <= MAX_SAMPLES or not 100 <= interval_ms <= 1000:
        raise ValueError("sampling-out-of-bounds")
    if (samples - 1) * interval_ms > MAX_DURATION_MS:
        raise ValueError("sampling-duration-too-long")
    # Validate the independent, operator-supplied UUIDs before running a probe.
    if len(expected_uuids) != 2 or len(set(expected_uuids)) != 2 or any(
        GPU_UUID.fullmatch(uuid) is None for uuid in expected_uuids
    ):
        raise ValueError("expected-exactly-two-reviewed-gpus")
    started_unix_ms = int(time.time() * 1000)
    started = clock()
    minimum = {uuid: sys.maxsize for uuid in expected_uuids}
    observed = 0
    stopped_at_first_floor_violation = False
    for i in range(samples):
        # Bound the *actual* elapsed time, not only nominal sleep intervals:
        # an individually bounded NVML call can still be slow on every sample.
        if (clock() - started) * 1000 >= MAX_DURATION_MS:
            raise ValueError("sampling-wall-time-exceeded")
        current = parse_sample(query(), expected_uuids)
        if (clock() - started) * 1000 > MAX_DURATION_MS:
            raise ValueError("sampling-wall-time-exceeded")
        observed += 1
        for uuid in expected_uuids:
            minimum[uuid] = min(minimum[uuid], current[uuid])
        if any(minimum[uuid] < MIN_FREE_MIB for uuid in expected_uuids):
            # End observation immediately on an unsafe sample; this does NOT
            # stop the model or serve as automatic production rollback.
            stopped_at_first_floor_violation = True
            break
        if i + 1 < samples:
            sleep(interval_ms / 1000.0)
    if (clock() - started) * 1000 > MAX_DURATION_MS:
        raise ValueError("sampling-wall-time-exceeded")
    finished_unix_ms = int(time.time() * 1000)
    minima = [minimum[uuid] for uuid in expected_uuids]
    capacity_passed = all(free >= MIN_FREE_MIB for free in minima)
    return {
        "started_unix_ms_unattested": started_unix_ms,
        "finished_unix_ms_unattested": finished_unix_ms,
        "status": "VRAM_OBSERVATION_ONLY_NOT_12K_QUALIFIED",
        "phase": phase,
        "gpu_count": 2,
        "sample_count": observed,
        "requested_sample_count": samples,
        "stopped_at_first_floor_violation": stopped_at_first_floor_violation,
        "max_actual_elapsed_ms": MAX_DURATION_MS,
        "sample_interval_ms": interval_ms,
        "min_free_mib_per_reviewed_gpu": minima,
        "min_free_floor_mib": MIN_FREE_MIB,
        "observed_vram_gate": (
            "FAIL" if not capacity_passed
            else "NOT_APPLICABLE_IDLE_ONLY" if phase == "idle"
            else "PASS"
        ),
        "actual_12k_context_verified": False,
        "prefill_overlap_independently_verified": False,
        "serving_identity_verified": False,
        "chat_tool_and_limits_verified": False,
        "live_8k_rollback_verified": False,
        "production_qualified": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--read-only", action="store_true", required=True)
    parser.add_argument("--expected-gpu-uuid", action="append", required=True)
    parser.add_argument("--phase", choices=("idle", "prefill"), required=True)
    parser.add_argument("--samples", type=int, default=300)
    parser.add_argument("--interval-ms", type=int, default=200)
    args = parser.parse_args()
    try:
        result = observe(
            args.expected_gpu_uuid, phase=args.phase,
            samples=args.samples, interval_ms=args.interval_ms,
        )
    except (ValueError, OSError, subprocess.SubprocessError):
        print(json.dumps({"status": "BLOCKED", "reason": "read-only-vram-probe-failed"}))
        return 2
    print(json.dumps(result, sort_keys=True))
    # Idle-only is intentionally not qualified as a prefill peak check.
    if result["observed_vram_gate"] == "FAIL":
        return 3
    if result["observed_vram_gate"] == "NOT_APPLICABLE_IDLE_ONLY":
        return 4
    if result["observed_vram_gate"] != "PASS":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
