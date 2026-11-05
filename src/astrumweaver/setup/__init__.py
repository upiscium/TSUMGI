"""Shared deterministic runtime setup backend.

Both non-interactive automation and the interactive TUI consume this API.
Deployment-specific mutation is supplied later through SetupActionDriver.
"""

from .apply import (
    SetupActionDriver,
    SetupApprovalError,
    apply_plan,
    dry_run_plan,
    explain_plan,
    preview_plan,
)
from .audio_provider_binding import (
    AudioProviderBindingError,
    AudioProviderBindingReview,
    review_audio_provider_binding,
)
from .audio_review import AudioSetupReview, AudioSetupReviewError, review_audio_setup
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
from .planner import (
    SetupPlanningError,
    build_runtime_release_plan,
    build_runtime_setup_plan,
)
from .systemd import SystemdSetupDriver, create_systemd_driver

__all__ = [
    "ActionInspection",
    "ActionReceipt",
    "AudioProviderBindingError",
    "AudioProviderBindingReview",
    "AudioSetupReview",
    "AudioSetupReviewError",
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
    "SetupApplyResult",
    "SetupApplyStatus",
    "SetupApproval",
    "SetupApprovalError",
    "SetupHostSnapshot",
    "SetupPlan",
    "SetupPlanGoal",
    "SetupPlanningError",
    "SetupPreview",
    "SystemdSetupDriver",
    "apply_plan",
    "build_runtime_release_plan",
    "build_runtime_setup_plan",
    "create_systemd_driver",
    "discover_local_gpus",
    "discover_local_host",
    "dry_run_plan",
    "explain_plan",
    "preview_plan",
    "review_audio_provider_binding",
    "review_audio_setup",
]
