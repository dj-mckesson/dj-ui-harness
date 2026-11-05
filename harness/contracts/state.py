"""Contract for saved supervisor workflow state."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StrictBool, model_validator

from harness.contracts.actions import (
    AgentAction,
    ApprovalAction,
    CandidateContext,
    CheckerContext,
    PendingAction,
    PlannerContext,
    QuestionAction,
    ResolvedAnswer,
    SpecAuthorContext,
)
from harness.contracts.core import (
    MAX_CANDIDATE_ATTEMPTS,
    CandidateAttempt,
    NonEmptyStr,
    Sha256Digest,
    StrictModel,
    TaskId,
    ValidationRound,
)
from harness.contracts.results import CompletionEvidence

Revision = Annotated[int, Field(strict=True, ge=0)]
WorkflowStage = Literal[
    "spec-authoring",
    "plan-authoring",
    "implementation",
    "review",
    "validation",
    "change-approval",
    "repair",
    "blocked",
    "complete",
]


class RunState(StrictModel):
    """Store the small amount of state needed to resume one workflow.

    Every supervisor process loads this before deciding what to do. It contains one pending
    external action at most, current approvals, candidate progress, and terminal evidence.

    :ivar task_id: Numeric task directory name.
    :ivar original_request: Coding request supplied when the run started.
    :ivar stage: Current point in the closed workflow.
    :ivar spec_revision: Current specification revision, or zero before one is ready.
    :ivar spec_approved: Whether the current specification revision is approved.
    :ivar spec_digest: SHA-256 digest of the approved ``SPEC.md``. Set only when the spec is
        approved. Plan approval compares the current spec with it.
    :ivar plan_revision: Current plan revision, or zero before one is ready.
    :ivar plan_approved: Whether the current plan revision is approved.
    :ivar resolved_spec_answers: Human answers collected for spec authoring.
    :ivar resolved_plan_answers: Human answers collected for plan authoring.
    :ivar rejection_feedback: Current feedback for the next author attempt.
    :ivar candidate_attempt: Current implementation candidate, when implementation has started.
    :ivar validation_round: Validation round of the current candidate. Each approved change after
        the checks passed starts a new round.
    :ivar pending_action: One saved agent or human action, or none during internal validation.
    :ivar blocked_reason: Reason for a blocked terminal run.
    :ivar completion_evidence: Matching evidence for a complete terminal run.
    """

    task_id: TaskId
    original_request: NonEmptyStr
    stage: WorkflowStage
    spec_revision: Revision = 0
    spec_approved: StrictBool = False
    spec_digest: Sha256Digest | None = None
    plan_revision: Revision = 0
    plan_approved: StrictBool = False
    resolved_spec_answers: tuple[ResolvedAnswer, ...] = ()
    resolved_plan_answers: tuple[ResolvedAnswer, ...] = ()
    rejection_feedback: NonEmptyStr | None = None
    candidate_attempt: CandidateAttempt | None = None
    validation_round: ValidationRound = 1
    pending_action: PendingAction | None
    blocked_reason: NonEmptyStr | None = None
    completion_evidence: CompletionEvidence | None = None

    @model_validator(mode="after")
    def workflow_state_is_consistent(self) -> RunState:
        """Reject state combinations that cannot occur in the workflow.

        Pydantic calls this after creating or loading ``RunState``. The checks keep approvals,
        candidates, terminal evidence, and the saved action aligned with the current stage.

        :return: The checked run state.
        :raises ValueError: If fields describe conflicting workflow states.
        """

        if self.spec_approved and self.spec_revision == 0:
            raise ValueError("spec approval requires a current spec revision")
        if self.spec_approved != (self.spec_digest is not None):
            raise ValueError("spec_digest must be set exactly when the spec is approved")
        if self.plan_approved and self.plan_revision == 0:
            raise ValueError("plan approval requires a current plan revision")
        if self.plan_revision > 0 and self.spec_revision == 0:
            raise ValueError("a plan revision requires a spec revision")
        if self.plan_approved and not self.spec_approved:
            raise ValueError("plan approval requires spec approval")
        if (
            self.pending_action is not None
            and self.pending_action.task_id != self.task_id
        ):
            raise ValueError("pending action task ID must match run state")

        waiting_stages = {
            "spec-authoring",
            "plan-authoring",
            "implementation",
            "review",
            "repair",
            "change-approval",
        }
        if self.stage in waiting_stages and self.pending_action is None:
            raise ValueError("the current stage requires one pending external action")
        if (
            self.stage in {"validation", "blocked", "complete"}
            and self.pending_action is not None
        ):
            raise ValueError("this stage must not have a pending external action")

        self._check_stage_action()
        self._check_action_context()
        self._check_stage_progress()
        self._check_terminal_fields()
        return self

    def _check_stage_action(self) -> None:
        """Check that the pending action belongs to the current stage.

        :raises ValueError: If the saved action and stage do not agree.
        """

        action = self.pending_action
        if action is None:
            return
        if self.stage == "spec-authoring":
            valid = (
                isinstance(action, AgentAction) and action.role == "spec-author"
            ) or (
                isinstance(action, (QuestionAction, ApprovalAction))
                and action.artifact == "spec"
                and (
                    not isinstance(action, ApprovalAction)
                    or action.revision == self.spec_revision
                )
            )
        elif self.stage == "plan-authoring":
            valid = (isinstance(action, AgentAction) and action.role == "planner") or (
                isinstance(action, (QuestionAction, ApprovalAction))
                and action.artifact == "plan"
                and (
                    not isinstance(action, ApprovalAction)
                    or action.revision == self.plan_revision
                )
            )
        elif self.stage == "implementation":
            valid = isinstance(action, AgentAction) and action.role == "maker"
        elif self.stage == "repair":
            valid = isinstance(action, AgentAction) and action.role == "repair"
        elif self.stage == "change-approval":
            valid = (
                isinstance(action, ApprovalAction)
                and action.artifact == "changes"
                and action.revision == self.candidate_attempt
            )
        else:
            valid = (
                self.stage == "review"
                and isinstance(action, AgentAction)
                and action.role == "checker"
                and action.candidate_attempt == self.candidate_attempt
            )
        if not valid:
            raise ValueError("pending action does not match the current stage")

    def _check_action_context(self) -> None:
        """Check that saved agent input matches the current run state.

        The supervisor stores request and revision values in both the run and current action.
        Comparing them prevents an old action from running after the workflow changes.

        :raises ValueError: If agent input contains a stale request or revision.
        """

        action = self.pending_action
        if not isinstance(action, AgentAction):
            return
        context = action.context
        if isinstance(context, SpecAuthorContext):
            if context.original_request != self.original_request:
                raise ValueError("spec action request must match run state")
            return
        if context.spec_revision != self.spec_revision:
            raise ValueError("agent spec revision must match run state")
        if isinstance(context, PlannerContext):
            return
        if isinstance(context, (CandidateContext, CheckerContext)):
            if context.plan_revision != self.plan_revision:
                raise ValueError("agent plan revision must match run state")

    def _check_stage_progress(self) -> None:
        """Check approvals and candidate numbers for the current stage.

        :raises ValueError: If the run has moved farther than its approvals or candidate allow.
        """

        pre_implementation = {
            "spec-authoring",
            "plan-authoring",
        }
        if self.stage in pre_implementation and self.candidate_attempt is not None:
            raise ValueError("candidate attempt must be empty before implementation")
        if (
            self.stage in {*pre_implementation, "implementation", "review", "repair"}
            and self.validation_round != 1
        ):
            raise ValueError("a candidate starts at validation round 1")
        if self.stage == "spec-authoring":
            if self.spec_approved or self.plan_approved:
                raise ValueError("spec authoring requires unapproved current intent")
            if (
                isinstance(self.pending_action, ApprovalAction)
                and self.spec_revision == 0
            ):
                raise ValueError("spec approval requires a positive revision")
        if self.stage == "plan-authoring":
            if not self.spec_approved or self.plan_approved:
                raise ValueError(
                    "plan work requires an approved spec and unapproved plan"
                )
            if (
                isinstance(self.pending_action, ApprovalAction)
                and self.plan_revision == 0
            ):
                raise ValueError("plan approval requires a positive revision")

        candidate_stages = {
            "implementation",
            "review",
            "validation",
            "change-approval",
            "repair",
            "complete",
        }
        if self.stage in candidate_stages and not (
            self.spec_approved and self.plan_approved
        ):
            raise ValueError("candidate work requires approved spec and plan revisions")
        if self.candidate_attempt is not None and not (
            self.spec_revision > 0
            and self.plan_revision > 0
            and self.spec_approved
            and self.plan_approved
        ):
            raise ValueError(
                "a saved candidate requires approved spec and plan revisions"
            )
        if self.stage == "implementation" and self.candidate_attempt != 1:
            raise ValueError("implementation stage requires candidate 1")
        if self.stage == "repair" and self.candidate_attempt != MAX_CANDIDATE_ATTEMPTS:
            raise ValueError("repair stage requires candidate 2")
        if self.stage in {
            "review",
            "validation",
            "change-approval",
            "complete",
        } and self.candidate_attempt not in range(1, MAX_CANDIDATE_ATTEMPTS + 1):
            raise ValueError("this stage requires candidate 1 or 2")

    def _check_terminal_fields(self) -> None:
        """Check blocked and completion details for terminal and active runs.

        :raises ValueError: If terminal evidence appears in the wrong stage or is incomplete.
        """

        if self.stage == "blocked":
            if self.blocked_reason is None or self.completion_evidence is not None:
                raise ValueError("blocked state requires only a blocked reason")
            return
        if self.stage == "complete":
            if self.blocked_reason is not None or self.completion_evidence is None:
                raise ValueError("complete state requires only completion evidence")
            if self.completion_evidence.candidate_attempt != self.candidate_attempt:
                raise ValueError("completion evidence must match the current candidate")
            expected_checker = (
                f"runs/{self.task_id}/reviews/checker-{self.candidate_attempt}.json"
            )
            expected_validation = f"runs/{self.task_id}/validation/results.json"
            if self.completion_evidence.checker_evidence_path != expected_checker:
                raise ValueError("completion must point to current checker evidence")
            if self.completion_evidence.validation_evidence_path != expected_validation:
                raise ValueError("completion must point to current validation evidence")
            expected_changes = (
                f"runs/{self.task_id}/changes/checks-{self.candidate_attempt}.json"
            )
            changes_path = self.completion_evidence.changes_evidence_path
            if changes_path is not None and changes_path != expected_changes:
                raise ValueError("completion must point to current changed-file evidence")
            return
        if self.blocked_reason is not None or self.completion_evidence is not None:
            raise ValueError("active state must not contain terminal details")
