"""Artifact-independent audio schema/catalog validation, not an audio Gateway.

Values are operator configuration snapshots, never readiness, admission, license
verification or execution authority. No transport, artifact or runtime imports.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
import hashlib
import json
import re
from types import MappingProxyType

from ..serving import (
    DeploymentIdentity,
    LogicalServingProfile,
    ResolvedServingProfile,
    ServingContract,
    ServingContractError,
    ServingJobBinding,
    resolve_profile,
)


AUDIO_CATALOG_SCHEMA = "audio-gateway-catalog-v1"
AUDIO_SEMANTICS_SCHEMA = "audio-gateway-semantics-v1"
SPEECH_OPERATION_SCHEMA = "openai-audio-speech-v1"
TRANSCRIPTION_OPERATION_SCHEMA = "openai-audio-transcription-metadata-v1"
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_KINDS = frozenset({"voicevox", "model-tts", "model-stt"})
_SPEECH_FIELDS = frozenset({"model", "input", "voice", "response_format"})
_TRANSCRIPTION_FIELDS = frozenset({"model", "language", "response_format"})


class AudioGatewayError(ValueError):
    """Structured errors containing only fixed codes/field names, never values."""

    def __init__(
        self, code: str, field_name: str, *, status_code: int = 400,
    ) -> None:
        self.code = code
        self.field_name = field_name
        self.status_code = status_code
        super().__init__(f"{code}: {field_name}")

    def to_dict(self) -> dict[str, object]:
        return {"error": {"code": self.code, "param": self.field_name,
                          "message": str(self)}}


def _invalid_config(field_name: str) -> AudioGatewayError:
    return AudioGatewayError("invalid_audio_configuration", field_name, status_code=500)


def _identifier(value: object, field_name: str, *, config: bool = False) -> str:
    if type(value) is not str or _ID.fullmatch(value) is None:
        if config:
            raise _invalid_config(field_name)
        raise AudioGatewayError("invalid_request", field_name)
    return value


def _object(value: object, required: frozenset[str], field_name: str) -> dict:
    # Decoded JSON objects only; do not coerce sequences, keys or unknown fields.
    if type(value) is not dict:
        raise AudioGatewayError("invalid_request", field_name)
    if value.keys() - required:
        raise AudioGatewayError("unknown_fields", field_name)
    if required - value.keys():
        raise AudioGatewayError("missing_fields", field_name)
    return value


@dataclass(frozen=True, slots=True)
class VoiceAlias:
    """Reviewed alias to exact backend speaker/style and public attribution.

    Speaker/style IDs are opaque stable identifiers (including decimal strings),
    not discovered names. These declarations do not grant license permission.
    """

    alias: str
    speaker_id: str
    style_id: str
    locale: str
    license_id: str
    attribution: str

    def __post_init__(self) -> None:
        for name in ("alias", "speaker_id", "style_id", "license_id"):
            _identifier(getattr(self, name), name, config=True)
        if self.locale != "ja" or type(self.locale) is not str:
            raise _invalid_config("locale")
        if (
            type(self.attribution) is not str
            or not 1 <= len(self.attribution) <= 2048
        ):
            raise _invalid_config("attribution")
        try:
            encoded = self.attribution.encode("utf-8")
        except UnicodeEncodeError:
            raise _invalid_config("attribution") from None
        if (
            len(encoded) > 2048 or not self.attribution.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in self.attribution)
        ):
            raise _invalid_config("attribution")

    def to_dict(self) -> dict[str, str]:
        return {item.name: getattr(self, item.name) for item in fields(self)}


def _voices(value: object, backend_kind: str) -> tuple[VoiceAlias, ...]:
    if type(value) not in (tuple, list):
        raise _invalid_config("voices")
    result = tuple(value)
    if any(not isinstance(voice, VoiceAlias) for voice in result):
        raise _invalid_config("voices")
    if len({voice.alias for voice in result}) != len(result):
        raise _invalid_config("voices")
    if (backend_kind == "model-stt") != (not result):
        raise _invalid_config("voices")
    return tuple(sorted(result, key=lambda voice: voice.alias))


def audio_semantic_revision(
    backend_kind: str, voices: tuple[VoiceAlias, ...] = (),
) -> str:
    """Digest to pin in ServingContract.semantic_revision before resolution.

    Deployment/model digests are already pinned by the containing contract.
    Voice, license, attribution, kind, language and format changes require review
    and a new serving contract/profile revision; ordering alone does not.
    """
    if type(backend_kind) is not str or backend_kind not in _KINDS:
        raise _invalid_config("backend_kind")
    reviewed = _voices(voices, backend_kind)
    payload = {
        "schema_version": AUDIO_SEMANTICS_SCHEMA,
        "backend_kind": backend_kind,
        "language": "ja",
        "response_format": "json" if backend_kind == "model-stt" else "wav",
        "voices": [voice.to_dict() for voice in reviewed],
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class AudioGatewayProfile:
    resolved: ResolvedServingProfile
    backend_kind: str
    voices: tuple[VoiceAlias, ...] = ()

    def __post_init__(self) -> None:
        semantic_revision = audio_semantic_revision(self.backend_kind, self.voices)
        if not isinstance(self.resolved, ResolvedServingProfile):
            raise _invalid_config("resolved")
        # Reuse existing binding validation, including effective-limit integrity.
        try:
            ResolvedServingProfile(
                self.resolved.profile_revision, self.resolved.deployment,
                self.resolved.contract, self.resolved.profile,
                self.resolved.effective_limits,
            )
        except ServingContractError:
            raise _invalid_config("resolved") from None
        stt = self.backend_kind == "model-stt"
        operation = TRANSCRIPTION_OPERATION_SCHEMA if stt else SPEECH_OPERATION_SCHEMA
        capability = "audio.transcribe" if stt else "audio.synthesize"
        if (
            self.resolved.profile.operation_schema != operation
            or self.resolved.profile.capability != capability
        ):
            raise _invalid_config("operation_schema")
        provider = self.resolved.deployment.provider_id
        if (self.backend_kind == "voicevox") != (provider == "voicevox"):
            raise _invalid_config("backend_kind")
        if self.resolved.contract.semantic_revision != semantic_revision:
            raise _invalid_config("semantic_revision")
        required_features = {"ja", "json" if stt else "wav"}
        if not required_features <= self.resolved.contract.features:
            raise _invalid_config("features")
        limits = self.resolved.effective_limits
        required_limits = (
            {"file_bytes", "metadata_bytes", "request_bytes"} if stt
            else {"input_bytes", "request_bytes"}
        )
        if limits.keys() != required_limits:
            raise _invalid_config("limits")
        content_bytes = (
            limits["file_bytes"] + limits["metadata_bytes"] if stt
            else limits["input_bytes"]
        )
        if content_bytes >= limits["request_bytes"]:
            raise _invalid_config("limits")
        object.__setattr__(self, "voices", _voices(self.voices, self.backend_kind))

    @property
    def profile_id(self) -> str:
        # The public model is the exact logical profile ID, not a runtime alias.
        return self.resolved.profile.profile_id

    @property
    def binding(self) -> ServingJobBinding:
        return ServingJobBinding.from_resolved(self.resolved)

    def to_dict(self) -> dict[str, object]:
        return {
            "deployment": self.resolved.deployment.to_dict(),
            "contract": self.resolved.contract.to_dict(),
            "profile": self.resolved.profile.to_dict(),
            "backend_kind": self.backend_kind,
            "voices": [voice.to_dict() for voice in self.voices],
        }

    @classmethod
    def from_dict(cls, value: object) -> AudioGatewayProfile:
        try:
            data = _object(value, frozenset({
                "deployment", "contract", "profile", "backend_kind", "voices",
            }), "profile")
            if any(type(data[key]) is not dict for key in (
                "deployment", "contract", "profile",
            )) or type(data["voices"]) is not list:
                raise _invalid_config("profile")
            deployment = DeploymentIdentity(**data["deployment"])
            contract = ServingContract(**data["contract"])
            profile = LogicalServingProfile(**data["profile"])
            voices = tuple(VoiceAlias(**item) for item in data["voices"])
            return cls(resolve_profile(profile, contract, deployment),
                       data["backend_kind"], voices)
        except (TypeError, ValueError, KeyError):
            # No raw unknown key, license value, digest or nested exception leaks.
            raise _invalid_config("profile") from None


@dataclass(frozen=True, slots=True)
class AudioProfileCatalog:
    profiles: tuple[AudioGatewayProfile, ...]

    def __post_init__(self) -> None:
        if type(self.profiles) not in (tuple, list) or not self.profiles:
            raise _invalid_config("profiles")
        profiles = tuple(self.profiles)
        if any(not isinstance(profile, AudioGatewayProfile) for profile in profiles):
            raise _invalid_config("profiles")
        if len({profile.profile_id for profile in profiles}) != len(profiles):
            raise _invalid_config("profiles")
        # A VOICEVOX deployment/model cannot be relabeled as distinct model-TTS.
        voicevox_models = {
            profile.resolved.deployment.model_artifact_sha256
            for profile in profiles if profile.backend_kind == "voicevox"
        }
        if any(
            profile.resolved.deployment.model_artifact_sha256 in voicevox_models
            for profile in profiles if profile.backend_kind == "model-tts"
        ):
            raise _invalid_config("backend_kind")
        object.__setattr__(self, "profiles", tuple(sorted(
            profiles, key=lambda profile: profile.profile_id,
        )))

    def get(self, model: object, *, operation_schema: str) -> AudioGatewayProfile:
        model = _identifier(model, "model")
        for profile in self.profiles:
            if profile.profile_id == model:
                if profile.resolved.profile.operation_schema != operation_schema:
                    raise AudioGatewayError("unsupported_model", "model")
                return profile
        raise AudioGatewayError("model_not_found", "model", status_code=404)

    def to_dict(self) -> dict[str, object]:
        return {"schema_version": AUDIO_CATALOG_SCHEMA,
                "profiles": [profile.to_dict() for profile in self.profiles]}

    @classmethod
    def from_dict(cls, value: object) -> AudioProfileCatalog:
        try:
            data = _object(value, frozenset({"schema_version", "profiles"}), "catalog")
            if (
                data["schema_version"] != AUDIO_CATALOG_SCHEMA
                or type(data["profiles"]) is not list
            ):
                raise _invalid_config("catalog")
            return cls(tuple(AudioGatewayProfile.from_dict(item)
                             for item in data["profiles"]))
        except (TypeError, ValueError, KeyError):
            raise _invalid_config("catalog") from None


def _size(value: object, limit: int, field_name: str) -> int:
    if type(value) is not int or value < 1:
        raise AudioGatewayError("invalid_request", field_name)
    if value > limit:
        raise AudioGatewayError("request_too_large", field_name, status_code=413)
    return value


@dataclass(frozen=True, slots=True)
class ValidatedSpeechRequest:
    profile: AudioGatewayProfile
    input: str
    voice: VoiceAlias
    input_size_bytes: int
    response_format: str = "wav"


def validate_speech_request(
    body: object, catalog: AudioProfileCatalog, *, request_size_bytes: int,
) -> ValidatedSpeechRequest:
    """Validate decoded JSON; size must be measured by trusted future ingress.

    The caller must bound raw bytes before JSON decoding. This pure validator
    neither parses JSON nor grants permission to submit/execute the result.
    """
    data = _object(body, _SPEECH_FIELDS, "request")
    profile = catalog.get(data["model"], operation_schema=SPEECH_OPERATION_SCHEMA)
    limits = profile.resolved.effective_limits
    measured_size = _size(request_size_bytes, limits["request_bytes"], "request_bytes")
    if type(data["response_format"]) is not str or data["response_format"] != "wav":
        raise AudioGatewayError("unsupported_format", "response_format")
    alias = _identifier(data["voice"], "voice")
    voice = next((voice for voice in profile.voices if voice.alias == alias), None)
    if voice is None:
        raise AudioGatewayError("unsupported_voice", "voice")
    text = data["input"]
    if type(text) is not str or not text:
        raise AudioGatewayError("invalid_request", "input")
    # Bound code points before allocating encoded bytes; then enforce UTF-8 bytes.
    _size(len(text), limits["input_bytes"], "input")
    try:
        encoded_size = len(text.encode("utf-8"))
    except UnicodeEncodeError:
        raise AudioGatewayError("invalid_utf8", "input") from None
    _size(encoded_size, limits["input_bytes"], "input")
    if not text.strip():
        raise AudioGatewayError("invalid_request", "input")
    if encoded_size > measured_size:
        raise AudioGatewayError("invalid_request", "request_bytes")
    return ValidatedSpeechRequest(profile, text, voice, encoded_size)


@dataclass(frozen=True, slots=True)
class ValidatedTranscriptionMetadata:
    """Metadata only: no file acceptance, decoding or multipart size assertion."""

    profile: AudioGatewayProfile
    language: str = "ja"
    response_format: str = "json"

    @property
    def transport_limits(self) -> Mapping[str, int]:
        return MappingProxyType(dict(self.profile.resolved.effective_limits))


def validate_transcription_metadata(
    body: object, catalog: AudioProfileCatalog, *, metadata_size_bytes: int,
) -> ValidatedTranscriptionMetadata:
    """Validate fields extracted by a future bounded multipart parser, not files.

    metadata_size_bytes must be ingress-measured bytes for all metadata parts.
    The future transport must separately enforce file/request bounds and codec,
    duration, decoding and artifact rules before any admission or execution.
    """
    data = _object(body, _TRANSCRIPTION_FIELDS, "metadata")
    profile = catalog.get(data["model"], operation_schema=TRANSCRIPTION_OPERATION_SCHEMA)
    _size(metadata_size_bytes, profile.resolved.effective_limits["metadata_bytes"],
          "metadata_bytes")
    if type(data["language"]) is not str or data["language"] != "ja":
        raise AudioGatewayError("unsupported_language", "language")
    if type(data["response_format"]) is not str or data["response_format"] != "json":
        raise AudioGatewayError("unsupported_format", "response_format")
    return ValidatedTranscriptionMetadata(profile)
