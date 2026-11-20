import json
from collections.abc import Callable
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path

import pytest
from pydantic import ValidationError

from harness.config import load_preset, preset_names, project_config
from harness.contracts.checks import approved_checks, mandatory_checks
from harness.contracts.plan import PlanDocument
from harness.contracts.validation import (
    CandidateValidationRecord,
    ValidationCommandResult,
)
from harness.feedback.run_tests import run_tests
from tests.harness.support import TASK_ID, plan_data, write_approved_plan

runner = import_module("harness.feedback.run_tests")
APPROVED_CHECKS = approved_checks()
MANDATORY_CHECKS = mandatory_checks()
TEST_COMMAND = project_config().tests[0].command
PASS = {"status": "pass", "exit_code": 0, "stdout": "", "stderr": "", "timed_out": False}
SKIPPED = {"status": "skipped", "stdout": "", "stderr": "", "timed_out": False}


def _repo(tmp_path: Path, checks: list[dict[str, object]] | None = None) -> Path:
    repo_root = tmp_path / "repo"
    write_approved_plan(repo_root, plan_data(checks))
    return repo_root


def _fake_run(
    failing: set[str], calls: list[tuple[str, ...]]
) -> Callable[[Path, tuple[str, ...], int], ValidationCommandResult]:
    def run(
        repo_root: Path,
        command: tuple[str, ...],
        timeout: int,
        env: object = None,
    ) -> ValidationCommandResult:
        calls.append(command)
        failed = any(name in " ".join(command) for name in failing)
        return ValidationCommandResult(
            status="fail" if failed else "pass",
            exit_code=1 if failed else 0,
            stdout="out",
            stderr="err",
            timed_out=False,
        )

    return run


def _record(checks: list[dict[str, object]]) -> dict[str, object]:
    return {
        "candidate_attempt": 1,
        "timestamp": datetime.now(UTC).isoformat(),
        "result": PASS,
        "checks": checks,
    }


def test_nextjs_preset_matches_the_original_pricewave_checks() -> None:
    harness_ruff = ("uv", "run", "--project", ".harness", "--no-sync", "ruff")
    assert MANDATORY_CHECKS == ("ruff-lint", "eslint", "typecheck")
    assert {name: spec.command for name, spec in APPROVED_CHECKS.items()} == {
        "ruff-lint": (*harness_ruff, "check", "--config=.harness/ruff.toml"),
        "ruff-format": (
            *harness_ruff,
            "format",
            "--check",
            "--config=.harness/ruff.toml",
        ),
        "eslint": ("pnpm", "exec", "eslint", "--no-warn-ignored"),
        "prettier": ("pnpm", "exec", "prettier", "--check", "--ignore-unknown"),
        "typecheck": ("pnpm", "typecheck"),
        "red-tests": ("node_modules/.bin/vitest", "run"),
        "browser": ("pnpm", "exec", "playwright", "test", "--project=smoke"),
        "a11y": ("pnpm", "exec", "playwright", "test", "--project=a11y"),
    }
    timeouts = {
        "ruff-lint": 120,
        "ruff-format": 120,
        "eslint": 180,
        "prettier": 120,
        "browser": 600,
        "a11y": 600,
    }
    scopes = {
        "ruff-lint": "changed-files",
        "ruff-format": "changed-files",
        "eslint": "changed-files",
        "prettier": "changed-files",
        "typecheck": "repository",
        "red-tests": "changed-files",
        "browser": "repository",
        "a11y": "repository",
    }
    for name, spec in APPROVED_CHECKS.items():
        assert isinstance(spec.command, tuple)
        assert spec.timeout == timeouts.get(name, 300)
        assert spec.scope == scopes[name]
        with pytest.raises(FrozenInstanceError):
            spec.timeout = 1  # type: ignore[misc]
    assert all("." not in spec.command for spec in APPROVED_CHECKS.values())
    assert TEST_COMMAND == ("pnpm", "test", "--maxWorkers=1")


@pytest.mark.parametrize("preset", preset_names())
def test_every_preset_loads_and_its_task_types_name_defined_checks(preset: str) -> None:
    config = load_preset(preset)

    assert config.tests and config.mandatory
    for names in config.task_types.values():
        assert set(names) <= set(config.checks)
        assert not set(names) & set(config.mandatory)
    assert all(
        spec.files and spec.failure_pattern
        for spec in config.checks.values()
        if spec.kind == "red-tests"
    )


def test_empty_plan_runs_mandatory_checks_and_skips_changed_file_checks_without_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = _repo(tmp_path)
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(runner, "_execute", _fake_run(set(), calls))

    record = run_tests(
        repo_root,
        TASK_ID,
        1,
        include_harness_tests=False,
        reference_digests={".ruff.toml": "abc"},
    )

    assert PlanDocument.model_validate(plan_data()).checks == ()
    assert calls == [TEST_COMMAND, APPROVED_CHECKS["typecheck"].command]
    assert [(c.name, c.required, c.result.status) for c in record.checks] == [
        ("ruff-lint", True, "skipped"),
        ("eslint", True, "skipped"),
        ("typecheck", True, "pass"),
    ]
    assert record.status == "pass"


def test_changed_file_checks_use_sorted_changed_paths_after_separator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = _repo(tmp_path, [{"name": "ruff-format", "required": False}])
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(runner, "_execute", _fake_run({"format"}, calls))
    digests = {
        "z.py": "changed",
        "-odd.py": "added",
        "gone.py": "deleted",
        "a.txt": "added",
        "b.tsx": "added",
        "a.ts": "changed",
        "old.ts": "deleted",
    }

    record = run_tests(
        repo_root, TASK_ID, 1, include_harness_tests=False, reference_digests=digests
    )

    paths = ("--", "-odd.py", "z.py")
    web_paths = ("--", "a.ts", "b.tsx")
    assert calls == [
        TEST_COMMAND,
        (*APPROVED_CHECKS["ruff-lint"].command, *paths),
        (*APPROVED_CHECKS["eslint"].command, *web_paths),
        APPROVED_CHECKS["typecheck"].command,
        (*APPROVED_CHECKS["ruff-format"].command, *paths),
    ]
    assert [(c.name, c.required, c.result.status) for c in record.checks] == [
        ("ruff-lint", True, "pass"),
        ("eslint", True, "pass"),
        ("typecheck", True, "pass"),
        ("ruff-format", False, "fail"),
    ]
    assert record.status == "pass"


def test_mandatory_failure_fails_candidate_even_when_later_checks_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = _repo(tmp_path)
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(runner, "_execute", _fake_run({"ruff check"}, calls))

    record = run_tests(
        repo_root,
        TASK_ID,
        1,
        include_harness_tests=False,
        reference_digests={"a.py": "changed"},
    )

    assert [c.result.status for c in record.checks] == ["fail", "skipped", "pass"]
    assert len(calls) == 3
    assert record.status == "fail"


def test_approved_plan_controls_optional_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = _repo(tmp_path, [{"name": "ruff-format"}])
    draft = repo_root / "planning" / TASK_ID / "plan.json"
    draft.parent.mkdir(parents=True)
    draft.write_text(json.dumps(plan_data()), encoding="utf-8")
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(runner, "_execute", _fake_run(set(), calls))

    record = run_tests(
        repo_root, TASK_ID, 1, include_harness_tests=False, reference_digests={}
    )

    assert [c.name for c in record.checks] == [
        "ruff-lint",
        "eslint",
        "typecheck",
        "ruff-format",
    ]
    assert [c.result.status for c in record.checks] == [
        "skipped",
        "skipped",
        "pass",
        "skipped",
    ]


def test_missing_approved_plan_rejects_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "repo"
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(runner, "_execute", _fake_run(set(), calls))
    with pytest.raises(ValueError, match="approved plan could not be read"):
        run_tests(
            repo_root, TASK_ID, 1, include_harness_tests=False, reference_digests={}
        )
    assert calls == []


def test_check_timeout_uses_registry_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = _repo(tmp_path)
    timeouts: list[object] = []

    def run(
        repo_root: Path,
        command: tuple[str, ...],
        timeout: int,
        env: object = None,
    ) -> ValidationCommandResult:
        timeouts.append(timeout)
        if command[:5] == APPROVED_CHECKS["ruff-lint"].command[:5]:
            return ValidationCommandResult(
                status="fail",
                stdout="x" * runner.TEST_OUTPUT_LIMIT,
                stderr="",
                timed_out=True,
            )
        return ValidationCommandResult(
            status="pass", exit_code=0, stdout="", stderr="", timed_out=False
        )

    monkeypatch.setattr(runner, "_execute", run)
    record = run_tests(
        repo_root,
        TASK_ID,
        1,
        include_harness_tests=False,
        reference_digests={"a.py": "changed"},
    )
    assert timeouts == [runner.TEST_TIMEOUT_SECONDS, 120, 300]
    assert record.checks[0].result.timed_out
    assert len(record.checks[0].result.stdout) <= runner.TEST_OUTPUT_LIMIT
    assert record.status == "fail"


@pytest.mark.parametrize("name", MANDATORY_CHECKS)
@pytest.mark.parametrize("required", [True, False])
def test_plan_rejects_mandatory_names(name: str, required: bool) -> None:
    with pytest.raises(ValidationError, match="mandatory check"):
        PlanDocument.model_validate(plan_data([{"name": name, "required": required}]))


@pytest.mark.parametrize("required", [True, False])
def test_plan_accepts_optional_format(required: bool) -> None:
    plan = PlanDocument.model_validate(
        plan_data([{"name": "ruff-format", "required": required}])
    )
    assert plan.checks[0].required is required


def test_plan_rejects_unknown_duplicate_and_command() -> None:
    with pytest.raises(ValidationError):
        PlanDocument.model_validate(plan_data([{"name": "unsafe"}]))
    with pytest.raises(ValidationError, match="plan check names"):
        PlanDocument.model_validate(
            plan_data([{"name": "ruff-format"}, {"name": "ruff-format"}])
        )
    with pytest.raises(ValidationError):
        PlanDocument.model_validate(plan_data([{"name": "ruff-format", "command": "ls"}]))


@pytest.mark.parametrize(
    "mutation",
    [
        lambda checks: checks[:1],
        lambda checks: [*checks, checks[0]],
        lambda checks: [{**checks[0], "required": False}, *checks[1:]],
        lambda checks: [*checks[:2], {**checks[2], "result": SKIPPED}],
    ],
)
def test_invalid_saved_check_evidence_is_rejected(
    mutation: Callable[[list[dict[str, object]]], list[dict[str, object]]],
) -> None:
    checks = [
        {"name": "ruff-lint", "required": True, "result": PASS},
        {"name": "eslint", "required": True, "result": PASS},
        {"name": "typecheck", "required": True, "result": PASS},
    ]
    with pytest.raises(ValidationError):
        CandidateValidationRecord.model_validate(_record(mutation(checks)))


def test_required_tests_cannot_be_skipped() -> None:
    checks = [
        {"name": "ruff-lint", "required": True, "result": SKIPPED},
        {"name": "eslint", "required": True, "result": SKIPPED},
        {"name": "typecheck", "required": True, "result": PASS},
    ]
    with pytest.raises(ValidationError, match="required tests cannot be skipped"):
        CandidateValidationRecord.model_validate({**_record(checks), "result": SKIPPED})
    assert CandidateValidationRecord.model_validate(_record(checks)).status == "pass"


def test_skipped_command_has_no_execution_evidence() -> None:
    with pytest.raises(ValidationError, match="execution evidence"):
        ValidationCommandResult.model_validate({**SKIPPED, "stdout": "output"})


def _typed_repo(tmp_path: Path, task_type: str) -> Path:
    repo_root = tmp_path / "repo"
    write_approved_plan(repo_root, {**plan_data(), "task_type": task_type})
    return repo_root


@pytest.mark.parametrize(
    ("task_type", "added"),
    [
        ("feature", ("red-tests", "browser")),
        ("bug-fix", ("red-tests",)),
        ("ui-change", ("browser", "a11y")),
        ("refactor", ()),
        ("docs", ()),
        ("other", ()),
    ],
)
def test_task_type_adds_required_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    task_type: str,
    added: tuple[str, ...],
) -> None:
    repo_root = _typed_repo(tmp_path, task_type)
    monkeypatch.setattr(runner, "_execute", _fake_run(set(), []))
    monkeypatch.setattr(
        runner, "_run_red_tests", lambda *_: ValidationCommandResult(**PASS)
    )

    record = run_tests(
        repo_root, TASK_ID, 1, include_harness_tests=False, reference_digests={}
    )

    assert [(c.name, c.required) for c in record.checks] == [
        ("ruff-lint", True),
        ("eslint", True),
        ("typecheck", True),
        *((name, True) for name in added),
    ]


def test_plan_without_task_type_is_other_and_adds_no_checks() -> None:
    plan = PlanDocument.model_validate(plan_data())
    assert plan.task_type == "other"
    assert plan.task_checks == ()


def test_plan_rejects_a_check_that_its_task_type_requires() -> None:
    data = {**plan_data([{"name": "browser"}]), "task_type": "ui-change"}
    with pytest.raises(ValidationError, match="task type already requires"):
        PlanDocument.model_validate(data)


def _git_repo(tmp_path: Path) -> Path:
    repo_root = _typed_repo(tmp_path, "bug-fix")
    real_execute = runner._execute
    for command in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "harness@example.com"),
        ("git", "config", "user.name", "Harness"),
        ("git", "commit", "-q", "--allow-empty", "-m", "baseline"),
    ):
        assert real_execute(repo_root, command, 30).status == "pass"
    (repo_root / "src").mkdir()
    (repo_root / "src" / "a.test.ts").write_text("new test\n", encoding="utf-8")
    return repo_root


def _red_run(
    repo_root: Path, *, tests_fail: bool, seen: list[Path]
) -> ValidationCommandResult:
    spec = APPROVED_CHECKS["red-tests"]
    real_execute = runner._execute

    def run(cwd: Path, command: tuple[str, ...], timeout: int) -> ValidationCommandResult:
        if command[0] == "git":
            return real_execute(cwd, command, timeout)
        seen.append(cwd)
        assert command == (*spec.command, "src/a.test.ts")
        assert (cwd / "src" / "a.test.ts").read_text(encoding="utf-8") == "new test\n"
        return ValidationCommandResult(
            status="fail" if tests_fail else "pass",
            exit_code=1 if tests_fail else 0,
            stdout=" Test Files  1 failed (1)\n"
            if tests_fail
            else " Test Files  1 passed (1)\n",
            stderr="",
            timed_out=False,
        )

    runner._execute = run
    try:
        return runner._run_red_tests(repo_root, spec, ("src/a.test.ts",))
    finally:
        runner._execute = real_execute


def test_red_tests_pass_when_the_changed_tests_fail_on_a_clean_head(
    tmp_path: Path,
) -> None:
    repo_root = _git_repo(tmp_path)
    seen: list[Path] = []

    result = _red_run(repo_root, tests_fail=True, seen=seen)

    assert result.status == "pass"
    assert "fail on HEAD (exit 1)" in result.stdout
    assert len(seen) == 1 and seen[0] != repo_root
    assert not seen[0].exists()


def test_red_tests_fail_when_the_changed_tests_also_pass_without_the_change(
    tmp_path: Path,
) -> None:
    repo_root = _git_repo(tmp_path)

    result = _red_run(repo_root, tests_fail=False, seen=[])

    assert result.status == "fail"
    assert result.launch_error == "the changed tests also pass without the change"


def test_red_tests_fail_when_no_test_file_changed(tmp_path: Path) -> None:
    result = runner._run_red_tests(tmp_path, APPROVED_CHECKS["red-tests"], ())

    assert result.status == "fail"
    assert result.launch_error is not None
    assert "add a test for the change" in result.launch_error


def test_red_tests_fail_when_the_baseline_run_breaks_before_reporting_tests(
    tmp_path: Path,
) -> None:
    repo_root = _git_repo(tmp_path)
    real_execute = runner._execute

    def run(cwd: Path, command: tuple[str, ...], timeout: int) -> ValidationCommandResult:
        if command[0] == "git":
            return real_execute(cwd, command, timeout)
        return ValidationCommandResult(
            status="fail",
            exit_code=1,
            stdout="",
            stderr="Error: failed to load config",
            timed_out=False,
        )

    runner._execute = run
    try:
        result = runner._run_red_tests(
            repo_root, APPROVED_CHECKS["red-tests"], ("src/a.test.ts",)
        )
    finally:
        runner._execute = real_execute

    assert result.status == "fail"
    assert result.launch_error == "the baseline run failed before reporting test results"
