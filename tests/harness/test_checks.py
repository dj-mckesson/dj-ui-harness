import json
from collections.abc import Callable
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from typing import get_args

import pytest
from pydantic import ValidationError

from harness.contracts.checks import APPROVED_CHECKS, MANDATORY_CHECKS, CheckName
from harness.contracts.plan import PlanDocument
from harness.contracts.validation import (
    CandidateValidationRecord,
    ValidationCommandResult,
)
from harness.feedback.run_tests import run_tests
from tests.harness.support import TASK_ID, plan_data, write_approved_plan

runner = import_module("harness.feedback.run_tests")
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
        repo_root: Path, command: tuple[str, ...], timeout: int
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


def test_registry_is_exact_and_frozen() -> None:
    assert set(APPROVED_CHECKS) == set(get_args(CheckName))
    assert MANDATORY_CHECKS == ("ruff-lint", "eslint", "typecheck")
    assert {name: spec.command for name, spec in APPROVED_CHECKS.items()} == {
        "ruff-lint": ("uv", "run", "--no-sync", "ruff", "check", "--config=.ruff.toml"),
        "ruff-format": (
            "uv",
            "run",
            "--no-sync",
            "ruff",
            "format",
            "--check",
            "--config=.ruff.toml",
        ),
        "eslint": ("pnpm", "exec", "eslint", "--no-warn-ignored"),
        "prettier": ("pnpm", "exec", "prettier", "--check", "--ignore-unknown"),
        "typecheck": ("pnpm", "typecheck"),
    }
    timeouts = {"ruff-lint": 120, "ruff-format": 120, "eslint": 180, "prettier": 120}
    scopes = {
        "ruff-lint": "changed-python-files",
        "ruff-format": "changed-python-files",
        "eslint": "changed-web-files",
        "prettier": "changed-web-files",
        "typecheck": "repository",
    }
    for name, spec in APPROVED_CHECKS.items():
        assert isinstance(spec.command, tuple)
        assert spec.timeout == timeouts.get(name, 300)
        assert spec.scope == scopes[name]
        with pytest.raises(FrozenInstanceError):
            spec.timeout = 1
    assert all("." not in spec.command for spec in APPROVED_CHECKS.values())


def test_registry_config_exists_in_the_repository() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    for name in (".ruff.toml", "eslint.config.mjs", "package.json"):
        assert (repo_root / name).is_file()


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
    assert calls == [runner.TEST_COMMAND, APPROVED_CHECKS["typecheck"].command]
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
        runner.TEST_COMMAND,
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
        repo_root: Path, command: tuple[str, ...], timeout: int
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
def test_plan_rejects_mandatory_names(name: CheckName, required: bool) -> None:
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
