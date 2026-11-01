"""Source-only generic-systemd staging; never start Control or use the live root."""

from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
EXECUTABLE = "/reviewed/bin/astrumweaver-control"
# Public synthetic sentinels, not usable deployment credentials.
TOKENS = (
    "synthetic-b2b-control-bearer",
    "synthetic-b2b-audio-bearer",
    "synthetic-b2b-worker-bearer",
)
CHANGED_TOKEN = "synthetic-b2b-audio-bearer-changed"
ENVIRONMENT = (
    "ASTRUMWEAVER_DATABASE_URL=postgresql://unused.invalid/synthetic_b2b\n"
    f"ASTRUMWEAVER_CLIENT_TOKEN={TOKENS[0]}\n"
    f"ASTRUMWEAVER_AUDIO_CLIENT_TOKEN={TOKENS[1]}\n"
    f"ASTRUMWEAVER_WORKER_TOKEN={TOKENS[2]}\n"
).encode("utf-8")


def _config(mode: str) -> bytes:
    control = '[control]\nhost = "127.0.0.1"\nport = 9000\n'
    if mode == "absent":
        # Existing anonymous, non-audio installs need no environment file.
        return (control + 'client_auth = "none"\n').encode("utf-8")
    # A schema-shaped bounded policy; the helper copies, but never loads, the
    # deliberately absent synthetic catalog or the operator-provisioned roots.
    return (control + 'client_auth = "bearer"\n' + f"""
[audio_gateway]
enabled = {str(mode == "enabled").lower()}
catalog = "/etc/astrumweaver/synthetic-b2b-audio-profiles.json"

[audio_gateway.ingress]
request_bytes = 2048
input_bytes = 128
file_bytes = 1024
metadata_bytes = 256
part_header_bytes = 256
total_header_bytes = 2048
receive_events = 16
idle_timeout_seconds = 0.5
total_timeout_seconds = 1.0

[audio_gateway.speech]
output_bytes = 512
request_timeout_seconds = 2.0
max_pending_admissions = 2
poll_interval_seconds = 0.005

[audio_gateway.transcriptions]
text_bytes = 128
request_timeout_seconds = 1.5
max_pending_admissions = 1
poll_interval_seconds = 0.01

[audio_gateway.pcm]
max_decoded_bytes = 128
max_duration_seconds = 1

[audio_gateway.input_spool]
root = "/var/lib/astrumweaver-control/audio/input"
ttl_seconds = 60.0
max_total_bytes = 4096
max_artifacts = 4

[audio_gateway.output_spool]
root = "/var/lib/astrumweaver-control/audio/output"
ttl_seconds = 30.0
max_total_bytes = 2048
max_artifacts = 3
""").encode("utf-8")


def _run_setup(
    tmp_path: Path, config: Path, environment: Path | None,
) -> subprocess.CompletedProcess[str]:
    installer_tmp = tmp_path / "installer-tmp"
    installer_tmp.mkdir(exist_ok=True)
    command = [
        "bash",
        str(ROOT / "setup/setup-control-plane.sh"),
        "--config", str(config),
    ]
    if environment is not None:
        command.extend(["--environment-file", str(environment)])
    command.extend([
        "--executable", EXECUTABLE,
        "--root", str(tmp_path / "staged"),
    ])
    result = subprocess.run(
        command,
        cwd=ROOT,
        # Do not inherit host credentials, shell startup hooks or tracing flags.
        env={
            "PATH": os.environ.get("PATH", os.defpath),
            "LC_ALL": "C",
            "TMPDIR": str(installer_tmp),
        },
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    for token in (*TOKENS, CHANGED_TOKEN):
        assert token not in result.stdout
        assert token not in result.stderr
    return result


def _snapshot(staged: Path) -> dict[str, tuple[bytes, int]]:
    return {
        str(path.relative_to(staged)): (
            path.read_bytes(), stat.S_IMODE(path.stat().st_mode),
        )
        for path in staged.rglob("*") if path.is_file()
    }


def _assert_staged_contract(
    staged: Path, config: bytes, environment: bytes | None,
) -> None:
    template = (ROOT / "systemd/astrumweaver-control.service.in").read_bytes()
    unit = template.replace(b"@USER@", b"astrumweaver-control").replace(
        b"@EXECUTABLE@", EXECUTABLE.encode("utf-8"),
    )
    expected = {
        "etc/astrumweaver/control.toml": (config, 0o640),
        "etc/systemd/system/astrumweaver-control.service": (unit, 0o644),
    }
    if environment is not None:
        expected["etc/astrumweaver/control.env"] = (environment, 0o640)
    assert _snapshot(staged) == expected
    # Pin existing role identity, state and optional env semantics in addition
    # to verifying the whole unchanged rendered template (including sandbox).
    lines = unit.decode("utf-8").splitlines()
    for directive in (
        "User=astrumweaver-control",
        "Group=astrumweaver-control",
        "SupplementaryGroups=astrumweaver-config",
        "StateDirectory=astrumweaver-control",
        "StateDirectoryMode=0750",
        "EnvironmentFile=-/etc/astrumweaver/control.env",
        f"ExecStart={EXECUTABLE} --config /etc/astrumweaver/control.toml",
    ):
        key = directive.split("=", 1)[0] + "="
        assert [line for line in lines if line.startswith(key)] == [directive]
    state = staged / "var/lib/astrumweaver-control"
    assert state.is_dir()
    assert stat.S_IMODE(state.stat().st_mode) == 0o750
    assert list(state.iterdir()) == []  # No audio parent, roots or spool allocation.
    assert not (staged / "var/lib/astrumweaver").exists()
    assert not (staged / "etc/astrumweaver/synthetic-b2b-audio-profiles.json").exists()


@pytest.mark.parametrize(
    ("mode", "with_environment"),
    [("enabled", True), ("absent", False), ("disabled", False)],
)
def test_audio_policy_stages_exactly_and_idempotently(
    tmp_path: Path, mode: str, with_environment: bool,
) -> None:
    config = tmp_path / "control.toml"
    payload = _config(mode)
    config.write_bytes(payload)
    environment = tmp_path / "control.env" if with_environment else None
    if environment is not None:
        environment.write_bytes(ENVIRONMENT)

    for _ in range(2):
        result = _run_setup(tmp_path, config, environment)
        assert result.returncode == 0, result.stderr
        _assert_staged_contract(
            tmp_path / "staged", payload,
            ENVIRONMENT if with_environment else None,
        )
    assert config.read_bytes() == payload
    if environment is not None:
        assert environment.read_bytes() == ENVIRONMENT


@pytest.mark.parametrize("changed_source", ["config", "environment"])
def test_audio_setup_refuses_changed_sources_without_overwriting(
    tmp_path: Path, changed_source: str,
) -> None:
    config = tmp_path / "control.toml"
    payload = _config("enabled")
    config.write_bytes(payload)
    environment = tmp_path / "control.env"
    environment.write_bytes(ENVIRONMENT)
    first = _run_setup(tmp_path, config, environment)
    assert first.returncode == 0, first.stderr
    _assert_staged_contract(tmp_path / "staged", payload, ENVIRONMENT)

    if changed_source == "config":
        config.write_bytes(payload + b"\n# synthetic reviewed policy changed\n")
        destination = "control.toml"
    else:
        environment.write_bytes(ENVIRONMENT.replace(
            TOKENS[1].encode("utf-8"), CHANGED_TOKEN.encode("utf-8"),
        ))
        destination = "control.env"
    refused = _run_setup(tmp_path, config, environment)
    assert refused.returncode != 0
    assert "destination differs; refusing overwrite:" in refused.stderr
    assert str(tmp_path / "staged/etc/astrumweaver" / destination) in refused.stderr
    _assert_staged_contract(tmp_path / "staged", payload, ENVIRONMENT)
