"""Build complete, bounded actions for agents and humans."""

from pathlib import Path
from uuid import uuid4

from pydantic import StrictBool, validate_call

from harness.contracts.actions import (
    ActionId,
    AgentAction,
    ApprovalAction,
    ArtifactName,
    CandidateContext,
    CheckerContext,
    PlannerContext,
    PositiveRevision,
    QuestionAction,
    ResolvedAnswer,
    SpecAuthorContext,
)
from harness.contracts.changes import FileDrift
from harness.contracts.core import (
    MAX_CANDIDATE_ATTEMPTS,
    CandidateAttempt,
    NonEmptyStr,
    RepoPath,
    TaskId,
    TaskPaths,
    load_paths,
    load_task_paths,
)
from harness.contracts.plan import PlanDocument
from harness.contracts.scopes import REPOSITORY_ROOT_SCOPE, requires_exact_file_scope
from harness.design.validation import validate_plan, validate_spec
from harness.feedback.changes import load_approved_plan
from harness.utils.artifact_paths import (
    normalize_scope_path,
    require_artifact_file,
    require_safe_artifact_path,
)
from harness.utils.digests import sha256_artifact


def _new_action_id() -> str:
    return uuid4().hex


def repository_path(repo_root: Path, path: Path) -> str:
    """Return one safe repository-relative path for an action.

    :param repo_root: Repository that owns the path.
    :param path: Absolute or repository-relative path to include.
    :return: Normalized POSIX path relative to the repository.
    :raises ValueError: If the path is unsafe or outside the repository.
    """

    safe_path = require_safe_artifact_path(repo_root, path, label="action path")
    return safe_path.relative_to(repo_root.resolve()).as_posix()


def _unique_paths(paths: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(paths))


def _checked_supplied_paths(
    repo_root: Path,
    paths: tuple[RepoPath, ...],
) -> tuple[str, ...]:
    """Check caller-supplied repository paths before adding them to an action."""

    return _unique_paths(
        tuple(repository_path(repo_root, repo_root / path) for path in paths)
    )


def _action_id(value: ActionId | None) -> str:
    """Use a supplied action ID or create a new one."""

    return value if value is not None else _new_action_id()


def _checked_plan(
    repo_root: Path,
    task_paths: TaskPaths,
    *,
    spec_approved: StrictBool,
    plan_approved: StrictBool,
) -> PlanDocument:
    """Load the approved plan after checking both human approvals."""

    if not spec_approved:
        raise ValueError("the current specification must be approved")
    if not plan_approved:
        raise ValueError("the current plan must be approved")
    return load_approved_plan(repo_root, task_paths)


def _scope_path(repo_root: Path, path: str) -> str:
    """Return one safe repository-relative scope path, allowing ``.`` for the root."""

    _, normalized = normalize_scope_path(
        repo_root, path, label="action path", allow_root=True
    )
    return normalized


def _work_scopes(
    repo_root: Path, plan: PlanDocument
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Read the write scopes from a plan."""

    items = tuple(item for step in plan.steps for item in step.work_items)
    return (
        _unique_paths(
            tuple(
                _scope_path(repo_root, item.path)
                for item in items
                if item.scope == "file"
            )
        ),
        _unique_paths(
            tuple(
                _scope_path(repo_root, item.path)
                for item in items
                if item.scope == "folder"
            )
        ),
    )


@validate_call
def build_spec_author_action(
    repo_root: Path,
    task_id: TaskId,
    original_request: NonEmptyStr,
    *,
    resolved_answers: tuple[ResolvedAnswer, ...] = (),
    rejection_feedback: NonEmptyStr | None = None,
    context_paths: tuple[RepoPath, ...] | None = None,
    action_id: ActionId | None = None,
) -> AgentAction:
    """Build the next bounded spec-author action.

    :param repo_root: Repository containing the task.
    :param task_id: Task whose specification should be authored.
    :param original_request: Coding request the specification must describe.
    :param resolved_answers: Human answers already collected for this spec.
    :param rejection_feedback: Feedback from a rejected spec, when present.
    :param context_paths: Read-only repository context; defaults to ``src``.
    :param action_id: Optional known action ID, mainly useful in tests.
    :return: Complete spec-author action ready to save in ``RunState``.
    """

    resolved_root = repo_root.resolve()
    paths = load_paths(resolved_root)
    task_paths = load_task_paths(resolved_root, task_id)
    readable = (
        _checked_supplied_paths(resolved_root, context_paths)
        if context_paths is not None
        else (repository_path(resolved_root, paths.app),)
    )
    writable = (repository_path(resolved_root, task_paths.spec),)
    return AgentAction(
        action_id=_action_id(action_id),
        task_id=task_id,
        role="spec-author",
        persona_id="spec-author",
        skill_id="spec",
        candidate_attempt=None,
        required_input_paths=_unique_paths(readable),
        writable_paths=writable,
        context=SpecAuthorContext(
            original_request=original_request,
            resolved_answers=resolved_answers,
            rejection_feedback=rejection_feedback,
        ),
    )


@validate_call
def build_planner_action(
    repo_root: Path,
    task_id: TaskId,
    spec_revision: PositiveRevision,
    *,
    spec_approved: StrictBool,
    resolved_answers: tuple[ResolvedAnswer, ...] = (),
    rejection_feedback: NonEmptyStr | None = None,
    context_paths: tuple[RepoPath, ...] | None = None,
    action_id: ActionId | None = None,
) -> AgentAction:
    """Build a planner action for the approved specification.

    :param repo_root: Repository containing the task.
    :param task_id: Task whose implementation plan should be authored.
    :param spec_revision: Approved specification revision to plan against.
    :param spec_approved: Whether that current revision has human approval.
    :param resolved_answers: Human answers already collected for this plan.
    :param rejection_feedback: Feedback from a rejected plan, when present.
    :param context_paths: Read-only repository context; defaults to ``src``.
    :param action_id: Optional known action ID, mainly useful in tests.
    :return: Complete planner action ready to save in ``RunState``.
    :raises ValueError: If the specification is not approved or is invalid.
    """

    if not spec_approved:
        raise ValueError("the current specification must be approved before planning")
    resolved_root = repo_root.resolve()
    paths = load_paths(resolved_root)
    task_paths = load_task_paths(resolved_root, task_id)
    validate_spec(resolved_root, task_id)
    readable_context = (
        _checked_supplied_paths(resolved_root, context_paths)
        if context_paths is not None
        else (repository_path(resolved_root, paths.app),)
    )
    return AgentAction(
        action_id=_action_id(action_id),
        task_id=task_id,
        role="planner",
        persona_id="planner",
        skill_id="planner",
        candidate_attempt=None,
        required_input_paths=_unique_paths(
            (
                repository_path(resolved_root, task_paths.spec),
                *readable_context,
            )
        ),
        writable_paths=(repository_path(resolved_root, task_paths.canonical_plan),),
        context=PlannerContext(
            spec_revision=spec_revision,
            permitted_work_paths=(REPOSITORY_ROOT_SCOPE,),
            resolved_answers=resolved_answers,
            rejection_feedback=rejection_feedback,
        ),
    )


@validate_call
def build_maker_action(
    repo_root: Path,
    task_id: TaskId,
    spec_revision: PositiveRevision,
    plan_revision: PositiveRevision,
    *,
    spec_approved: StrictBool,
    plan_approved: StrictBool,
    action_id: ActionId | None = None,
) -> AgentAction:
    """Build the candidate-1 implementation action from the approved plan.

    :param repo_root: Repository containing the approved artifacts.
    :param task_id: Task being implemented.
    :param spec_revision: Approved specification revision.
    :param plan_revision: Approved plan revision.
    :param spec_approved: Whether the current specification is approved.
    :param plan_approved: Whether the current plan is approved.
    :param action_id: Optional known action ID, mainly useful in tests.
    :return: Complete candidate-1 maker action.
    :raises ValueError: If an approval is missing or the approved plan cannot be read.
    """

    resolved_root = repo_root.resolve()
    task_paths = load_task_paths(resolved_root, task_id)
    plan = _checked_plan(
        resolved_root,
        task_paths,
        spec_approved=spec_approved,
        plan_approved=plan_approved,
    )
    work_files, work_folders = _work_scopes(resolved_root, plan)
    artifacts = (
        repository_path(resolved_root, task_paths.spec),
        repository_path(resolved_root, task_paths.approved_plan),
    )
    return AgentAction(
        action_id=_action_id(action_id),
        task_id=task_id,
        role="maker",
        persona_id="maker",
        skill_id="maker",
        candidate_attempt=1,
        required_input_paths=_unique_paths(
            (
                *artifacts,
                *work_files,
                *(f for f in work_folders if f != REPOSITORY_ROOT_SCOPE),
            )
        ),
        writable_paths=work_files,
        writable_folders=work_folders,
        context=CandidateContext(
            spec_revision=spec_revision,
            plan_revision=plan_revision,
        ),
    )


@validate_call
def build_repair_action(
    repo_root: Path,
    task_id: TaskId,
    spec_revision: PositiveRevision,
    plan_revision: PositiveRevision,
    *,
    spec_approved: StrictBool,
    plan_approved: StrictBool,
    action_id: ActionId | None = None,
) -> AgentAction:
    """Build the one candidate-2 repair action.

    :param repo_root: Repository containing the approved artifacts and candidate-1 evidence.
    :param task_id: Task receiving its one repair.
    :param spec_revision: Approved specification revision.
    :param plan_revision: Approved plan revision.
    :param spec_approved: Whether the current specification is approved.
    :param plan_approved: Whether the current plan is approved.
    :param action_id: Optional known action ID, mainly useful in tests.
    :return: Complete candidate-2 repair action.
    :raises ValueError: If an approval is missing or the approved plan cannot be read.
    """

    resolved_root = repo_root.resolve()
    task_paths = load_task_paths(resolved_root, task_id)
    plan = _checked_plan(
        resolved_root,
        task_paths,
        spec_approved=spec_approved,
        plan_approved=plan_approved,
    )
    work_files, work_folders = _work_scopes(resolved_root, plan)
    evidence_paths = (
        repository_path(resolved_root, task_paths.checker_evidence(1)),
        repository_path(resolved_root, task_paths.validation_results),
    )
    artifacts = (
        repository_path(resolved_root, task_paths.spec),
        repository_path(resolved_root, task_paths.approved_plan),
    )
    return AgentAction(
        action_id=_action_id(action_id),
        task_id=task_id,
        role="repair",
        persona_id="maker",
        skill_id="maker",
        candidate_attempt=MAX_CANDIDATE_ATTEMPTS,
        required_input_paths=_unique_paths(
            (
                *artifacts,
                *work_files,
                *(f for f in work_folders if f != REPOSITORY_ROOT_SCOPE),
                *evidence_paths,
            )
        ),
        writable_paths=work_files,
        writable_folders=work_folders,
        context=CandidateContext(
            spec_revision=spec_revision,
            plan_revision=plan_revision,
        ),
    )


@validate_call
def build_checker_action(
    repo_root: Path,
    task_id: TaskId,
    spec_revision: PositiveRevision,
    plan_revision: PositiveRevision,
    candidate_attempt: CandidateAttempt,
    candidate_summary: NonEmptyStr,
    *,
    spec_approved: StrictBool,
    plan_approved: StrictBool,
    action_id: ActionId | None = None,
) -> AgentAction:
    """Build a read-only checker action for the current candidate.

    :param repo_root: Repository containing the approved artifacts and candidate files.
    :param task_id: Task being reviewed.
    :param spec_revision: Approved specification revision.
    :param plan_revision: Approved plan revision.
    :param candidate_attempt: Candidate 1 or candidate 2.
    :param candidate_summary: Short maker or repair result summary.
    :param spec_approved: Whether the current specification is approved.
    :param plan_approved: Whether the current plan is approved.
    :param action_id: Optional known action ID, mainly useful in tests.
    :return: Complete checker action with no writable paths.
    :raises ValueError: If an approval is missing or the approved plan cannot be read.
    """

    resolved_root = repo_root.resolve()
    task_paths = load_task_paths(resolved_root, task_id)
    plan = _checked_plan(
        resolved_root,
        task_paths,
        spec_approved=spec_approved,
        plan_approved=plan_approved,
    )
    work_files, work_folders = _work_scopes(resolved_root, plan)
    artifacts = (
        repository_path(resolved_root, task_paths.spec),
        repository_path(resolved_root, task_paths.approved_plan),
    )
    return AgentAction(
        action_id=_action_id(action_id),
        task_id=task_id,
        role="checker",
        persona_id="checker",
        skill_id="checker",
        candidate_attempt=candidate_attempt,
        required_input_paths=_unique_paths(
            (
                *artifacts,
                *work_files,
                *(f for f in work_folders if f != REPOSITORY_ROOT_SCOPE),
            )
        ),
        writable_paths=(),
        context=CheckerContext(
            spec_revision=spec_revision,
            plan_revision=plan_revision,
            candidate_summary=candidate_summary,
        ),
    )


@validate_call
def build_question_action(
    task_id: TaskId,
    artifact: ArtifactName,
    questions: tuple[NonEmptyStr, ...],
    *,
    action_id: ActionId | None = None,
) -> QuestionAction:
    """Build a durable human question pause for spec or plan authoring.

    :param task_id: Task waiting for human input.
    :param artifact: Whether the spec author or planner asked the questions.
    :param questions: Non-empty material questions to show the human.
    :param action_id: Optional known action ID, mainly useful in tests.
    :return: Question action ready to save in ``RunState``.
    """

    return QuestionAction(
        action_id=_action_id(action_id),
        task_id=task_id,
        artifact=artifact,
        questions=questions,
    )


@validate_call
def build_approval_action(
    repo_root: Path,
    task_id: TaskId,
    artifact: ArtifactName,
    revision: PositiveRevision,
    *,
    harness_paths: tuple[RepoPath, ...] = (),
    harness_intents: dict[RepoPath, str] | None = None,
    drift: tuple[FileDrift, ...] = (),
    notice: NonEmptyStr | None = None,
    action_id: ActionId | None = None,
) -> ApprovalAction:
    """Build a human review action for a spec, plan, or file change.

    :param repo_root: Repository containing the review artifacts.
    :param task_id: Task waiting for human approval.
    :param artifact: Specification or plan being reviewed.
    :param revision: Current positive revision number.
    :param harness_paths: For changes, the protected files that changed.
    :param harness_intents: For changes, the planned intent of each changed protected file.
    :param drift: For changes, each file that changed after the checks passed.
    :param notice: For a spec or plan, why this approval replaces an earlier one.
    :param action_id: Optional known action ID, mainly useful in tests.
    :return: Complete approval action ready to save in ``RunState``.
    :raises ValueError: If required review artifacts are invalid or missing.
    :raises OSError: If a review file cannot be read.
    """

    resolved_root = repo_root.resolve()
    task_paths = load_task_paths(resolved_root, task_id)
    if artifact == "changes":
        record = require_artifact_file(
            resolved_root,
            task_paths.check_changes(revision),
            label="changed-file record for approval",
        )
        return ApprovalAction(
            action_id=_action_id(action_id),
            task_id=task_id,
            artifact="changes",
            revision=revision,
            review_paths=(repository_path(resolved_root, record),),
            notice=notice,
            harness_paths=harness_paths,
            harness_intents=harness_intents or {},
            drift=drift,
        )
    if artifact == "spec":
        review_files = (task_paths.spec,)
    else:
        review_files = (task_paths.canonical_plan, task_paths.rendered_plan)
    # Hash before validation. A change after this point then shows at approval.
    review_digests = {
        repository_path(resolved_root, path): sha256_artifact(
            resolved_root, path, label=f"{artifact} review file"
        )
        for path in review_files
    }
    if artifact == "spec":
        validate_spec(resolved_root, task_id)
        return ApprovalAction(
            action_id=_action_id(action_id),
            task_id=task_id,
            artifact="spec",
            revision=revision,
            review_paths=tuple(review_digests),
            review_digests=review_digests,
            notice=notice,
        )

    spec_identifiers = validate_spec(resolved_root, task_id)
    plan = validate_plan(resolved_root, task_id, spec_identifiers)
    planned_files, planned_folders = _work_scopes(resolved_root, plan)
    return ApprovalAction(
        action_id=_action_id(action_id),
        task_id=task_id,
        artifact="plan",
        revision=revision,
        review_paths=tuple(review_digests),
        review_digests=review_digests,
        notice=notice,
        planned_files=planned_files,
        planned_folders=planned_folders,
        harness_paths=tuple(
            path for path in planned_files if requires_exact_file_scope(path)
        ),
        whole_repository=REPOSITORY_ROOT_SCOPE in planned_folders,
    )
