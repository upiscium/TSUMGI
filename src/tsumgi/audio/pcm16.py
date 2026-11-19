"""Bounded PCM16/WAV input validation for the single-Control audio MVI.

The HTTP wire limit is checked upstream. This validator checks the complete
RIFF chunk structure, exact PCM format fields and decoded-frame limits before
asking the standard WAV reader to consume any sample frames.
"""

from __future__ import annotations

from io import BytesIO
import struct
import wave


_PCM_SUBFORMAT_GUID = bytes.fromhex("0100000000001000800000aa00389b71")


def _pcm16_layout(data: bytes) -> tuple[int, int, int]:
    """Return (channels, sample_rate, data_bytes) for a complete RIFF/WAVE."""
    position = 12
    fmt = None
    data_bytes = None
    while position < len(data):
        if len(data) - position < 8:
            raise ValueError("malformed PCM16 WAV chunk")
        chunk_id = data[position:position + 4]
        chunk_size = int.from_bytes(data[position + 4:position + 8], "little")
        first = position + 8
        end = first + chunk_size
        padded_end = end + (chunk_size & 1)
        if padded_end > len(data):
            raise ValueError("malformed PCM16 WAV chunk")
        if chunk_id == b"fmt ":
            if fmt is not None or data_bytes is not None or chunk_size < 16:
                raise ValueError("malformed PCM16 WAV format")
            fields = data[first:end]
            tag = int.from_bytes(fields[0:2], "little")
            channels = int.from_bytes(fields[2:4], "little")
            rate = int.from_bytes(fields[4:8], "little")
            byte_rate = int.from_bytes(fields[8:12], "little")
            block_align = int.from_bytes(fields[12:14], "little")
            bits = int.from_bytes(fields[14:16], "little")
            if tag == 0xFFFE:  # Valid 16-bit extensible PCM is also allowed.
                if (len(fields) < 40
                        or int.from_bytes(fields[16:18], "little") < 22
                        or len(fields) < 18 + int.from_bytes(fields[16:18], "little")
                        or int.from_bytes(fields[18:20], "little") != 16
                        or fields[24:40] != _PCM_SUBFORMAT_GUID):
                    raise ValueError("unsupported PCM16 WAV format")
            elif tag != 1:
                raise ValueError("unsupported PCM16 WAV format")
            if (bits != 16 or channels not in (1, 2)
                    or not 8000 <= rate <= 48000
                    or block_align != channels * 2
                    or byte_rate != rate * block_align):
                raise ValueError("unsupported PCM16 WAV format")
            fmt = (channels, rate, block_align)
        elif chunk_id == b"data":
            if fmt is None or data_bytes is not None:
                raise ValueError("malformed PCM16 WAV data")
            data_bytes = chunk_size
        position = padded_end

    if fmt is None or data_bytes is None:
        raise ValueError("missing PCM16 WAV chunks")
    channels, rate, frame_bytes = fmt
    if data_bytes == 0 or data_bytes % frame_bytes:
        raise ValueError("truncated PCM16 WAV frames")
    return channels, rate, data_bytes


def validate_pcm16_wav(
    data: bytes, *, max_wire_bytes: int, max_decoded_bytes: int,
    max_duration_seconds: int,
) -> None:
    """Accept complete mono/stereo, 16-bit PCM WAV at 8–48 kHz only."""
    if (type(data) is not bytes or not 44 <= len(data) <= max_wire_bytes
            or data[:4] != b"RIFF" or data[8:12] != b"WAVE"
            or int.from_bytes(data[4:8], "little") != len(data) - 8):
        raise ValueError("bounded PCM16 WAV required")

    channels, rate, decoded_bytes = _pcm16_layout(data)
    frames = decoded_bytes // (channels * 2)
    if (decoded_bytes > max_decoded_bytes
            or frames > rate * max_duration_seconds):
        raise ValueError("unsupported PCM16 WAV format or size")
    try:
        with wave.open(BytesIO(data), "rb") as source:
            if (source.getcomptype() != "NONE"
                    or source.getsampwidth() != 2
                    or source.getnchannels() != channels
                    or source.getframerate() != rate
                    or source.getnframes() != frames):
                raise ValueError("unsupported PCM16 WAV format")
            if len(source.readframes(frames)) != decoded_bytes:
                raise ValueError("truncated PCM16 WAV frames")
    except (wave.Error, EOFError, OSError, OverflowError,
            struct.error, RuntimeError) as exc:
        raise ValueError("malformed PCM16 WAV") from exc
