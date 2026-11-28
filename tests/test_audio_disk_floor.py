"""#318 G10: free-disk floor for the audio spools, with injected free space."""

from __future__ import annotations

import pytest

from tsumgi.audio import ArtifactSpool, AudioOutputChannel
from tsumgi.audio.disk_floor import (
    DiskFloor, SpoolDiskLow, default_min_free_bytes,
)
from tsumgi.audio.input_channel import AudioInputChannel
from tsumgi.control import InMemoryControlRepository
from tsumgi.control.repository import StorageUnavailable

GIB = 1024 ** 3


class Free:
    def __init__(self, value):
        self.value = value
        self.calls = []

    def __call__(self, path):
        self.calls.append(path)
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


def test_default_is_one_gib_or_twice_the_largest_artifact():
    assert default_min_free_bytes(1024) == GIB
    assert default_min_free_bytes(GIB) == 2 * GIB


def test_floor_refuses_below_and_admits_at_or_above():
    free = Free(999)
    floor = DiskFloor("/spool", 1000, free)
    with pytest.raises(SpoolDiskLow):
        floor.check()
    assert isinstance(SpoolDiskLow("x"), StorageUnavailable)
    free.value = 1000
    floor.check()
    assert floor.status() == {"free_bytes": 1000, "min_free_bytes": 1000,
                              "low": False, "refusals": 1}
    assert free.calls[0] == "/spool"


def test_zero_disables_and_unmeasurable_fails_closed():
    DiskFloor("/spool", 0, Free(0)).check()
    broken = DiskFloor("/spool", 1, Free(OSError("gone")))
    with pytest.raises(SpoolDiskLow):
        broken.check()
    assert broken.status()["low"] is True and broken.status()["free_bytes"] == -1


@pytest.mark.parametrize("args", [("", 1), ("/p", -1), ("/p", True), ("/p", 1.5)])
def test_floor_validates_arguments(args):
    with pytest.raises(ValueError):
        DiskFloor(*args)


@pytest.fixture
def spools(tmp_path):
    repo = InMemoryControlRepository()
    roots = []
    for name in ("in", "out"):
        root = tmp_path / name
        root.mkdir(mode=0o700)
        roots.append(root)
    free = Free(10 * GIB)
    in_spool = ArtifactSpool(roots[0], max_bytes=256, ttl_seconds=60,
                             max_total_bytes=1024, max_artifacts=4)
    out_spool = ArtifactSpool(roots[1], max_bytes=256, ttl_seconds=60,
                              max_total_bytes=1024, max_artifacts=4)
    inp = AudioInputChannel(repo, in_spool, max_bytes=256, max_decoded_bytes=128,
                            max_duration_seconds=1,
                            disk_floor=DiskFloor(str(roots[0]), GIB, free))
    out = AudioOutputChannel(repo, out_spool, max_bytes=256,
                             disk_floor=DiskFloor(str(roots[1]), GIB, free))
    return repo, inp, out, free


def test_input_stage_is_refused_without_writing_when_disk_is_low(spools):
    from test_audio_input_channel import WAV
    _, inp, _, free = spools
    free.value = GIB - 1
    with pytest.raises(SpoolDiskLow):
        inp.stage(WAV)
    assert not inp.spool._records and inp.disk_status()["low"] is True
    free.value = GIB
    assert inp.stage(WAV)["size_bytes"] == len(WAV)


def test_output_check_and_status(spools):
    _, _, out, free = spools
    out.check_disk()
    free.value = 0
    with pytest.raises(SpoolDiskLow):
        out.check_disk()
    assert out.disk_status()["refusals"] == 1


def test_channels_without_floor_are_unchanged(tmp_path):
    root = tmp_path / "o"
    root.mkdir(mode=0o700)
    spool = ArtifactSpool(root, max_bytes=64, ttl_seconds=5, max_total_bytes=64, max_artifacts=1)
    channel = AudioOutputChannel(InMemoryControlRepository(), spool, max_bytes=64)
    channel.check_disk()
    assert channel.disk_status() is None
    with pytest.raises(TypeError):
        AudioOutputChannel(InMemoryControlRepository(), spool, max_bytes=64, disk_floor=1)
