"""Contracts for the file baseline and the changed-file records of one run."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from harness.contracts.core import CandidateAttempt, StrictModel, TaskId, ValidationRound

# How a scan lists the files. A run keeps the method of its baseline.
Inventory = Literal["git", "filesystem"]


class ChangeSet(StrictModel):
    """List the files that differ from the baseline.

    :ivar added: Files that did not exist at the baseline.
    :ivar changed: Files whose content or link target differs from the baseline.
    :ivar deleted: Files that existed at the baseline and are gone.
    """

    added: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()

    @property
    def paths(self) -> tuple[str, ...]:
        """Return every differing path once, in sorted order."""

        return tuple(sorted({*self.added, *self.changed, *self.deleted}))


class Baseline(StrictModel):
    """Record the file state that existed before the first candidate started.

    :ivar task_id: Task that owns the run.
    :ivar files: Digest of every tracked file, keyed by repository-relative path.
    :ivar preexisting_changes: Paths that already differed from the last commit, or ``None`` when
        Git could not report them.
    :ivar declared_outputs: Names of check outputs that the file comparison ignores.
    :ivar inventory: How the baseline listed the files. Every later scan uses the same method.
    :ivar output_files: Digest of each declared check output that existed at the baseline.
    """

    task_id: TaskId
    files: dict[str, str]
    preexisting_changes: tuple[str, ...] | None
    declared_outputs: tuple[str, ...]
    inventory: Inventory
    output_files: dict[str, str] = Field(default_factory=dict)


class ChangeRecord(StrictModel):
    """Record what changed after one candidate or after its checks.

    :ivar task_id: Task that owns the run.
    :ivar candidate_attempt: Candidate that was compared.
    :ivar phase: ``candidate`` after the maker or repair finished, ``checks`` after the checks.
    :ivar changes: Added, changed, and deleted files compared with the baseline.
    :ivar digests: Digest of each differing file, or ``deleted``.
    :ivar reported_paths: Paths the maker and repair results reported, across candidates.
    :ivar out_of_scope: Differing paths that the approved scopes do not allow.
    :ivar unreported: Differing paths that no maker report accounts for.
    :ivar link_escapes: Differing symlinks whose target leaves the repository.
    :ivar changed_by_checks: Paths whose state differs from the validated state after the checks.
    :ivar harness_changes: Protected files that differ. A person must approve them.
    :ivar preexisting_changes: Files already changed at the baseline, kept apart from this run.
    :ivar declared_outputs: Names of check outputs that the file comparison ignores.
    :ivar output_changes: Declared check outputs that appeared, changed, or vanished since the
        baseline. They are reported apart from ``changes`` and never block completion.
    :ivar status: ``pass`` when nothing blocks completion, otherwise ``blocked``.
    :ivar problems: Plain description of each blocking problem.
    """

    task_id: TaskId
    candidate_attempt: CandidateAttempt
    phase: Literal["candidate", "checks"]
    changes: ChangeSet
    digests: dict[str, str]
    reported_paths: tuple[str, ...]
    out_of_scope: tuple[str, ...] = ()
    unreported: tuple[str, ...] = ()
    link_escapes: tuple[str, ...] = ()
    changed_by_checks: tuple[str, ...] = ()
    harness_changes: tuple[str, ...] = ()
    preexisting_changes: tuple[str, ...] | None = None
    declared_outputs: tuple[str, ...] = ()
    output_changes: ChangeSet = Field(default_factory=ChangeSet)
    status: Literal["pass", "blocked"]
    problems: tuple[str, ...] = Field(default=())


class FileDrift(StrictModel):
    """Describe one file that changed after the checks passed.

    :ivar path: Repository-relative path of the file.
    :ivar old_digest: Digest in the validated state, or ``None`` when the file was absent.
    :ivar new_digest: Digest in the current state, or ``None`` when the file is absent.
    """

    path: str
    old_digest: str | None
    new_digest: str | None

    @model_validator(mode="after")
    def digests_differ(self) -> FileDrift:
        """Check that the file state changed.

        :return: The checked file drift.
        :raises ValueError: If the old and new digests are equal.
        """

        if self.old_digest == self.new_digest:
            raise ValueError("a file drift requires different old and new digests")
        return self


class DriftRecord(StrictModel):
    """Record the files that changed after the checks of one candidate passed.

    The next validation round uses this record as its reference instead of the candidate record.

    :ivar task_id: Task that owns the run.
    :ivar candidate_attempt: Candidate whose validated state changed.
    :ivar validation_round: Validation round that checks the changed state. It is 2 or later.
    :ivar drift: Each file that differs from the validated state.
    :ivar digests: Digest of each file that differs from the baseline, or ``deleted``.
    :ivar reported_paths: Reported paths of the validated state, and the drift paths.
    """

    task_id: TaskId
    candidate_attempt: CandidateAttempt
    validation_round: Annotated[ValidationRound, Field(ge=2)]
    drift: tuple[FileDrift, ...] = Field(min_length=1)
    digests: dict[str, str]
    reported_paths: tuple[str, ...]
