"""Render design-stage plan artifacts for human review."""

from pathlib import Path

from pydantic import validate_call

from harness.contracts.checks import MANDATORY_CHECKS
from harness.contracts.core import TaskId, load_task_paths
from harness.contracts.plan import PlanDocument
from harness.contracts.scopes import requires_exact_file_scope
from harness.utils.artifact_paths import require_safe_artifact_path
from harness.utils.atomic import write_bytes


def _render_plan_text(plan: PlanDocument) -> str:
    """Turn a checked plan into stable Markdown text.

    The supervisor uses this before human plan review. The workflow never reads the output. It
    reads ``plan.json`` before approval and ``approved-plan.json`` after.

    :param plan: Checked plan used by the workflow.
    :return: Complete Markdown text ending with one newline.
    """

    lines = ["# Implementation Plan", "", f"Task: `{plan.task_id}`", ""]
    items = [item for step in plan.steps for item in step.work_items]
    if any(item.path == "." for item in items):
        lines.extend(
            [
                "> **Whole-repository scope.** This plan may change any file in the repository",
                "> except supervisor paths and protected files. Approval must confirm this scope.",
                "",
            ]
        )
    protected_items = [
        item
        for item in items
        if item.scope == "file" and requires_exact_file_scope(item.path)
    ]
    if protected_items:
        lines.extend(
            [
                "> **Protected files.** Confirm the task needs each of these:",
                *(f"> - `{item.path}`" for item in protected_items),
                "",
            ]
        )
    for step in plan.steps:
        lines.extend(
            [
                f"## {step.id}: {step.title}",
                "",
                step.intent,
                "",
                "### Work items",
                "",
            ]
        )
        lines.extend(
            f"- {item.scope} `{item.path}` — {item.intent}" for item in step.work_items
        )
        lines.extend(
            [
                "",
                "### Traceability",
                "",
                "- Requirements: "
                + ", ".join(f"`{identifier}`" for identifier in step.requirement_ids),
                "- Acceptance criteria: "
                + (
                    ", ".join(f"`{identifier}`" for identifier in step.acceptance_ids)
                    or "None"
                ),
                "",
            ]
        )
    lines.extend(["## Test intent", ""])
    for acceptance_id in sorted(plan.test_intents):
        lines.append(f"- `{acceptance_id}` — {plan.test_intents[acceptance_id]}")
    lines.extend(
        ["", "## Checks", "", "The tests always run. These checks also run:", ""]
    )
    lines.extend(f"- `{name}` — required" for name in MANDATORY_CHECKS)
    lines.extend(
        f"- `{check.name}` — {'required' if check.required else 'report only'}"
        for check in plan.checks
    )
    if plan.manual_reviews:
        lines.extend(["", "## Manual reviews", ""])
        for review in plan.manual_reviews:
            lines.extend(
                [
                    f"- `{review.acceptance_id}`",
                    f"  - Inspect: {review.inspect}",
                    f"  - How: {review.how}",
                    f"  - Pass if: {review.pass_if}",
                ]
            )
    return "\n".join(lines) + "\n"


def _write_text_atomically(repo_root: Path, path: Path, text: str) -> None:
    """Write a rendered plan without leaving a partial file.

    The supervisor uses this when a checked plan is ready for human review. If writing fails, the
    previous rendered plan stays in place.

    :param repo_root: Repository that must contain the rendered plan.
    :param path: Destination for ``PLAN.md``.
    :param text: Markdown text to write.
    :raises ValueError: If the destination is unsafe.
    :raises OSError: If the file cannot be written or replaced.
    """

    path = require_safe_artifact_path(repo_root, path, label="rendered plan")
    path.parent.mkdir(parents=True, exist_ok=True)
    require_safe_artifact_path(repo_root, path, label="rendered plan")
    write_bytes(path, text.encode("utf-8"))


@validate_call
def render_plan_markdown(
    repo_root: Path,
    task_id: TaskId,
    plan: PlanDocument,
) -> Path:
    """Create the required human-readable ``PLAN.md``.

    The supervisor calls this after plan validation and before asking a human to approve the plan.
    Gates do not call it, and the workflow never reads this file to decide what to do next.

    :param repo_root: Repository containing the task.
    :param task_id: Task whose readable plan should be created.
    :param plan: Checked plan used by the workflow.
    :return: Path to the written ``PLAN.md``.
    :raises ValueError: If the plan belongs to another task or the path is unsafe.
    :raises OSError: If ``PLAN.md`` cannot be written.
    """

    if plan.task_id != task_id:
        raise ValueError(
            f"plan task ID {plan.task_id!r} does not match current task {task_id!r}"
        )
    resolved_root = repo_root.resolve()
    task_paths = load_task_paths(resolved_root, task_id)
    _write_text_atomically(
        resolved_root, task_paths.rendered_plan, _render_plan_text(plan)
    )
    return task_paths.rendered_plan
