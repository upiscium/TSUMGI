"""Strict, bounded PCM16/WAV input validation for the Control-local audio MVI.

The wire byte limit is applied before this function. Decoded byte and time
ceilings are checked from header metadata before decoding sample frames.
"""

from __future__ import annotations

from io import BytesIO
import struct
import wave


def validate_pcm16_wav(
    data: bytes, *, max_wire_bytes: int, max_decoded_bytes: int,
    max_duration_seconds: int,
) -> None:
    """Reject non-PCM16, truncated and over-limit RIFF/WAVE inputs.

    Accepted: mono/stereo, 16-bit uncompressed PCM, 8–48 kHz, nonempty
    samples. No audio transcoding or model-dependent decoding is performed.
    """
    if (type(data) is not bytes or len(data) < 44
            or len(data) > max_wire_bytes
            or data[:4] != b"RIFF" or data[8:12] != b"WAVE"
            or int.from_bytes(data[4:8], "little") != len(data) - 8):
        raise ValueError("bounded PCM16 WAV required")

    try:
        with wave.open(BytesIO(data), "rb") as source:
            channels = source.getnchannels()
            rate = source.getframerate()
            frames = source.getnframes()
            sample_bytes = source.getsampwidth()
            if (source.getcomptype() != "NONE"
                    or sample_bytes != 2
                    or channels not in (1, 2)
                    or not 8000 <= rate <= 48000
                    or frames < 1
                    or frames > rate * max_duration_seconds
                    or frames * channels * sample_bytes > max_decoded_bytes):
                raise ValueError("unsupported PCM16 WAV format or size")
            if len(source.readframes(frames)) != frames * channels * sample_bytes:
                raise ValueError("truncated PCM16 WAV frames")
    except (wave.Error, EOFError, OSError, OverflowError, struct.error) as exc:
        raise ValueError("malformed PCM16 WAV") from exc
