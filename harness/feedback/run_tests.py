"""Run the application tests for one candidate and save their result."""

import os
import signal
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import StrictBool, validate_call

from harness.contracts.checks import (
    APPROVED_CHECKS,
    MANDATORY_CHECKS,
    PYTHON_SUFFIXES,
    WEB_SUFFIXES,
)
from harness.contracts.core import (
    CandidateAttempt,
    TaskId,
    ValidationRound,
    load_task_paths,
)
from harness.contracts.plan import PlanDocument
from harness.contracts.validation import (
    CandidateValidationRecord,
    NamedCheckResult,
    ValidationCommandResult,
    ValidationResultsDocument,
    is_next_validation_key,
)
from harness.feedback.changes import load_approved_plan
from harness.utils.artifact_paths import require_safe_artifact_path
from harness.utils.atomic import write_bytes

TEST_COMMAND = ("pnpm", "test", "--maxWorkers=1")
HARNESS_TEST_COMMAND = (
    "uv",
    "run",
    "--no-sync",
    "python",
    "-m",
    "pytest",
    "-q",
    "tests/harness",
)
TEST_TIMEOUT_SECONDS = 300
TEST_OUTPUT_LIMIT = 100_000
_TRUNCATION_MARKER = "\n...[truncated]..."


def _bounded_output(value: str | bytes | None) -> str:
    """Convert command output to bounded text for saved evidence."""

    if value is None:
        return ""
    if isinstance(value, bytes):
        output = value.decode("utf-8", errors="replace")
    else:
        output = value
    if len(output) <= TEST_OUTPUT_LIMIT:
        return output
    retained = TEST_OUTPUT_LIMIT - len(_TRUNCATION_MARKER)
    return output[:retained] + _TRUNCATION_MARKER


def _execute(
    repo_root: Path, command: tuple[str, ...], timeout: int
) -> ValidationCommandResult:
    """Run a command and stop its process group on timeout.

    :raises OSError: If process group cleanup fails.
    """

    try:
        process = subprocess.Popen(
            command,
            cwd=repo_root,
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except OSError as exc:
        return ValidationCommandResult(
            status="fail",
            exit_code=None,
            launch_error=f"failed to launch command: {exc}",
            stdout="",
            stderr="",
            timed_out=False,
        )

    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.communicate(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.wait(timeout=1)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()
            raise
        stdout, stderr = process.communicate()

    status: Literal["pass", "fail"] = (
        "pass" if not timed_out and process.returncode == 0 else "fail"
    )
    return ValidationCommandResult(
        status=status,
        exit_code=None if timed_out else process.returncode,
        launch_error=None,
        stdout=_bounded_output(stdout),
        stderr=_bounded_output(stderr),
        timed_out=timed_out,
    )


def _execute_tests(
    repo_root: Path, *, include_harness_tests: bool
) -> ValidationCommandResult:
    """Run the application tests, then the harness tests when requested.

    :return: The first failing result, or the passing result of the last command.
    """

    result = _execute(repo_root, TEST_COMMAND, TEST_TIMEOUT_SECONDS)
    if result.status != "pass" or not include_harness_tests:
        return result
    return _execute(repo_root, HARNESS_TEST_COMMAND, TEST_TIMEOUT_SECONDS)


def _run_checks(
    repo_root: Path, plan: PlanDocument, reference_digests: dict[str, str]
) -> tuple[NamedCheckResult, ...]:
    """Run mandatory checks, then the checks selected by the plan."""

    results = []
    selected = [(name, True) for name in MANDATORY_CHECKS]
    selected.extend((check.name, check.required) for check in plan.checks)
    for name, required in selected:
        spec = APPROVED_CHECKS[name]
        if spec.scope == "repository":
            result = _execute(repo_root, spec.command, spec.timeout)
        else:
            suffixes = (
                PYTHON_SUFFIXES if spec.scope == "changed-python-files" else WEB_SUFFIXES
            )
            changed = _changed_paths(reference_digests, suffixes)
            result = (
                _execute(
                    repo_root, _command_with_paths(spec.command, changed), spec.timeout
                )
                if changed
                else ValidationCommandResult(
                    status="skipped", stdout="", stderr="", timed_out=False
                )
            )
        results.append(
            NamedCheckResult(
                name=name,
                required=required,
                result=result,
            )
        )
    return tuple(results)


def _changed_paths(
    reference_digests: dict[str, str], suffixes: tuple[str, ...]
) -> tuple[str, ...]:
    """Select sorted paths with a matching suffix that the round reference did not delete."""

    return tuple(
        sorted(
            path
            for path, digest in reference_digests.items()
            if path.endswith(suffixes) and digest != "deleted"
        )
    )


def _command_with_paths(base: tuple[str, ...], paths: tuple[str, ...]) -> tuple[str, ...]:
    """Append file paths after the argument separator."""

    return (*base, "--", *paths)


def _load_results(path: Path, task_id: str) -> ValidationResultsDocument:
    """Load checked results or create an empty document for the task.

    :raises OSError: If the file cannot be read.
    :raises ValueError: If the results are invalid or belong to another task.
    """

    if not path.exists():
        return ValidationResultsDocument(task_id=task_id)
    results = ValidationResultsDocument.model_validate_json(
        path.read_text(encoding="utf-8")
    )
    if results.task_id != task_id:
        raise ValueError(
            f"validation evidence task ID {results.task_id!r} does not match {task_id!r}"
        )
    return results


def _validate_next_attempt(
    results: ValidationResultsDocument, candidate_attempt: int, validation_round: int
) -> None:
    """Reject a candidate or round that repeats or skips a result.

    :raises ValueError: If the key is repeated or out of order.
    """

    keys = [
        (record.candidate_attempt, record.validation_round) for record in results.runs
    ]
    key = (candidate_attempt, validation_round)
    name = f"candidate attempt {candidate_attempt} round {validation_round}"
    if key in keys:
        raise ValueError(f"{name} has already been recorded")
    if not is_next_validation_key(keys[-1] if keys else None, key):
        raise ValueError(f"{name} is out of order")


def _write_results_atomically(
    repo_root: Path, path: Path, results: ValidationResultsDocument
) -> None:
    """Replace test results without leaving partial JSON.

    :raises ValueError: If the destination is unsafe.
    :raises OSError: If the file cannot be written or replaced.
    """

    path = require_safe_artifact_path(repo_root, path, label="validation results")
    path.parent.mkdir(parents=True, exist_ok=True)
    require_safe_artifact_path(repo_root, path, label="validation results")
    write_bytes(path, (results.model_dump_json(indent=2) + "\n").encode("utf-8"))


@validate_call
def run_tests(
    repo_root: Path,
    task_id: TaskId,
    candidate_attempt: CandidateAttempt,
    validation_round: ValidationRound = 1,
    *,
    include_harness_tests: StrictBool,
    reference_digests: dict[str, str],
) -> CandidateValidationRecord:
    """Run the fixed tests and all supervisor checks, and save the result.

    :param include_harness_tests: Whether to also run the ``tests/harness`` Python tests.
    :param reference_digests: Current round's change digests.
    :return: The saved record for the candidate and round.
    :raises OSError: If results cannot be read or written.
    :raises ValueError: If the request, results, or approved plan is invalid.
    """

    resolved_root = repo_root.resolve()
    task_paths = load_task_paths(resolved_root, task_id)
    results_path = require_safe_artifact_path(
        resolved_root, task_paths.validation_results, label="validation results"
    )
    existing = _load_results(results_path, task_id)
    _validate_next_attempt(existing, candidate_attempt, validation_round)
    plan = load_approved_plan(resolved_root, task_paths)

    result = _execute_tests(resolved_root, include_harness_tests=include_harness_tests)
    checks = _run_checks(resolved_root, plan, reference_digests)
    record = CandidateValidationRecord(
        candidate_attempt=candidate_attempt,
        validation_round=validation_round,
        timestamp=datetime.now(UTC),
        result=result,
        checks=checks,
    )
    combined = ValidationResultsDocument(
        task_id=task_id,
        runs=(*existing.runs, record),
    )
    _write_results_atomically(resolved_root, results_path, combined)
    return record
