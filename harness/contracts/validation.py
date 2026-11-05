from __future__ import annotations

from typing import Literal

from pydantic import (
    AwareDatetime,
    Field,
    StrictBool,
    model_validator,
)

from harness.contracts.checks import APPROVED_CHECKS, MANDATORY_CHECKS, CheckName
from harness.contracts.core import (
    MAX_CANDIDATE_ATTEMPTS,
    CandidateAttempt,
    NonEmptyStr,
    StrictModel,
    TaskId,
    ValidationRound,
)

# A validation key is (candidate attempt, validation round).
type ValidationKey = tuple[int, int]


def is_next_validation_key(previous: ValidationKey | None, key: ValidationKey) -> bool:
    """Return whether ``key`` may come after ``previous`` in the validation history.

    The history starts at ``(1, 1)``. Each next key is the next round of the same candidate, or
    round 1 of the next candidate.

    :param previous: Last saved key, or ``None`` for an empty history.
    :param key: Key to add.
    :return: ``True`` when the key is the next allowed key.
    """

    if previous is None:
        return key == (1, 1)
    attempt, validation_round = previous
    return key in {(attempt, validation_round + 1), (attempt + 1, 1)}


class ValidationCommandResult(StrictModel):
    """Record the outcome and bounded output of one validation command."""

    status: Literal["pass", "fail", "skipped"]
    exit_code: int | None = Field(default=None, strict=True)
    launch_error: NonEmptyStr | None = None
    stdout: str
    stderr: str
    timed_out: StrictBool

    @model_validator(mode="after")
    def outcome_is_consistent(self) -> ValidationCommandResult:
        """Check that a command result describes one clear outcome.

        :return: The checked command result.
        :raises ValueError: If the result fields describe conflicting outcomes.
        """

        if self.status == "skipped":
            if (
                self.exit_code is not None
                or self.launch_error is not None
                or self.timed_out
                or self.stdout
                or self.stderr
            ):
                raise ValueError("a skipped command cannot have execution evidence")
            return self
        if self.status == "pass":
            if self.exit_code != 0 or self.launch_error is not None or self.timed_out:
                raise ValueError(
                    "a passing command requires exit code 0 without launch error or timeout"
                )
            return self

        if self.timed_out:
            if self.exit_code is not None or self.launch_error is not None:
                raise ValueError(
                    "a timeout cannot also have an exit code or launch error"
                )
            return self
        if self.launch_error is not None:
            if self.exit_code is not None:
                raise ValueError("a launch failure cannot also have an exit code")
            return self
        if self.exit_code is None:
            raise ValueError(
                "a failed command requires a nonzero exit code, launch error, or timeout"
            )
        if self.exit_code == 0:
            raise ValueError("a failing command cannot have exit code 0")
        return self


class NamedCheckResult(StrictModel):
    """Hold the result of one supervisor check.

    :ivar name: Name of the supervisor-owned check.
    :ivar required: Whether a failure blocks the candidate.
    :ivar result: Outcome of the check command.
    """

    name: CheckName
    required: StrictBool
    result: ValidationCommandResult

    @model_validator(mode="after")
    def skipped_check_is_per_file(self) -> NamedCheckResult:
        """Allow a skipped result only for a check that runs on changed files.

        :return: The validated check result.
        :raises ValueError: If a repository check was skipped.
        """

        if (
            self.result.status == "skipped"
            and APPROVED_CHECKS[self.name].scope == "repository"
        ):
            raise ValueError("only a changed-file check may be skipped")
        return self


class CandidateValidationRecord(StrictModel):
    """Hold the tests and all checks for one candidate.

    :ivar validation_round: Round of this candidate. A round after the first validates files
        that changed after the checks passed.
    :ivar result: Outcome of the mandatory tests.
    :ivar checks: Outcomes of mandatory checks and checks selected by the plan.
    """

    candidate_attempt: CandidateAttempt
    validation_round: ValidationRound = 1
    timestamp: AwareDatetime
    result: ValidationCommandResult
    checks: tuple[NamedCheckResult, ...]

    @model_validator(mode="after")
    def required_evidence_is_present(self) -> CandidateValidationRecord:
        """Require unique check results and mandatory validation evidence.

        :return: The validated record.
        :raises ValueError: If a required result is missing or invalid.
        """

        if self.result.status == "skipped":
            raise ValueError("required tests cannot be skipped")
        names = [check.name for check in self.checks]
        if len(names) != len(set(names)):
            raise ValueError("duplicate check result names")
        for name in MANDATORY_CHECKS:
            matches = [check for check in self.checks if check.name == name]
            if len(matches) != 1 or not matches[0].required:
                raise ValueError(f"missing or non-required mandatory check: {name}")
        return self

    @property
    def status(self) -> Literal["pass", "fail"]:
        """Return the candidate result used by completion and repair decisions.

        :return: ``pass`` when the tests and every required check pass, otherwise ``fail``.
        """

        required_pass = all(
            c.result.status in {"pass", "skipped"} for c in self.checks if c.required
        )
        return "pass" if self.result.status == "pass" and required_pass else "fail"


class ValidationResultsDocument(StrictModel):
    """Hold ordered validation records for one task."""

    task_id: TaskId
    runs: tuple[CandidateValidationRecord, ...] = ()

    @model_validator(mode="after")
    def validation_keys_are_ordered(self) -> ValidationResultsDocument:
        """Check that the candidates and their rounds are saved in order.

        :return: The checked validation results document.
        :raises ValueError: If a candidate or round is missing, repeated, or out of order.
        """

        previous: ValidationKey | None = None
        for run in self.runs:
            key = (run.candidate_attempt, run.validation_round)
            if not is_next_validation_key(previous, key):
                raise ValueError(
                    "validation results must start at candidate 1, round 1, and add "
                    f"one round or the next candidate at a time (up to candidate {MAX_CANDIDATE_ATTEMPTS})"
                )
            previous = key
        return self
