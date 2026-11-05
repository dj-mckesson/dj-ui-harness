"""Read and persist the small supervisor run state."""

import json
from pathlib import Path

from pydantic import TypeAdapter, ValidationError, validate_call

from harness.contracts.core import (
    TaskId,
    load_paths,
    load_task_paths,
)
from harness.contracts.state import RunState
from harness.utils.artifact_paths import require_safe_artifact_path
from harness.utils.atomic import write_bytes


class StateStorageError(ValueError):
    """Report saved-state problems that the harness cannot safely guess around."""


def _active_task_path(repo_root: Path) -> Path:
    """Return the safe path to the selected-task marker."""

    return require_safe_artifact_path(
        repo_root,
        load_paths(repo_root).runs / "active.json",
        label="active task marker",
    )


def _state_path(repo_root: Path, task_id: TaskId) -> Path:
    """Return the safe ``state.json`` path for one task."""

    task_paths = load_task_paths(repo_root, task_id)
    return require_safe_artifact_path(
        repo_root,
        task_paths.run_dir / "state.json",
        label="run state",
    )


@validate_call
def load_active_task(repo_root: Path) -> TaskId | None:
    """Read the task currently selected by the supervisor.

    :return: Selected task ID, or ``None`` when no task is selected.
    :raises StateStorageError: If the marker is unsafe, unreadable, or malformed.
    """

    resolved_root = repo_root.resolve()
    try:
        path = _active_task_path(resolved_root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise StateStorageError(f"active task marker is invalid: {exc}") from exc
    try:
        exists = path.exists()
        regular = path.is_file() if exists else False
        value = json.loads(path.read_text(encoding="utf-8")) if regular else None
    except (OSError, RuntimeError, ValueError) as exc:
        raise StateStorageError(f"active task marker is invalid: {exc}") from exc
    if not exists:
        return None
    if not regular:
        raise StateStorageError("active task marker is not a regular file")
    if not isinstance(value, dict) or set(value) != {"task_id"}:
        raise StateStorageError(
            "active task marker must contain exactly one task_id field"
        )
    try:
        return TypeAdapter(TaskId).validate_python(value["task_id"])
    except ValidationError as exc:
        raise StateStorageError(f"active task marker is invalid: {exc}") from exc


@validate_call
def save_active_task(repo_root: Path, task_id: TaskId) -> Path:
    """Atomically select one task for registered runtime hooks.

    :param task_id: Task selected by the supervisor.
    :return: Absolute path to the saved marker.
    :raises StateStorageError: If the marker is unsafe, already malformed, or cannot be saved.
    """

    resolved_root = repo_root.resolve()
    try:
        path = _active_task_path(resolved_root)
        parent_exists = path.parent.is_dir()
    except (OSError, RuntimeError, ValidationError, ValueError) as exc:
        raise StateStorageError(
            f"could not save active task marker for task {task_id}: {exc}"
        ) from exc
    if not parent_exists:
        raise StateStorageError("run directory is missing for active task marker")
    try:
        marker_exists = path.exists()
    except (OSError, RuntimeError, ValueError) as exc:
        raise StateStorageError(
            f"could not save active task marker for task {task_id}: {exc}"
        ) from exc
    if marker_exists:
        load_active_task(resolved_root)
    try:
        content = (json.dumps({"task_id": task_id}, indent=2) + "\n").encode("utf-8")
        write_bytes(path, content, fsync=False)
    except (OSError, RuntimeError, ValidationError, ValueError) as exc:
        raise StateStorageError(
            f"could not save active task marker for task {task_id}: {exc}"
        ) from exc
    return path


@validate_call
def clear_active_task(repo_root: Path, task_id: TaskId) -> None:
    """Clear the selected-task marker only when it still names this task.

    :param repo_root: Repository containing the workflow runs.
    :param task_id: Terminal task that may currently own the marker.
    :raises StateStorageError: If the marker is unsafe, unreadable, malformed, or cannot be removed.
    """

    resolved_root = repo_root.resolve()
    selected_task = load_active_task(resolved_root)
    if selected_task != task_id:
        return
    try:
        path = _active_task_path(resolved_root)
        path.unlink()
    except (OSError, RuntimeError, ValidationError, ValueError) as exc:
        raise StateStorageError(
            f"could not clear active task marker for task {task_id}: {exc}"
        ) from exc


@validate_call
def load_state(repo_root: Path, task_id: TaskId) -> RunState:
    """Load and validate the saved state for one workflow run.

    :param task_id: Task whose state should be loaded.
    :return: Validated saved run state.
    :raises StateStorageError: If state is missing, unreadable, malformed, or mismatched.
    """

    resolved_root = repo_root.resolve()
    try:
        path = _state_path(resolved_root, task_id)
        exists = path.is_file()
    except (OSError, ValidationError, ValueError) as exc:
        raise StateStorageError(
            f"run state is invalid for task {task_id}: {exc}"
        ) from exc
    if not exists:
        raise StateStorageError(f"run state is missing for task {task_id}")
    try:
        state = RunState.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, ValueError) as exc:
        raise StateStorageError(
            f"run state is invalid for task {task_id}: {exc}"
        ) from exc
    if state.task_id != task_id:
        raise StateStorageError(
            f"run state task ID {state.task_id!r} does not match directory {task_id!r}"
        )
    return state


@validate_call
def save_state(repo_root: Path, state: RunState) -> Path:
    """Atomically replace ``state.json`` with one complete checked state.

    :param state: Complete state to save.
    :return: Absolute path to the saved ``state.json``.
    :raises StateStorageError: If the run directory is missing or the state cannot be replaced.
    """

    resolved_root = repo_root.resolve()
    try:
        path = _state_path(resolved_root, state.task_id)
        parent_exists = path.parent.is_dir()
    except (OSError, ValueError) as exc:
        raise StateStorageError(
            f"could not save run state for task {state.task_id}: {exc}"
        ) from exc
    if not parent_exists:
        raise StateStorageError(f"run directory is missing for task {state.task_id}")
    try:
        content = (state.model_dump_json(indent=2) + "\n").encode("utf-8")
        write_bytes(path, content, fsync=False)
    except (OSError, ValueError) as exc:
        raise StateStorageError(
            f"could not save run state for task {state.task_id}: {exc}"
        ) from exc
    return path
