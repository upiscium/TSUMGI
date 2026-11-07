"""#247 R1: bounded discovery of *user-managed* local VOICEVOX, no admission.

A successful compatibility observation is NOT process ownership, license proof,
loaded-model attestation, a ManagedRuntime, or Worker serving readiness.
Only the user's already-running local engine is queried; no installation,
launch, config-write endpoint, provider registry, Control or Worker access.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlsplit
from uuid import UUID

from .voicevox_http import _decoded_json, _read_response

_DEFAULT_ENDPOINT = "http://127.0.0.1:50021"
_VERSION_LIMIT = 256
_SPEAKERS_LIMIT = 256 * 1024
_MAX_STYLES = 1024
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z")


def _invalid() -> ValueError:
    return ValueError("VOICEVOX discovery config is invalid")


class VoicevoxDiscoveryStatus(str, Enum):
    """Discovery observations, none of which grants Worker admission."""

    DISABLED = "disabled"
    ABSENT = "absent"
    INCOMPATIBLE = "incompatible"
    COMPATIBLE = "compatible_observed"


@dataclass(frozen=True, slots=True, repr=False)
class VoicevoxDiscoveryConfig:
    """Single opt-in local HTTP authority, never an arbitrary remote URL."""

    enabled: bool = False
    endpoint: str = _DEFAULT_ENDPOINT
    timeout_seconds: float = 2.0

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise _invalid()
        if not self.enabled:
            # Disabled means *no candidate inspection*, including malformed
            # private parameters, and no network access.
            return
        if type(self.endpoint) is not str:
            raise _invalid()
        try:
            parsed = urlsplit(self.endpoint)
            port = parsed.port
        except ValueError:
            raise _invalid() from None
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or port is None
            or not 1024 <= port <= 65535
            or self.endpoint != f"http://127.0.0.1:{port}"
            or type(self.timeout_seconds) not in (int, float)
            or not math.isfinite(self.timeout_seconds)
            or not 0 < self.timeout_seconds <= 5.0
        ):
            raise _invalid()


@dataclass(frozen=True, slots=True)
class VoicevoxObservedStyle:
    """Observed, non-authorized speaker UUID and global numeric style ID."""

    speaker_uuid: str
    style_id: int


@dataclass(frozen=True, slots=True)
class VoicevoxObservedCatalog:
    """Deterministic observation; digest is NOT native artifact attestation."""

    version: str
    styles: tuple[VoicevoxObservedStyle, ...]
    observed_digest: str


@dataclass(frozen=True, slots=True)
class VoicevoxDiscoveryResult:
    status: VoicevoxDiscoveryStatus
    reason: str
    snapshot: VoicevoxObservedCatalog | None = None

    @property
    def ready(self) -> bool:
        """R1 never grants audio readiness from discovery alone."""
        return False

    @property
    def can_apply(self) -> bool:
        return False


class _EndpointAbsent(Exception):
    pass


def _uuid(value: object) -> bool:
    if type(value) is not str:
        return False
    try:
        parsed = UUID(value)
    except (ValueError, TypeError, AttributeError):
        return False
    return parsed.int != 0 and str(parsed) == value


def _catalog(
    version: object, speakers: object, endpoint: str
) -> VoicevoxObservedCatalog:
    if type(version) is not str or _VERSION.fullmatch(version) is None:
        raise ValueError("invalid version")
    if type(speakers) is not list or not 1 <= len(speakers) <= _MAX_STYLES:
        raise ValueError("invalid speakers")
    ids: set[int] = set()
    uuids: set[str] = set()
    styles: list[VoicevoxObservedStyle] = []
    for speaker in speakers:
        if type(speaker) is not dict:
            raise ValueError("invalid speaker entry")
        speaker_uuid = speaker.get("speaker_uuid")
        choices = speaker.get("styles")
        if (
            not _uuid(speaker_uuid)
            or speaker_uuid in uuids
            or type(choices) is not list
            or not 1 <= len(choices) <= 256
        ):
            raise ValueError("invalid speaker identity")
        uuids.add(speaker_uuid)
        for choice in choices:
            if type(choice) is not dict:
                raise ValueError("invalid style entry")
            style_id = choice.get("id")
            if (
                type(style_id) is not int
                or not 0 <= style_id < 2**31
                or style_id in ids
                or choice.get("type", "talk") != "talk"
            ):
                # Do not mistake VOICEVOX singers or unknown styles for
                # a compatible text-to-speech numeric speaker selection.
                raise ValueError("incompatible style")
            ids.add(style_id)
            styles.append(
                VoicevoxObservedStyle(speaker_uuid=speaker_uuid, style_id=style_id)
            )
            if len(styles) > _MAX_STYLES:
                raise ValueError("too many styles")

    canonical_styles = tuple(sorted(styles, key=lambda x: (x.speaker_uuid, x.style_id)))
    payload = {
        "schema": "voicevox-external-observation-v1",
        "endpoint": endpoint,
        "version": version,
        "styles": [[s.speaker_uuid, s.style_id] for s in canonical_styles],
    }
    # This fingerprint detects *observed* changes; it does not vouch for
    # executable bytes, loaded models, process identity, or voice rights.
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()
    return VoicevoxObservedCatalog(
        version=version,
        styles=canonical_styles,
        observed_digest="sha256:" + digest,
    )


class VoicevoxDiscoveryProbe:
    """One read-only HTTP probe on the opt-in loopback candidate.

    R1 intentionally has NO scheduler, background poller, ManagedRuntime,
    Provider Registry or Client/Control integration. A later coordinator
    may repeat probe_once but must separately qualify immutable serving
    identity, user trust, rights, and Worker epoch before admission.
    """

    def __init__(self, config: VoicevoxDiscoveryConfig) -> None:
        if type(config) is not VoicevoxDiscoveryConfig:
            raise _invalid()
        self._config = config
        self._port = urlsplit(config.endpoint).port if config.enabled else None
        self._probe_lock = asyncio.Lock()

    async def _get_json(self, target: str, *, max_bytes: int) -> object:
        try:
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", self._port, limit=8192
            )
        except OSError:
            raise _EndpointAbsent() from None
        try:
            request = (
                f"GET {target} HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{self._port}\r\n"
                "Connection: close\r\n"
                "Accept: application/json\r\n"
                "Content-Length: 0\r\n\r\n"
            ).encode("ascii")
            writer.write(request)
            await writer.drain()
            raw = await _read_response(
                reader, media_type="application/json", max_bytes=max_bytes
            )
            return _decoded_json(raw)
        finally:
            # Never signal, terminate or configure the user-managed engine.
            # Only the discovery connection opened by TSUMGI is closed.
            writer.close()

    async def probe_once(self) -> VoicevoxDiscoveryResult:
        if not self._config.enabled:
            return VoicevoxDiscoveryResult(VoicevoxDiscoveryStatus.DISABLED, "disabled")
        try:
            async with asyncio.timeout(self._config.timeout_seconds):
                # A concurrent caller must not overlap two endpoint scans.
                # Waiting for the lock consumes the caller's probe budget.
                async with self._probe_lock:
                    version = await self._get_json("/version", max_bytes=_VERSION_LIMIT)
                    if type(version) is not str or _VERSION.fullmatch(version) is None:
                        raise ValueError("invalid version")
                    speakers = await self._get_json(
                        "/speakers", max_bytes=_SPEAKERS_LIMIT
                    )
                    snapshot = _catalog(version, speakers, self._config.endpoint)
        except asyncio.CancelledError:
            raise
        except _EndpointAbsent:
            return VoicevoxDiscoveryResult(
                VoicevoxDiscoveryStatus.ABSENT, "endpoint_unreachable"
            )
        except TimeoutError:
            return VoicevoxDiscoveryResult(
                VoicevoxDiscoveryStatus.INCOMPATIBLE, "probe_timeout"
            )
        except Exception:  # noqa: BLE001 - raw native HTTP and paths are private
            return VoicevoxDiscoveryResult(
                VoicevoxDiscoveryStatus.INCOMPATIBLE, "incompatible_response"
            )
        return VoicevoxDiscoveryResult(
            VoicevoxDiscoveryStatus.COMPATIBLE, "observed_compatible", snapshot
        )
