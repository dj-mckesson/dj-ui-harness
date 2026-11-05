from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from harness.contracts.core import (
    CandidateAttempt,
    NonEmptyStr,
    RepoPath,
    StrictModel,
    TaskId,
)
from harness.contracts.plan import AcceptanceCriterionId


class HookDecision(StrictModel):
    """Hold a tool hook decision and the paths it covers."""

    decision: Literal["allow", "deny"]
    reason: NonEmptyStr
    targets: list[RepoPath] = Field(default_factory=list)

    @model_validator(mode="after")
    def allowed_decision_has_targets(self) -> HookDecision:
        """Require at least one path for an allowed tool action."""

        if self.decision == "allow" and not self.targets:
            raise ValueError("an allowed decision requires at least one target")
        return self


class HookOutput(StrictModel):
    """Hold the denial fields returned to a tool hook."""

    hookEventName: Literal["PreToolUse"] = "PreToolUse"
    permissionDecision: Literal["deny"] = "deny"
    permissionDecisionReason: NonEmptyStr


class DenyResponse(StrictModel):
    """Wrap one tool hook denial response."""

    hookSpecificOutput: HookOutput


class CheckerFinding(StrictModel):
    """Describe one issue found during candidate review."""

    severity: Literal["blocking", "non_blocking"]
    path: RepoPath
    issue: NonEmptyStr
    evidence: NonEmptyStr
    recommended_correction: NonEmptyStr


class ManualObservation(StrictModel):
    """Record what the checker saw for one planned manual review.

    :ivar acceptance_id: Acceptance criterion of the manual review.
    :ivar outcome: ``pending`` when only a person can verify the criterion.
    :ivar note: One line that says what the checker inspected and saw.
    """

    acceptance_id: AcceptanceCriterionId
    outcome: Literal["pass", "fail", "pending"]
    note: NonEmptyStr


class ReviewedCheckerResult(StrictModel):
    """Hold a completed candidate review and its observations."""

    status: Literal["reviewed"]
    verdict: Literal["pass", "revise"]
    findings: list[CheckerFinding]
    summary: NonEmptyStr
    blockers: list[NonEmptyStr] = Field(max_length=0)
    observations: list[ManualObservation] = Field(default_factory=list)

    @model_validator(mode="after")
    def findings_match_verdict(self) -> ReviewedCheckerResult:
        """Keep the verdict consistent with findings and observations."""

        has_blocking_finding = any(
            finding.severity == "blocking" for finding in self.findings
        )
        if self.verdict == "pass" and has_blocking_finding:
            raise ValueError("a passing review cannot contain blocking findings")
        if self.verdict == "revise" and not has_blocking_finding:
            raise ValueError("a revision verdict requires a blocking finding")
        if self.verdict == "pass" and any(o.outcome == "fail" for o in self.observations):
            raise ValueError(
                "a passing review cannot contain a failed manual observation"
            )
        ids = [o.acceptance_id for o in self.observations]
        if len(ids) != len(set(ids)):
            raise ValueError("manual observations must not repeat an acceptance ID")
        return self


class BlockedCheckerResult(StrictModel):
    """Describe why the checker cannot complete a review."""

    status: Literal["blocked"]
    verdict: None
    findings: list[CheckerFinding] = Field(max_length=0)
    summary: Literal[""]
    blockers: list[NonEmptyStr] = Field(min_length=1)


CheckerOutcome = Annotated[
    ReviewedCheckerResult | BlockedCheckerResult,
    Field(discriminator="status"),
]


class CheckerEvidence(StrictModel):
    """Bind a checker result to one task and candidate."""

    task_id: TaskId
    attempt: CandidateAttempt
    role: Literal["checker"] = "checker"
    result: CheckerOutcome


class GateResult(StrictModel):
    """Record one supervisor gate decision and its evidence paths."""

    gate: Literal["before-maker", "after-checker", "before-complete"]
    status: Literal["pass", "fail"]
    reason: NonEmptyStr
    evidence_paths: list[RepoPath] = Field(default_factory=list)
