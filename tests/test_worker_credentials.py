from __future__ import annotations

import json
import os

import pytest

from tsumgi.control.worker_auth import WorkerCredentials, load_worker_credentials

TOKEN_A = "a" * 32
TOKEN_B = "b" * 32


def _private_file(tmp_path, content: bytes, mode: int = 0o600):
    path = tmp_path / "credentials.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "wb") as handle:
        handle.write(content)
    return path


def _document(workers=None):
    return json.dumps({
        "schema_version": "worker-credentials-v1",
        "workers": {"worker-a": TOKEN_A} if workers is None else workers,
    }).encode()


def test_authentication_is_per_worker_and_registry_copies_mapping():
    source = {"worker-a": TOKEN_A, "worker-b": TOKEN_B}
    credentials = WorkerCredentials(source)
    source["worker-a"] = TOKEN_B
    assert credentials.authenticate(TOKEN_A) == "worker-a"
    assert credentials.authenticate(TOKEN_B) == "worker-b"
    assert credentials.authenticate("x" * 32) is None
    assert credentials.authenticate(None) is None
    assert repr(credentials) == "WorkerCredentials(count=2)"
    assert TOKEN_A not in repr(credentials)
    with pytest.raises(AttributeError):
        credentials._token_workers = {}
    with pytest.raises(AttributeError):
        del credentials._token_workers


@pytest.mark.parametrize("mapping", [
    {}, {str(i): "a" * 32 for i in range(1025)},
    {"worker-a": "short"}, {"worker-a": "a" * 32, "worker-b": "a" * 32},
    {"bad/id": "a" * 32}, {"worker-a": True}, {1: "a" * 32},
    {" worker-a": "a" * 32}, {"ümlaut": "a" * 32},
    {"worker-a": "a" * 32 + "\n"},
    {"worker-a": "a" * 513}, {"worker-a": "é" * 32},
])
def test_invalid_registries_fail_closed(mapping):
    with pytest.raises(ValueError, match="^invalid worker credentials configuration$"):
        WorkerCredentials(mapping)


def test_role_token_collision_rejected_without_disclosure():
    credentials = WorkerCredentials({"worker-a": TOKEN_A})
    with pytest.raises(ValueError, match="^invalid worker credentials configuration$") as error:
        credentials.reject_token_collisions([None, "", TOKEN_A])
    assert TOKEN_A not in str(error.value)
    credentials.reject_token_collisions([None, "", TOKEN_B])


@pytest.mark.parametrize("mode", [0o400, 0o600])
def test_load_private_file(tmp_path, mode):
    path = _private_file(tmp_path, _document(), mode)
    credentials = load_worker_credentials(str(path))
    assert credentials.authenticate(TOKEN_A) == "worker-a"


@pytest.mark.parametrize("mode", [0o640, 0o700, 0o644])
def test_reject_non_private_modes(tmp_path, mode):
    path = _private_file(tmp_path, _document(), mode)
    with pytest.raises(ValueError, match="^invalid worker credentials configuration$"):
        load_worker_credentials(str(path))


@pytest.mark.parametrize("content", [
    b"\xff",
    b'{"schema_version":"worker-credentials-v1","workers":{},"workers":{}}',
    b'{"schema_version":"worker-credentials-v1","schema_version":"worker-credentials-v1","workers":{}}',
    b'{"schema_version":"worker-credentials-v2","workers":{"worker-a":"' + TOKEN_A.encode() + b'"}}',
    b'{"schema_version":"worker-credentials-v1","workers":{"worker-a":"' + TOKEN_A.encode() + b'"},"extra":0}',
    b'{"schema_version":"worker-credentials-v1","workers":{}}',
    b"x" * (1024 * 1024 + 1),
])
def test_invalid_file_content_fails_closed_without_leaking(tmp_path, content):
    path = _private_file(tmp_path, content)
    with pytest.raises(ValueError) as error:
        load_worker_credentials(str(path))
    assert str(path) not in str(error.value)
    assert TOKEN_A not in str(error.value)
    assert error.value.__cause__ is None


def test_reject_symlink_nonregular_and_bad_paths(tmp_path):
    target = _private_file(tmp_path, _document())
    link = tmp_path / "link.json"
    os.symlink(target, link)
    with pytest.raises(ValueError):
        load_worker_credentials(str(link))
    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(ValueError):
        load_worker_credentials(str(directory))
    for bad in ("relative.json", str(tmp_path / ".." / "credentials.json"),
                str(tmp_path / "missing.json")):
        with pytest.raises(ValueError):
            load_worker_credentials(bad)


def test_loader_rejects_duplicate_worker_ids(tmp_path):
    content = (
        b'{"schema_version":"worker-credentials-v1","workers":{"worker-a":"'
        + TOKEN_A.encode() + b'","worker-a":"' + TOKEN_B.encode() + b'"}}'
    )
    path = _private_file(tmp_path, content)
    with pytest.raises(ValueError):
        load_worker_credentials(str(path))


def test_file_must_have_single_link(tmp_path):
    path = _private_file(tmp_path, _document())
    os.link(path, tmp_path / "second-link")
    with pytest.raises(ValueError):
        load_worker_credentials(str(path))


def test_private_file_owned_by_another_user_is_rejected(tmp_path, monkeypatch):
    path = _private_file(tmp_path, _document())
    owner = path.stat().st_uid
    if owner == 0:
        pytest.skip("root-owned credentials are explicitly supported")
    monkeypatch.setattr(os, "geteuid", lambda: owner + 1)
    with pytest.raises(ValueError, match="^invalid worker credentials configuration$"):
        load_worker_credentials(str(path))


def test_padded_token_length_counts_padding():
    token = "a" * 31 + "="
    assert WorkerCredentials({"worker-a": token}).authenticate(token) == "worker-a"
