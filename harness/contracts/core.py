from pathlib import Path, PurePosixPath
from typing import Annotated

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    validate_call,
)

APPLICATION_DIRECTORY = "src"
MAX_CANDIDATE_ATTEMPTS = 2


def repository_relative_path(value: object) -> str:
    """Require a normalized repository-relative POSIX path."""

    if not isinstance(value, str) or not value:
        raise ValueError("repository path must be a non-empty string")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or "\\" in value
        or str(path) != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("repository path must be normalized, relative POSIX syntax")
    return value


TaskId = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^[0-9]{2,}$"),
]
CandidateAttempt = Annotated[int, Field(strict=True, ge=1, le=MAX_CANDIDATE_ATTEMPTS)]
ValidationRound = Annotated[int, Field(strict=True, ge=1)]
RepoPath = Annotated[str, BeforeValidator(repository_relative_path)]
NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Sha256Digest = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$"),
]


class RuntimeEventModel(BaseModel):
    """Immutable runtime input that tolerates unrelated event metadata."""

    model_config = ConfigDict(extra="ignore", frozen=True)


class StrictModel(BaseModel):
    """Reject unknown fields and prevent field reassignment."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class HarnessPaths(StrictModel):
    """Hold the fixed directories of one repository."""

    root: Path
    planning: Path
    runs: Path
    app: Path


class TaskPaths(StrictModel):
    """Hold the planning and run directories of one task."""

    task_id: TaskId
    planning_dir: Path
    run_dir: Path

    @property
    def spec(self) -> Path:
        """Return the specification path."""

        return self.planning_dir / "SPEC.md"

    @property
    def canonical_plan(self) -> Path:
        """Return the draft ``plan.json``. After approval, the run reads ``approved_plan``."""

        return self.planning_dir / "plan.json"

    @property
    def rendered_plan(self) -> Path:
        """Return ``PLAN.md``, which is the plan people review."""

        return self.planning_dir / "PLAN.md"

    @property
    def validation_results(self) -> Path:
        """Return the saved validation results path."""

        return self.run_dir / "validation" / "results.json"

    @property
    def changes_dir(self) -> Path:
        """Return the supervisor-owned folder for file-change evidence."""

        return self.run_dir / "changes"

    @property
    def approved_plan(self) -> Path:
        """Return the copy of ``plan.json`` saved when the plan was approved."""

        return self.changes_dir / "approved-plan.json"

    @property
    def baseline(self) -> Path:
        """Return the file baseline saved before coding starts."""

        return self.changes_dir / "baseline.json"

    def candidate_changes(self, attempt: CandidateAttempt) -> Path:
        """Return the changed-file record taken after one candidate."""

        return self.changes_dir / f"candidate-{attempt}.json"

    def check_changes(self, attempt: CandidateAttempt) -> Path:
        """Return the changed-file record taken after the checks of one candidate."""

        return self.changes_dir / f"checks-{attempt}.json"

    def drift_changes(self, attempt: CandidateAttempt) -> Path:
        """Return the record of files changed after the checks passed."""

        return self.changes_dir / f"drift-{attempt}.json"

    def checker_evidence(self, attempt: CandidateAttempt) -> Path:
        """Return the checker evidence path for one candidate."""

        return self.run_dir / "reviews" / f"checker-{attempt}.json"


def load_paths(repo_root: Path) -> HarnessPaths:
    """Return the fixed directories used by the harness.

    :param repo_root: Repository that contains the harness and application.
    :return: Absolute paths for the root, ``planning``, ``runs``, and ``src``.
    """

    resolved_root = repo_root.resolve()
    return HarnessPaths(
        root=resolved_root,
        planning=resolved_root / "planning",
        runs=resolved_root / "runs",
        app=resolved_root / APPLICATION_DIRECTORY,
    )


@validate_call
def load_task_paths(repo_root: Path, task_id: TaskId) -> TaskPaths:
    """Build the planning and run paths for one task."""

    paths = load_paths(repo_root)
    return TaskPaths(
        task_id=task_id,
        planning_dir=paths.planning / task_id,
        run_dir=paths.runs / task_id,
    )
