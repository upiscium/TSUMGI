"""Synthetic child framing helpers only; no model/runtime is executed."""
import io
import struct
import pytest
from tsumgi.runtime.providers.parakeet_child import _read_exact, _recv, _send

def test_bounded_frame_roundtrip():
    stream=io.BytesIO()
    _send(stream,{"kind":"fixture"})
    stream.seek(0)
    assert _recv(stream)=={"kind":"fixture"}

def test_truncated_frame_rejected():
    with pytest.raises(ValueError): _read_exact(io.BytesIO(b"x"),2)
