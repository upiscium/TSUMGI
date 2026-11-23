"""Explicit offline VOICEVOX review-file staging, never service activation.

Uses SetupPlan approval/rollback, but has distinct action kinds and goal. The
old informational AudioSetupReview and the LLM activation planner are untouched.
Plans and rollback data contain operator-local paths/configuration: unlike the
old review summary, they are private artifacts, not public-safe reports.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from ..contracts import ResourceShape, WorkerSpec
from ..runtime.deployment import RuntimeDeploymentSpec
from ..runtime.providers.voicevox_manifest import VoicevoxProviderConfig, _DEMAND
from ..runtime.providers.voicevox_release import verify_bundle
from ..serving import ServingDeploymentDeclaration
from .contracts import (
    ActionInspection, ActionReceipt, DeploymentPath, SetupAction, SetupActionKind,
    SetupActionState, SetupHostSnapshot, SetupPlan, SetupPlanGoal,
)
from .filesystem import SetupFilesystem

_MARKER = "voicevox-offline-staging-v1"
_DESTINATION = "/etc/tsumgi/audio/voicevox"
_FILES = ("runtime-deployment.json", "serving-deployment.json", "worker.toml")
_MAX_CONTENT = 128 * 1024
_PATH = re.compile(r"/[A-Za-z0-9_./+-]+\Z")
_NAME = re.compile(r"[a-z_][a-z0-9_-]{0,31}\Z")
_CONTROL = re.compile(r"http://127\.0\.0\.1:([1-9][0-9]{0,4})\Z")


class VoicevoxStagingError(ValueError):
    def __init__(self) -> None:
        super().__init__("VOICEVOX offline staging rejected")


class VoicevoxStagingRollbackError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("VOICEVOX staging recovery is incomplete")


def _reject():
    raise VoicevoxStagingError()


def _json(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False) + "\n"


def _sha(content):
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def _path(value, *, prefix="/"):
    if (type(value) is not str or not _PATH.fullmatch(value)
            or value == "/" or len(value) > 4096 or not value.startswith(prefix)
            or any(part in ("", ".", "..") for part in value.split("/")[1:])):
        _reject()
    return value


def _account(value, *, user=False):
    if (type(value) is not str or not _NAME.fullmatch(value) or value == "root"
            or (user and value == "tsumgi")):
        _reject()
    return value


def _worker_toml(worker, control_url):
    values = {
        "id": worker.worker_id, "class": worker.worker_class,
        "control_url": control_url, "capabilities": ["audio.synthesize"],
        "gpu_uuids": [], "gpu_count": 0, "total_vram_mb": 0, "max_single_gpu_vram_mb": 0,
        "max_concurrency": 1, "gpu_preflight": False, "health_host": "127.0.0.1",
        "health_port": 9100,
    }
    lines = ["[worker]", *(f"{key} = {json.dumps(value)}" for key, value in values.items())]
    lines += [
        "", "[executor]", 'factory = ""', "", "[runtime]",
        f'manifest = "{_DESTINATION}/runtime-deployment.json"',
        "startup_timeout_seconds = 600", "shutdown_timeout_seconds = 60",
        "", "[serving]", f'manifest = "{_DESTINATION}/serving-deployment.json"', "",
    ]
    return "\n".join(lines)


def _unit(config, user, group, environment, executable):
    return "\n".join((
        "# Review artifact only: not installed, enabled, reloaded or started.",
        "[Unit]", "Description=TSUMGI owned VOICEVOX Worker", "After=network-online.target",
        "", "[Service]", "Type=simple", f"User={user}", f"Group={group}",
        f"EnvironmentFile={environment}",
        f"ExecStart={executable} --config {_DESTINATION}/worker.toml",
        f"WorkingDirectory={config.private_home}", f"ReadWritePaths={config.private_home}",
        "UMask=0077", "Restart=no", "KillMode=control-group",
        "NoNewPrivileges=true", "PrivateTmp=true", "ProtectSystem=strict", "ProtectHome=true",
        "ProtectKernelTunables=true", "ProtectKernelModules=true", "ProtectControlGroups=true",
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6", "",
    ))


def build_voicevox_staging_plan(
    *, enabled: bool = False, config: VoicevoxProviderConfig | None = None,
    serving: ServingDeploymentDeclaration | None = None, worker: WorkerSpec | None = None,
    snapshot: SetupHostSnapshot | None = None, service_user: str | None = None,
    service_group: str | None = None, environment_file: str | None = None,
    control_url: str | None = None, worker_executable: str | None = None,
    bundle_receipt_path: str | None = None,
) -> SetupPlan | None:
    """Build only review-file staging actions; disabled means no input inspection."""
    if type(enabled) is not bool:
        _reject()
    if not enabled:
        return None
    if (type(config) is not VoicevoxProviderConfig
            or type(serving) is not ServingDeploymentDeclaration
            or type(worker) is not WorkerSpec or type(snapshot) is not SetupHostSnapshot):
        _reject()
    if (serving != config.serving_declaration()
            or worker.capabilities != {"audio.synthesize"} or worker.resources != ResourceShape()
            or worker.gpu_uuids or worker.accelerators or worker.labels
            or snapshot.runtime_host.architecture != "x86_64"
            or snapshot.service_manager != "systemd"
            or (snapshot.deployment_path is DeploymentPath.NIXOS) != (snapshot.os_id == "nixos")):
        _reject()
    user, group = _account(service_user, user=True), _account(service_group)
    environment = _path(environment_file)
    if not environment.startswith(("/etc/", "/run/")):
        _reject()
    executable = _path(worker_executable)
    receipt = _path(bundle_receipt_path)
    _path(config.engine_root)
    _path(config.private_home, prefix="/var/lib/tsumgi-audio/")
    if Path(receipt).is_relative_to(Path(config.engine_root)):
        _reject()
    control = _CONTROL.fullmatch(control_url) if type(control_url) is str else None
    if control is None or int(control[1]) > 65535:
        _reject()
    runtime = RuntimeDeploymentSpec(
        provider_id="voicevox", provider_config=asdict(config), demand=_DEMAND,
    ).to_dict()
    files = {
        "runtime-deployment.json": _json(runtime),
        "serving-deployment.json": _json(serving.to_dict()),
        "worker.toml": _worker_toml(worker, control_url),
    }
    if snapshot.deployment_path is DeploymentPath.NIXOS:
        files["nixos-worker-options.json"] = _json({
            "enable": False,  # Importing staged options must not activate a service.
            "workerId": worker.worker_id, "workerClass": worker.worker_class,
            "capabilities": ["audio.synthesize"], "user": user, "group": group,
            "controlUrl": control_url, "environmentFile": environment, "command": executable,
            "maxConcurrency": 1, "healthHost": "127.0.0.1",
            "runtime": {
                "enable": True, "provider": "voicevox", "providerConfig": asdict(config),
                "packages": [], "modelRef": "model/0.vvm", "modelFormat": "vvm",
                "residencyPolicy": "cpu_gpu_hybrid", "gpuTopology": "none",
            },
            "serving": {"declaration": serving.to_dict()},
        })
    else:
        files["worker.service"] = _unit(config, user, group, environment, executable)
    actions = [SetupAction(
        "01-verify-audio-bundle", SetupActionKind.VERIFY_AUDIO_BUNDLE,
        "Verify pre-provisioned VOICEVOX content (not executable ABI)",
        payload={
            "engine_root": config.engine_root, "receipt_path": receipt,
            "rights_review_sha256": config.rights_review_sha256,
        },
    )]
    for index, (name, content) in enumerate(files.items(), 2):
        if len(content.encode("utf-8")) > _MAX_CONTENT:
            _reject()
        actions.append(SetupAction(
            f"{index:02d}-stage-audio-config", SetupActionKind.STAGE_AUDIO_CONFIG,
            "Stage a reviewed VOICEVOX configuration artifact",
            payload={"filename": name, "content": content, "sha256": _sha(content)},
            requires_confirmation=True, reversible=True,
        ))
    return SetupPlan(
        "voicevox", snapshot.deployment_path, SetupPlanGoal.STAGE, tuple(actions),
        metadata={
            "staging_schema": _MARKER, "activation_allowed": False,
            "remaining_gates": [
                "native_package_abi", "installed_apply_rollback", "standard_control_e2e",
                "operator_activation_approval",
            ],
        },
    )


@dataclass(frozen=True)
class _FileState:
    content: str
    mode: int
    gid: int
    identity: tuple[int, ...]


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_uid, info.st_gid, info.st_mode,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


class VoicevoxStagingDriver:
    """Only caller-owned 0700 staging roots; no subprocess/network/service API.

    Directory creation may leave empty staging directories after rollback.
    Individual files are atomic, but the set is not a filesystem transaction.
    Only returned in-process receipts authorize rollback; concurrent user edits
    detected at rollback are preserved and reported as rollback failures.
    """

    def __init__(self, plan: SetupPlan, *, root: Path):
        root = Path(root)
        if (not root.is_absolute() or root == Path("/") or root.resolve(strict=True) != root
                or not root.is_dir() or root.stat().st_uid != os.geteuid()
                or stat.S_IMODE(root.stat().st_mode) != 0o700):
            _reject()
        if (type(plan) is not SetupPlan or plan.goal is not SetupPlanGoal.STAGE
                or plan.provider_id != "voicevox" or plan.schema_version != "v1"
                or plan.metadata.get("staging_schema") != _MARKER
                or plan.metadata.get("activation_allowed") is not False):
            _reject()
        expected_files = (*_FILES, "nixos-worker-options.json" if plan.deployment_path
                          is DeploymentPath.NIXOS else "worker.service")
        if len(plan.actions) != 1 + len(expected_files):
            _reject()
        verify = plan.actions[0]
        if (verify.kind is not SetupActionKind.VERIFY_AUDIO_BUNDLE
                or set(verify.payload) != {"engine_root", "receipt_path", "rights_review_sha256"}
                or verify.reversible or verify.requires_confirmation):
            _reject()
        for action in plan.actions:
            if action.requires_network or action.requires_privilege:
                _reject()
        for action, name in zip(plan.actions[1:], expected_files, strict=True):
            if (action.kind is not SetupActionKind.STAGE_AUDIO_CONFIG
                    or set(action.payload) != {"filename", "content", "sha256"}
                    or action.payload["filename"] != name
                    or type(action.payload["content"]) is not str
                    or len(action.payload["content"].encode("utf-8")) > _MAX_CONTENT
                    or action.payload["sha256"] != _sha(action.payload["content"])
                    or not action.requires_confirmation or not action.reversible):
                _reject()
        # Bind staged manifests back to the same exact provider/bundle inputs.
        runtime = RuntimeDeploymentSpec.from_dict(json.loads(plan.actions[1].payload["content"]))
        cfg = VoicevoxProviderConfig(**dict(runtime.provider_config))
        declaration = ServingDeploymentDeclaration.from_dict(json.loads(plan.actions[2].payload["content"]))
        if (runtime.provider_id != "voicevox" or runtime.demand != _DEMAND
                or runtime.setup_intent is not None or declaration != cfg.serving_declaration()
                or verify.payload["engine_root"] != cfg.engine_root
                or verify.payload["rights_review_sha256"] != cfg.rights_review_sha256):
            _reject()
        _path(verify.payload["receipt_path"])
        if Path(verify.payload["receipt_path"]).is_relative_to(Path(cfg.engine_root)):
            _reject()
        for source in (Path(cfg.engine_root), Path(cfg.private_home), Path(verify.payload["receipt_path"])):
            if root.is_relative_to(source) or source.is_relative_to(root):
                _reject()  # Staging must not modify an input tree or private HOME.
        self.plan, self.root = plan, root
        self.filesystem = SetupFilesystem(root)
        with self.filesystem.directory(root):
            pass
        self._root_identity = (root.stat().st_dev, root.stat().st_ino)
        self._verified = False
        self._receipts: dict[int, tuple[SetupAction, ActionReceipt, _FileState | None, _FileState]] = {}

    def _guard(self, action):
        if action not in self.plan.actions:
            _reject()
        with self.filesystem.directory(self.root) as fd:
            info = os.fstat(fd)
            if ((info.st_dev, info.st_ino) != self._root_identity
                    or stat.S_IMODE(info.st_mode) != 0o700):
                _reject()

    def _target(self, action):
        return self.root / "voicevox" / action.payload["filename"]

    def _read(self, path):
        try:
            with self.filesystem.directory(path.parent) as parent:
                try:
                    before = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    return None
                if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                        or before.st_mode & 0o022 or before.st_nlink != 1
                        or before.st_mode & (stat.S_ISUID | stat.S_ISGID)
                        or before.st_size > _MAX_CONTENT):
                    _reject()
                fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
                             | os.O_NONBLOCK, dir_fd=parent)
                try:
                    if _identity(os.fstat(fd)) != _identity(before):
                        _reject()
                    raw = bytearray()
                    while len(raw) <= before.st_size:
                        block = os.read(fd, min(8192, before.st_size + 1 - len(raw)))
                        if not block:
                            break
                        raw.extend(block)
                    if (len(raw) != before.st_size or _identity(os.fstat(fd)) != _identity(before)
                            or _identity(os.stat(path.name, dir_fd=parent, follow_symlinks=False))
                            != _identity(before)):
                        _reject()
                finally:
                    os.close(fd)
                return _FileState(raw.decode("utf-8"), stat.S_IMODE(before.st_mode),
                                  before.st_gid, _identity(before))
        except FileNotFoundError:
            return None

    def _verify(self):
        payload = self.plan.actions[0].payload
        return verify_bundle(Path(payload["engine_root"]), Path(payload["receipt_path"]),
                             rights_review_sha256=payload["rights_review_sha256"])

    def inspect(self, action):
        self._guard(action)
        if action.kind is SetupActionKind.VERIFY_AUDIO_BUNDLE:
            self._verify()
            return ActionInspection(SetupActionState.NEEDS_APPLY, "content-only verification required")
        current = self._read(self._target(action))
        state = (SetupActionState.SATISFIED if current and current.content == action.payload["content"]
                 else SetupActionState.NEEDS_APPLY)
        return ActionInspection(state)

    def _restore(self, path, before, installed):
        current = self._read(path)
        if current != installed:
            raise VoicevoxStagingRollbackError()
        if before is None:
            self.filesystem.remove_file(path, expected=installed.content)
        else:
            self.filesystem.write_text(path, before.content, mode=before.mode, gid=before.gid)

    def apply(self, action):
        self._guard(action)
        if action.kind is SetupActionKind.VERIFY_AUDIO_BUNDLE:
            self._verified = False
            result = self._verify()
            self._verified = True
            return ActionReceipt(False, "content verified; native execution remains unqualified",
                                 evidence={"tree_sha256": result["tree_sha256"]})
        if not self._verified:
            _reject()
        path = self._target(action)
        before = self._read(path)
        content = action.payload["content"]
        if before is not None and before.content == content:
            return ActionReceipt(False, "already staged")
        try:
            self.filesystem.write_text(path, content, mode=0o600, preserve_metadata=before is not None)
            installed = self._read(path)
            if installed is None or installed.content != content:
                raise VoicevoxStagingRollbackError()
        except Exception:
            # apply_plan only knows completed receipts. Recover this action's
            # possible replacement before allowing its earlier actions to unwind.
            current = self._read(path)
            if current != before:
                if current is None or current.content != content:
                    raise VoicevoxStagingRollbackError() from None
                try:
                    self._restore(path, before, current)
                except Exception:
                    raise VoicevoxStagingRollbackError() from None
            raise VoicevoxStagingError() from None
        receipt = ActionReceipt(True, "review file staged; no activation",
                                evidence={"content_sha256": action.payload["sha256"]})
        self._receipts[id(receipt)] = (action, receipt, before, installed)
        return receipt

    def rollback(self, action, receipt):
        self._guard(action)
        saved = self._receipts.get(id(receipt))
        if saved is None or saved[0] != action or saved[1] is not receipt:
            raise VoicevoxStagingRollbackError()
        self._restore(self._target(action), saved[2], saved[3])
        del self._receipts[id(receipt)]
        return ActionReceipt(True, "staged file restored; no service action")
