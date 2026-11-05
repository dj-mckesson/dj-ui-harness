"""Contracts for results returned by agents and humans."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, StrictBool, model_validator

from harness.contracts.actions import (
    ActionId,
    ArtifactName,
    PositiveRevision,
    ResolvedAnswer,
)
from harness.contracts.core import (
    CandidateAttempt,
    NonEmptyStr,
    RepoPath,
    StrictModel,
    TaskId,
)
from harness.contracts.models import CheckerOutcome
from harness.contracts.plan import AcceptanceCriterionId


class QuestionResponse(StrictModel):
    """Return human answers for one saved question action.

    The supervisor accepts this only when its task, action, and artifact match the current pending
    question.

    :ivar kind: Fixed value identifying a question response.
    :ivar task_id: Task that asked the questions.
    :ivar action_id: Saved question action being answered.
    :ivar artifact: Artifact whose author needs the answers.
    :ivar answers: Question and answer pairs supplied by the human.
    """

    kind: Literal["question-response"] = "question-response"
    task_id: TaskId
    action_id: ActionId
    artifact: ArtifactName
    answers: tuple[ResolvedAnswer, ...] = Field(min_length=1)


class ApprovalResponse(StrictModel):
    """Return a strict human approval decision for one revision.

    The supervisor compares this with the saved approval action before changing state. A rejection
    must explain what the author should change on the next attempt.

    :ivar kind: Fixed value identifying an approval response.
    :ivar task_id: Task whose artifact was reviewed.
    :ivar action_id: Saved approval action being answered.
    :ivar artifact: Artifact that was reviewed.
    :ivar revision: Exact revision that was reviewed.
    :ivar approved: Strict true or false decision.
    :ivar feedback: Required correction guidance when approval is false.
    :ivar confirm_whole_repository: Set to true when approving a plan whose scope covers the
        whole repository. The supervisor rejects that approval without this confirmation.
    """

    kind: Literal["approval-response"] = "approval-response"
    task_id: TaskId
    action_id: ActionId
    artifact: ArtifactName
    revision: PositiveRevision
    approved: StrictBool
    feedback: NonEmptyStr | None = None
    confirm_whole_repository: StrictBool = False

    @model_validator(mode="after")
    def rejection_has_feedback(self) -> ApprovalResponse:
        """Require useful feedback for a rejected spec or plan.

        :return: The checked approval response.
        :raises ValueError: If a rejection has no feedback.
        """

        if not self.approved and self.feedback is None:
            raise ValueError("a rejected artifact requires feedback")
        return self


class AuthorResult(StrictModel):
    """Return the result of a spec-author or planner action.

    Both author roles use the same result shape. The supervisor knows which role produced it from
    the saved action, so the result only repeats the task and action IDs.

    :ivar task_id: Task that owns the saved action.
    :ivar action_id: Saved author action that produced the result.
    :ivar status: Whether the author has questions, a ready artifact, or a blocker.
    :ivar files_written: Exact artifact path written for a ready result.
    :ivar questions: Material questions for a question result.
    :ivar blockers: Reasons the author could not continue.
    """

    task_id: TaskId
    action_id: ActionId
    status: Literal["questions", "ready_for_approval", "blocked"]
    files_written: tuple[RepoPath, ...]
    questions: tuple[NonEmptyStr, ...]
    blockers: tuple[NonEmptyStr, ...]

    @model_validator(mode="after")
    def fields_match_status(self) -> AuthorResult:
        """Check that an author result describes exactly one outcome.

        :return: The checked author result.
        :raises ValueError: If files, questions, or blockers conflict with the status.
        """

        if self.status == "questions":
            if self.files_written or not self.questions or self.blockers:
                raise ValueError("a question result requires only non-empty questions")
        elif self.status == "ready_for_approval":
            if len(self.files_written) != 1 or self.questions or self.blockers:
                raise ValueError("a ready result requires one written artifact only")
        elif self.files_written or self.questions or not self.blockers:
            raise ValueError("a blocked author result requires only non-empty blockers")
        return self


class MakerResult(StrictModel):
    """Return the result of one implementation or repair action.

    The maker reports changed paths and a short summary, but not authoritative test evidence. The
    supervisor runs the fixed validation command after the checker finishes.

    :ivar task_id: Task that owns the saved action.
    :ivar action_id: Saved maker or repair action that produced the result.
    :ivar status: Whether the candidate is ready for review or blocked.
    :ivar files_changed: Approved work paths changed by the agent.
    :ivar summary: Short description of the completed or partial work.
    :ivar blockers: Reasons the agent could not produce a reviewable candidate.
    """

    task_id: TaskId
    action_id: ActionId
    status: Literal["implemented", "blocked"]
    files_changed: tuple[RepoPath, ...]
    summary: str
    blockers: tuple[NonEmptyStr, ...]

    @model_validator(mode="after")
    def fields_match_status(self) -> MakerResult:
        """Check that maker fields match implemented or blocked status.

        :return: The checked maker result.
        :raises ValueError: If the summary or blockers conflict with the status.
        """

        if self.status == "implemented":
            if not self.summary.strip() or self.blockers:
                raise ValueError(
                    "an implemented result requires a summary and no blockers"
                )
        elif not self.blockers:
            raise ValueError("a blocked maker result requires blockers")
        if self.files_changed and not self.summary.strip():
            raise ValueError("changed files require a summary")
        if len(self.files_changed) != len(set(self.files_changed)):
            raise ValueError("changed files must not contain duplicates")
        return self


class CheckerResult(StrictModel):
    """Return the existing checker outcome for one saved checker action.

    The supervisor wraps this outcome with task and candidate details when it saves checker
    evidence. The checker remains read-only and does not create validation evidence.

    :ivar task_id: Task that owns the saved checker action.
    :ivar action_id: Saved checker action that produced the result.
    :ivar result: Existing typed checker review outcome.
    """

    task_id: TaskId
    action_id: ActionId
    result: CheckerOutcome


class ConditionReport(StrictModel):
    """Show how one acceptance criterion was verified, and what that showed.

    :ivar acceptance_id: Acceptance criterion.
    :ivar method: ``tests`` for the mandatory test suite, or ``manual-review``.
    :ivar result: ``pending`` means that nobody has verified the criterion yet.
    :ivar note: The planned test intent or the observation, with any limit.
    """

    acceptance_id: AcceptanceCriterionId
    method: Literal["tests", "manual-review"]
    result: Literal["pass", "fail", "pending"]
    note: NonEmptyStr


class CheckReport(StrictModel):
    """Show the saved outcome of one supervisor check.

    :ivar name: Name of the check.
    :ivar required: Whether a failure blocked completion.
    :ivar result: ``pass``, ``fail``, or ``skipped``.
    """

    name: NonEmptyStr
    required: StrictBool
    result: Literal["pass", "fail", "skipped"]


class CompletionEvidence(StrictModel):
    """Point to the matching evidence that allowed workflow completion.

    The supervisor stores this only after the final completion gate passes. Both paths belong to
    the same candidate attempt recorded here.

    :ivar candidate_attempt: Candidate that passed review and validation.
    :ivar checker_evidence_path: Saved checker evidence for that candidate.
    :ivar validation_evidence_path: Saved fixed-command results for the task.
    :ivar changes_evidence_path: Saved record of the actual added, changed, and deleted files after
        the checks.
    :ivar conditions: How each acceptance criterion was verified. Runs completed before this
        report existed have none.
    :ivar checks: Saved outcomes of mandatory and plan-selected checks.
    """

    candidate_attempt: CandidateAttempt
    checker_evidence_path: RepoPath
    validation_evidence_path: RepoPath
    changes_evidence_path: RepoPath | None = None
    conditions: tuple[ConditionReport, ...] = ()
    checks: tuple[CheckReport, ...] = ()


class BlockedOutput(StrictModel):
    """Return a stable terminal result for a blocked workflow.

    The supervisor can return this on every later workflow call without changing state.

    :ivar kind: Fixed terminal output type.
    :ivar task_id: Blocked task.
    :ivar reason: Saved reason that work cannot continue.
    """

    kind: Literal["blocked"] = "blocked"
    task_id: TaskId
    reason: NonEmptyStr


class CompleteOutput(StrictModel):
    """Return a stable terminal result for a completed workflow.

    The supervisor returns the saved evidence paths so the caller can inspect why completion was
    allowed.

    :ivar kind: Fixed terminal output type.
    :ivar task_id: Completed task.
    :ivar evidence: Matching checker and validation evidence.
    """

    kind: Literal["complete"] = "complete"
    task_id: TaskId
    evidence: CompletionEvidence


class ErrorResponse(StrictModel):
    """Describe one simple error returned by the JSON CLI.

    The CLI uses this for invalid input or invalid saved state. It contains no routing details
    and does not change the workflow.

    :ivar code: Short stable name for the error type.
    :ivar message: Plain explanation that a learner can act on.
    """

    code: NonEmptyStr
    message: NonEmptyStr
