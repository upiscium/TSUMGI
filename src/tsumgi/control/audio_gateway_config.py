"""Explicit Control-only audio configuration; no discovery or runtime setup."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import re
import stat

from ..audio.artifact_spool import ArtifactSpool
from ..audio.disk_floor import DiskFloor, default_min_free_bytes
from ..audio.input_channel import AudioInputChannel
from ..audio.output_channel import AudioOutputChannel
from ..gateway.audio import (
    AudioProfileCatalog, SPEECH_OPERATION_SCHEMA, TRANSCRIPTION_OPERATION_SCHEMA,
)
from ..gateway.audio_ingress import AudioIngressLimits
from ..gateway.audio_speech_api import AudioSpeechLimits, create_audio_speech_router
from ..gateway.audio_transcriptions_api import (
    AudioTranscriptionsLimits, create_audio_transcriptions_router,
)
from ..gateway.audio_voice_api import create_audio_voice_router
from .auth import ClientAuthMode


_INVALID = "audio_gateway configuration is invalid"
_DEFAULT_TEXT_RETENTION_SECONDS = 3600
_TOKEN = re.compile(r"[A-Za-z0-9._~+/-]+=*\Z")
_CATALOG_BYTES = 1024 * 1024


def _table(value: object, keys: set[str], optional: frozenset[str] = frozenset()) -> dict:
    """Exact key set: every required key, plus only the listed optional keys."""
    if (type(value) is not dict or not keys <= value.keys()
            or value.keys() - keys - optional):
        raise ValueError(_INVALID)
    return value


_SPOOL_KEYS = {"root", "ttl_seconds", "max_total_bytes", "max_artifacts"}
_SPOOL_OPTIONAL = frozenset({"min_free_bytes", "read_grace_seconds"})
_DEFAULT_READ_GRACE_SECONDS = 60.0


def _min_free(data: dict) -> int | None:
    value = data.get("min_free_bytes")
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise ValueError(_INVALID)
    return value


def _read_grace(data: dict) -> float:
    value = data.get("read_grace_seconds", _DEFAULT_READ_GRACE_SECONDS)
    if (type(value) not in (int, float) or not math.isfinite(value)
            or not 0 <= value <= 3600):
        raise ValueError(_INVALID)
    return float(value)


def _positive_int(value: object, minimum: int = 1) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(_INVALID)
    return value


def _json_pairs(items: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError(_INVALID)
        result[key] = value
    return result


def _no_constant(_: str) -> object:
    raise ValueError(_INVALID)


def _load_catalog(path: object) -> AudioProfileCatalog:
    if type(path) is not str or not path.strip():
        raise ValueError(_INVALID)
    with open(path, "rb") as handle:
        raw = handle.read(_CATALOG_BYTES + 1)
    if len(raw) > _CATALOG_BYTES:
        raise ValueError(_INVALID)
    return AudioProfileCatalog.from_dict(json.loads(
        raw.decode("utf-8"), object_pairs_hook=_json_pairs,
        parse_constant=_no_constant,
    ))


def _check_root(value: object) -> Path:
    """Read-only preflight; ArtifactSpool repeats checks through secure dirfds.

    Preflight both roots before either constructor allocates a private directory.
    Same-effective-UID actors are trusted, as in ArtifactSpool itself.
    """
    if (type(value) is not str or not value or "\0" in value
            or not os.path.isabs(value) or ".." in value.split(os.sep)):
        raise ValueError(_INVALID)
    root = Path(value)
    for path in (*reversed(root.parents), root):
        info = path.lstat()
        if (not stat.S_ISDIR(info.st_mode)
                or info.st_uid not in (0, os.geteuid())):
            raise ValueError(_INVALID)
        if stat.S_IMODE(info.st_mode) & 0o022:
            if not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX
                    and str(path) in ("/tmp", "/var/tmp", "/dev/shm")):
                raise ValueError(_INVALID)
    info = root.lstat()
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError(_INVALID)
    return root


@dataclass(frozen=True)
class _SpoolConfig:
    root: Path
    ttl_seconds: float
    max_total_bytes: int
    max_artifacts: int
    # None means the documented default, max(1 GiB, 2 x max artifact); 0 disables.
    min_free_bytes: int | None = None
    # Output spool only: delete this long after the first full read. Default
    # 60 s; 0 disables early release (TTL only).
    read_grace_seconds: float = _DEFAULT_READ_GRACE_SECONDS

    @classmethod
    def parse(
        cls, value: object, *, max_bytes: int, deadline: float, output: bool = False,
    ) -> _SpoolConfig:
        data = _table(value, _SPOOL_KEYS, _SPOOL_OPTIONAL)
        if not output and "read_grace_seconds" in data:
            raise ValueError(_INVALID)
        ttl = data["ttl_seconds"]
        if (type(ttl) not in (int, float) or not math.isfinite(ttl)
                or ttl <= deadline):
            raise ValueError(_INVALID)
        return cls(
            _check_root(data["root"]), ttl,
            _positive_int(data["max_total_bytes"], max_bytes),
            _positive_int(data["max_artifacts"]),
            _min_free(data),
            _read_grace(data),
        )

    def floor(self, max_bytes: int) -> DiskFloor:
        minimum = (default_min_free_bytes(max_bytes)
                   if self.min_free_bytes is None else self.min_free_bytes)
        return DiskFloor(str(self.root), minimum)

    def create(self, max_bytes: int) -> ArtifactSpool:
        return ArtifactSpool(
            self.root, max_bytes=max_bytes, ttl_seconds=self.ttl_seconds,
            max_total_bytes=self.max_total_bytes, max_artifacts=self.max_artifacts,
            recover_on_startup=True, exclusive_root=True,
        )


def _discard_new_empty_spools(*spools: ArtifactSpool | None) -> None:
    """Rollback startup allocations only, with inode-checked empty rmdir.

    Never scan an old spool or delete artifacts. All candidate directories were
    created by this particular build, before any Control app was served.
    """
    failed = False
    for spool in spools:
        if spool is None:
            continue
        try:
            if spool._records:
                raise ValueError("nonempty startup spool")
            root_fd = spool._open_root()
            try:
                root_info = os.fstat(root_fd)
                info = os.stat(spool._name, dir_fd=root_fd, follow_symlinks=False)
                if (spool._identity(root_info) != spool._root_identity
                        or not stat.S_ISDIR(info.st_mode)
                        or info.st_uid != os.geteuid()
                        or stat.S_IMODE(info.st_mode) != 0o700
                        or spool._identity(info) != spool._directory_identity):
                    raise ValueError("startup spool identity mismatch")
                # Fail closed on nonempty/replaced occupants; never recursively delete.
                os.rmdir(spool._name, dir_fd=root_fd)
            finally:
                os.close(root_fd)
        except (OSError, ValueError, AttributeError):
            failed = True
        finally:
            if spool._release_owner is not None:
                spool._release_owner()
    if failed:
        raise RuntimeError("audio_gateway startup cleanup requires inspection") from None


@dataclass(frozen=True)
class AudioGatewaySetup:
    """Unpublished channels plus routers; rollback only before app publication."""

    input_channel: AudioInputChannel
    output_channel: AudioOutputChannel
    routers: tuple

    def rollback(self) -> None:
        _discard_new_empty_spools(self.output_channel._spool, self.input_channel.spool)


@dataclass(frozen=True)
class AudioGatewayConfig:
    catalog: AudioProfileCatalog
    token: str = field(repr=False)
    ingress: AudioIngressLimits
    speech: AudioSpeechLimits
    transcriptions: AudioTranscriptionsLimits
    input_spool: _SpoolConfig
    output_spool: _SpoolConfig
    max_decoded_bytes: int
    max_duration_seconds: int
    # Lease for audio.* Jobs only (None: the generic lease applies). Always below
    # both request deadlines so an unconfirmed Worker failure ends the Job
    # (502 backend_failure) before the Gateway gives up (504).
    claim_lease_seconds: int | None = None
    # Seconds after a Job is terminal before its TTS input / STT transcript in
    # the Job row is replaced by a placeholder; None = retained (0 in config).
    text_retention_seconds: int | None = _DEFAULT_TEXT_RETENTION_SECONDS

    def build(self, repository) -> AudioGatewaySetup:
        """Build shared Control/Worker transport and public routers atomically."""
        input_spool = output_spool = None
        try:
            input_spool = self.input_spool.create(self.ingress.file_bytes)
            input_channel = AudioInputChannel(
                repository, input_spool,
                max_bytes=self.ingress.file_bytes,
                max_decoded_bytes=self.max_decoded_bytes,
                max_duration_seconds=self.max_duration_seconds,
                disk_floor=self.input_spool.floor(self.ingress.file_bytes),
            )
            output_spool = self.output_spool.create(self.speech.output_bytes)
            output_channel = AudioOutputChannel(
                repository, output_spool, max_bytes=self.speech.output_bytes,
                disk_floor=self.output_spool.floor(self.speech.output_bytes),
                read_grace_seconds=self.output_spool.read_grace_seconds or None,
            )
            auth = {"client_auth": ClientAuthMode.BEARER, "client_token": self.token}
            routers = (
                create_audio_voice_router(self.catalog, **auth),
                create_audio_speech_router(
                    self.catalog, repository, output_channel, **auth,
                    ingress_limits=self.ingress, limits=self.speech,
                ),
                create_audio_transcriptions_router(
                    self.catalog, repository, input_channel, **auth,
                    ingress_limits=self.ingress, limits=self.transcriptions,
                ),
            )
            return AudioGatewaySetup(input_channel, output_channel, routers)
        except Exception:
            _discard_new_empty_spools(output_spool, input_spool)
            raise RuntimeError(_INVALID) from None


def load_audio_gateway_config(value: object) -> AudioGatewayConfig | None:
    """Validate exact TOML keys and reviewed JSON; disabled performs no audio I/O."""
    try:
        if type(value) is not dict:
            raise ValueError(_INVALID)
        enabled = value.get("enabled", False)
        if type(enabled) is not bool:
            raise ValueError(_INVALID)
        allowed = {"enabled", "catalog", "ingress", "speech", "transcriptions",
                   "input_spool", "output_spool", "pcm"}
        optional = frozenset({"claim_lease_seconds", "text_retention_seconds"})
        if value.keys() - allowed - optional:
            raise ValueError(_INVALID)
        if not enabled:
            # Disabled tables may retain a complete valid policy, but incomplete
            # or mistyped policy must not silently become an operator surprise.
            if value.keys() - {"enabled"}:
                _validate_policy_shapes(value)
            return None
        _table(value, allowed, optional)
        token = os.environ.get("TSUMGI_AUDIO_CLIENT_TOKEN", "")
        if (_TOKEN.fullmatch(token) is None or token.lower() == "anon"
                or token in (os.environ.get("TSUMGI_CLIENT_TOKEN", ""),
                             os.environ.get("TSUMGI_WORKER_TOKEN", ""))):
            raise ValueError(_INVALID)
        ingress, speech, transcriptions, pcm = _validate_policy_shapes(value)
        catalog = _load_catalog(value["catalog"])
        schemas = {p.resolved.profile.operation_schema for p in catalog.profiles}
        if not {SPEECH_OPERATION_SCHEMA, TRANSCRIPTION_OPERATION_SCHEMA} <= schemas:
            raise ValueError(_INVALID)
        input_spool = _SpoolConfig.parse(
            value["input_spool"], max_bytes=ingress.file_bytes,
            deadline=transcriptions.request_timeout_seconds,
        )
        output_spool = _SpoolConfig.parse(
            value["output_spool"], max_bytes=speech.output_bytes,
            deadline=speech.request_timeout_seconds, output=True,
        )
        left, right = input_spool.root, output_spool.root
        if (left == right or left in right.parents or right in left.parents
                or os.path.samestat(left.stat(), right.stat())):
            raise ValueError(_INVALID)
        return AudioGatewayConfig(
            catalog, token, ingress, speech, transcriptions, input_spool, output_spool,
            pcm["max_decoded_bytes"], pcm["max_duration_seconds"],
            _claim_lease(value, speech, transcriptions),
            _text_retention(value),
        )
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        # Never chain parser, filesystem or schema exceptions with operator data.
        raise RuntimeError(_INVALID) from None


_DEFAULT_CLAIM_LEASE_SECONDS = 30
_MAX_TEXT_RETENTION_SECONDS = 30 * 24 * 3600


def _text_retention(value: dict) -> int | None:
    seconds = value.get("text_retention_seconds", _DEFAULT_TEXT_RETENTION_SECONDS)
    if type(seconds) is not int or not 0 <= seconds <= _MAX_TEXT_RETENTION_SECONDS:
        raise ValueError(_INVALID)
    return seconds or None  # 0 disables the purge


def _claim_lease(value: dict, speech, transcriptions) -> int | None:
    deadline = min(speech.request_timeout_seconds, transcriptions.request_timeout_seconds)
    if "claim_lease_seconds" not in value:
        # Documented default: min(30, deadline / 2), when that is a whole second.
        derived = min(_DEFAULT_CLAIM_LEASE_SECONDS, int(deadline // 2))
        return derived if derived >= 1 else None
    lease = value["claim_lease_seconds"]
    if type(lease) is not int or lease < 1 or lease >= deadline:
        raise ValueError(_INVALID)
    return lease


def _validate_policy_shapes(value: dict) -> tuple:
    ingress = AudioIngressLimits(**_table(value.get("ingress"), {
        "request_bytes", "input_bytes", "file_bytes", "metadata_bytes",
        "part_header_bytes", "total_header_bytes", "receive_events",
        "idle_timeout_seconds", "total_timeout_seconds",
    }))
    speech = AudioSpeechLimits(**_table(value.get("speech"), {
        "output_bytes", "request_timeout_seconds", "max_pending_admissions",
        "poll_interval_seconds",
    }, frozenset({"retry_after_seconds"})))
    transcriptions = AudioTranscriptionsLimits(**_table(value.get("transcriptions"), {
        "text_bytes", "request_timeout_seconds", "max_pending_admissions",
        "poll_interval_seconds",
    }, frozenset({
        "retry_after_seconds", "credit", "accepted_sample_rates", "accepted_channels",
    })))
    _claim_lease(value, speech, transcriptions)
    _text_retention(value)
    pcm = _table(value.get("pcm"), {"max_decoded_bytes", "max_duration_seconds"})
    _positive_int(pcm["max_decoded_bytes"], 2)
    _positive_int(pcm["max_duration_seconds"])
    if (ingress.file_bytes < 46 or ingress.input_bytes >= ingress.request_bytes
            or ingress.file_bytes + ingress.metadata_bytes >= ingress.request_bytes
            or pcm["max_decoded_bytes"] > ingress.file_bytes - 44):
        raise ValueError(_INVALID)
    # Shape-check retained disabled policy without touching paths or the catalog.
    if type(value.get("catalog")) is not str or not value["catalog"].strip():
        raise ValueError(_INVALID)
    for name in ("input_spool", "output_spool"):
        data = _table(value.get(name), _SPOOL_KEYS, _SPOOL_OPTIONAL)
        _min_free(data)
        _read_grace(data)
        if name == "input_spool" and "read_grace_seconds" in data:
            raise ValueError(_INVALID)  # inputs are released on terminal, not on read
        if type(data["root"]) is not str or not data["root"].strip():
            raise ValueError(_INVALID)
        ttl = data["ttl_seconds"]
        deadline = (transcriptions.request_timeout_seconds if name == "input_spool"
                    else speech.request_timeout_seconds)
        if type(ttl) not in (int, float) or not math.isfinite(ttl) or ttl <= deadline:
            raise ValueError(_INVALID)
        cap = ingress.file_bytes if name == "input_spool" else speech.output_bytes
        _positive_int(data["max_total_bytes"], cap)
        _positive_int(data["max_artifacts"])
    return ingress, speech, transcriptions, pcm
