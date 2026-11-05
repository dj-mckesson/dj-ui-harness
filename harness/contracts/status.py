"""Contracts for the read-only status report of one saved run."""

from typing import Literal

from pydantic import StrictBool

from harness.contracts.core import CandidateAttempt, NonEmptyStr, StrictModel, TaskId
from harness.contracts.results import CompletionEvidence


class PendingSummary(StrictModel):
    """Describe the one action a run is waiting for.

    :ivar kind: Whether an agent, a question, or an approval is pending.
    :ivar action_id: ID that the matching result must return.
    :ivar role: Agent role, for an agent action.
    :ivar artifact: Spec, plan, or changes, for a question or an approval.
    """

    kind: Literal["agent", "question", "approval"]
    action_id: NonEmptyStr
    role: NonEmptyStr | None = None
    artifact: NonEmptyStr | None = None


class ChangeRecordSummary(StrictModel):
    """Summarize one saved changed-file record.

    :ivar path: Repository-relative path of the record.
    :ivar status: ``pass`` or ``blocked``, or ``unreadable`` when the record is damaged.
    :ivar problems: Blocking problems with their exact paths.
    :ivar harness_changes: Protected files that changed and need human approval.
    """

    path: NonEmptyStr
    status: Literal["pass", "blocked", "unreadable"]
    problems: tuple[str, ...] = ()
    harness_changes: tuple[str, ...] = ()


class StatusReport(StrictModel):
    """Report where one saved run stands, without changing it.

    :ivar kind: Fixed value that identifies a status report.
    :ivar task_id: Task being reported.
    :ivar stage: Current workflow stage.
    :ivar original_request: Request the run started with.
    :ivar spec_revision: Current specification revision.
    :ivar spec_approved: Whether the current specification is approved.
    :ivar plan_revision: Current plan revision.
    :ivar plan_approved: Whether the current plan is approved.
    :ivar candidate_attempt: Current candidate, once implementation has started.
    :ivar pending: The action the run is waiting for, if any.
    :ivar blocked_reason: Why a blocked run stopped.
    :ivar completion: Evidence paths of a complete run.
    :ivar baseline_saved: Whether the approved plan and file baseline exist.
    :ivar change_records: Saved changed-file records, oldest first.
    :ivar next_step: Plain instruction for what to do next.
    """

    kind: Literal["status"] = "status"
    task_id: TaskId
    stage: NonEmptyStr
    original_request: NonEmptyStr
    spec_revision: int
    spec_approved: StrictBool
    plan_revision: int
    plan_approved: StrictBool
    candidate_attempt: CandidateAttempt | None
    pending: PendingSummary | None
    blocked_reason: NonEmptyStr | None
    completion: CompletionEvidence | None
    baseline_saved: StrictBool
    change_records: tuple[ChangeRecordSummary, ...]
    next_step: NonEmptyStr
