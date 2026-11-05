"""Guard registered file-edit events with the current saved agent action."""

from harness.contracts.models import (
    DenyResponse,
    HookDecision,
    HookOutput,
)
from harness.hooks.protect_paths import (
    evaluate_paths,
    load_run_state,
)

__all__ = [
    "DenyResponse",
    "HookDecision",
    "HookOutput",
    "evaluate_paths",
    "load_run_state",
]
