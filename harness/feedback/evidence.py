"""Save and load checker and test evidence."""

from pathlib import Path

from harness.contracts.core import CandidateAttempt, TaskPaths
from harness.contracts.models import CheckerEvidence
from harness.contracts.results import CheckReport, ConditionReport
from harness.contracts.validation import ValidationResultsDocument
from harness.feedback.changes import load_approved_plan
from harness.utils.artifact_paths import require_safe_artifact_path
from harness.utils.atomic import write_bytes


def save_checker_evidence(
    repo_root: Path,
    task_paths: TaskPaths,
    evidence: CheckerEvidence,
) -> str:
    """Save one completed checker review as typed evidence.

    The supervisor calls this after accepting a checker result. The later checker and completion
    gates read the same file, so the write is completed before the workflow moves to validation.
    An identical retry reuses the saved review. A retry with different content is rejected so it
    cannot replace the review that the workflow already accepted.

    :param repo_root: Repository containing the workflow run.
    :param task_paths: Paths for the current task.
    :param evidence: Checked review result to save.
    :return: Repository-relative path to the saved review.
    :raises ValueError: If the evidence belongs to another task or conflicts with a saved review.
    :raises OSError: If the review cannot be read or written.
    """

    if evidence.task_id != task_paths.task_id:
        raise ValueError("checker evidence task ID must match the current task")
    resolved_root = repo_root.resolve()
    review_path = task_paths.checker_evidence(evidence.attempt)
    review_path = require_safe_artifact_path(
        resolved_root, review_path, label="checker evidence"
    )
    relative_path = review_path.relative_to(resolved_root).as_posix()
    if review_path.exists():
        if not review_path.is_file():
            raise ValueError("saved checker evidence must be a regular file")
        existing = CheckerEvidence.model_validate_json(
            review_path.read_text(encoding="utf-8")
        )
        if existing != evidence:
            raise ValueError(
                "checker evidence has already been recorded with different content"
            )
        return relative_path

    review_path.parent.mkdir(parents=True, exist_ok=True)
    write_bytes(review_path, (evidence.model_dump_json(indent=2) + "\n").encode("utf-8"))
    return relative_path


def validated_evidence_path(repo_root: Path, path: Path) -> str:
    """Check an evidence file and return its repository-relative path.

    The maker gate uses this to check required design files. Evidence loaders use it before reading
    checker reviews or test results. It makes sure the file exists inside the repository and cannot
    be redirected through a symlink.

    :param repo_root: Repository that owns the evidence.
    :param path: Evidence file to check.
    :return: Safe path relative to the repository.
    :raises ValueError: If the file is missing, unsafe, or not a regular file.
    """

    resolved_root = repo_root.resolve()
    safe_path = require_safe_artifact_path(resolved_root, path, label="evidence file")
    if not safe_path.is_file():
        raise ValueError(f"required evidence file is missing: {safe_path}")
    return safe_path.relative_to(resolved_root).as_posix()


def load_checker_evidence(
    repo_root: Path,
    task_paths: TaskPaths,
    attempt: CandidateAttempt,
) -> tuple[CheckerEvidence, str]:
    """Load the checker review for one candidate.

    The checker and completion gates call this after a checker finishes. Matching the task and
    candidate keeps an old or misplaced review from moving the workflow.

    :param repo_root: Repository containing the run.
    :param task_paths: Paths for the current task.
    :param attempt: Candidate whose review is needed.
    :return: Checked review data and its repository-relative path.
    :raises OSError: If the review cannot be read.
    :raises ValueError: If the review is invalid or belongs elsewhere.
    """

    checker_evidence_path = task_paths.checker_evidence(attempt)
    evidence_path = validated_evidence_path(repo_root, checker_evidence_path)
    checker_evidence = CheckerEvidence.model_validate_json(
        checker_evidence_path.read_text(encoding="utf-8")
    )
    if checker_evidence.task_id != task_paths.task_id:
        raise ValueError(
            f"checker evidence task ID {checker_evidence.task_id!r} does not match "
            f"{task_paths.task_id!r}"
        )
    if checker_evidence.attempt != attempt:
        raise ValueError(
            f"checker evidence attempt {checker_evidence.attempt} does not match {attempt}"
        )
    return checker_evidence, evidence_path


def load_validation_evidence(
    repo_root: Path,
    task_paths: TaskPaths,
) -> tuple[ValidationResultsDocument, str]:
    """Load the fixed test results for the current task.

    The completion gate calls this after tests run. The checks stop results for another task, a
    skipped candidate, or damaged JSON from completing the workflow.

    :param repo_root: Repository containing the run.
    :param task_paths: Paths for the current task.
    :return: Checked test results and their repository-relative path.
    :raises OSError: If the results cannot be read.
    :raises ValueError: If the results are invalid or belong to another task.
    """

    results_path = task_paths.validation_results
    evidence_path = validated_evidence_path(repo_root, results_path)
    results = ValidationResultsDocument.model_validate_json(
        results_path.read_text(encoding="utf-8")
    )
    if results.task_id != task_paths.task_id:
        raise ValueError(
            f"validation evidence task ID {results.task_id!r} does not match "
            f"{task_paths.task_id!r}"
        )
    return results, evidence_path


def build_report(
    repo_root: Path,
    task_paths: TaskPaths,
    attempt: CandidateAttempt,
) -> tuple[tuple[ConditionReport, ...], tuple[CheckReport, ...]]:
    """Map each criterion and saved check to its verification and result.

    The supervisor calls this when a run completes. A criterion has the test-suite entry unless
    it has a manual review.
    A criterion with a manual review has only the manual-review entry, so a passing suite is not
    shown as proof of it. That entry reports the checker's observation, or ``pending`` when the
    checker recorded none.

    :param repo_root: Repository containing the run.
    :param task_paths: Paths for the current task.
    :param attempt: Candidate that completed.
    :return: One entry per criterion and method, and one entry per saved check.
    :raises OSError: If saved evidence cannot be read.
    :raises ValueError: If the approved plan is missing, or saved evidence is invalid or has no
        result for the candidate.
    """

    plan = load_approved_plan(repo_root, task_paths)
    checker, _ = load_checker_evidence(repo_root, task_paths, attempt)
    results, _ = load_validation_evidence(repo_root, task_paths)
    records = [run for run in results.runs if run.candidate_attempt == attempt]
    if not records:
        raise ValueError(f"no validation result for candidate {attempt}")
    record = records[-1]

    manually_reviewed = {review.acceptance_id for review in plan.manual_reviews}
    conditions = [
        ConditionReport(
            acceptance_id=acceptance_id,
            method="tests",
            result=record.result.status,
            note=f"{intent} (result of the whole suite; tests are not mapped to criteria)",
        )
        for acceptance_id, intent in sorted(plan.test_intents.items())
        if acceptance_id not in manually_reviewed
    ]
    observed = {}
    if checker.result.status == "reviewed":
        observed = {o.acceptance_id: o for o in checker.result.observations}
    for review in plan.manual_reviews:
        observation = observed.get(review.acceptance_id)
        conditions.append(
            ConditionReport(
                acceptance_id=review.acceptance_id,
                method="manual-review",
                result=observation.outcome if observation else "pending",
                note=observation.note
                if observation
                else f"Not verified. Inspect: {review.inspect}. Pass if: {review.pass_if}",
            )
        )
    conditions.sort(key=lambda c: c.acceptance_id)
    checks = tuple(
        CheckReport(name=c.name, required=c.required, result=c.result.status)
        for c in record.checks
    )
    return tuple(conditions), checks
