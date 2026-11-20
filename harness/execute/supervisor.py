"""Expose the small public API for one saved coding workflow."""

import re
from pathlib import Path

from pydantic import validate_call

from harness.contracts.actions import PendingAction
from harness.contracts.core import (
    NonEmptyStr,
    RepoPath,
    TaskId,
    load_paths,
    load_task_paths,
)
from harness.contracts.results import BlockedOutput, CompleteOutput
from harness.contracts.state import RunState
from harness.execute.actions import build_spec_author_action
from harness.execute.routing import SupervisorError, continue_validation, route_submission
from harness.execute.state import (
    StateStorageError,
    clear_active_task,
    load_state,
    save_active_task,
    save_state,
)
from harness.utils.artifact_paths import require_safe_artifact_path

type SupervisorOutput = PendingAction | BlockedOutput | CompleteOutput


def _next_task_id(runs_dir: Path) -> str:
    """Choose the next numeric task ID from immediate run directories."""

    existing = [
        int(child.name)
        for child in runs_dir.iterdir()
        if child.is_dir() and re.fullmatch(r"[0-9]{2,}", child.name)
    ]
    return f"{max(existing, default=0) + 1:02d}"


@validate_call
def create_run(
    repo_root: Path,
    original_request: NonEmptyStr,
    *,
    context_paths: tuple[RepoPath, ...] | None = None,
) -> RunState:
    """Create a new saved run with its first spec-author action.

    :param repo_root: Repository where the workflow runs.
    :param original_request: Coding request from the user.
    :param context_paths: Read paths for the first spec author, when supplied.
    :return: Initial spec-authoring state.
    :raises OSError: If required directories cannot be created.
    :raises StateStorageError: If the repository is unsafe or the state cannot be saved.
    """

    try:
        resolved_root = repo_root.resolve()
    except (OSError, RuntimeError) as exc:
        raise StateStorageError(f"repository root is unsafe: {repo_root}: {exc}") from exc
    if not resolved_root.is_dir():
        raise StateStorageError(
            f"repository root must already exist as a directory: {repo_root}"
        )
    paths = load_paths(resolved_root)
    try:
        for context in paths.context:
            require_safe_artifact_path(
                resolved_root,
                context,
                label="context directory",
            )
        require_safe_artifact_path(
            resolved_root,
            paths.planning,
            label="planning directory",
        )
        require_safe_artifact_path(
            resolved_root,
            paths.runs,
            label="run directory",
        )
    except ValueError as exc:
        raise StateStorageError(f"repository layout is unsafe: {exc}") from exc

    paths.planning.mkdir(parents=True, exist_ok=True)
    paths.runs.mkdir(parents=True, exist_ok=True)
    task_id = _next_task_id(paths.runs)
    task_paths = load_task_paths(resolved_root, task_id)
    try:
        action = build_spec_author_action(
            resolved_root,
            task_id,
            original_request,
            context_paths=context_paths,
        )
    except (OSError, ValueError) as exc:
        raise StateStorageError(
            f"could not create the first workflow action: {exc}"
        ) from exc
    state = RunState(
        task_id=task_id,
        original_request=original_request,
        stage="spec-authoring",
        pending_action=action,
    )

    task_paths.planning_dir.mkdir()
    task_paths.run_dir.mkdir()
    (task_paths.run_dir / "reviews").mkdir()
    (task_paths.run_dir / "validation").mkdir()
    save_state(resolved_root, state)
    return state


@validate_call
def next_action(repo_root: Path, task_id: TaskId) -> SupervisorOutput:
    """Return the saved action or terminal result, completing pending validation.

    :param repo_root: Repository containing the run.
    :param task_id: Task to resume.
    :return: The pending action or terminal result.
    """

    resolved_root = repo_root.resolve()
    state = load_state(resolved_root, task_id)
    if state.stage == "validation":
        state = continue_validation(resolved_root, state)
        save_state(resolved_root, state)
    output = _output_for_state(state)
    _update_active_task(resolved_root, state)
    return output


@validate_call
def submit_result(
    repo_root: Path,
    task_id: TaskId,
    submitted_result: object,
) -> SupervisorOutput:
    """Save one matching result and return the next action or terminal result.

    Save the validation checkpoint before checks so a stopped run can resume.

    :param submitted_result: Result for the current action.
    :return: The next action or terminal result.
    :raises SupervisorError: If no action is pending or the submission is stale or malformed.
    """

    resolved_root = repo_root.resolve()
    state = load_state(resolved_root, task_id)
    next_state = route_submission(resolved_root, state, submitted_result)
    save_state(resolved_root, next_state)

    if next_state.stage == "validation":
        next_state = continue_validation(resolved_root, next_state)
        save_state(resolved_root, next_state)
    output = _output_for_state(next_state)
    _update_active_task(resolved_root, next_state)
    return output


def _update_active_task(repo_root: Path, state: RunState) -> None:
    """Select unfinished work or clear a matching finished task."""

    if state.pending_action is not None:
        save_active_task(repo_root, state.task_id)
    else:
        clear_active_task(repo_root, state.task_id)


def _output_for_state(state: RunState) -> SupervisorOutput:
    """Return the pending action or terminal result for a saved state.

    :raises SupervisorError: If an internal-only state reaches the external boundary.
    """

    if state.pending_action is not None:
        return state.pending_action
    if state.stage == "blocked" and state.blocked_reason is not None:
        return BlockedOutput(task_id=state.task_id, reason=state.blocked_reason)
    if state.stage == "complete" and state.completion_evidence is not None:
        return CompleteOutput(task_id=state.task_id, evidence=state.completion_evidence)
    raise SupervisorError(f"stage {state.stage!r} has no action to return")


__all__ = ["SupervisorError", "next_action", "submit_result"]
