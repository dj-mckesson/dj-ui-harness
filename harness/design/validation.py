"""Validate specification and plan artifacts for the design stage."""

import re
from pathlib import Path

from pydantic import StrictBool, validate_call

from harness.contracts.core import TaskId, load_task_paths
from harness.contracts.plan import PlanDocument, SpecIdentifiers
from harness.contracts.scopes import is_protected_path, requires_exact_file_scope
from harness.utils.artifact_paths import (
    normalize_scope_path,
    require_artifact_file,
    require_safe_artifact_path,
)

SPEC_HEADINGS = (
    "# Specification",
    "## Objective and observable behavior",
    "## Boundaries, errors, and non-goals",
    "## Resolved decisions",
    "## Requirements",
    "## Acceptance criteria",
)


def _section_bounds(lines: list[str], heading: str) -> tuple[int, int]:
    """Find the lines that belong to one specification section.

    Spec validation uses this after checking the required headings. It prevents ID references in
    ordinary prose from being treated as ID definitions.

    :param lines: Lines from ``SPEC.md``.
    :param heading: Heading that starts the section.
    :return: Start and end positions, excluding the heading.
    :raises ValueError: If the heading is missing.
    """

    try:
        start = lines.index(heading) + 1
    except ValueError as exc:
        raise ValueError(f"required specification heading is missing: {heading}") from exc
    end = next(
        (index for index in range(start, len(lines)) if lines[index].startswith("## ")),
        len(lines),
    )
    return start, end


def _extract_ids(
    lines: list[str],
    *,
    prefix: str,
    label: str,
    require_colon: bool,
) -> tuple[str, ...]:
    """Read IDs from top-level bullets in one specification section.

    Spec validation calls this when the spec author returns a completed ``SPEC.md``. Indented
    details are ignored, so only requirement and acceptance definition bullets become IDs.

    :param lines: Lines in the requirement or acceptance section.
    :param prefix: Expected prefix, such as ``REQ`` or ``AC``.
    :param label: Name used in error messages.
    :param require_colon: Whether each bullet must use ``ID: description``.
    :return: Extracted IDs in the order written.
    :raises ValueError: If a definition is malformed or the section has no IDs.
    """

    top_level_bullet = re.compile(r"^[-*]\s+(.*)$")
    identifiers: list[str] = []
    for line in lines:
        bullet = top_level_bullet.match(line)
        if bullet is None:
            continue
        body = bullet.group(1).strip()
        definition = (
            re.fullmatch(r"([^\s:]+)\s*:\s*.+", body)
            if require_colon
            else re.fullmatch(r"([^\s:]+)(?:\s*:\s*.+)?", body)
        )
        if definition is None:
            raise ValueError(f"malformed {label} ID definition: {body!r}")
        candidate = definition.group(1)
        if not candidate.startswith(f"{prefix}-"):
            raise ValueError(f"malformed {label} ID: {candidate!r}")
        identifiers.append(candidate)

    if not identifiers:
        raise ValueError(f"the specification must define at least one {label} ID")
    return tuple(identifiers)


@validate_call
def validate_spec(repo_root: Path, task_id: TaskId) -> SpecIdentifiers:
    """Check a completed ``SPEC.md`` and return its IDs.

    The supervisor calls this before asking a human to approve the specification. The maker gate
    calls it again before implementation starts. This function checks structure only; the human
    decides whether the specification says the right thing.

    :param repo_root: Repository containing the task.
    :param task_id: Task whose specification should be checked.
    :return: Requirement and acceptance IDs from the specification.
    :raises OSError: If ``SPEC.md`` cannot be read.
    :raises ValueError: If ``SPEC.md`` is missing, empty, or has the wrong structure.
    """

    resolved_root = repo_root.resolve()
    task_paths = load_task_paths(resolved_root, task_id)
    spec_path = require_safe_artifact_path(
        resolved_root, task_paths.spec, label="canonical specification"
    )
    if not spec_path.is_file():
        raise ValueError(f"required specification file is missing: {spec_path}")
    text = spec_path.read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError("the specification must not be empty")

    lines = [line.rstrip() for line in text.splitlines()]
    positions: list[int] = []
    for heading in SPEC_HEADINGS:
        occurrences = [index for index, line in enumerate(lines) if line == heading]
        if len(occurrences) != 1:
            raise ValueError(
                f"required specification heading must appear exactly once: {heading}"
            )
        positions.append(occurrences[0])
    if positions != sorted(positions):
        raise ValueError(
            "required specification headings must appear in the expected order"
        )

    requirement_start, requirement_end = _section_bounds(lines, "## Requirements")
    acceptance_start, acceptance_end = _section_bounds(lines, "## Acceptance criteria")
    return SpecIdentifiers(
        requirement_ids=_extract_ids(
            lines[requirement_start:requirement_end],
            prefix="REQ",
            label="requirement",
            require_colon=True,
        ),
        acceptance_criterion_ids=_extract_ids(
            lines[acceptance_start:acceptance_end],
            prefix="AC",
            label="acceptance-criterion",
            require_colon=False,
        ),
    )


def _validate_work_paths(repo_root: Path, plan: PlanDocument) -> None:
    """Check that every plan work item is a safe file or folder write scope.

    Plan validation calls this before human plan review. These scopes later become the only paths
    the maker may change, so repository escapes and symlinks are rejected. Supervisor paths cannot
    be named. Protected files must be named individually.

    :param repo_root: Repository containing the work paths.
    :param plan: Plan whose work-item paths should be checked.
    :raises ValueError: If a work item is not a safe scope.
    """

    for step in plan.steps:
        for item in step.work_items:
            is_folder = item.scope == "folder"
            label = f"plan work-item path {item.path!r}"
            work_path, normalized = normalize_scope_path(
                repo_root,
                item.path,
                label=label,
                allow_root=is_folder,
            )
            if normalized != ".":
                if is_protected_path(normalized):
                    raise ValueError(
                        f"{label} is owned by the supervisor and cannot be a write scope"
                    )
                if is_folder and requires_exact_file_scope(normalized):
                    raise ValueError(
                        f"{label} is a protected path; name each file individually"
                    )
            if is_folder:
                if not work_path.is_dir():
                    raise ValueError(f"{label} is a folder scope but is not a folder")
            elif work_path.exists() and not work_path.is_file():
                raise ValueError(
                    f"{label} must be a regular file or a future file; use a folder scope "
                    "for a folder"
                )


@validate_call
def validate_plan(
    repo_root: Path,
    task_id: TaskId,
    spec_identifiers: SpecIdentifiers,
    *,
    approved: StrictBool = False,
) -> PlanDocument:
    """Read and check the task's ``plan.json`` or its approved copy.

    The supervisor checks the draft after the planner finishes and before human plan review. The
    maker gate checks the approved copy before implementation. Both checks confirm that the plan
    matches the spec and that its write scopes are safe. It never chooses a command to run.

    :param repo_root: Repository containing the task.
    :param task_id: Task whose plan should be checked.
    :param spec_identifiers: IDs read from the current specification.
    :param approved: Whether to read ``approved-plan.json`` instead of the draft ``plan.json``.
    :return: The checked plan.
    :raises OSError: If the plan cannot be read.
    :raises ValueError: If the plan is invalid or does not match the task and specification.
    """

    resolved_root = repo_root.resolve()
    task_paths = load_task_paths(resolved_root, task_id)
    if approved:
        plan_path, label = task_paths.approved_plan, "approved plan"
    else:
        plan_path, label = task_paths.canonical_plan, "canonical plan"
    safe_plan_path = require_artifact_file(resolved_root, plan_path, label=label)
    plan = PlanDocument.model_validate_json(safe_plan_path.read_text(encoding="utf-8"))
    if plan.task_id != task_id:
        raise ValueError(
            f"plan task ID {plan.task_id!r} does not match current task {task_id!r}"
        )

    known_requirements = set(spec_identifiers.requirement_ids)
    known_acceptance = set(spec_identifiers.acceptance_criterion_ids)
    referenced_requirements = {
        requirement_id for step in plan.steps for requirement_id in step.requirement_ids
    }
    referenced_acceptance = {
        acceptance_id for step in plan.steps for acceptance_id in step.acceptance_ids
    }
    unknown_requirements = referenced_requirements - known_requirements
    if unknown_requirements:
        raise ValueError(
            "plan references unknown requirement IDs: "
            + ", ".join(sorted(unknown_requirements))
        )
    unknown_acceptance = referenced_acceptance - known_acceptance
    if unknown_acceptance:
        raise ValueError(
            "plan references unknown acceptance-criterion IDs: "
            + ", ".join(sorted(unknown_acceptance))
        )
    unknown_reviews = {r.acceptance_id for r in plan.manual_reviews} - known_acceptance
    if unknown_reviews:
        raise ValueError(
            "plan has a manual review for unknown acceptance-criterion IDs: "
            + ", ".join(sorted(unknown_reviews))
        )
    missing_requirement_coverage = known_requirements - referenced_requirements
    if missing_requirement_coverage:
        raise ValueError(
            "plan steps do not cover requirements: "
            + ", ".join(sorted(missing_requirement_coverage))
        )
    missing_acceptance_coverage = known_acceptance - referenced_acceptance
    if missing_acceptance_coverage:
        raise ValueError(
            "plan steps do not cover acceptance criteria: "
            + ", ".join(sorted(missing_acceptance_coverage))
        )

    test_intent_ids = set(plan.test_intents)
    missing_test_intents = known_acceptance - test_intent_ids
    unknown_test_intents = test_intent_ids - known_acceptance
    if missing_test_intents:
        raise ValueError(
            "plan is missing test intent for acceptance criteria: "
            + ", ".join(sorted(missing_test_intents))
        )
    if unknown_test_intents:
        raise ValueError(
            "plan has test intent for unknown acceptance criteria: "
            + ", ".join(sorted(unknown_test_intents))
        )

    _validate_work_paths(resolved_root, plan)
    return plan
