from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from harness.contracts.checks import MANDATORY_CHECKS, CheckName
from harness.contracts.core import NonEmptyStr, RepoPath, StrictModel, TaskId

RequirementId = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^REQ-[0-9]{2,}$"),
]
AcceptanceCriterionId = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^AC-[0-9]{2,}$"),
]
PlanStepId = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^STEP-[0-9]{2,}$"),
]


class SpecIdentifiers(StrictModel):
    """Store the requirement and acceptance IDs found in ``SPEC.md``.

    :ivar requirement_ids: Requirement IDs in the order written.
    :ivar acceptance_criterion_ids: Acceptance IDs in the order written.
    """

    requirement_ids: tuple[RequirementId, ...] = Field(min_length=1)
    acceptance_criterion_ids: tuple[AcceptanceCriterionId, ...] = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def identifiers_are_well_formed(cls, values: object) -> object:
        """Give a clear error when an extracted ID has the wrong format.

        :param values: IDs extracted from the specification.
        :return: The unchanged values when every ID has the expected format.
        :raises ValueError: If a requirement or acceptance ID is malformed.
        """

        if not isinstance(values, dict):
            return values
        definitions = (
            ("requirement_ids", "requirement", r"REQ-[0-9]{2,}"),
            (
                "acceptance_criterion_ids",
                "acceptance-criterion",
                r"AC-[0-9]{2,}",
            ),
        )
        for field_name, label, pattern in definitions:
            candidates = values.get(field_name)
            if not isinstance(candidates, (list, tuple)):
                continue
            for candidate in candidates:
                if (
                    not isinstance(candidate, str)
                    or re.fullmatch(pattern, candidate) is None
                ):
                    raise ValueError(f"malformed {label} ID: {candidate!r}")
        return values

    @model_validator(mode="after")
    def identifiers_are_unique(self) -> SpecIdentifiers:
        """Reject repeated IDs in a completed specification.

        :return: The validated ID collection.
        :raises ValueError: If a requirement or acceptance ID is repeated.
        """

        if len(self.requirement_ids) != len(set(self.requirement_ids)):
            raise ValueError("duplicate requirement ID")
        if len(self.acceptance_criterion_ids) != len(set(self.acceptance_criterion_ids)):
            raise ValueError("duplicate acceptance-criterion ID")
        return self


def _require_unique(values: tuple[object, ...], label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{label} must not contain duplicates")


class PlanWorkItem(StrictModel):
    """Name one file or folder that the plan allows an agent to change.

    :ivar path: Repository-relative path. A folder scope may be ``.`` for the whole repository.
    :ivar scope: ``file`` for one named file, or ``folder`` for everything below a folder.
    :ivar intent: What the change does at this path.
    """

    path: RepoPath
    scope: Literal["file", "folder"] = "file"
    intent: NonEmptyStr

    @model_validator(mode="after")
    def root_is_a_folder(self) -> PlanWorkItem:
        """Allow the repository root only as a folder scope."""

        if self.path == "." and self.scope != "folder":
            raise ValueError("the repository root can only be a folder scope")
        return self


class PlanStep(StrictModel):
    """Describe one bounded implementation step and its linked requirements."""

    id: PlanStepId
    title: NonEmptyStr
    intent: NonEmptyStr
    work_items: tuple[PlanWorkItem, ...] = Field(min_length=1)
    requirement_ids: tuple[RequirementId, ...] = Field(min_length=1)
    acceptance_ids: tuple[AcceptanceCriterionId, ...] = ()

    @model_validator(mode="after")
    def references_are_unique(self) -> PlanStep:
        """Reject repeated requirement, acceptance, and work paths."""

        _require_unique(self.requirement_ids, "step requirement IDs")
        _require_unique(self.acceptance_ids, "step acceptance IDs")
        paths = tuple(item.path for item in self.work_items)
        _require_unique(paths, "step work-item paths")
        return self


class PlanCheck(StrictModel):
    """Select one supervisor-owned check, in addition to the mandatory tests.

    :ivar name: Name of an approved check.
    :ivar required: A failed required check blocks completion. A failed optional check is only
        reported.
    """

    name: CheckName
    required: bool = Field(default=True, strict=True)


class ManualReview(StrictModel):
    """State how to verify one acceptance criterion without an automated check.

    :ivar acceptance_id: Acceptance criterion that the review covers.
    :ivar inspect: What to look at.
    :ivar how: How to look at it.
    :ivar pass_if: What counts as success.
    """

    acceptance_id: AcceptanceCriterionId
    inspect: NonEmptyStr
    how: NonEmptyStr
    pass_if: NonEmptyStr


class PlanDocument(StrictModel):
    """Hold the complete plan for one task."""

    schema_version: Literal[1]
    task_id: TaskId
    steps: tuple[PlanStep, ...] = Field(min_length=1)
    test_intents: dict[AcceptanceCriterionId, NonEmptyStr] = Field(min_length=1)
    checks: tuple[PlanCheck, ...] = ()
    manual_reviews: tuple[ManualReview, ...] = ()

    @model_validator(mode="after")
    def identifiers_are_unique(self) -> PlanDocument:
        """Reject duplicate IDs and supervisor-owned check names.

        :return: The validated plan.
        :raises ValueError: If an ID repeats or a mandatory check is selected.
        """

        _require_unique(tuple(step.id for step in self.steps), "plan step IDs")
        _require_unique(tuple(check.name for check in self.checks), "plan check names")
        if any(check.name in MANDATORY_CHECKS for check in self.checks):
            raise ValueError("a plan cannot select a mandatory check")
        _require_unique(
            tuple(review.acceptance_id for review in self.manual_reviews),
            "manual review acceptance IDs",
        )
        return self
