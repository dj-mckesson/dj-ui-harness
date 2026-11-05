"""Decide whether saved workflow evidence allows the next action."""

from pathlib import Path

from pydantic import StrictBool, validate_call

from harness import CandidateAttempt, TaskId, load_task_paths
from harness.contracts.models import GateResult
from harness.design.validation import validate_plan, validate_spec
from harness.feedback.evidence import (
    load_checker_evidence,
    load_validation_evidence,
    validated_evidence_path,
)


@validate_call
def before_maker(
    repo_root: Path,
    task_id: TaskId,
    spec_approved: StrictBool,
    plan_approved: StrictBool,
) -> GateResult:
    """Decide whether implementation may start.

    The supervisor calls this after it saves the approved plan. It checks both approvals.
    It reads the spec and the approved plan copy again, so the maker starts from valid
    input. The gate never creates or changes those files.

    :param repo_root: Repository containing the task.
    :param task_id: Task that is ready for implementation.
    :param spec_approved: Whether the current specification revision is approved.
    :param plan_approved: Whether the current plan revision is approved.
    :return: A pass or fail decision with the spec and the approved plan as evidence.
    :raises pydantic.ValidationError: If an input has the wrong type or format.
    """

    if not spec_approved:
        return GateResult(
            gate="before-maker",
            status="fail",
            reason="The current specification has not received human approval.",
        )
    if not plan_approved:
        return GateResult(
            gate="before-maker",
            status="fail",
            reason="The current plan has not received human approval.",
        )

    resolved_root = repo_root.resolve()
    evidence_paths: list[str] = []
    try:
        task_paths = load_task_paths(resolved_root, task_id)
        spec_identifiers = validate_spec(resolved_root, task_id)
        evidence_paths.append(validated_evidence_path(resolved_root, task_paths.spec))
        validate_plan(resolved_root, task_id, spec_identifiers, approved=True)
        evidence_paths.append(
            validated_evidence_path(resolved_root, task_paths.approved_plan)
        )
    except (OSError, ValueError) as exc:
        return GateResult(
            gate="before-maker",
            status="fail",
            reason=f"Required planning evidence is invalid: {exc}",
            evidence_paths=evidence_paths,
        )

    return GateResult(
        gate="before-maker",
        status="pass",
        reason="The current specification and plan are approved and valid.",
        evidence_paths=evidence_paths,
    )


@validate_call
def after_checker(
    repo_root: Path,
    task_id: TaskId,
    attempt: CandidateAttempt,
) -> GateResult:
    """Decide whether the supervisor may validate a reviewed candidate.

    The supervisor calls this when the checker returns. A completed pass or revise
    review lets the fixed tests run. A blocked or invalid review stops the workflow.
    This gate only reads the review and never runs tests.

    :param repo_root: Repository containing the run.
    :param task_id: Task whose checker review is being handled.
    :param attempt: Candidate that the checker reviewed.
    :return: A pass when validation may run, otherwise a fail.
    :raises pydantic.ValidationError: If an input has the wrong type or format.
    """

    resolved_root = repo_root.resolve()
    try:
        task_paths = load_task_paths(resolved_root, task_id)
        checker_evidence, checker_evidence_path = load_checker_evidence(
            resolved_root, task_paths, attempt
        )
    except (OSError, ValueError) as exc:
        return GateResult(
            gate="after-checker",
            status="fail",
            reason=f"Checker evidence is invalid: {exc}",
        )

    evidence_paths = [checker_evidence_path]
    if checker_evidence.result.status == "blocked":
        return GateResult(
            gate="after-checker",
            status="fail",
            reason="The checker was blocked, so validation must not run: "
            + "; ".join(checker_evidence.result.blockers),
            evidence_paths=evidence_paths,
        )
    if checker_evidence.result.verdict == "revise":
        return GateResult(
            gate="after-checker",
            status="pass",
            reason=(
                "The checker requested changes, but completed the review, so "
                "validation may run."
            ),
            evidence_paths=evidence_paths,
        )
    return GateResult(
        gate="after-checker",
        status="pass",
        reason="The checker passed the candidate, so validation may run.",
        evidence_paths=evidence_paths,
    )


@validate_call
def before_complete(
    repo_root: Path,
    task_id: TaskId,
    attempt: CandidateAttempt,
) -> GateResult:
    """Decide whether the current candidate may complete the workflow.

    The supervisor calls this after it runs the test suite generated by the maker.
    Completion is allowed only when the checker and validation both pass for
    the same latest candidate. No extra human approval is needed.
    This gate only reads saved evidence.

    :param repo_root: Repository containing the run.
    :param task_id: Task that may be complete.
    :param attempt: Current candidate that must have matching pass evidence.
    :return: A pass when completion is allowed, otherwise a fail.
    :raises pydantic.ValidationError: If an input has the wrong type or format.
    """

    resolved_root = repo_root.resolve()
    try:
        task_paths = load_task_paths(resolved_root, task_id)
        checker_evidence, checker_evidence_path = load_checker_evidence(
            resolved_root, task_paths, attempt
        )
    except (OSError, ValueError) as exc:
        return GateResult(
            gate="before-complete",
            status="fail",
            reason=f"Checker evidence is invalid: {exc}",
        )

    evidence_paths = [checker_evidence_path]
    if (
        checker_evidence.result.status != "reviewed"
        or checker_evidence.result.verdict != "pass"
    ):
        return GateResult(
            gate="before-complete",
            status="fail",
            reason="The current candidate does not have a passing checker review.",
            evidence_paths=evidence_paths,
        )

    try:
        results, results_evidence_path = load_validation_evidence(
            resolved_root, task_paths
        )
        evidence_paths.append(results_evidence_path)
    except (OSError, ValueError) as exc:
        return GateResult(
            gate="before-complete",
            status="fail",
            reason=f"Validation evidence is invalid: {exc}",
            evidence_paths=evidence_paths,
        )

    if not results.runs:
        return GateResult(
            gate="before-complete",
            status="fail",
            reason="Validation evidence has no candidate results.",
            evidence_paths=evidence_paths,
        )
    latest_record = results.runs[-1]
    if latest_record.candidate_attempt != attempt:
        return GateResult(
            gate="before-complete",
            status="fail",
            reason="The latest validation result does not match the current candidate.",
            evidence_paths=evidence_paths,
        )
    if latest_record.status != "pass":
        return GateResult(
            gate="before-complete",
            status="fail",
            reason="The latest validation result did not pass.",
            evidence_paths=evidence_paths,
        )

    return GateResult(
        gate="before-complete",
        status="pass",
        reason=(
            "The latest candidate has matching passing review and validation evidence."
        ),
        evidence_paths=evidence_paths,
    )
