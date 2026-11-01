"""Shared deterministic runtime setup backend.

Both non-interactive automation and the interactive TUI consume this API.
Deployment-specific mutation is supplied later through SetupActionDriver.
"""

from .audio_review import AudioSetupReview, AudioSetupReviewError, review_audio_setup
from .apply import (
    SetupActionDriver,
    SetupApprovalError,
    apply_plan,
    dry_run_plan,
    explain_plan,
    preview_plan,
)
from .contracts import (
    ActionInspection,
    ActionReceipt,
    DeploymentPath,
    PrivilegeMode,
    SecretReference,
    SetupAction,
    SetupActionKind,
    SetupActionPreview,
    SetupActionResult,
    SetupActionResultStatus,
    SetupActionState,
    SetupApplyResult,
    SetupApplyStatus,
    SetupApproval,
    SetupHostSnapshot,
    SetupPlan,
    SetupPlanGoal,
    SetupPreview,
)
from .discovery import (
    DiscoveredGpu,
    HostDiscoveryError,
    discover_local_gpus,
    discover_local_host,
)
from .systemd import SystemdSetupDriver, create_systemd_driver
from .planner import (
    SetupPlanningError,
    build_runtime_release_plan,
    build_runtime_setup_plan,
)

__all__ = [
    "AudioSetupReview",
    "AudioSetupReviewError",
    "ActionInspection",
    "ActionReceipt",
    "DeploymentPath",
    "DiscoveredGpu",
    "HostDiscoveryError",
    "PrivilegeMode",
    "SecretReference",
    "SetupAction",
    "SetupActionDriver",
    "SetupActionKind",
    "SetupActionPreview",
    "SetupActionResult",
    "SetupActionResultStatus",
    "SetupActionState",
    "SystemdSetupDriver",
    "SetupApplyResult",
    "SetupApplyStatus",
    "SetupApproval",
    "SetupApprovalError",
    "SetupHostSnapshot",
    "SetupPlan",
    "SetupPlanGoal",
    "SetupPlanningError",
    "SetupPreview",
    "apply_plan",
    "build_runtime_release_plan",
    "build_runtime_setup_plan",
    "create_systemd_driver",
    "discover_local_gpus",
    "discover_local_host",
    "dry_run_plan",
    "explain_plan",
    "preview_plan",
    "review_audio_setup",
]
