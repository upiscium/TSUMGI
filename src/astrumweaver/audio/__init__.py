"""Standalone audio artifact storage, independent of Core and Gateway."""

from .artifact_spool import ArtifactSpool
from .output_channel import AudioOutputChannel

__all__ = ["ArtifactSpool", "AudioOutputChannel"]
