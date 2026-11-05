"""Guard registered file-edit events with the current saved agent action.

This policy covers edit events that a runtime sends to the hook. It does not cover shell commands,
scripts, or every other way a process could change files.
"""

import os
from collections.abc import Iterable
from pathlib import Path

from pydantic import validate_call

from harness.contracts.actions import AgentAction
from harness.contracts.core import TaskId
from harness.contracts.models import HookDecision
from harness.contracts.scopes import (
    is_protected_path,
    requires_exact_file_scope,
    scope_allows,
)
from harness.contracts.state import RunState
from harness.execute.state import load_active_task, load_state
from harness.utils.artifact_paths import normalize_repository_path, normalize_scope_path


@validate_call
def load_run_state(repo_root: Path, task_id: TaskId) -> RunState:
    """Load the complete saved workflow state for one known task.

    ``load_active_run_state`` calls this after reading the supervisor-selected task ID. Keeping the
    task-specific lookup in one helper also makes that boundary easy to test directly.

    :param repo_root: Repository that contains the workflow run.
    :param task_id: Known task whose saved state should be loaded.
    :return: Validated state loaded from ``runs/<task_id>/state.json``.
    :raises StateStorageError: If the state is missing, malformed, or belongs to another task.
    """

    return load_state(repo_root.resolve(), task_id)


@validate_call
def load_active_run_state(repo_root: Path) -> RunState | None:
    """Load the complete state selected by the supervisor.

    Registered runtime adapters use this helper because their command line identifies the
    repository, while the supervisor-owned marker identifies the current task.

    :param repo_root: Repository that contains the workflow runs.
    :return: Selected run state, or ``None`` when no task is selected.
    :raises StateStorageError: If the marker or selected run state is invalid.
    """

    resolved_root = repo_root.resolve()
    task_id = load_active_task(resolved_root)
    if task_id is None:
        return None
    return load_run_state(resolved_root, task_id)


def _deny(reason: str, targets: list[str] | None = None) -> HookDecision:
    """Build one consistent denial for the runtime adapter.

    :param reason: Plain-language explanation of why the edit was rejected.
    :param targets: Valid target paths that were found before the rejection.
    :return: A denied hook decision.
    """

    return HookDecision(decision="deny", reason=reason, targets=targets or [])


def _writable_scopes(repo_root: Path, state: RunState) -> tuple[set[str], set[str]]:
    """Read and validate the writable scopes from the pending agent action.

    ``evaluate_paths`` uses this after validating the saved state. Human pauses, internal work,
    terminal runs, checker actions, and any other empty write scope are rejected here. A candidate
    scope may not name a supervisor path, and a candidate folder may not name a protected path.

    :param repo_root: Resolved repository root used by the hook.
    :param state: Complete current workflow state.
    :return: Normalized file scopes and folder scopes that the current agent may edit.
    :raises ValueError: If there is no writable pending agent action or a saved scope is unsafe.
    """

    action = state.pending_action
    if not isinstance(action, AgentAction):
        raise ValueError("the workflow is not waiting for an agent edit")
    if not (action.writable_paths or action.writable_folders):
        raise ValueError(f"the {action.role} action is read-only")

    candidate = action.role in {"maker", "repair"}
    files: set[str] = set()
    for path in action.writable_paths:
        _, normalized = normalize_repository_path(
            repo_root,
            path,
            label="saved writable path",
        )
        if candidate and is_protected_path(normalized):
            raise ValueError("candidate writable paths must not be supervisor paths")
        files.add(normalized)
    folders: set[str] = set()
    for path in action.writable_folders:
        _, normalized = normalize_scope_path(
            repo_root,
            path,
            label="saved writable folder",
            allow_root=True,
        )
        if (
            candidate
            and normalized != "."
            and (is_protected_path(normalized) or requires_exact_file_scope(normalized))
        ):
            raise ValueError(
                "candidate writable folders must not be supervisor or protected paths"
            )
        folders.add(normalized)
    return files, folders


def evaluate_paths(
    repo_root: Path,
    targets: Iterable[object] | None,
    run_state: object,
) -> HookDecision:
    """Allow an edit only when every target is inside the saved action's write scopes.

    Runtime adapters call this after extracting paths from a supported edit event. The function
    validates the complete state again so malformed, human, internal, and terminal states fail
    closed instead of accidentally granting a broad role-based scope. A folder scope never covers
    supervisor paths or protected files; only a named file does.

    :param repo_root: Repository where the edit was requested.
    :param targets: Collection of paths affected by the edit event.
    :param run_state: Complete state loaded for the supplied task.
    :return: Allow or deny decision with normalized target paths.
    """

    try:
        state = RunState.model_validate(run_state)
        resolved_root = repo_root.resolve()
        permitted_files, permitted_folders = _writable_scopes(resolved_root, state)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return _deny(f"invalid run state or repository context: {exc}")

    if targets is None or isinstance(targets, (str, bytes, os.PathLike)):
        return _deny("at least one target must be supplied as a collection")

    try:
        supplied_targets = list(targets)
    except (TypeError, RuntimeError) as exc:
        return _deny(f"targets could not be read: {exc}")
    if not supplied_targets:
        return _deny("at least one target is required")

    normalized_targets: list[str] = []
    normalization_errors: list[str] = []
    for index, target in enumerate(supplied_targets):
        try:
            _, normalized = normalize_repository_path(
                resolved_root,
                target,
                label="target",
            )
            normalized_targets.append(normalized)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            normalization_errors.append(f"target {index} is invalid: {exc}")

    if normalization_errors:
        return _deny("; ".join(normalization_errors), normalized_targets)

    outside_scope = [
        target
        for target in normalized_targets
        if not scope_allows(target, permitted_files, permitted_folders)
    ]
    if outside_scope:
        return _deny(
            "every target must be inside the current agent action's writable scopes; "
            f"outside scope: {', '.join(outside_scope)}",
            normalized_targets,
        )

    action = state.pending_action
    assert isinstance(action, AgentAction)
    return HookDecision(
        decision="allow",
        reason=f"all targets are writable by the current {action.role} action",
        targets=normalized_targets,
    )
