"""Define the supervisor's validation check policy.

The checks themselves come from the project's ``harness.toml``. This module gives the contracts
one place to ask which checks exist, which are mandatory, and which a task type adds.
"""

from typing import Annotated

from pydantic import AfterValidator

from harness.config import (
    TASK_TYPES,
    CheckSpec,
    ProjectConfig,
    TaskType,
    matches,
    project_config,
)

__all__ = [
    "TASK_TYPES",
    "CheckName",
    "CheckSpec",
    "TaskType",
    "approved_checks",
    "check_spec",
    "mandatory_checks",
    "matches",
    "task_type_checks",
]


def approved_checks(config: ProjectConfig | None = None) -> dict[str, CheckSpec]:
    """Return every check the project approves, by name."""

    return dict((config or project_config()).checks)


def check_spec(name: str) -> CheckSpec:
    """Return one approved check.

    :raises KeyError: If the project does not define the check.
    """

    return project_config().check(name)


def mandatory_checks() -> tuple[str, ...]:
    """Return the checks that run for every candidate."""

    return project_config().mandatory


def task_type_checks(task_type: TaskType) -> tuple[str, ...]:
    """Return the required checks that a task type adds."""

    return project_config().task_types[task_type]


def _known_check(name: str) -> str:
    checks = project_config().checks
    if name not in checks:
        raise ValueError(
            f"unknown check {name!r}; harness.toml defines {', '.join(sorted(checks))}"
        )
    return name


CheckName = Annotated[str, AfterValidator(_known_check)]
