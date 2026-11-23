"""Synthetic-only tests for the offline #178 comparative scorer."""

import errno
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


# ---- review follow-ups -------------------------------------------------------

from astrumweaver.validation import audio_stt_178 as stt  # noqa: E402


def results_value(**override):
    item = {
        "name": "synthetic-fixture-only", "model": "not-a-real-model", "release": "synthetic",
        "quantization": "none", "runtime": "pytest", "support_evidence": "unknown; synthetic test only",
        "predictions": {"homo-sanpo": "今日は散歩に行く", "homo-sanbon": "水を3本買う"},
        "measured": {
            "warm_latency_seconds": [0.1, 0.2], "cold_latency_seconds": [0.3, 0.4],
            "cpu_utilization_percent": [10, 20], "gpu_utilization_percent": [0, 0],
            "peak_vram_mib": [0, 0], "peak_ram_mib": [100, 120],
        },
        "limitations": ["Synthetic test data; no inference performed."],
    }
    item.update(override)
    return {"candidates": [item]}


def write_inputs(tmp_path, results=None, fixture=None):
    fx, rs = tmp_path / "fixture.json", tmp_path / "results.json"
    fx.write_text(json.dumps(fixture or fixture_value(), ensure_ascii=False), encoding="utf-8")
    rs.write_text(json.dumps(results or results_value(), ensure_ascii=False), encoding="utf-8")
    return fx, rs


def run_cli(monkeypatch, fx, rs, report):
    monkeypatch.setattr("sys.argv", ["audio-stt-178", "--fixture", str(fx), "--results", str(rs),
                                     "--report", str(report)])
    stt.main()


@pytest.mark.parametrize("prediction", ["水を13本買う", "水を23本買う", "水を103本買う"])
def test_a_wrong_count_that_contains_the_digit_is_not_a_hit(prediction):
    c = replace(candidate(), predictions={"homo-sanpo": "今日は散歩に行く", "homo-sanbon": prediction})
    result = compare(fixture_value(), "sha256:f", [c])["candidates"][0]
    assert result["homophone_target_hits"]["per_target"]["3本"]["hits"] == 0
    assert result["homophone_target_hits"]["per_target"]["散歩"]["hits"] == 1


def test_the_digit_rule_also_protects_the_fixture(tmp_path):
    broken = fixture_value()
    broken["cases"][1]["reference"] = "水を13本買う"  # tagged as the 3本 target, but it says 13
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(AudioStt178Error, match="sentence context"):
        stt.load_fixture(path)


def test_error_rates_have_pinned_nonzero_values():
    assert stt.error_rate("今日は晴れ", "今日は雨", "cer") == pytest.approx(2 / 5)
    assert stt.error_rate("a b c", "a x c", "wer") == pytest.approx(1 / 3)
    assert stt.error_rate("a b c", "a b", "wer") == pytest.approx(1 / 3)
    assert stt.error_rate("", "x", "cer") == 1.0
    assert normalize("ＡＢ　ｃ", form="NFKC", remove_whitespace=False) == "AB c"
    assert normalize("ＡＢ", form="none") == "ＡＢ"
    c = replace(candidate(), predictions={"homo-sanpo": "今日は散歩に行く", "homo-sanbon": "水を2本買う"})
    result = compare(fixture_value(), "sha256:f", [c])["candidates"][0]
    assert result["exact_match_rate"] == 0.5
    assert result["cer_macro"] == pytest.approx((0 + 1 / 6) / 2)


@pytest.mark.parametrize("field,value", [
    ("warm_latency_seconds", [0.1]),                    # wrong length
    ("warm_latency_seconds", [0.1, float("nan")]),
    ("warm_latency_seconds", [0.1, -1]),
    ("warm_latency_seconds", [0.1, True]),
    ("cpu_utilization_percent", [10, 101]),
    ("gpu_utilization_percent", [0, 100.5]),
])
def test_bad_measurement_series_are_rejected(field, value):
    c = candidate()
    measured = dict(c.measured)
    measured[field] = value
    with pytest.raises(AudioStt178Error):
        compare(fixture_value(), "sha256:f", [replace(c, measured=measured)])


def test_duplicate_names_cases_and_extra_or_missing_fixture_keys_are_rejected(tmp_path):
    with pytest.raises(AudioStt178Error, match="unique"):
        compare(fixture_value(), "sha256:f", [candidate(), candidate()])
    path = tmp_path / "fixture.json"
    for mutate in (
        lambda f: f["cases"].append(dict(f["cases"][0])),
        lambda f: f.update(extra=1),
        lambda f: f.pop("segmentation"),
        lambda f: f["cases"][0].update(extra=1),
    ):
        broken = fixture_value()
        mutate(broken)
        path.write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")
        with pytest.raises(AudioStt178Error):
            stt.load_fixture(path)


@pytest.mark.parametrize("raw", [
    b'{"a": NaN}', b'{"a": Infinity}', b'{"a": 1, "a": 2}', b"\xff\xfe", b"[" * 100000, b"",
])
def test_json_input_is_strict(raw):
    with pytest.raises(AudioStt178Error):
        stt.strict_json_loads(raw)


def test_a_non_utf8_or_nan_fixture_is_one_clean_error(tmp_path):
    path = tmp_path / "fixture.json"
    path.write_bytes(b"\xff\xfe\x00")
    with pytest.raises(AudioStt178Error):
        stt.load_fixture(path)
    bad = fixture_value()
    bad["test_conditions"]["x"] = float("nan")
    path.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")  # writes a bare NaN
    with pytest.raises(AudioStt178Error):
        stt.load_fixture(path)


@pytest.mark.parametrize("override", [
    {"failures": "abc"}, {"limitations": "abc"}, {"failures": [1, 2]}, {"limitations": ["x"] * 101},
    {"failures": ["x" * 2001]}, {"support_evidence": "x" * 4097}, {"model": "m" * 257},
])
def test_notes_and_identity_fields_are_typed_and_bounded(override):
    base = candidate()
    with pytest.raises(AudioStt178Error):
        compare(fixture_value(), "sha256:f", [replace(base, **override)])


def test_inputs_must_be_regular_files_without_links(tmp_path):
    real = tmp_path / "real.json"
    real.write_text("{}")
    link = tmp_path / "link.json"
    link.symlink_to(real)
    with pytest.raises(OSError):
        stt._read_bounded(link, 100)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(AudioStt178Error, match="regular file"):
        stt._read_bounded(fifo, 100)


def test_cli_writes_a_private_report_with_digests_and_prints_no_private_data(tmp_path, monkeypatch, capsys):
    fx, rs = write_inputs(tmp_path)
    report = tmp_path / "report.json"
    run_cli(monkeypatch, fx, rs, report)
    out = capsys.readouterr()
    assert json.loads(out.out) == {"status": "report_written", "empirical_completion": False}
    assert str(tmp_path) not in out.out + out.err and "散歩" not in out.out + out.err
    written = json.loads(report.read_text(encoding="utf-8"))
    assert oct(report.stat().st_mode & 0o777) == "0o600"
    import hashlib
    assert written["results_sha256"] == "sha256:" + hashlib.sha256(rs.read_bytes()).hexdigest()
    assert written["fixture_sha256"].startswith("sha256:")


def test_cli_failure_is_one_generic_message_and_exit_code_2(tmp_path, monkeypatch, capsys):
    fx, rs = write_inputs(tmp_path, results=results_value(failures="not-a-list"))
    with pytest.raises(SystemExit) as raised:
        run_cli(monkeypatch, fx, rs, tmp_path / "report.json")
    err = capsys.readouterr()
    assert raised.value.code == 2 and err.err.strip().startswith("audio-stt-178: evaluation failed")
    assert str(tmp_path) not in err.err + err.out and "not-a-list" not in err.err
    assert not (tmp_path / "report.json").exists()


def test_report_destination_rules(tmp_path):
    target = tmp_path / "real.json"
    link = tmp_path / "link.json"
    link.symlink_to(target)  # dangling link at the destination
    with pytest.raises(AudioStt178Error, match="already exists"):
        stt.write_report_exclusive(link, {"a": 1})
    assert not target.exists()
    with pytest.raises(AudioStt178Error, match="could not be created"):
        stt.write_report_exclusive(tmp_path / "missing-dir" / "r.json", {"a": 1})
    with pytest.raises(AudioStt178Error):
        stt.write_report_exclusive(tmp_path / "nan.json", {"a": float("nan")})
    assert not (tmp_path / "nan.json").exists()


def test_a_file_system_without_hard_links_gets_an_exclusive_private_report(tmp_path, monkeypatch):
    def no_links(*_a, **_k):
        raise OSError(errno.EPERM, "hard links are not supported here")

    monkeypatch.setattr(stt.os, "link", no_links)
    report = tmp_path / "report.json"
    stt.write_report_exclusive(report, {"a": 1})
    assert json.loads(report.read_text())["a"] == 1 and oct(report.stat().st_mode & 0o777) == "0o600"
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".audio-stt-178-")]
    with pytest.raises(AudioStt178Error, match="already exists"):
        stt.write_report_exclusive(report, {"a": 2})
    assert json.loads(report.read_text())["a"] == 1  # never replaced


def test_a_failed_fallback_write_leaves_no_partial_report(tmp_path, monkeypatch):
    monkeypatch.setattr(stt.os, "link", lambda *_a, **_k: (_ for _ in ()).throw(OSError(errno.EOPNOTSUPP, "no links")))
    real_fsync = os.fsync
    calls = {"n": 0}

    def flaky(fd):
        calls["n"] += 1
        if calls["n"] == 2:  # the temp file synced; the exclusive create fails
            raise OSError("disk error")
        return real_fsync(fd)

    monkeypatch.setattr(stt.os, "fsync", flaky)
    report = tmp_path / "report.json"
    with pytest.raises(AudioStt178Error, match="could not be created"):
        stt.write_report_exclusive(report, {"a": 1})
    assert not report.exists()


def test_a_failing_temp_cleanup_after_a_successful_publish_is_not_an_error(tmp_path, monkeypatch):
    real_unlink = os.unlink

    def flaky_unlink(name, *a, **k):
        if os.path.basename(str(name)).startswith(".audio-stt-178-"):
            raise OSError("cannot remove the temp file")
        return real_unlink(name, *a, **k)

    monkeypatch.setattr(stt.os, "unlink", flaky_unlink)
    report = tmp_path / "report.json"
    stt.write_report_exclusive(report, {"a": 1})  # no exception: the report exists
    assert json.loads(report.read_text())["a"] == 1


# ---- second review follow-ups ---------------------------------------------------------

@pytest.mark.parametrize("prediction", ["水を0.3本買う", "水を1.3本買う", "水を1,3本買う", "水を１3本買う", "水を13本買う"])
def test_other_numbers_that_end_in_3_hon_are_not_hits(prediction):
    f = fixture_value()
    f["normalization"] = {"form": "none", "remove_whitespace": True}  # no NFKC: full-width stays full-width
    c = replace(candidate(), predictions={"homo-sanpo": "今日は散歩に行く", "homo-sanbon": prediction})
    assert compare(f, "sha256:f", [c])["candidates"][0]["homophone_target_hits"]["per_target"]["3本"]["hits"] == 0


def test_the_plain_3_hon_and_a_leading_particle_are_still_hits():
    for prediction in ("水を3本買う", "3本", "約3本です"):
        c = replace(candidate(), predictions={"homo-sanpo": "今日は散歩に行く", "homo-sanbon": prediction})
        hits = compare(fixture_value(), "sha256:f", [c])["candidates"][0]["homophone_target_hits"]
        assert hits["per_target"]["3本"]["hits"] == 1, prediction


@pytest.mark.parametrize("code", [errno.ENOSPC, errno.EIO, errno.EACCES])
def test_only_unsupported_hard_links_fall_back_to_a_second_write(tmp_path, monkeypatch, code):
    monkeypatch.setattr(stt.os, "link", lambda *_a, **_k: (_ for _ in ()).throw(OSError(code, "disk")))
    report = tmp_path / "report.json"
    with pytest.raises(AudioStt178Error, match="could not be created"):
        stt.write_report_exclusive(report, {"a": 1})
    assert not report.exists()
    assert not [p for p in tmp_path.iterdir()]  # and no temp file is left


def test_the_fallback_removes_only_the_file_it_created(tmp_path, monkeypatch):
    monkeypatch.setattr(stt.os, "link", lambda *_a, **_k: (_ for _ in ()).throw(OSError(errno.EPERM, "no links")))
    report = tmp_path / "report.json"
    real_fsync = os.fsync
    state = {"n": 0}

    def swap_then_fail(fd):
        state["n"] += 1
        if state["n"] == 2:  # the exclusive create is being written: someone replaces the path
            os.rename(report, tmp_path / "ours.json")
            report.write_text("somebody else's file")
            raise OSError(errno.EIO, "disk error")
        return real_fsync(fd)

    monkeypatch.setattr(stt.os, "fsync", swap_then_fail)
    with pytest.raises(AudioStt178Error):
        stt.write_report_exclusive(report, {"a": 1})
    assert report.read_text() == "somebody else's file"  # not deleted


@pytest.mark.parametrize("big", [10**400, -(10**400)])
def test_huge_integers_are_a_clean_error_not_an_overflow(big):
    c = candidate()
    measured = dict(c.measured)
    measured["warm_latency_seconds"] = [0.1, big]
    with pytest.raises(AudioStt178Error):
        compare(fixture_value(), "sha256:f", [replace(c, measured=measured)])


def test_a_huge_duration_in_a_fixture_file_is_one_clean_error(tmp_path):
    f = fixture_value()
    text = json.dumps(f, ensure_ascii=False).replace('"duration_seconds": 1.0', '"duration_seconds": ' + "9" * 400)
    path = tmp_path / "fixture.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(AudioStt178Error):
        stt.load_fixture(path)


def test_a_utf8_byte_order_mark_is_refused_cleanly(tmp_path):
    path = tmp_path / "fixture.json"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(fixture_value(), ensure_ascii=False).encode("utf-8"))
    with pytest.raises(AudioStt178Error):
        stt.load_fixture(path)
