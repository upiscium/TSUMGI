"""#247 R3: synthetic-only, deterministic external VOICEVOX voice review tests."""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace

import pytest

from astrumweaver.gateway.audio import VoiceAlias
from astrumweaver.runtime.providers.voicevox_discovery import (
    VoicevoxDiscoveryConfig,
    VoicevoxDiscoveryResult,
    VoicevoxDiscoveryStatus,
    VoicevoxObservedStyle,
    _catalog,
)
from astrumweaver.runtime.providers.voicevox_external_catalog import (
    VoicevoxExternalCatalogError,
    review_external_voicevox_catalog,
)

_UUID_A = "7ffcb7ce-00ec-4bdc-82cd-45a8889e43ff"
_UUID_B = "388f246b-8c41-4ac1-8e2d-5d79f3ff56d9"
_ENDPOINT = "http://127.0.0.1:50021"


def _config(port=50021):
    return VoicevoxDiscoveryConfig(enabled=True, endpoint=f"http://127.0.0.1:{port}")


def _observation(
    *,
    endpoint=_ENDPOINT,
    version="0.15.0",
    include_second=False,
):
    rows = [
        {
            "speaker_uuid": _UUID_A,
            "styles": [{"id": 8}, {"id": 17, "type": "streaming_talk"}],
        }
    ]
    if include_second:
        rows.append({"speaker_uuid": _UUID_B, "styles": [{"id": 2}]})
    catalog = _catalog(version, rows, endpoint)
    return VoicevoxDiscoveryResult(
        VoicevoxDiscoveryStatus.COMPATIBLE,
        "observed_compatible",
        catalog,
    )


def _voice(
    alias="narrator",
    *,
    speaker_id=_UUID_A,
    style_id="8",
    license_id="operator-terms-v1",
    attribution="Voice provided by the operator",
):
    return VoiceAlias(
        alias=alias,
        speaker_id=speaker_id,
        style_id=style_id,
        locale="ja",
        license_id=license_id,
        attribution=attribution,
    )


def _review(
    observation=None,
    *,
    config=None,
    voices=None,
    expected_observed_digest=None,
):
    observed = _observation() if observation is None else observation
    digest = (
        observed.snapshot.observed_digest
        if expected_observed_digest is None and observed.snapshot is not None
        else expected_observed_digest
    )
    return review_external_voicevox_catalog(
        enabled=True,
        config=config or _config(),
        observation=observed,
        expected_observed_digest=digest,
        voices=voices if voices is not None else (_voice(),),
    )


def _blocked(review):
    assert review.ready is False
    assert review.can_apply is False
    assert review.external_engine_verified is False
    assert review.rights_verified is False
    assert "worker_admission_unavailable" in review.blockers
    assert "voice_rights_unverified" in review.blockers
    assert "external_engine_identity_unverified" in review.blockers
    assert "native_access_log_policy_unverified" in review.blockers
    assert "live_observation_requalification_required" in review.blockers


def test_matching_declaration_is_only_a_pinned_review_not_engine_authority():
    observed = _observation(include_second=True)
    declared = (
        _voice(alias="stream", style_id="17"),
        _voice(alias="second", speaker_id=_UUID_B, style_id="2"),
        _voice(),
    )
    report = _review(observed, voices=declared)
    assert report.enabled and report.status == "review_only_blocked"
    assert report.observed_digest == observed.snapshot.observed_digest
    assert report.voice_semantic_revision.startswith("sha256:")
    assert report.review_digest.startswith("sha256:")
    assert [v.alias for v in report.declared_voices] == ["narrator", "second", "stream"]
    _blocked(report)
    encoded = report.to_json()
    assert report.to_dict()["declared_voice_count"] == 3
    assert encoded == report.to_json()
    for private in (
        "narrator",
        "stream",
        "second",
        _UUID_A,
        _UUID_B,
        "operator-terms",
        "Voice provided",
        _ENDPOINT,
    ):
        assert private not in encoded
        assert private not in repr(report)


def test_review_stable_when_operator_voices_arrive_in_different_order():
    observed = _observation()
    a = _voice()
    b = _voice(alias="stream", style_id="17")
    first = _review(observed, voices=(a, b))
    second = _review(observed, voices=(b, a))
    assert first.to_json() == second.to_json()
    assert first.review_digest == second.review_digest
    assert first.voice_semantic_revision == second.voice_semantic_revision
    _blocked(first)


@pytest.mark.parametrize(
    "field,value",
    [
        ("license_id", "terms-different"),
        ("attribution", "Credit changed"),
        ("alias", "different-alias"),
    ],
)
def test_operator_declaration_changes_invalidate_revision(field, value):
    old = _review(voices=(_voice(),))
    updated = replace(_voice(), **{field: value})
    new = _review(voices=(updated,))
    assert old.voice_semantic_revision != new.voice_semantic_revision
    assert old.review_digest != new.review_digest
    _blocked(new)


def test_version_or_endpoint_change_changes_observation_fingerprint():
    a = _observation()
    b = _observation(version="0.16.0")
    c = _observation(endpoint="http://127.0.0.1:51021")
    assert a.snapshot.observed_digest != b.snapshot.observed_digest
    assert a.snapshot.observed_digest != c.snapshot.observed_digest
    first = _review(a)
    second = _review(b)
    assert first.review_digest != second.review_digest
    mismatch = _review(c)
    assert mismatch.status == "invalid_observation"
    _blocked(mismatch)


def test_disabled_inspects_nothing_including_adversarial_objects():
    class Trap:
        def __getattribute__(self, name):
            pytest.fail(f"disabled accessed {name}")

        def __eq__(self, other):
            pytest.fail("disabled evaluated arbitrary equality")

    result = review_external_voicevox_catalog(
        enabled=False,
        config=Trap(),
        observation=Trap(),
        expected_observed_digest=Trap(),
        voices=Trap(),
    )
    assert result.status == "disabled"
    assert result.to_dict()["declared_voice_count"] == 0
    assert result.to_dict()["observed_digest"] is None
    assert result.ready is False and result.can_apply is False
    assert result.blockers == ()


@pytest.mark.parametrize("enabled", [0, 1, None, "yes", (), object()])
def test_enabled_must_be_exact_boolean(enabled):
    with pytest.raises(VoicevoxExternalCatalogError):
        review_external_voicevox_catalog(enabled=enabled)


def test_enabled_requires_revalidated_exact_loopback_config():
    with pytest.raises(VoicevoxExternalCatalogError):
        review_external_voicevox_catalog(enabled=True, config=object())
    with pytest.raises(VoicevoxExternalCatalogError):
        review_external_voicevox_catalog(
            enabled=True, config=VoicevoxDiscoveryConfig(enabled=False)
        )
    mutated = _config()
    object.__setattr__(mutated, "endpoint", "http://127.0.0.1:50021/path")
    with pytest.raises(VoicevoxExternalCatalogError):
        review_external_voicevox_catalog(enabled=True, config=mutated)


def test_missing_observation_remains_unqualified():
    review = review_external_voicevox_catalog(enabled=True, config=_config())
    assert review.status == "observation_required"
    assert review.declared_voices == ()
    _blocked(review)


@pytest.mark.parametrize(
    "status",
    [
        VoicevoxDiscoveryStatus.ABSENT,
        VoicevoxDiscoveryStatus.INCOMPATIBLE,
        VoicevoxDiscoveryStatus.DISABLED,
    ],
)
def test_noncompatible_observation_never_selects_voices(status):
    observed = VoicevoxDiscoveryResult(status, "private raw URL or token")
    result = review_external_voicevox_catalog(
        enabled=True,
        config=_config(),
        observation=observed,
        expected_observed_digest="anything-not-checked",
        voices=object(),
    )
    assert result.status == "observation_unavailable"
    assert result.declared_voices == ()
    assert "private" not in repr(result) and "private" not in result.to_json()
    _blocked(result)


@pytest.mark.parametrize(
    "observation",
    [
        object(),
        VoicevoxDiscoveryResult("COMPATIBLE", "private"),
        VoicevoxDiscoveryResult(VoicevoxDiscoveryStatus.COMPATIBLE, "private", None),
    ],
)
def test_malformed_or_fabricated_result_stays_non_applicable(observation):
    report = review_external_voicevox_catalog(
        enabled=True,
        config=_config(),
        observation=observation,
    )
    assert report.status == "invalid_observation"
    _blocked(report)


@pytest.mark.parametrize(
    "modification",
    [
        lambda s: replace(s, observed_digest="sha256:" + "a" * 64),
        lambda s: replace(s, version="9.9.9"),
        lambda s: replace(s, styles=s.styles[::-1]),
        lambda s: replace(s, styles=()),
        lambda s: replace(s, styles=list(s.styles)),
        lambda s: replace(s, styles=(VoicevoxObservedStyle(_UUID_A, True),)),
        lambda s: replace(s, styles=(VoicevoxObservedStyle("bad-uuid", 8),)),
        lambda s: replace(
            s,
            styles=(
                VoicevoxObservedStyle(_UUID_A, 8),
                VoicevoxObservedStyle(_UUID_A, 8),
            ),
        ),
        lambda s: replace(s, styles=(VoicevoxObservedStyle(_UUID_A, 2**31),)),
        lambda s: replace(s, observed_digest="bad"),
    ],
)
def test_corrupted_snapshot_fails_closed(modification):
    observed = _observation()
    forged = replace(observed, snapshot=modification(observed.snapshot))
    report = _review(forged)
    assert report.status == "invalid_observation"
    assert report.declared_voices == ()
    _blocked(report)


def test_altered_snapshot_is_not_an_attestation_of_engine_process_identity():
    source = _observation()
    forged = replace(source, reason="http://private.example.test/?token=secret")
    review = _review(forged)
    assert review.status == "review_only_blocked"
    for txt in ("private.example", "token", "secret"):
        assert txt not in review.to_json()
        assert txt not in repr(review)
    _blocked(review)


def test_missing_operator_pinned_observation_digest_is_blocked():
    report = review_external_voicevox_catalog(
        enabled=True,
        config=_config(),
        observation=_observation(),
        voices=(_voice(),),
    )
    assert report.status == "observation_pin_required"
    assert report.declared_voices == ()
    _blocked(report)


def test_operator_observation_drift_blocks_stale_voice_selection():
    observed = _observation(version="0.16.0")
    old_digest = _observation().snapshot.observed_digest
    report = _review(observed, expected_observed_digest=old_digest)
    assert report.status == "observation_changed"
    assert report.review_digest is None
    assert report.declared_voices == ()
    _blocked(report)


@pytest.mark.parametrize(
    "digest",
    ["", "sha256:00", "sha256:" + "F" * 64, False, 9, object()],
)
def test_invalid_pin_is_sanitized(digest):
    with pytest.raises(VoicevoxExternalCatalogError) as exc:
        _review(expected_observed_digest=digest)
    assert str(exc.value) == "VOICEVOX external catalog review is invalid"


def test_empty_voice_selection_requires_operator_input():
    report = _review(voices=())
    assert report.status == "voice_selection_required"
    assert report.declared_voices == ()
    _blocked(report)


@pytest.mark.parametrize(
    "wrong_voice",
    [
        _voice(speaker_id=_UUID_B),
        _voice(style_id="9"),
        _voice(alias="wrong", style_id="2"),
    ],
)
def test_operator_selected_voice_must_exist_in_observed_snapshot(wrong_voice):
    report = _review(voices=(wrong_voice,))
    assert report.status == "voice_selection_changed"
    assert report.review_digest is None
    assert report.declared_voices == ()
    _blocked(report)


@pytest.mark.parametrize(
    "voices",
    [
        [_voice()],
        "private-voice-list",
        object(),
        (_voice(),) * 2,
        (_voice(), _voice(alias="another")),
        (_voice(), _voice(alias="narrator", style_id="17")),
        (_voice(style_id="08"),),
        (_voice(style_id="99999999999"),),
        (_voice(style_id="2147483648"),),
        (_voice(style_id="text"),),
    ],
)
def test_invalid_voices_or_ambiguous_duplicates_are_redacted(voices):
    with pytest.raises(VoicevoxExternalCatalogError) as exc:
        _review(voices=voices)
    assert str(exc.value) == "VOICEVOX external catalog review is invalid"


def test_mutated_frozen_voice_metadata_rechecked_and_rejected():
    voice = _voice()
    object.__setattr__(voice, "attribution", "private\npath")
    with pytest.raises(VoicevoxExternalCatalogError):
        _review(voices=(voice,))


def test_rights_statement_is_never_equivalent_to_verified_permission():
    claimed = _voice(
        license_id="I-am-the-owner",
        attribution="This is a synthetic declaration only",
    )
    report = _review(voices=(claimed,))
    assert report.status == "review_only_blocked"
    assert not report.rights_verified
    assert "voice_rights_unverified" in report.blockers
    assert not report.external_engine_verified
    assert report.declared_voices == (claimed,)
    assert report.to_dict()["rights_verified"] is False


@pytest.mark.asyncio
async def test_review_is_pure_and_never_opens_network_or_starts_native_process(
    monkeypatch,
):
    def reject(*_args, **_kwargs):
        pytest.fail("catalog review must not do native process or network I/O")

    async def reject_async(*_args, **_kwargs):
        reject()

    monkeypatch.setattr(asyncio, "open_connection", reject_async)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", reject_async)
    monkeypatch.setattr(os, "kill", reject)
    source = _observation()
    report = _review(source)
    _blocked(report)


def test_review_ignores_unverified_engine_artifact_and_native_pid_claims():
    report = _review()
    metadata = report.to_dict()
    assert "executable_sha256" not in metadata
    assert "model_sha256" not in metadata
    assert "pid" not in metadata
    assert "endpoint" not in metadata
    assert metadata["external_engine_verified"] is False
    assert metadata["can_apply"] is False


def test_reflectively_mutated_observation_reason_cannot_appear_in_public_json():
    observation = _observation()
    object.__setattr__(observation, "reason", "user-secret-live-URL")
    review = _review(observation)
    assert review.status == "review_only_blocked"
    assert "secret" not in review.to_json()
    assert "secret" not in repr(review)


def test_arbitrary_voice_object_is_rejected_before_equality_is_invoked():
    class Evil:
        def __eq__(self, other):
            raise RuntimeError("private-auth-token")

        def __bool__(self):
            raise RuntimeError("private-token")

    with pytest.raises(VoicevoxExternalCatalogError) as caught:
        _review(voices=Evil())
    assert str(caught.value) == "VOICEVOX external catalog review is invalid"


def test_oversized_or_reflectively_modified_timeout_config_is_revalidated():
    mutated = _config()
    object.__setattr__(mutated, "timeout_seconds", 10**1000)
    with pytest.raises(VoicevoxExternalCatalogError) as caught:
        _review(config=mutated)
    assert str(caught.value) == "VOICEVOX external catalog review is invalid"


def test_observed_snapshot_with_injected_private_reason_remains_a_point_in_time_report():
    observed = _observation()
    forged = replace(observed, reason="never-log-this-secret-token")
    review = _review(forged)
    assert review.status == "review_only_blocked"
    assert review.to_dict()["ready"] is False
    assert review.to_dict()["can_apply"] is False
    assert "never-log-this" not in review.to_json()
    assert "never-log-this" not in repr(review)
