"""Synthetic-only tests for the offline #178 comparative scorer."""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier

import pytest

from astrumweaver.validation.audio_stt_178 import (
    AudioStt178Error, Candidate, compare, load_fixture, normalize, percentile,
)


def fixture_value():
    return {
        "schema_version": "audio-stt-178-v1",
        "normalization": {"form": "NFKC", "remove_whitespace": True},
        "segmentation": "Whitespace-delimited Japanese tokens for WER; no automatic tokenizer.",
        "test_conditions": {
            "provenance": "synthetic unit-test fixture; no recording or inference",
            "audio_domain": "invented Japanese sentences; audio paths are placeholders",
            "measurements": "invented test values; no hardware was measured",
        },
        "cases": [
            {"id": "homo-sanpo", "audio": "local/homo-sanpo.wav",
             "reference": "今日は散歩に行く", "target": "散歩", "duration_seconds": 1.0,
             "tags": ["homophone:散歩-vs-3本"]},
            {"id": "homo-sanbon", "audio": "local/homo-sanbon.wav",
             "reference": "水を3本買う", "target": "3本", "duration_seconds": 2.0,
             "tags": ["homophone:散歩-vs-3本"]},
        ],
    }


def candidate():
    return Candidate(
        name="synthetic-fixture-only", model="not-a-real-model",
        release="synthetic", quantization="none", runtime="pytest",
        support_evidence="unknown; synthetic test only",
        predictions={"homo-sanpo": "今日は散歩に行く", "homo-sanbon": "水を3本買う"},
        measured={
            "warm_latency_seconds": [0.1, 0.2],
            "cold_latency_seconds": [0.3, 0.4],
            "cpu_utilization_percent": [10, 20],
            "gpu_utilization_percent": [0, 0],
            "peak_vram_mib": [0, 0], "peak_ram_mib": [100, 120],
        },
        limitations=("Synthetic test data; no inference performed.",),
    )


def test_normalization_and_percentiles_are_deterministic():
    assert normalize(" ３ 本 ", form="NFKC") == "3本"
    assert percentile([4, 1, 3, 2], 50) == 2.5
    assert percentile([0.1, 0.2], 95) == pytest.approx(0.195)


def test_load_fixture_requires_distinct_homophone_contrast(tmp_path):
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(fixture_value(), ensure_ascii=False), encoding="utf-8")
    value, digest = load_fixture(path)
    assert value["cases"][0]["reference"] == "今日は散歩に行く"
    assert value["cases"][1]["reference"] == "水を3本買う"
    assert digest.startswith("sha256:")
    broken = fixture_value()
    broken["cases"][0]["tags"] = []
    path.write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(AudioStt178Error, match="contextual"):
        load_fixture(path)


def test_comparison_reports_metrics_resources_identity_and_blocking_gate():
    fixture = fixture_value()
    report = compare(fixture, "sha256:fixture", [candidate()])
    result = report["candidates"][0]
    assert result["exact_match_rate"] == 1
    assert result["cer_macro"] == 0
    assert result["wer_macro"] == 0
    assert result["homophone_target_hits"]["per_target"]["散歩"]["hits"] == 1
    assert result["homophone_target_hits"]["per_target"]["3本"]["hits"] == 1
    assert result["p50_warm_latency_seconds"] == pytest.approx(0.15)
    assert result["p95_warm_latency_seconds"] == pytest.approx(0.195)
    assert result["mean_rtf"] == pytest.approx((0.1 / 1.0 + 0.2 / 2.0) / 2)
    assert result["resource_measurements"]["gpu_utilization_percent"] == [0.0, 0.0]
    assert report["empirical_completion"] is False
    assert report["test_conditions"]["provenance"] == "synthetic unit-test fixture; no recording or inference"
    assert report["human_selection_gate"]["status"] == "DECISION_REQUIRED"
    assert report["human_selection_gate"]["downstream_des_audio_006"] == "BLOCKED_PENDING_HUMAN_SELECTION"
    assert "predictions" not in result
    assert report["metric_aggregation"] == "macro-average per utterance; not corpus-weighted"


def test_incomplete_or_unmeasured_candidates_are_not_scored():
    fixture = fixture_value()
    item = candidate()
    with pytest.raises(AudioStt178Error, match="exactly the fixture cases"):
        compare(fixture, "sha256:fixture", [
            replace(item, predictions={"homo-sanpo": "散歩"})
        ])
    with pytest.raises(AudioStt178Error, match="resource measurements"):
        compare(fixture, "sha256:fixture", [
            replace(item, measured={})
        ])


def test_empty_reference_has_defined_error_rate():
    from astrumweaver.validation.audio_stt_178 import error_rate
    assert error_rate("", "", "cer") == 0
    assert error_rate("", "x", "cer") == 1


def test_contextual_homophone_score_is_independent_of_full_sentence_exact_match():
    item = candidate()
    broken = replace(item, predictions={
        "homo-sanpo": "今日は山歩きに行く",
        "homo-sanbon": "水を3本買う",
    })
    result = compare(fixture_value(), "sha256:fixture", [broken])["candidates"][0]
    assert result["exact_match_rate"] < 1
    assert result["homophone_target_hits"]["per_target"]["散歩"]["hits"] == 0
    assert result["homophone_target_hits"]["per_target"]["3本"]["hits"] == 1


@pytest.mark.parametrize("prediction", ["散歩と3本", "３ 本、それから散 歩"])
def test_ambiguous_prediction_misses_both_contextual_targets(prediction):
    item = candidate()
    ambiguous = replace(item, predictions=dict.fromkeys(item.predictions, prediction))
    score = compare(fixture_value(), "sha256:fixture", [ambiguous])["candidates"][0]
    hits = score["homophone_target_hits"]
    assert hits["overall_cases"] == 2
    assert hits["overall_hits"] == 0
    assert hits["overall_hit_rate"] == 0
    for target in ("散歩", "3本"):
        assert hits["per_target"][target] == {"hits": 0, "cases": 1, "hit_rate": 0}


@pytest.mark.parametrize("reference", ["散歩", "散歩して水を３本買った"])
def test_tagged_reference_must_give_unambiguous_context(tmp_path, reference):
    fixture = fixture_value()
    fixture["cases"][0]["reference"] = reference
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(fixture), encoding="utf-8")
    with pytest.raises(AudioStt178Error, match="sentence context"):
        load_fixture(path)


def test_edit_distance_and_input_lengths_are_bounded():
    from astrumweaver.validation.audio_stt_178 import (
        MAX_TEXT_CHARS, error_rate, normalize,
    )
    with pytest.raises(AudioStt178Error, match="length bound"):
        error_rate("a" * (MAX_TEXT_CHARS + 1), "a", "cer")
    with pytest.raises(AudioStt178Error, match="work bound"):
        error_rate("a" * 4000, "b" * 4000, "cer")
    with pytest.raises(AudioStt178Error, match="length bound"):
        normalize("a" * (MAX_TEXT_CHARS + 1))


def test_fixture_byte_and_case_limits_are_enforced(tmp_path):
    from astrumweaver.validation.audio_stt_178 import MAX_CASES, MAX_FIXTURE_BYTES
    path = tmp_path / "fixture.json"
    path.write_bytes(b" " * (MAX_FIXTURE_BYTES + 1))
    with pytest.raises(AudioStt178Error, match="byte bound"):
        load_fixture(path)
    value = fixture_value()
    value["cases"] = [value["cases"][0]] * (MAX_CASES + 1)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(AudioStt178Error, match="cases"):
        load_fixture(path)


def test_report_refuses_overwrite_and_has_private_mode(tmp_path):
    from astrumweaver.validation.audio_stt_178 import write_report_exclusive
    target = tmp_path / "private-report.json"
    report = compare(fixture_value(), "sha256:fixture", [candidate()])
    write_report_exclusive(target, report)
    assert os.stat(target).st_mode & 0o777 == 0o600
    with pytest.raises(AudioStt178Error, match="already exists"):
        write_report_exclusive(target, report)


def test_concurrent_report_publication_has_one_complete_private_winner(tmp_path, monkeypatch):
    from astrumweaver.validation.audio_stt_178 import write_report_exclusive

    target = tmp_path / "private-report.json"
    reports = [compare(fixture_value(), "sha256:fixture", [replace(candidate(), name=name)])
               for name in ("synthetic-A", "synthetic-B")]
    barrier = Barrier(len(reports), timeout=5)
    link = os.link

    def simultaneous_link(source, destination):
        # Hold each writer at the actual filesystem publication boundary.
        # Both independent temporary files must be complete and owner-only.
        assert destination == target and not target.exists()
        assert Path(source).stat().st_mode & 0o777 == 0o600
        assert json.loads(Path(source).read_bytes()) in reports
        barrier.wait()
        link(source, destination)

    monkeypatch.setattr(os, "link", simultaneous_link)
    def publish(report):
        try:
            write_report_exclusive(target, report)
            return True
        except AudioStt178Error as exc:
            assert str(exc) == "report already exists"
            return False

    with ThreadPoolExecutor(max_workers=len(reports)) as pool:
        outcomes = list(pool.map(publish, reports))
    assert outcomes.count(True) == 1
    assert list(tmp_path.iterdir()) == [target]
    assert json.loads(target.read_bytes()) == reports[outcomes.index(True)]
    assert target.stat().st_mode & 0o777 == 0o600


def test_failed_sync_does_not_publish_partial_report(tmp_path, monkeypatch):
    from astrumweaver.validation.audio_stt_178 import write_report_exclusive

    def failed_sync(fd):
        raise OSError("simulated sync failure")

    monkeypatch.setattr(os, "fsync", failed_sync)
    with pytest.raises(AudioStt178Error, match="report could not be created"):
        write_report_exclusive(tmp_path / "private-report.json", {"synthetic": True})
    assert not list(tmp_path.iterdir())
