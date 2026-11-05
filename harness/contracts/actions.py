"""Contracts for work given to agents and humans."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from harness.contracts.changes import FileDrift
from harness.contracts.core import (
    MAX_CANDIDATE_ATTEMPTS,
    CandidateAttempt,
    NonEmptyStr,
    RepoPath,
    Sha256Digest,
    StrictModel,
    TaskId,
)

ActionId = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^[0-9a-f]{32}$"),
]
PositiveRevision = Annotated[int, Field(strict=True, ge=1)]
ArtifactName = Literal["spec", "plan", "changes"]


class ResolvedAnswer(StrictModel):
    """Store one human answer for a later author action.

    The supervisor saves these pairs after a spec or plan question. The next matching author
    receives them, so restarting the workflow does not lose the human decision.

    :ivar question: Question that the agent asked.
    :ivar answer: Human answer to that question.
    """

    question: NonEmptyStr
    answer: NonEmptyStr


class SpecAuthorContext(StrictModel):
    """Hold the non-file input needed to write the specification.

    The spec action builder creates this at the start of the workflow and after a spec question or
    rejection. Repository paths remain on the surrounding action.

    :ivar context_type: Fixed value that identifies spec-author input.
    :ivar original_request: Coding request that the specification must describe.
    :ivar resolved_answers: Human answers collected during spec authoring.
    :ivar rejection_feedback: Feedback from the most recent rejected spec, when present.
    """

    context_type: Literal["spec-author"] = "spec-author"
    original_request: NonEmptyStr
    resolved_answers: tuple[ResolvedAnswer, ...] = ()
    rejection_feedback: NonEmptyStr | None = None


class PlannerContext(StrictModel):
    """Hold the non-file input needed to write the implementation plan.

    The planner receives this only after the current specification is approved. It records the
    approved spec revision and the repository root, which bounds where work may be planned.

    :ivar context_type: Fixed value that identifies planner input.
    :ivar spec_revision: Approved specification revision used by the plan.
    :ivar permitted_work_paths: Repository root. Plan work items may name files and folders below
        it, but not supervisor paths, and protected files must be named individually.
    :ivar resolved_answers: Human answers collected during plan authoring.
    :ivar rejection_feedback: Feedback from the most recent rejected plan, when present.
    """

    context_type: Literal["planner"] = "planner"
    spec_revision: PositiveRevision
    permitted_work_paths: tuple[RepoPath, ...] = Field(min_length=1)
    resolved_answers: tuple[ResolvedAnswer, ...] = ()
    rejection_feedback: NonEmptyStr | None = None


class CandidateContext(StrictModel):
    """Identify the approved revisions used by a maker or repair action.

    Candidate 1 and candidate 2 use the same approved spec and plan. Their different candidate
    numbers and writable paths remain on the surrounding action.

    :ivar context_type: Fixed value that identifies candidate input.
    :ivar spec_revision: Approved specification revision.
    :ivar plan_revision: Approved plan revision.
    """

    context_type: Literal["candidate"] = "candidate"
    spec_revision: PositiveRevision
    plan_revision: PositiveRevision


class CheckerContext(StrictModel):
    """Hold the non-file input needed for an independent review.

    The checker action builder creates this after a maker or repair result is accepted. The checker
    uses the summary for orientation and reads the supplied files for evidence.

    :ivar context_type: Fixed value that identifies checker input.
    :ivar spec_revision: Approved specification revision.
    :ivar plan_revision: Approved plan revision.
    :ivar candidate_summary: Short description returned by the maker or repair agent.
    """

    context_type: Literal["checker"] = "checker"
    spec_revision: PositiveRevision
    plan_revision: PositiveRevision
    candidate_summary: NonEmptyStr


AgentContext = Annotated[
    SpecAuthorContext | PlannerContext | CandidateContext | CheckerContext,
    Field(discriminator="context_type"),
]


class AgentAction(StrictModel):
    """Describe one exact agent call chosen by the supervisor.

    The ``harness-run`` skill passes this saved action to a fresh agent. Python chooses the role,
    identity, input paths, and writable paths instead of asking the skill to reconstruct them.

    :ivar kind: Fixed value used by the pending-action union.
    :ivar action_id: Unique ID that a result must return.
    :ivar task_id: Task that owns this action.
    :ivar role: Workflow role that should perform the action.
    :ivar persona_id: Persona the ``harness-run`` skill should start.
    :ivar skill_id: Skill that the persona should use.
    :ivar candidate_attempt: Candidate number for maker, repair, and checker actions.
    :ivar required_input_paths: Exact repository paths the agent should read.
    :ivar writable_paths: Exact repository files the agent may change.
    :ivar writable_folders: Repository folders whose contents the agent may change. ``.`` covers
        the whole repository except supervisor paths and protected files.
    :ivar context: Small role-specific values that are not stored in repository files.
    """

    kind: Literal["agent"] = "agent"
    action_id: ActionId
    task_id: TaskId
    role: Literal["spec-author", "planner", "maker", "repair", "checker"]
    persona_id: Literal["spec-author", "planner", "maker", "checker"]
    skill_id: Literal["spec", "planner", "maker", "checker"]
    candidate_attempt: CandidateAttempt | None
    required_input_paths: tuple[RepoPath, ...]
    writable_paths: tuple[RepoPath, ...]
    writable_folders: tuple[RepoPath, ...] = ()
    context: AgentContext

    @model_validator(mode="after")
    def identity_matches_role(self) -> AgentAction:
        """Reject identity or scope combinations that cannot run.

        Pydantic calls this whenever an action is built or loaded from ``state.json``. This keeps a
        malformed action from launching the wrong persona or receiving the wrong write scope.

        :return: The checked agent action.
        :raises ValueError: If the fields do not describe one supported role.
        """

        expected = {
            "spec-author": ("spec-author", "spec", None, "spec-author"),
            "planner": ("planner", "planner", None, "planner"),
            "maker": ("maker", "maker", 1, "candidate"),
            "repair": ("maker", "maker", MAX_CANDIDATE_ATTEMPTS, "candidate"),
            "checker": ("checker", "checker", self.candidate_attempt, "checker"),
        }
        actual = (
            self.persona_id,
            self.skill_id,
            self.candidate_attempt,
            self.context.context_type,
        )
        if actual != expected[self.role]:
            raise ValueError("agent action identity does not match its role")
        if self.role == "checker" and self.candidate_attempt not in range(
            1, MAX_CANDIDATE_ATTEMPTS + 1
        ):
            raise ValueError("a checker action requires candidate 1 or 2")

        if len(self.required_input_paths) != len(set(self.required_input_paths)):
            raise ValueError("agent input paths must not contain duplicates")
        if len(self.writable_paths) != len(set(self.writable_paths)):
            raise ValueError("agent writable paths must not contain duplicates")

        expected_output = {
            "spec-author": f"planning/{self.task_id}/SPEC.md",
            "planner": f"planning/{self.task_id}/plan.json",
        }
        if len(self.writable_folders) != len(set(self.writable_folders)):
            raise ValueError("agent writable folders must not contain duplicates")
        if self.role in expected_output and (
            self.writable_paths != (expected_output[self.role],) or self.writable_folders
        ):
            raise ValueError("author action must have its one exact artifact output path")
        if self.role in {"maker", "repair"} and not (
            self.writable_paths or self.writable_folders
        ):
            raise ValueError("maker and repair actions require writable scopes")
        if self.role == "checker" and (self.writable_paths or self.writable_folders):
            raise ValueError("checker actions must be read-only")
        return self


class QuestionAction(StrictModel):
    """Pause authoring until a human answers material questions.

    The supervisor saves this when the spec author or planner needs a decision. Repeated workflow
    calls return the same action until a matching response is recorded.

    :ivar kind: Fixed value used by the pending-action union.
    :ivar action_id: Unique ID that the response must return.
    :ivar task_id: Task waiting for an answer.
    :ivar artifact: Artifact whose author asked the questions.
    :ivar questions: Material questions shown to the human.
    """

    kind: Literal["question"] = "question"
    action_id: ActionId
    task_id: TaskId
    artifact: ArtifactName
    questions: tuple[NonEmptyStr, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def questions_are_unique(self) -> QuestionAction:
        """Reject a question action that repeats the same prompt.

        :return: The checked question action.
        :raises ValueError: If a question is repeated.
        """

        if len(self.questions) != len(set(self.questions)):
            raise ValueError("question action must not contain duplicate questions")
        return self


class ApprovalAction(StrictModel):
    """Pause the workflow for human review of one artifact revision.

    Spec approval points to ``SPEC.md``. Plan approval points to authoritative ``plan.json`` and
    readable ``PLAN.md``. Review details stay in those artifacts instead of being copied here.

    :ivar kind: Fixed value used by the pending-action union.
    :ivar action_id: Unique ID that the response must return.
    :ivar task_id: Task waiting for approval.
    :ivar artifact: Artifact being reviewed.
    :ivar revision: Current revision being reviewed.
    :ivar review_paths: Exact repository paths shown to the human.
    :ivar review_digests: For a spec or plan, the SHA-256 digest of each review path when the
        action was built. The approval applies only to this content. Empty for changes.
    :ivar notice: For a spec or plan, why this approval replaced an earlier one, when it did.
    :ivar planned_files: Files the plan allows an agent to change. Empty for a spec.
    :ivar planned_folders: Folders the plan allows an agent to change. Empty for a spec.
    :ivar harness_paths: Protected files. For a plan these are planned; for
        changes these actually changed. The human should confirm that the task needs each one.
    :ivar harness_intents: For changes, what the approved plan said each changed protected path is
        for. The human compares each change with its stated purpose.
    :ivar drift: For changes, each file that changed after the checks passed. The harness
        validated the current files again before it asked.
    :ivar whole_repository: Whether the plan covers the whole repository with ``.``. Approval
        must then confirm that scope explicitly.
    """

    kind: Literal["approval"] = "approval"
    action_id: ActionId
    task_id: TaskId
    artifact: ArtifactName
    revision: PositiveRevision
    review_paths: tuple[RepoPath, ...] = Field(min_length=1)
    review_digests: dict[RepoPath, Sha256Digest] = Field(default_factory=dict)
    notice: NonEmptyStr | None = None
    planned_files: tuple[RepoPath, ...] = ()
    planned_folders: tuple[RepoPath, ...] = ()
    harness_paths: tuple[RepoPath, ...] = ()
    whole_repository: bool = False
    harness_intents: dict[RepoPath, str] = Field(default_factory=dict)
    drift: tuple[FileDrift, ...] = ()

    @model_validator(mode="after")
    def paths_match_artifact(self) -> ApprovalAction:
        """Check that the review paths belong to the selected artifact.

        :return: The checked approval action.
        :raises ValueError: If the action points to the wrong review files.
        """

        if self.artifact == "changes":
            if self.review_paths != (
                f"runs/{self.task_id}/changes/checks-{self.revision}.json",
            ):
                raise ValueError(
                    "changes approval must review the saved changed-file record"
                )
            if not (self.harness_paths or self.drift):
                raise ValueError(
                    "changes approval requires changed protected paths or drift"
                )
            if set(self.harness_intents) != set(self.harness_paths):
                raise ValueError(
                    "changes approval needs a planned intent for each protected path"
                )
            if self.planned_files or self.planned_folders or self.whole_repository:
                raise ValueError("changes approval must not carry write scopes")
            if self.review_digests:
                raise ValueError("changes approval must not carry review digests")
            if self.notice is not None:
                raise ValueError("changes approval must not carry a notice")
            return self

        spec_path = f"planning/{self.task_id}/SPEC.md"
        plan_paths = (
            f"planning/{self.task_id}/plan.json",
            f"planning/{self.task_id}/PLAN.md",
        )
        if self.artifact == "spec" and self.review_paths != (spec_path,):
            raise ValueError("spec approval must review the exact specification path")
        if self.artifact == "plan" and self.review_paths != plan_paths:
            raise ValueError(
                "plan approval must review canonical and rendered plan paths"
            )
        if set(self.review_digests) != set(self.review_paths):
            raise ValueError("approval needs one review digest for each review path")
        scope_details = (
            self.planned_files,
            self.planned_folders,
            self.harness_paths,
            self.whole_repository,
        )
        if self.harness_intents or self.drift:
            raise ValueError("only changes approval carries harness intents or drift")
        if self.artifact == "spec" and any(scope_details):
            raise ValueError("spec approval must not carry write scopes")
        if self.whole_repository != ("." in self.planned_folders):
            raise ValueError("whole_repository must match a planned '.' folder scope")
        if not set(self.harness_paths) <= set(self.planned_files):
            raise ValueError("protected paths must be planned files")
        return self


PendingAction = Annotated[
    AgentAction | QuestionAction | ApprovalAction,
    Field(discriminator="kind"),
]
