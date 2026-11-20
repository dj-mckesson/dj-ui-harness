"""Run the application tests for one candidate and save their result."""

import os
import re
import shutil
import signal
import subprocess
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import StrictBool, validate_call

from harness.config import (
    HARNESS_TEST_COMMAND,
    CheckSpec,
    TestCommand,
    matches,
    project_config,
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
    repo_root: Path,
    command: tuple[str, ...],
    timeout: int,
    env: Mapping[str, str] | None = None,
) -> ValidationCommandResult:
    """Run a command and stop its process group on timeout.

    :param env: Extra environment variables for the command.
    :raises OSError: If process group cleanup fails.
    """

    try:
        process = subprocess.Popen(
            command,
            cwd=repo_root,
            **({"env": {**os.environ, **env}} if env else {}),
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


def _run(
    repo_root: Path,
    cwd: str,
    command: tuple[str, ...],
    timeout: int,
    env: Mapping[str, str],
) -> ValidationCommandResult:
    """Run one configured command in its folder, passing extra variables only when set."""

    folder = repo_root if cwd == "." else repo_root / cwd
    if env:
        return _execute(folder, command, timeout, env)
    return _execute(folder, command, timeout)


def _labelled(test: TestCommand, result: ValidationCommandResult) -> str:
    return f"== {test.name}: {' '.join(test.command)} ({result.status}) ==\n"


def _execute_tests(
    repo_root: Path, *, include_harness_tests: bool
) -> ValidationCommandResult:
    """Run the project tests from ``harness.toml``, then the harness tests when requested.

    The commands run in order and stop at the first failure.

    :return: The failing result, or a passing result that holds every command's output.
    """

    tests = list(project_config().tests)
    if include_harness_tests:
        tests.append(TestCommand("harness", HARNESS_TEST_COMMAND, TEST_TIMEOUT_SECONDS))
    if len(tests) == 1:
        test = tests[0]
        return _run(repo_root, test.cwd, test.command, test.timeout, test.env)
    stdout: list[str] = []
    stderr: list[str] = []
    for test in tests:
        result = _run(repo_root, test.cwd, test.command, test.timeout, test.env)
        stdout.append(_labelled(test, result) + result.stdout)
        stderr.append(_labelled(test, result) + result.stderr)
        if result.status != "pass":
            return result.model_copy(
                update={
                    "stdout": _bounded_output("\n".join(stdout)),
                    "stderr": _bounded_output("\n".join(stderr)),
                }
            )
    return ValidationCommandResult(
        status="pass",
        exit_code=0,
        stdout=_bounded_output("\n".join(stdout)),
        stderr=_bounded_output("\n".join(stderr)),
        timed_out=False,
    )


def _paths_for(spec: CheckSpec, paths: tuple[str, ...]) -> tuple[str, ...]:
    """Keep the paths inside the check's folder, relative to that folder."""

    if spec.cwd == ".":
        return paths
    prefix = f"{spec.cwd}/"
    return tuple(path[len(prefix) :] for path in paths if path.startswith(prefix))


def _run_checks(
    repo_root: Path, plan: PlanDocument, reference_digests: dict[str, str]
) -> tuple[NamedCheckResult, ...]:
    """Run mandatory checks, then the checks the task type adds, then the plan's checks.

    When several ``red-tests`` checks run, as in a repository with a backend and a frontend,
    a check with no changed test file is skipped as long as another one has a changed test.
    """

    config = project_config()
    selected = [(name, True) for name in (*config.mandatory, *plan.task_checks)]
    selected.extend((check.name, check.required) for check in plan.checks)
    red_paths = {
        name: _paths_for(spec, _changed_paths(reference_digests, spec.files or ()))
        for name, _ in selected
        if (spec := config.check(name)).kind == "red-tests"
    }
    any_red_tests = any(red_paths.values())
    results = []
    for name, required in selected:
        spec = config.check(name)
        if spec.kind == "red-tests":
            paths = red_paths[name]
            result = (
                _skipped()
                if not paths and any_red_tests
                else _run_red_tests(repo_root, spec, paths)
            )
        elif spec.files is None:
            result = _run(repo_root, spec.cwd, spec.command, spec.timeout, spec.env)
        else:
            changed = _paths_for(spec, _changed_paths(reference_digests, spec.files))
            result = (
                _run(
                    repo_root,
                    spec.cwd,
                    _command_with_paths(spec.command, changed, spec.separator),
                    spec.timeout,
                    spec.env,
                )
                if changed
                else _skipped()
            )
        results.append(
            NamedCheckResult(
                name=name,
                required=required,
                result=result,
            )
        )
    return tuple(results)


def _skipped() -> ValidationCommandResult:
    return ValidationCommandResult(
        status="skipped", stdout="", stderr="", timed_out=False
    )


def _run_red_tests(
    repo_root: Path, spec: CheckSpec, paths: tuple[str, ...]
) -> ValidationCommandResult:
    """Run the changed test files on a clean checkout of ``HEAD`` and require a failure.

    A test that also passes without the change proves nothing about the change. The baseline
    is ``HEAD``, so uncommitted changes made before the run count as part of the change.

    :param paths: Changed test files, relative to the check's folder.
    :return: ``pass`` when the tests fail on the baseline, otherwise ``fail``.
    """

    if not paths:
        return _failed_check("no added or changed test files; add a test for the change")
    with tempfile.TemporaryDirectory(prefix="harness-red-") as temporary:
        baseline = Path(temporary) / "baseline"
        added = _execute(
            repo_root,
            ("git", "worktree", "add", "--detach", "--quiet", str(baseline), "HEAD"),
            120,
        )
        if added.status != "pass":
            return added
        try:
            for link in spec.link:
                source = repo_root / link
                target = baseline / link
                if source.is_dir() and not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.symlink_to(source, target_is_directory=True)
            folder = baseline if spec.cwd == "." else baseline / spec.cwd
            for path in paths:
                target = folder / path
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(
                    (repo_root if spec.cwd == "." else repo_root / spec.cwd) / path,
                    target,
                )
            result = _run(
                baseline,
                spec.cwd,
                _command_with_paths(spec.command, paths, spec.separator),
                spec.timeout,
                spec.env,
            )
        finally:
            _execute(
                repo_root, ("git", "worktree", "remove", "--force", str(baseline)), 120
            )
    if result.launch_error is not None or result.timed_out:
        return result
    pattern = re.compile(spec.failure_pattern or "")
    if result.status == "fail" and pattern.search(result.stdout + result.stderr) is None:
        return _failed_check(
            "the baseline run failed before reporting test results",
            result.stdout,
            result.stderr,
        )
    if result.status == "pass":
        return _failed_check(
            "the changed tests also pass without the change", result.stdout, result.stderr
        )
    return ValidationCommandResult(
        status="pass",
        exit_code=0,
        stdout=f"The changed tests fail on HEAD (exit {result.exit_code}).\n{result.stdout}",
        stderr=result.stderr,
        timed_out=False,
    )


def _failed_check(
    reason: str, stdout: str = "", stderr: str = ""
) -> ValidationCommandResult:
    """Build a failed result for a check whose outcome is not a command exit code."""

    return ValidationCommandResult(
        status="fail",
        exit_code=None,
        launch_error=reason,
        stdout=stdout,
        stderr=stderr,
        timed_out=False,
    )


def _changed_paths(
    reference_digests: dict[str, str], patterns: tuple[str, ...]
) -> tuple[str, ...]:
    """Select sorted paths that match a pattern and that the round reference did not delete."""

    return tuple(
        sorted(
            path
            for path, digest in reference_digests.items()
            if matches(path, patterns) and digest != "deleted"
        )
    )


def _command_with_paths(
    base: tuple[str, ...], paths: tuple[str, ...], separator: str | None = "--"
) -> tuple[str, ...]:
    """Append file paths, after the separator when the check uses one."""

    return (*base, *((separator,) if separator else ()), *paths)


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

    :param include_harness_tests: Whether to also run the harness's own tests.
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
