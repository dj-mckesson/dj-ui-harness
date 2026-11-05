"""Report and resume saved runs without choosing any new workflow rule."""

import re
from pathlib import Path

from pydantic import validate_call

from harness.contracts.actions import AgentAction, ApprovalAction, QuestionAction
from harness.contracts.changes import ChangeRecord
from harness.contracts.core import (
    MAX_CANDIDATE_ATTEMPTS,
    TaskId,
    load_paths,
    load_task_paths,
)
from harness.contracts.state import RunState
from harness.contracts.status import ChangeRecordSummary, PendingSummary, StatusReport
from harness.execute.state import StateStorageError, load_active_task, load_state
from harness.execute.supervisor import SupervisorError, SupervisorOutput, next_action
from harness.utils.artifact_paths import require_safe_artifact_path

# Stages that start after the plan is approved. Each one needs the saved baseline.
_BASELINE_STAGES = {"implementation", "review", "validation", "change-approval", "repair"}


def resolve_task_id(repo_root: Path, task_id: TaskId | None) -> TaskId:
    """Pick the task to report on or resume.

    A named task wins. Otherwise the supervisor's active task is used, then the newest saved run.

    :param repo_root: Repository containing the runs.
    :param task_id: Task named by the caller, if any.
    :return: The chosen task ID.
    :raises StateStorageError: If no saved run exists.
    """

    if task_id is not None:
        return task_id
    resolved_root = repo_root.resolve()
    active = load_active_task(resolved_root)
    if active is not None:
        return active
    runs_dir = load_paths(resolved_root).runs
    names = (
        sorted(
            entry.name
            for entry in runs_dir.iterdir()
            if entry.is_dir() and re.fullmatch(r"[0-9]{2,}", entry.name)
        )
        if runs_dir.is_dir()
        else []
    )
    if not names:
        raise StateStorageError(f"no saved runs were found in {runs_dir}")
    return names[-1]


def _pending_summary(state: RunState) -> PendingSummary | None:
    action = state.pending_action
    if isinstance(action, AgentAction):
        return PendingSummary(kind="agent", action_id=action.action_id, role=action.role)
    if isinstance(action, QuestionAction):
        return PendingSummary(
            kind="question", action_id=action.action_id, artifact=action.artifact
        )
    if isinstance(action, ApprovalAction):
        return PendingSummary(
            kind="approval", action_id=action.action_id, artifact=action.artifact
        )
    return None


def _next_step(state: RunState) -> str:
    """Say in plain words what the person or the harness runner does next."""

    if state.stage == "complete":
        return "The run is complete. Read the evidence paths for the review, tests, and changes."
    if state.stage == "blocked":
        return f"The run is blocked: {state.blocked_reason} Fix the cause, then start a new run."
    if state.stage == "validation":
        return "The checks were interrupted. Run resume to finish them."
    action = state.pending_action
    if isinstance(action, QuestionAction):
        return f"Answer the {action.artifact} questions, then submit the response."
    if isinstance(action, ApprovalAction):
        return f"Review the {action.artifact} and submit an approval or a rejection."
    if isinstance(action, AgentAction):
        return f"Run next-action, start the {action.role} agent, then submit its result."
    return "Run resume."


def _change_summaries(
    repo_root: Path, state: RunState
) -> tuple[ChangeRecordSummary, ...]:
    task_paths = load_task_paths(repo_root, state.task_id)
    summaries = []
    for attempt in range(1, MAX_CANDIDATE_ATTEMPTS + 1):
        for path in (
            task_paths.candidate_changes(attempt),
            task_paths.check_changes(attempt),
        ):
            relative = path.relative_to(repo_root).as_posix()
            try:
                safe = require_safe_artifact_path(repo_root, path, label="change record")
                if not safe.is_file():
                    continue
                record = ChangeRecord.model_validate_json(
                    safe.read_text(encoding="utf-8")
                )
            except (OSError, ValueError):
                summaries.append(ChangeRecordSummary(path=relative, status="unreadable"))
                continue
            summaries.append(
                ChangeRecordSummary(
                    path=relative,
                    status=record.status,
                    problems=record.problems,
                    harness_changes=record.harness_changes,
                )
            )
    return tuple(summaries)


@validate_call
def run_status(repo_root: Path, task_id: TaskId | None = None) -> StatusReport:
    """Report where one saved run stands. This never changes the run.

    :param repo_root: Repository containing the run.
    :param task_id: Task to report on; defaults to the active or newest run.
    :return: The status report.
    :raises StateStorageError: If no run exists or its saved state is invalid.
    """

    resolved_root = repo_root.resolve()
    state = load_state(resolved_root, resolve_task_id(resolved_root, task_id))
    task_paths = load_task_paths(resolved_root, state.task_id)
    return StatusReport(
        task_id=state.task_id,
        stage=state.stage,
        original_request=state.original_request,
        spec_revision=state.spec_revision,
        spec_approved=state.spec_approved,
        plan_revision=state.plan_revision,
        plan_approved=state.plan_approved,
        candidate_attempt=state.candidate_attempt,
        pending=_pending_summary(state),
        blocked_reason=state.blocked_reason,
        completion=state.completion_evidence,
        baseline_saved=task_paths.baseline.is_file()
        and task_paths.approved_plan.is_file(),
        change_records=_change_summaries(resolved_root, state),
        next_step=_next_step(state),
    )


@validate_call
def resume_run(repo_root: Path, task_id: TaskId | None = None) -> SupervisorOutput:
    """Continue a saved run and return its pending action or terminal result.

    This works after a restart. It selects the run for the edit hooks, finishes checks that a
    stopped process left behind, and returns the same pending action again until its result is
    accepted. It refuses a run whose file baseline is missing, because that run could no longer
    compare its actual changes with the approved plan.

    :param repo_root: Repository containing the run.
    :param task_id: Task to resume; defaults to the active or newest run.
    :return: Current pending action, blocked output, or complete output.
    :raises StateStorageError: If no run exists or its saved state is invalid.
    :raises SupervisorError: If the saved run lost its approved plan or file baseline.
    """

    resolved_root = repo_root.resolve()
    chosen = resolve_task_id(resolved_root, task_id)
    state = load_state(resolved_root, chosen)
    if state.stage in _BASELINE_STAGES:
        task_paths = load_task_paths(resolved_root, chosen)
        missing = [
            path.relative_to(resolved_root).as_posix()
            for path in (task_paths.approved_plan, task_paths.baseline)
            if not path.is_file()
        ]
        if missing:
            raise SupervisorError(
                "the saved run cannot continue because its file baseline is missing: "
                + ", ".join(missing)
            )
    return next_action(resolved_root, chosen)
