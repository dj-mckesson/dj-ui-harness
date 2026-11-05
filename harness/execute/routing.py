"""Apply the internal routing rules for one coding workflow."""

from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ValidationError

from harness.contracts.actions import (
    AgentAction,
    ApprovalAction,
    PendingAction,
    QuestionAction,
)
from harness.contracts.core import MAX_CANDIDATE_ATTEMPTS, load_task_paths
from harness.contracts.models import CheckerEvidence
from harness.contracts.results import (
    ApprovalResponse,
    AuthorResult,
    CheckerResult,
    CompletionEvidence,
    MakerResult,
    QuestionResponse,
)
from harness.contracts.scopes import requires_exact_file_scope, scope_allows
from harness.contracts.state import RunState
from harness.contracts.validation import CandidateValidationRecord
from harness.design import render_plan_markdown, validate_plan, validate_spec
from harness.execute.actions import (
    build_approval_action,
    build_checker_action,
    build_maker_action,
    build_planner_action,
    build_question_action,
    build_repair_action,
    build_spec_author_action,
    repository_path,
)
from harness.feedback import (
    FileStateError,
    build_report,
    find_drift,
    load_checker_evidence,
    load_validation_evidence,
    planned_intents,
    reference_state,
    run_tests,
    save_approved_plan,
    save_baseline,
    save_checker_evidence,
    verify_candidate,
    verify_checks,
)
from harness.gates import after_checker, before_complete, before_maker
from harness.utils.digests import sha256_artifact

type CandidateDecision = Literal["complete", "repair", "blocked"]


class SupervisorError(ValueError):
    """Report a submission or workflow state that cannot safely move forward."""


def _replace_state(state: RunState, **changes: object) -> RunState:
    """Create a fully checked state after one workflow decision."""

    data = state.model_dump(mode="python")
    data.update(changes)
    return RunState.model_validate(data)


def _blocked(state: RunState, reason: str) -> RunState:
    """Create the stable terminal state for a workflow that cannot continue."""

    return _replace_state(
        state,
        stage="blocked",
        pending_action=None,
        blocked_reason=reason,
        completion_evidence=None,
    )


def _submission_data(submitted_result: object) -> dict[str, object]:
    """Convert one submitted model or JSON object into a plain dictionary."""

    if isinstance(submitted_result, BaseModel):
        return submitted_result.model_dump(mode="python")
    if isinstance(submitted_result, Mapping):
        return dict(submitted_result)
    raise SupervisorError("submitted result must be a JSON object or result model")


def _validate_submission_identity(
    state: RunState,
    action: PendingAction,
    data: Mapping[str, object],
) -> None:
    """Reject a response that does not belong to the current saved action."""

    if data.get("task_id") != state.task_id:
        raise SupervisorError("submitted task ID does not match the current run")
    if data.get("action_id") != action.action_id:
        raise SupervisorError("submitted action ID is stale or does not match")


def _parse_expected_result(
    action: PendingAction,
    data: Mapping[str, object],
) -> AuthorResult | MakerResult | CheckerResult | QuestionResponse | ApprovalResponse:
    """Validate a submission against the result type required by its action."""

    if isinstance(action, QuestionAction):
        model = QuestionResponse
    elif isinstance(action, ApprovalAction):
        model = ApprovalResponse
    elif action.role in {"spec-author", "planner"}:
        model = AuthorResult
    elif action.role in {"maker", "repair"}:
        model = MakerResult
    else:
        model = CheckerResult
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise SupervisorError(
            f"submitted result does not match the pending {action.kind} action: {exc}"
        ) from exc


def _handle_author_result(
    repo_root: Path,
    state: RunState,
    action: AgentAction,
    result: AuthorResult,
) -> RunState:
    """Route questions, blockers, or a completed spec or plan."""

    artifact = "spec" if action.role == "spec-author" else "plan"
    if result.status == "blocked":
        return _blocked(
            state, f"{artifact} author was blocked: {'; '.join(result.blockers)}"
        )
    if result.status == "questions":
        resolved_answers = (
            state.resolved_spec_answers
            if artifact == "spec"
            else state.resolved_plan_answers
        )
        answered_questions = {answer.question for answer in resolved_answers}
        repeated_questions = tuple(
            question for question in result.questions if question in answered_questions
        )
        if repeated_questions:
            raise SupervisorError(
                f"{artifact} author asked an already-resolved question again: "
                + "; ".join(repeated_questions)
            )
        question = build_question_action(state.task_id, artifact, result.questions)
        return _replace_state(state, pending_action=question)

    if result.files_written != action.writable_paths:
        raise SupervisorError(
            f"{artifact} author must report the exact saved artifact path"
        )

    if artifact == "spec":
        return _request_spec_approval(repo_root, state)
    return _request_plan_approval(repo_root, state)


def _request_spec_approval(
    repo_root: Path, state: RunState, notice: str | None = None
) -> RunState:
    """Validate the draft spec and pause for its approval as the next revision."""

    try:
        validate_spec(repo_root, state.task_id)
    except (OSError, ValueError) as exc:
        raise SupervisorError(f"specification artifact is invalid: {exc}") from exc
    revision = state.spec_revision + 1
    try:
        approval = build_approval_action(
            repo_root, state.task_id, "spec", revision, notice=notice
        )
    except (OSError, ValueError) as exc:
        raise SupervisorError(
            f"specification approval action could not be created: {exc}"
        ) from exc
    return _replace_state(
        state,
        stage="spec-authoring",
        spec_revision=revision,
        spec_approved=False,
        spec_digest=None,
        plan_approved=False,
        rejection_feedback=None,
        pending_action=approval,
    )


def _request_plan_approval(
    repo_root: Path, state: RunState, notice: str | None = None
) -> RunState:
    """Validate the draft plan, render ``PLAN.md``, and pause for its approval."""

    try:
        spec_identifiers = validate_spec(repo_root, state.task_id)
        plan = validate_plan(repo_root, state.task_id, spec_identifiers)
        render_plan_markdown(repo_root, state.task_id, plan)
    except (OSError, ValueError) as exc:
        raise SupervisorError(f"plan artifact is invalid: {exc}") from exc
    revision = state.plan_revision + 1
    try:
        approval = build_approval_action(
            repo_root, state.task_id, "plan", revision, notice=notice
        )
    except (OSError, ValueError) as exc:
        raise SupervisorError(
            f"plan approval action could not be created: {exc}"
        ) from exc
    return _replace_state(
        state,
        stage="plan-authoring",
        plan_revision=revision,
        plan_approved=False,
        rejection_feedback=None,
        pending_action=approval,
    )


def _current_digest(repo_root: Path, path: Path) -> str | None:
    """Return the digest of one review file, or ``None`` when it cannot be read safely."""

    try:
        return sha256_artifact(repo_root, path, label="review file")
    except (OSError, ValueError):
        return None


def _changed_review_paths(repo_root: Path, action: ApprovalAction) -> tuple[str, ...]:
    """Return the review paths whose content differs from the content shown."""

    return tuple(
        path
        for path in action.review_paths
        if _current_digest(repo_root, repo_root / path) != action.review_digests[path]
    )


def _request_again(
    repo_root: Path,
    state: RunState,
    artifact: Literal["spec", "plan"],
    changed: tuple[str, ...],
    reason: str,
    advice: str,
) -> RunState:
    """Replace a stale approval with a new one for the current content."""

    notice = f"{', '.join(changed)} changed {reason}. Review the current content."
    request = _request_spec_approval if artifact == "spec" else _request_plan_approval
    try:
        return request(repo_root, state, notice)
    except SupervisorError as exc:
        raise SupervisorError(
            f"{', '.join(changed)} changed {reason}, and {exc}. {advice}"
        ) from exc


def _handle_question_response(
    repo_root: Path,
    state: RunState,
    action: QuestionAction,
    response: QuestionResponse,
) -> RunState:
    """Save complete human answers and return work to the matching author."""

    if response.artifact != action.artifact:
        raise SupervisorError(
            "question response artifact does not match the pending action"
        )
    if tuple(answer.question for answer in response.answers) != action.questions:
        raise SupervisorError(
            "question response must answer each pending question once and in order"
        )

    if action.artifact == "spec":
        answers = (*state.resolved_spec_answers, *response.answers)
        try:
            next_author = build_spec_author_action(
                repo_root,
                state.task_id,
                state.original_request,
                resolved_answers=answers,
                rejection_feedback=state.rejection_feedback,
            )
        except (OSError, ValueError) as exc:
            raise SupervisorError(
                f"spec-author action could not be rebuilt after human answers: {exc}"
            ) from exc
        return _replace_state(
            state,
            stage="spec-authoring",
            resolved_spec_answers=answers,
            pending_action=next_author,
        )

    answers = (*state.resolved_plan_answers, *response.answers)
    try:
        next_author = build_planner_action(
            repo_root,
            state.task_id,
            state.spec_revision,
            spec_approved=state.spec_approved,
            resolved_answers=answers,
            rejection_feedback=state.rejection_feedback,
        )
    except (OSError, ValueError) as exc:
        raise SupervisorError(
            f"planner action could not be rebuilt after human answers: {exc}"
        ) from exc
    return _replace_state(
        state,
        stage="plan-authoring",
        resolved_plan_answers=answers,
        pending_action=next_author,
    )


def _handle_approval_response(
    repo_root: Path,
    state: RunState,
    action: ApprovalAction,
    response: ApprovalResponse,
) -> RunState:
    """Route one human approval or rejection for the current artifact revision."""

    if response.artifact != action.artifact:
        raise SupervisorError("approval artifact does not match the pending action")
    if response.revision != action.revision:
        raise SupervisorError("approval revision is stale or does not match")
    current_revision = {
        "spec": state.spec_revision,
        "plan": state.plan_revision,
        "changes": state.candidate_attempt,
    }[action.artifact]
    if response.revision != current_revision:
        raise SupervisorError("approval revision does not match the current artifact")
    if action.artifact == "changes":
        return _handle_changes_approval(repo_root, state, response)

    if (
        response.approved
        and action.whole_repository
        and not response.confirm_whole_repository
    ):
        raise SupervisorError(
            "the plan covers the whole repository; approval must set "
            "confirm_whole_repository to true"
        )

    if not response.approved:
        if action.artifact == "spec":
            try:
                next_author = build_spec_author_action(
                    repo_root,
                    state.task_id,
                    state.original_request,
                    resolved_answers=state.resolved_spec_answers,
                    rejection_feedback=response.feedback,
                )
            except (OSError, ValueError) as exc:
                raise SupervisorError(
                    f"spec-author action could not be rebuilt after rejection: {exc}"
                ) from exc
            return _replace_state(
                state,
                stage="spec-authoring",
                spec_approved=False,
                spec_digest=None,
                plan_approved=False,
                rejection_feedback=response.feedback,
                pending_action=next_author,
            )
        try:
            next_author = build_planner_action(
                repo_root,
                state.task_id,
                state.spec_revision,
                spec_approved=state.spec_approved,
                resolved_answers=state.resolved_plan_answers,
                rejection_feedback=response.feedback,
            )
        except (OSError, ValueError) as exc:
            raise SupervisorError(
                f"planner action could not be rebuilt after rejection: {exc}"
            ) from exc
        return _replace_state(
            state,
            stage="plan-authoring",
            plan_approved=False,
            rejection_feedback=response.feedback,
            pending_action=next_author,
        )

    changed = _changed_review_paths(repo_root, action)
    if changed:
        return _request_again(
            repo_root,
            state,
            action.artifact,
            changed,
            "after the approval was requested",
            "Restore the file, or reject the approval with feedback.",
        )

    task_paths = load_task_paths(repo_root, state.task_id)
    spec_key = repository_path(repo_root, task_paths.spec)
    if action.artifact == "spec":
        try:
            planner = build_planner_action(
                repo_root,
                state.task_id,
                state.spec_revision,
                spec_approved=True,
                resolved_answers=state.resolved_plan_answers,
            )
        except (OSError, ValueError) as exc:
            raise SupervisorError(
                f"planner action could not be created from the approved specification: {exc}"
            ) from exc
        return _replace_state(
            state,
            stage="plan-authoring",
            spec_approved=True,
            spec_digest=action.review_digests[spec_key],
            rejection_feedback=None,
            pending_action=planner,
        )

    if _current_digest(repo_root, task_paths.spec) != state.spec_digest:
        return _request_again(
            repo_root,
            state,
            "spec",
            (spec_key,),
            "after the spec approval",
            f"Restore {spec_key} to the approved content.",
        )

    plan_key = repository_path(repo_root, task_paths.canonical_plan)
    try:
        save_approved_plan(repo_root, task_paths, action.review_digests[plan_key])
    except FileStateError as exc:
        raise SupervisorError(str(exc)) from exc
    gate = before_maker(repo_root, state.task_id, True, True)
    if gate.status == "fail":
        return _replace_state(
            state,
            stage="blocked",
            plan_approved=True,
            pending_action=None,
            blocked_reason=f"Implementation gate failed: {gate.reason}",
            completion_evidence=None,
        )
    try:
        save_baseline(repo_root, task_paths)
    except FileStateError as exc:
        raise SupervisorError(str(exc)) from exc
    try:
        maker = build_maker_action(
            repo_root,
            state.task_id,
            state.spec_revision,
            state.plan_revision,
            spec_approved=True,
            plan_approved=True,
        )
    except (OSError, ValueError) as exc:
        raise SupervisorError(
            f"maker action could not be created from the approved plan: {exc}"
        ) from exc
    return _replace_state(
        state,
        stage="implementation",
        spec_approved=True,
        plan_approved=True,
        rejection_feedback=None,
        candidate_attempt=1,
        pending_action=maker,
    )


def _handle_maker_result(
    repo_root: Path,
    state: RunState,
    action: AgentAction,
    result: MakerResult,
) -> RunState:
    """Route a maker or repair result to review or a blocked terminal state."""

    unexpected_paths = {
        path
        for path in result.files_changed
        if not scope_allows(path, action.writable_paths, action.writable_folders)
    }
    if unexpected_paths:
        raise SupervisorError(
            "maker reported paths outside its saved writable scope: "
            + ", ".join(sorted(unexpected_paths))
        )
    if result.status == "blocked":
        return _blocked(state, f"{action.role} was blocked: {'; '.join(result.blockers)}")

    if action.candidate_attempt is None:
        raise SupervisorError("maker action is missing its candidate attempt")
    try:
        change_record = verify_candidate(
            repo_root,
            load_task_paths(repo_root, state.task_id),
            action.candidate_attempt,
            result.files_changed,
        )
    except FileStateError as exc:
        return _blocked(
            state, f"The candidate's file changes could not be checked: {exc}"
        )
    if change_record.status == "blocked":
        return _blocked(
            state,
            "The candidate's file changes are not allowed: "
            + "; ".join(change_record.problems),
        )
    try:
        checker = build_checker_action(
            repo_root,
            state.task_id,
            state.spec_revision,
            state.plan_revision,
            action.candidate_attempt,
            result.summary,
            spec_approved=state.spec_approved,
            plan_approved=state.plan_approved,
        )
    except (OSError, ValueError) as exc:
        raise SupervisorError(
            f"checker action could not be created from candidate artifacts: {exc}"
        ) from exc
    return _replace_state(state, stage="review", pending_action=checker)


def _handle_checker_result(
    repo_root: Path,
    state: RunState,
    action: AgentAction,
    result: CheckerResult,
) -> RunState:
    """Save checker evidence and decide whether internal validation may start."""

    if action.candidate_attempt is None:
        raise SupervisorError("checker action is missing its candidate attempt")
    task_paths = load_task_paths(repo_root, state.task_id)
    evidence = CheckerEvidence(
        task_id=state.task_id,
        attempt=action.candidate_attempt,
        result=result.result,
    )
    save_checker_evidence(repo_root, task_paths, evidence)
    gate = after_checker(repo_root, state.task_id, action.candidate_attempt)
    if gate.status == "fail":
        return _blocked(state, f"Checker gate failed: {gate.reason}")
    return _replace_state(state, stage="validation", pending_action=None)


def _route_candidate_outcome(
    checker_evidence: CheckerEvidence,
    validation_record: CandidateValidationRecord,
) -> CandidateDecision:
    """Choose completion, one repair, or blocking from review and test evidence."""

    if checker_evidence.result.status == "blocked":
        return "blocked"
    if validation_record.result.launch_error is not None:
        return "blocked"
    needs_repair = (
        checker_evidence.result.verdict == "revise" or validation_record.status == "fail"
    )
    if not needs_repair:
        return "complete"
    return "repair" if validation_record.candidate_attempt == 1 else "blocked"


def _current_validation_record(
    repo_root: Path,
    state: RunState,
) -> CandidateValidationRecord:
    """Reuse saved tests or run the fixed command once."""

    attempt = state.candidate_attempt
    if attempt is None:
        raise ValueError("validation state is missing a candidate attempt")
    key = (attempt, state.validation_round)
    task_paths = load_task_paths(repo_root, state.task_id)
    if task_paths.validation_results.exists():
        results, _ = load_validation_evidence(repo_root, task_paths)
        matching = tuple(
            record
            for record in results.runs
            if (record.candidate_attempt, record.validation_round) == key
        )
        if matching:
            if results.runs[-1] != matching[0]:
                raise ValueError(
                    "saved validation result is stale for the current candidate"
                )
            return matching[0]

    reference = reference_state(repo_root, task_paths, *key)
    return run_tests(
        repo_root,
        state.task_id,
        *key,
        include_harness_tests=any(
            requires_exact_file_scope(path)
            for path in {*reference.digests, *reference.reported_paths}
        ),
        reference_digests=reference.digests,
    )


def _completion_evidence(repo_root: Path, state: RunState) -> CompletionEvidence:
    """Build the evidence that points to the review, tests, and changed files."""

    attempt = state.candidate_attempt
    if attempt is None:
        raise SupervisorError("completion requires a current candidate")
    try:
        conditions, checks = build_report(
            repo_root, load_task_paths(repo_root, state.task_id), attempt
        )
    except (OSError, ValueError) as exc:
        raise SupervisorError(f"the completion report could not be built: {exc}") from exc
    return CompletionEvidence(
        candidate_attempt=attempt,
        checker_evidence_path=f"runs/{state.task_id}/reviews/checker-{attempt}.json",
        validation_evidence_path=f"runs/{state.task_id}/validation/results.json",
        changes_evidence_path=f"runs/{state.task_id}/changes/checks-{attempt}.json",
        conditions=conditions,
        checks=checks,
    )


def _completed(repo_root: Path, state: RunState) -> RunState:
    """Return the complete state, or a blocked state when the report cannot be built."""

    try:
        evidence = _completion_evidence(repo_root, state)
    except SupervisorError as exc:
        return _blocked(state, str(exc))
    return _replace_state(
        state, stage="complete", pending_action=None, completion_evidence=evidence
    )


def _handle_changes_approval(
    repo_root: Path,
    state: RunState,
    response: ApprovalResponse,
) -> RunState:
    """Finish the run when approved files match the checked files."""

    if not response.approved:
        return _blocked(state, f"The changes were not approved: {response.feedback}")
    if state.candidate_attempt is None:
        raise SupervisorError("changes approval requires a current candidate")
    next_round = state.validation_round + 1
    try:
        drift = find_drift(
            repo_root,
            load_task_paths(repo_root, state.task_id),
            state.candidate_attempt,
            next_round,
        )
    except FileStateError as exc:
        return _blocked(
            state, f"The file changes could not be checked before completion: {exc}"
        )
    if drift is not None:
        return _replace_state(
            state, stage="validation", validation_round=next_round, pending_action=None
        )
    gate = before_complete(repo_root, state.task_id, state.candidate_attempt)
    if gate.status == "fail":
        return _blocked(state, f"Completion gate failed: {gate.reason}")
    return _completed(repo_root, state)


def continue_validation(repo_root: Path, state: RunState) -> RunState:
    """Finish supervisor-owned tests and return the resulting workflow state.

    :param repo_root: Repository containing candidate and evidence files.
    :param state: Saved internal validation state.
    :return: Checked complete, repair, or blocked state.
    :raises SupervisorError: If the supplied state is not ready for internal validation.
    """

    if state.stage != "validation" or state.candidate_attempt is None:
        raise SupervisorError("internal validation requires a current candidate")
    task_paths = load_task_paths(repo_root, state.task_id)
    try:
        checker_evidence, _ = load_checker_evidence(
            repo_root, task_paths, state.candidate_attempt
        )
        validation_record = _current_validation_record(repo_root, state)
    except (OSError, ValueError) as exc:
        return _blocked(state, f"Validation evidence is invalid: {exc}")

    if validation_record.result.launch_error is not None:
        return _blocked(
            state,
            "Validation command could not start: "
            + validation_record.result.launch_error,
        )

    try:
        change_record, drift = verify_checks(
            repo_root, task_paths, state.candidate_attempt, state.validation_round
        )
    except FileStateError as exc:
        return _blocked(
            state, f"The file changes could not be checked after the checks: {exc}"
        )
    if change_record.status == "blocked":
        return _blocked(
            state,
            "The file changes are not allowed after the checks: "
            + "; ".join(change_record.problems),
        )

    decision = _route_candidate_outcome(checker_evidence, validation_record)
    if decision == "complete":
        gate = before_complete(repo_root, state.task_id, state.candidate_attempt)
        if gate.status == "fail":
            return _blocked(state, f"Completion gate failed: {gate.reason}")
        # A round that checks drift always pauses, so a person sees the files that changed.
        if change_record.harness_changes or drift is not None:
            try:
                approval = build_approval_action(
                    repo_root,
                    state.task_id,
                    "changes",
                    state.candidate_attempt,
                    harness_paths=change_record.harness_changes,
                    harness_intents=planned_intents(
                        repo_root, task_paths, change_record.harness_changes
                    ),
                    drift=drift.drift if drift is not None else (),
                )
            except (OSError, ValueError) as exc:
                return _blocked(state, f"Changes approval could not be created: {exc}")
            return _replace_state(state, stage="change-approval", pending_action=approval)
        return _completed(repo_root, state)
    if decision == "repair":
        try:
            repair = build_repair_action(
                repo_root,
                state.task_id,
                state.spec_revision,
                state.plan_revision,
                spec_approved=state.spec_approved,
                plan_approved=state.plan_approved,
            )
            return _replace_state(
                state,
                stage="repair",
                candidate_attempt=MAX_CANDIDATE_ATTEMPTS,
                validation_round=1,
                pending_action=repair,
            )
        except (OSError, ValueError) as exc:
            return _blocked(state, f"Repair input evidence is invalid: {exc}")
    return _blocked(
        state,
        "The current candidate still needs changes and the one repair is exhausted.",
    )


def route_submission(
    repo_root: Path,
    state: RunState,
    submitted_result: object,
) -> RunState:
    """Check one submitted result and return the next workflow state.

    :param repo_root: Repository containing the workflow run.
    :param state: Current checked workflow state.
    :param submitted_result: Result model or decoded JSON object for the current action.
    :return: Next checked workflow state.
    :raises SupervisorError: If no action is pending or the submission is stale or malformed.
    """

    action = state.pending_action
    if action is None:
        raise SupervisorError("the current workflow state has no result to submit")
    data = _submission_data(submitted_result)
    _validate_submission_identity(state, action, data)
    result = _parse_expected_result(action, data)

    if isinstance(action, QuestionAction):
        if not isinstance(result, QuestionResponse):
            raise SupervisorError("pending question requires a question response")
        return _handle_question_response(repo_root, state, action, result)
    if isinstance(action, ApprovalAction):
        if not isinstance(result, ApprovalResponse):
            raise SupervisorError("pending approval requires an approval response")
        return _handle_approval_response(repo_root, state, action, result)
    if action.role in {"spec-author", "planner"}:
        if not isinstance(result, AuthorResult):
            raise SupervisorError("pending author action requires an author result")
        return _handle_author_result(repo_root, state, action, result)
    if action.role in {"maker", "repair"}:
        if not isinstance(result, MakerResult):
            raise SupervisorError("pending maker action requires a maker result")
        return _handle_maker_result(repo_root, state, action, result)
    if not isinstance(result, CheckerResult):
        raise SupervisorError("pending checker action requires a checker result")
    return _handle_checker_result(repo_root, state, action, result)


__all__ = ["SupervisorError", "continue_validation", "route_submission"]
