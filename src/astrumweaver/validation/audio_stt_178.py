"""Offline, private-safe scoring and reporting primitives for issue #178.

Model execution is deliberately injected by the operator; this module never
downloads models, reads audio, or writes outside the requested report path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA = "audio-stt-178-v1"
MAX_FIXTURE_BYTES = 4 * 1024 * 1024
MAX_RESULTS_BYTES = 32 * 1024 * 1024
MAX_CASES = 500
MAX_CANDIDATES = 8
MAX_TEXT_CHARS = 10_000
MAX_EDIT_CELLS_PER_CANDIDATE = 10_000_000
CONTRAST_TARGETS = ("散歩", "3本")


def _matches_contrast(text: str, target: str) -> bool:
    return {term for term in CONTRAST_TARGETS if term in text} == {target}


class AudioStt178Error(ValueError):
    """Invalid or incomplete evaluation input."""


def normalize(text: str, *, form: str = "NFKC", remove_whitespace: bool = True) -> str:
    if not isinstance(text, str) or len(text) > MAX_TEXT_CHARS:
        raise AudioStt178Error("input text length bound exceeded")
    if form not in {"NFC", "NFKC", "none"}:
        raise AudioStt178Error("normalization form must be NFC, NFKC, or none")
    result = unicodedata.normalize(form, text) if form != "none" else text
    if len(result) > MAX_TEXT_CHARS:
        raise AudioStt178Error("normalized text length bound exceeded")
    if remove_whitespace:
        result = "".join(result.split())
    return result


def _edit_distance(left: Sequence[str], right: Sequence[str]) -> int:
    if len(left) * len(right) > MAX_EDIT_CELLS_PER_CANDIDATE:
        raise AudioStt178Error("edit-distance work bound exceeded")
    previous = list(range(len(right) + 1))
    for i, a in enumerate(left, 1):
        current = [i]
        for j, b in enumerate(right, 1):
            current.append(min(current[-1] + 1, previous[j] + 1,
                               previous[j - 1] + (a != b)))
        previous = current
    return previous[-1]


def error_rate(reference: str, hypothesis: str, unit: str) -> float:
    if (not isinstance(reference, str) or not isinstance(hypothesis, str)
            or len(reference) > MAX_TEXT_CHARS or len(hypothesis) > MAX_TEXT_CHARS):
        raise AudioStt178Error("normalized text length bound exceeded")
    if unit == "cer":
        ref, hyp = list(reference), list(hypothesis)
    elif unit == "wer":
        # Japanese WER is whitespace-tokenized. Fixture segmentation must be
        # declared and consistent; character CER remains the primary metric.
        ref, hyp = reference.split(), hypothesis.split()
    else:
        raise AudioStt178Error("metric unit must be cer or wer")
    if len(ref) > MAX_TEXT_CHARS or len(hyp) > MAX_TEXT_CHARS:
        raise AudioStt178Error("normalized text length bound exceeded")
    if not ref:
        return 0.0 if not hyp else 1.0
    return _edit_distance(ref, hyp) / len(ref)


def percentile(values: Sequence[float], percent: float) -> float:
    if not values or not 0 <= percent <= 100:
        raise AudioStt178Error("percentile requires values and 0..100")
    ordered = sorted(values)
    position = (len(ordered) - 1) * percent / 100
    low = math.floor(position)
    high = math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def load_fixture(path: Path) -> tuple[dict[str, Any], str]:
    try:
        raw = _read_bounded(path, MAX_FIXTURE_BYTES)
        fixture = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise AudioStt178Error("fixture could not be loaded") from exc
    if (not isinstance(fixture, dict) or fixture.get("schema_version") != SCHEMA
            or set(fixture) != {"schema_version", "normalization", "segmentation",
                                "test_conditions", "cases"}):
        raise AudioStt178Error("unsupported #178 fixture schema")
    norm = fixture["normalization"]
    if (not isinstance(norm, dict) or set(norm) != {"form", "remove_whitespace"}
            or norm.get("form") not in {"NFC", "NFKC", "none"}
            or not isinstance(norm.get("remove_whitespace"), bool)):
        raise AudioStt178Error("fixture normalization must be explicit")
    if not isinstance(fixture["segmentation"], str) or not fixture["segmentation"].strip():
        raise AudioStt178Error("fixture segmentation must be explicit")
    if not isinstance(fixture["test_conditions"], dict) or not fixture["test_conditions"]:
        raise AudioStt178Error("fixture test conditions must be explicit")
    cases = fixture["cases"]
    if not isinstance(cases, list) or not cases or len(cases) > MAX_CASES:
        raise AudioStt178Error("fixture cases must be a nonempty list")
    ids: set[str] = set()
    contrast_targets: set[str] = set()
    for item in cases:
        if (not isinstance(item, dict) or set(item) != {
                    "id", "audio", "reference", "tags", "duration_seconds", "target"}
                or not all(isinstance(item[k], str) and item[k].strip()
                           for k in ("id", "audio", "reference"))
                or len(item["id"]) > 256 or len(item["audio"]) > 4096
                or len(item["reference"]) > MAX_TEXT_CHARS
                or isinstance(item["duration_seconds"], bool)
                or not isinstance(item["duration_seconds"], (int, float))
                or not math.isfinite(item["duration_seconds"])
                or item["duration_seconds"] <= 0
                or not isinstance(item["tags"], list)
                or not all(isinstance(tag, str) and len(tag) <= 256
                           for tag in item["tags"])
                or (item["target"] is not None and (
                    not isinstance(item["target"], str) or not item["target"].strip()
                    or len(item["target"]) > 256))):
            raise AudioStt178Error("fixture contains an invalid case")
        if item["id"] in ids:
            raise AudioStt178Error("fixture case IDs must be unique")
        ids.add(item["id"])
        if "homophone:散歩-vs-3本" in item["tags"]:
            normalized_reference = normalize(
                item["reference"], form=norm["form"], remove_whitespace=True
            )
            target = item["target"]
            if normalized_reference == target or not _matches_contrast(normalized_reference, target):
                raise AudioStt178Error("homophone cases need sentence context and target 散歩 or 3本")
            contrast_targets.add(target)
    if contrast_targets != {"散歩", "3本"}:
        raise AudioStt178Error("fixture lacks contextual 散歩 and 3本 target cases")
    conditions_size = len(json.dumps(fixture["test_conditions"], ensure_ascii=False))
    if conditions_size > 64 * 1024:
        raise AudioStt178Error("test conditions byte bound exceeded")
    # Hash canonical fixture bytes without returning any potentially private text.
    canonical = json.dumps(fixture, sort_keys=True, ensure_ascii=False,
                           separators=(",", ":")).encode("utf-8")
    return fixture, "sha256:" + hashlib.sha256(canonical).hexdigest()


@dataclass(frozen=True)
class Candidate:
    name: str
    model: str
    release: str
    quantization: str
    runtime: str
    support_evidence: str
    predictions: Mapping[str, str]
    # Values must be measurements from the actual run, not model-card estimates.
    measured: Mapping[str, Any]
    failures: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()


def _measured_resource(data: Mapping[str, Any], case_count: int) -> dict[str, Any]:
    required = {"warm_latency_seconds", "cold_latency_seconds", "cpu_utilization_percent",
                "gpu_utilization_percent", "peak_vram_mib", "peak_ram_mib"}
    if set(data) != required:
        raise AudioStt178Error("resource measurements must be complete and explicit")
    result: dict[str, Any] = {}
    for key, value in data.items():
        if (not isinstance(value, list) or len(value) != case_count or
                any(isinstance(v, bool) or not isinstance(v, (float, int)) or
                    not math.isfinite(v) or v < 0 for v in value)):
            raise AudioStt178Error(f"invalid measured resource series: {key}")
        result[key] = [float(v) for v in value]
    if any(v > 100 for v in result["cpu_utilization_percent"] + result["gpu_utilization_percent"]):
        raise AudioStt178Error("CPU/GPU utilization percentages cannot exceed 100")
    return result


def compare(fixture: Mapping[str, Any], fixture_digest: str,
            candidates: Sequence[Candidate]) -> dict[str, Any]:
    if not candidates or len(candidates) > MAX_CANDIDATES:
        raise AudioStt178Error("at least one candidate is required")
    case_ids = [case["id"] for case in fixture["cases"]]
    if len({candidate.name for candidate in candidates}) != len(candidates):
        raise AudioStt178Error("candidate names must be unique")
    reports = []
    norm = fixture["normalization"]
    target_cases = [case for case in fixture["cases"]
                    if "homophone:散歩-vs-3本" in case["tags"]]
    for candidate in candidates:
        identities = (candidate.name, candidate.model, candidate.release,
                      candidate.quantization, candidate.runtime)
        if not all(isinstance(x, str) and x.strip() for x in identities):
            raise AudioStt178Error("candidate model/release/quantization/runtime identity is required")
        if not candidate.support_evidence.strip():
            raise AudioStt178Error("model support evidence must be explicit (unknown is allowed)")
        if not isinstance(candidate.predictions, Mapping) or set(candidate.predictions) != set(case_ids):
            raise AudioStt178Error("candidate predictions must cover exactly the fixture cases")
        if any(not isinstance(value, str) or len(value) > MAX_TEXT_CHARS
               for value in candidate.predictions.values()):
            raise AudioStt178Error("prediction text length bound exceeded")
        resources = _measured_resource(candidate.measured, len(case_ids))
        # The declared whitespace policy applies to exact match and CER. WER
        # retains whitespace-tokenized segmentation; no Japanese tokenizer is
        # inferred by this evaluator.
        references = [normalize(case["reference"], form=norm["form"],
                                remove_whitespace=norm["remove_whitespace"])
                      for case in fixture["cases"]]
        hypotheses = [normalize(candidate.predictions[cid], form=norm["form"],
                                remove_whitespace=norm["remove_whitespace"])
                      for cid in case_ids]
        wer_references = [normalize(case["reference"], form=norm["form"],
                                     remove_whitespace=False) for case in fixture["cases"]]
        wer_hypotheses = [normalize(candidate.predictions[cid], form=norm["form"],
                                     remove_whitespace=False) for cid in case_ids]
        edit_cells = sum(
            len(ref) * len(hyp) + len(wref.split()) * len(whyp.split())
            for ref, hyp, wref, whyp in zip(
                references, hypotheses, wer_references, wer_hypotheses
            )
        )
        if edit_cells > MAX_EDIT_CELLS_PER_CANDIDATE:
            raise AudioStt178Error("candidate aggregate edit-distance work bound exceeded")
        cers = [error_rate(ref, hyp, "cer") for ref, hyp in zip(references, hypotheses)]
        wers = [error_rate(ref, hyp, "wer")
                for ref, hyp in zip(wer_references, wer_hypotheses)]
        hits: dict[str, dict[str, int]] = {
            "散歩": {"hits": 0, "cases": 0}, "3本": {"hits": 0, "cases": 0}
        }
        for case in target_cases:
            target = case["target"]
            prediction = normalize(candidate.predictions[case["id"]], form=norm["form"],
                                   remove_whitespace=True)
            hits[target]["cases"] += 1
            # Credit one side of the contrast, never an answer listing both.
            hits[target]["hits"] += _matches_contrast(prediction, target)
        target_count = sum(value["cases"] for value in hits.values())
        target_hits = sum(value["hits"] for value in hits.values())
        elapsed = resources["warm_latency_seconds"]
        durations = [float(case["duration_seconds"]) for case in fixture["cases"]]
        if len(durations) != len(case_ids) or any(d <= 0 or not math.isfinite(d) for d in durations):
            raise AudioStt178Error("each case needs a positive audio duration for RTF")
        rtf = [t / d for t, d in zip(elapsed, durations)]
        if any(not math.isfinite(value) for value in rtf):
            raise AudioStt178Error("RTF is outside the finite measurement range")
        reports.append({
            "name": candidate.name, "model": candidate.model, "release": candidate.release,
            "quantization": candidate.quantization, "runtime": candidate.runtime,
            "support_evidence": candidate.support_evidence,
            "exact_match_rate": sum(a == b for a, b in zip(references, hypotheses)) / len(case_ids),
            "cer_macro": sum(cers) / len(cers), "wer_macro": sum(wers) / len(wers),
            "aggregation": "macro-average of per-utterance rates",
            "homophone_target_hits": {
                "per_target": {
                    target: {"hits": data["hits"], "cases": data["cases"],
                             "hit_rate": data["hits"] / data["cases"] if data["cases"] else 0.0}
                    for target, data in hits.items()
                },
                "overall_hits": target_hits, "overall_cases": target_count,
                "overall_hit_rate": target_hits / target_count if target_count else 0.0,
            },
            "p50_warm_latency_seconds": percentile(elapsed, 50),
            "p95_warm_latency_seconds": percentile(elapsed, 95),
            "p50_cold_latency_seconds": percentile(resources["cold_latency_seconds"], 50),
            "p95_cold_latency_seconds": percentile(resources["cold_latency_seconds"], 95),
            "mean_rtf": sum(rtf) / len(rtf),
            "resource_measurements": resources,
            "observed_failures": list(candidate.failures),
            "limitations": list(candidate.limitations),
            "correctness_status": "MEASURED",
            "speed_status": "MEASURED",
            "threshold_assessment": {
                "cer_within_threshold": sum(cers) / len(cers) <= 0.10,
                "wer_within_threshold": sum(wers) / len(wers) <= 0.20,
                "mean_rtf_within_threshold": sum(rtf) / len(rtf) <= 1.0,
                "screening_only_not_selection": True,
            },
        })
    return {
        "report_version": SCHEMA, "fixture_sha256": fixture_digest,
        "case_count": len(case_ids), "normalization": dict(norm),
        "metric_aggregation": "macro-average per utterance; not corpus-weighted",
        "segmentation": fixture["segmentation"],
        "test_conditions": fixture["test_conditions"],
        "privacy": "Local private report; free-form conditions and identities may contain sensitive data.",
        "candidates": reports,
        "human_selection_gate": {
            "status": "DECISION_REQUIRED",
            "options": [item.name for item in candidates],
            "thresholds": {
                "maximum_cer": 0.10, "maximum_wer": 0.20,
                "maximum_mean_rtf": 1.0,
                "hardware_target": "NVIDIA GeForce GTX 1080 Ti, exact host/driver evidence required",
            },
            "rule": "Select only after every candidate is actually tested against a labelled private fixture; model-card claims do not qualify.",
            "downstream_des_audio_006": "BLOCKED_PENDING_HUMAN_SELECTION",
        },
        "empirical_completion": False,
    }


def _read_bounded(path: Path, limit: int) -> bytes:
    with path.open("rb") as handle:
        value = handle.read(limit + 1)
    if len(value) > limit:
        raise AudioStt178Error("input byte bound exceeded")
    return value


def write_report_exclusive(path: Path, report: Mapping[str, Any]) -> None:
    """Atomically publish a new local report with mode 0600; never replace one."""
    parent = path.parent
    temp_name: str | None = None
    try:
        fd, temp_name = tempfile.mkstemp(prefix=".audio-stt-178-", dir=parent)
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write((json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temp_name, path)
        os.unlink(temp_name)
        temp_name = None
    except FileExistsError as exc:
        raise AudioStt178Error("report already exists") from exc
    except OSError as exc:
        raise AudioStt178Error("report could not be created") from exc
    finally:
        if temp_name is not None:
            try:
                os.unlink(temp_name)
            except OSError:
                pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--results", type=Path, required=True,
                        help="local JSON containing candidate outputs and measured resources")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        fixture, digest = load_fixture(args.fixture)
        payload = json.loads(_read_bounded(args.results, MAX_RESULTS_BYTES))
        candidates = [Candidate(**{**item, "predictions": item["predictions"],
                                   "failures": tuple(item.get("failures", ())),
                                   "limitations": tuple(item.get("limitations", ()))})
                      for item in payload["candidates"]]
        report = compare(fixture, digest, candidates)
        write_report_exclusive(args.report, report)
    except Exception:
        # Exception text may contain local paths, fixture values, or transcripts.
        print("audio-stt-178: evaluation failed; see private local inputs", file=sys.stderr)
        raise SystemExit(2)
    print(json.dumps({"status": "report_written", "empirical_completion": False}))


if __name__ == "__main__":
    main()
