"""#247 R3: pure source-only review of externally observed VOICEVOX voices.

An R1 observation is NOT engine identity, model/artifact attestation or rights.
Operator declarations are NOT license grants, and this module never wires
Worker, Control, Registry, SetupPlan, HTTP synthesis, or serving admission.
All results intentionally remain non-ready and non-applicable.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

from ...gateway.audio import VoiceAlias, audio_semantic_revision
from .voicevox_discovery import (
    VoicevoxDiscoveryConfig,
    VoicevoxDiscoveryResult,
    VoicevoxDiscoveryStatus,
    VoicevoxObservedCatalog,
    VoicevoxObservedStyle,
    _catalog,
)

_SCHEMA = "voicevox-external-catalog-review-v1"
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_DECIMAL_STYLE_ID = re.compile(r"(?:0|[1-9][0-9]{0,9})\Z")
_MAX_VOICES = 1024
_UNVERIFIED = (
    "external_engine_identity_unverified",
    "voice_rights_unverified",
    "native_access_log_policy_unverified",
    "live_observation_requalification_required",
    "worker_admission_unavailable",
)


class VoicevoxExternalCatalogError(ValueError):
    """Fixed diagnostic: never disclose license text, aliases or URLs."""

    def __init__(self) -> None:
        super().__init__("VOICEVOX external catalog review is invalid")


@dataclass(frozen=True, slots=True, repr=False)
class VoicevoxExternalCatalogReview:
    """Informational, immutable review. No attribute is an admission grant.

    declared_voices are operator statements; *not* voices approved by TSUMGI.
    The public JSON deliberately excludes UUIDs, labels, rights text and URLs.
    """

    enabled: bool
    status: str
    observed_digest: str | None = None
    voice_semantic_revision: str | None = None
    review_digest: str | None = None
    declared_voices: tuple[VoiceAlias, ...] = field(
        default=(), repr=False, compare=False
    )
    blockers: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return False

    @property
    def can_apply(self) -> bool:
        return False

    @property
    def rights_verified(self) -> bool:
        return False

    @property
    def external_engine_verified(self) -> bool:
        return False

    def to_dict(self) -> dict[str, object]:
        """Redacted metadata only; never a voice credential or setup plan."""
        return {
            "schema_version": _SCHEMA,
            "enabled": self.enabled,
            "status": self.status,
            "ready": False,
            "can_apply": False,
            "external_engine_verified": False,
            "rights_verified": False,
            "observed_digest": self.observed_digest,
            "voice_semantic_revision": self.voice_semantic_revision,
            "review_digest": self.review_digest,
            "declared_voice_count": len(self.declared_voices),
            "blockers": list(self.blockers),
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )


def _blocked(
    status: str,
    *,
    observed_digest: str | None = None,
    extra: str | None = None,
) -> VoicevoxExternalCatalogReview:
    blockers = _UNVERIFIED + ((extra,) if extra is not None else ())
    return VoicevoxExternalCatalogReview(
        enabled=True,
        status=status,
        observed_digest=observed_digest,
        blockers=blockers,
    )


def _verified_shape(
    snapshot: VoicevoxObservedCatalog, endpoint: str
) -> VoicevoxObservedCatalog | None:
    """Replay R1's strict catalog validation and endpoint-bound digest.

    This only validates a deterministic HTTP *observation*, not a genuine
    VOICEVOX process. R4 must independently probe again immediately before
    serving any voice; a matching hash is neither a signature nor a license.
    """
    try:
        if (
            type(snapshot) is not VoicevoxObservedCatalog
            or type(snapshot.styles) is not tuple
            or not 1 <= len(snapshot.styles) <= _MAX_VOICES
            or type(snapshot.observed_digest) is not str
            or _DIGEST.fullmatch(snapshot.observed_digest) is None
        ):
            return None
        speakers: dict[str, list[dict[str, int]]] = {}
        for item in snapshot.styles:
            if type(item) is not VoicevoxObservedStyle:
                return None
            speakers.setdefault(item.speaker_uuid, []).append({"id": item.style_id})
        synthetic_speakers = [
            {"speaker_uuid": uuid, "styles": items}
            for uuid, items in sorted(speakers.items())
        ]
        canonical = _catalog(snapshot.version, synthetic_speakers, endpoint)
        if (
            snapshot.styles != canonical.styles
            or snapshot.version != canonical.version
            or snapshot.observed_digest != canonical.observed_digest
        ):
            return None
        return canonical
    except Exception:  # noqa: BLE001 - never propagate native/foreign data
        return None


def _operator_voices(value: object) -> tuple[VoiceAlias, ...]:
    """Revalidate immutable declarations; never interpret them as rights proof."""
    try:
        if type(value) is not tuple or not 1 <= len(value) <= _MAX_VOICES:
            raise VoicevoxExternalCatalogError()
        verified: list[VoiceAlias] = []
        aliases: set[str] = set()
        selections: set[tuple[str, int]] = set()
        for item in value:
            if type(item) is not VoiceAlias:
                raise VoicevoxExternalCatalogError()
            # Even frozen dataclasses can be reflectively tampered with.
            copied = VoiceAlias(
                alias=item.alias,
                speaker_id=item.speaker_id,
                style_id=item.style_id,
                locale=item.locale,
                license_id=item.license_id,
                attribution=item.attribution,
            )
            if (
                copied != item
                or type(copied.speaker_id) is not str
                or type(copied.style_id) is not str
                or _DECIMAL_STYLE_ID.fullmatch(copied.style_id) is None
            ):
                raise VoicevoxExternalCatalogError()
            style_id = int(copied.style_id)
            if style_id >= 2**31:
                raise VoicevoxExternalCatalogError()
            pair = (copied.speaker_id, style_id)
            if copied.alias in aliases or pair in selections:
                raise VoicevoxExternalCatalogError()
            aliases.add(copied.alias)
            selections.add(pair)
            verified.append(copied)
        return tuple(sorted(verified, key=lambda voice: voice.alias))
    except Exception:  # noqa: BLE001 - sanitize malformed declaration details
        raise VoicevoxExternalCatalogError() from None


def review_external_voicevox_catalog(
    *,
    enabled: bool,
    config: VoicevoxDiscoveryConfig | None = None,
    observation: VoicevoxDiscoveryResult | None = None,
    expected_observed_digest: str | None = None,
    voices: tuple[VoiceAlias, ...] = (),
) -> VoicevoxExternalCatalogReview:
    """Pure exact-observation + operator declaration review, always blocked.

    A positive comparison only means the selected UUID/style IDs were present
    in a compatible, canonical *point-in-time* observation. It never claims
    that VOICEVOX is still running, rights are verified or TTS is admissible.
    """
    if type(enabled) is not bool:
        raise VoicevoxExternalCatalogError()
    if not enabled:
        # Deliberately do NOT inspect config, observation, pins or voice lists.
        return VoicevoxExternalCatalogReview(enabled=False, status="disabled")

    try:
        if type(config) is not VoicevoxDiscoveryConfig or not config.enabled:
            raise VoicevoxExternalCatalogError()
        # Revalidate a possibly reflectively mutated frozen declaration.
        verified_config = VoicevoxDiscoveryConfig(
            enabled=True,
            endpoint=config.endpoint,
            timeout_seconds=config.timeout_seconds,
        )
    except Exception:  # noqa: BLE001 - no raw endpoint or exceptions
        raise VoicevoxExternalCatalogError() from None

    if observation is None:
        return _blocked("observation_required", extra="observation_missing")
    if type(observation) is not VoicevoxDiscoveryResult:
        return _blocked("invalid_observation", extra="observation_invalid")
    if type(observation.status) is not VoicevoxDiscoveryStatus:
        return _blocked("invalid_observation", extra="observation_invalid")
    if observation.status is not VoicevoxDiscoveryStatus.COMPATIBLE:
        return _blocked("observation_unavailable", extra="observation_not_compatible")

    snapshot = _verified_shape(observation.snapshot, verified_config.endpoint)
    if snapshot is None:
        return _blocked("invalid_observation", extra="observation_invalid")

    if expected_observed_digest is None:
        return _blocked(
            "observation_pin_required",
            observed_digest=snapshot.observed_digest,
            extra="operator_observation_pin_missing",
        )
    if (
        type(expected_observed_digest) is not str
        or _DIGEST.fullmatch(expected_observed_digest) is None
    ):
        raise VoicevoxExternalCatalogError()
    if expected_observed_digest != snapshot.observed_digest:
        return _blocked(
            "observation_changed",
            observed_digest=snapshot.observed_digest,
            extra="observation_pin_changed",
        )
    if type(voices) is not tuple:
        # Never evaluate equality on an arbitrary operator-supplied object.
        raise VoicevoxExternalCatalogError()
    if not voices:
        return _blocked(
            "voice_selection_required",
            observed_digest=snapshot.observed_digest,
            extra="operator_voice_selection_missing",
        )

    selected = _operator_voices(voices)
    observed = {(item.speaker_uuid, item.style_id) for item in snapshot.styles}
    if any((v.speaker_id, int(v.style_id)) not in observed for v in selected):
        return _blocked(
            "voice_selection_changed",
            observed_digest=snapshot.observed_digest,
            extra="declared_voice_not_observed",
        )

    # A deterministic review fingerprint binds snapshot + operator metadata
    # without copying rights/attribution/IDs into public JSON or error text.
    try:
        semantic = audio_semantic_revision("voicevox", selected)
        revision = {
            "schema": _SCHEMA,
            "mode": "user-managed-unverified",
            "endpoint": verified_config.endpoint,
            "observation": snapshot.observed_digest,
            "voice_semantic_revision": semantic,
            "declared_voices": [voice.to_dict() for voice in selected],
        }
        review_digest = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(
                    revision,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest()
        )
    except Exception:  # noqa: BLE001 - redacted, fail closed
        raise VoicevoxExternalCatalogError() from None

    return VoicevoxExternalCatalogReview(
        enabled=True,
        status="review_only_blocked",
        observed_digest=snapshot.observed_digest,
        voice_semantic_revision=semantic,
        review_digest=review_digest,
        declared_voices=selected,
        blockers=_UNVERIFIED,
    )
