"""#230 R13: bounded, offline inspection of a PRIVATE Sirius journal snapshot.

Source-only. Parses a previously acquired minimal JSON event projection;
no systemd/GPU/Control I/O, service stop, authorization or GPU release.
This checks logged identity and timeline consistency, NOT proof that
messages originated with PID1/kernel or that 12K serving is production-safe.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

_SCHEMA = "qwen12k-native-journal-inspection-v1"
_RUN_UNIT = "tsumgi-qwen12k-canary.service"
_KEYS = {
    "__REALTIME_TIMESTAMP", "_BOOT_ID", "_SYSTEMD_UNIT",
    "_SYSTEMD_INVOCATION_ID", "SYSLOG_IDENTIFIER", "_PID", "MESSAGE",
}
_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_SHA64 = re.compile(r"[0-9a-f]{64}\Z")
_TIME = re.compile(r"[0-9]{16}\Z")
_PID = re.compile(r"[1-9][0-9]{0,9}\Z")
_7841 = re.compile(r"prompt eval time\s*=.* /\s*7841 tokens")
_10052 = re.compile(r"prompt eval time\s*=.* /\s*10052 tokens")
_MAX_BYTES = 256 * 1024


class NativeJournalProofError(ValueError):
    """Journal source, generation, or evidence is ambiguous."""


def inspect_journal_projection(
    source: bytes, *, expected_sha256: str | None = None,
) -> dict[str, object]:
    """Verify canonical exported events and return NON-AUTHORITATIVE summary.

    A digest detects modifications relative to an independently acquired
    expected digest. A matching digest does not authenticate a fabricated
    journal projection. Tool never transforms this into a release receipt.
    """
    if not isinstance(source, bytes) or not 0 < len(source) <= _MAX_BYTES:
        raise NativeJournalProofError("journal projection size invalid")
    sha = hashlib.sha256(source).hexdigest()
    if expected_sha256 is not None and (
        not isinstance(expected_sha256, str)
        or _SHA64.fullmatch(expected_sha256) is None
        or sha != expected_sha256
    ):
        raise NativeJournalProofError("journal source digest mismatch")
    try:
        events = json.loads(source)
        if not isinstance(events, list) or not 12 <= len(events) <= 512:
            raise NativeJournalProofError("journal event count out of bound")
        raw = json.dumps(
            events, sort_keys=True, ensure_ascii=True,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
        if raw != source:
            raise NativeJournalProofError("noncanonical journal projection")
    except NativeJournalProofError:
        raise
    except (ValueError, TypeError) as exc:
        raise NativeJournalProofError("journal source malformed") from exc

    boots: set[str] = set()
    timeline: list[int] = []
    native: list[tuple[int, dict[str, str]]] = []
    systemd: list[tuple[int, dict[str, str]]] = []

    for event in events:
        if (
            type(event) is not dict or set(event) != _KEYS
            or any(type(v) is not str for v in event.values())
            or any(len(v) > 4096 for v in event.values())
            or _TIME.fullmatch(event["__REALTIME_TIMESTAMP"]) is None
            or _HEX32.fullmatch(event["_BOOT_ID"]) is None
        ):
            raise NativeJournalProofError("journal event fields invalid")
        t = int(event["__REALTIME_TIMESTAMP"])
        timeline.append(t)
        boots.add(event["_BOOT_ID"])
        if event["_SYSTEMD_UNIT"] == _RUN_UNIT:
            if event["SYSLOG_IDENTIFIER"] != "python3":
                raise NativeJournalProofError("native log identity changed")
            if _PID.fullmatch(event["_PID"]) is None:
                raise NativeJournalProofError("native PID invalid")
            if _HEX32.fullmatch(event["_SYSTEMD_INVOCATION_ID"]) is None:
                raise NativeJournalProofError("native InvocationID missing")
            native.append((t, event))
        elif (
            event["_SYSTEMD_UNIT"] == "init.scope"
            and event["_PID"] == "1"
            and event["SYSLOG_IDENTIFIER"] == "systemd"
        ):
            systemd.append((t, event))
        else:
            raise NativeJournalProofError("unrelated journal event injected")

    if len(boots) != 1 or timeline != sorted(timeline):
        raise NativeJournalProofError("journal boot/timeline inconsistent")
    ids = {(v["_PID"], v["_SYSTEMD_INVOCATION_ID"]) for _, v in native}
    if len(ids) != 1 or len(native) < 8:
        raise NativeJournalProofError("native process/InvocationID changed")
    native_pid, invocation = next(iter(ids))

    def exactly_one_event(
        pool: list[tuple[int, dict[str, str]]], pattern: str | re.Pattern[str],
    ) -> int:
        values = [
            t for t, e in pool
            if (
                pattern.search(e["MESSAGE"]) if isinstance(pattern, re.Pattern)
                else pattern in e["MESSAGE"]
            )
        ]
        if len(values) != 1:
            raise NativeJournalProofError("missing or duplicate native proof")
        return values[0]

    started = exactly_one_event(systemd, "Started tsumgi-qwen12k-canary.service")
    ctx = exactly_one_event(native, "n_ctx_slot = 12288")
    model_loaded = exactly_one_event(native, "llama_server: model loaded")
    tokens_7841 = exactly_one_event(native, _7841)
    tokens_10052 = exactly_one_event(native, _10052)
    stopping = exactly_one_event(systemd, "Stopping tsumgi-qwen12k-canary.service")
    stopped = exactly_one_event(systemd, "Stopped tsumgi-qwen12k-canary.service")
    if not started < ctx < model_loaded < tokens_7841 < tokens_10052 < stopping < stopped:
        raise NativeJournalProofError("canary timeline unexpectedly ordered")
    # A systemd unit terminal log is NOT a proof of native GPU teardown.
    failed_exit = any(
        "Failed with result 'exit-code'" in e["MESSAGE"]
        or "Main process exited, code=exited, status=143" in e["MESSAGE"]
        for _, e in systemd
    )

    return {
        "schema_version": _SCHEMA,
        "status": "NATIVE_LOG_CONFIRMED_NOT_SERVING_QUALIFIED",
        "journal_projection_sha256": "sha256:" + sha,
        "journal_event_count": len(events),
        "boot_id": next(iter(boots)),
        "invocation_id": invocation,
        "native_pid": native_pid,
        "context_size_logged": 12288,
        "uncached_7841_prompt_logged": True,
        "10052_prompt_logged": True,
        "native_model_loaded_logged": True,
        "parent_unit_stopped_logged": True,
        "unit_nonzero_exit_observed": failed_exit,
        "source_authenticated": False,
        "independent_nvml_receipt_verified": False,
        "gpu_release_proven": False,
        "gateway_serving_qualified": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal-json", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    args = parser.parse_args()
    try:
        if args.journal_json.stat().st_size > _MAX_BYTES:
            raise NativeJournalProofError("oversized source")
        result = inspect_journal_projection(
            args.journal_json.read_bytes(),
            expected_sha256=args.expected_sha256,
        )
    except (OSError, NativeJournalProofError, ValueError, TypeError):
        print(json.dumps({"status": "BLOCKED_JOURNAL_EVIDENCE"}))
        return 2
    # Does not publish raw journal messages, model paths or credentials.
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
