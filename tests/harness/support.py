"""Task data and planning helpers that several harness test modules share."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

import harness.execute.routing as routing_module
from harness.contracts.actions import AgentAction, ApprovalAction
from harness.contracts.core import load_task_paths
from harness.contracts.results import BlockedOutput, CompleteOutput
from harness.contracts.validation import (
    CandidateValidationRecord,
    NamedCheckResult,
    ValidationCommandResult,
)
from harness.execute.supervisor import create_run, submit_result
from harness.feedback import save_approved_plan
from harness.utils.digests import sha256_artifact

TASK_ID = "01"

SPEC_TEXT = """# Specification

## Objective and observable behavior
Add one observable behavior.

## Boundaries, errors, and non-goals
Keep the change inside the application.

## Resolved decisions
Use the existing public interface.

## Requirements
- REQ-01: Add the requested behavior.

## Acceptance criteria
- AC-01
  - Criterion: The behavior is observable.
  - Evidence: A focused test passes.
"""


def plan_data(checks: list[dict[str, object]] | None = None) -> dict[str, object]:
    """Return a new valid plan that matches ``SPEC_TEXT``.

    :param checks: Checks that the plan selects. ``None`` leaves the field out.
    :return: Plan data that the caller can change.
    """

    data: dict[str, object] = {
        "schema_version": 1,
        "task_id": TASK_ID,
        "steps": [
            {
                "id": "STEP-01",
                "title": "Implement and test",
                "intent": "Add the approved behavior and its focused test.",
                "work_items": [
                    {
                        "path": "src/feature.py",
                        "intent": "Implement the behavior.",
                    },
                    {
                        "path": "src/test_feature.py",
                        "intent": "Test the behavior.",
                    },
                ],
                "requirement_ids": ["REQ-01"],
                "acceptance_ids": ["AC-01"],
            }
        ],
        "test_intents": {"AC-01": "Exercise the public behavior."},
    }
    if checks is not None:
        data["checks"] = checks
    return data


def write_approved_plan(repo_root: Path, plan: dict[str, object]) -> Path:
    """Write ``plan`` as the approved copy without validation.

    :param repo_root: Repository that holds the task.
    :param plan: Plan data. It can be invalid on purpose.
    :return: The path of the approved copy.
    """

    path = load_task_paths(repo_root, TASK_ID).approved_plan
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan), encoding="utf-8")
    return path


def approve_draft_plan(repo_root: Path) -> None:
    """Save the approved copy of the draft ``plan.json``, as plan approval does.

    :param repo_root: Repository that holds the draft plan.
    """

    task_paths = load_task_paths(repo_root, TASK_ID)
    digest = sha256_artifact(repo_root, task_paths.canonical_plan, label="canonical plan")
    save_approved_plan(repo_root, task_paths, digest)


def _repo(tmp_path: Path) -> Path:
    """Create the fixed repository directories needed to start a run."""

    app = tmp_path / "src"
    app.mkdir(parents=True)
    (app / "feature.py").write_text("VALUE = 1\n", encoding="utf-8")
    (app / "test_feature.py").write_text(
        "def test_value(): assert True\n", encoding="utf-8"
    )
    return tmp_path


def _author_result(
    action: AgentAction, *, status: str, **fields: object
) -> dict[str, object]:
    """Build an author result for the current saved action."""

    result: dict[str, object] = {
        "task_id": action.task_id,
        "action_id": action.action_id,
        "status": status,
        "files_written": [],
        "questions": [],
        "blockers": [],
    }
    result.update(fields)
    return result


def _approval_response(
    action: ApprovalAction,
    approved: bool,
    feedback: str | None = None,
) -> dict[str, object]:
    """Build a human response for the current approval action."""

    return {
        "kind": "approval-response",
        "task_id": action.task_id,
        "action_id": action.action_id,
        "artifact": action.artifact,
        "revision": action.revision,
        "approved": approved,
        "feedback": feedback,
    }


def _reach_spec_approval(repo: Path) -> ApprovalAction:
    """Start a run and return its first specification approval action."""

    state = create_run(repo, "Add one behavior.")
    action = state.pending_action
    assert isinstance(action, AgentAction)
    (repo / "planning" / state.task_id / "SPEC.md").write_text(
        SPEC_TEXT, encoding="utf-8"
    )
    output = submit_result(
        repo,
        state.task_id,
        _author_result(
            action,
            status="ready_for_approval",
            files_written=list(action.writable_paths),
        ),
    )
    assert isinstance(output, ApprovalAction)
    return output


def _reach_plan_approval(
    repo: Path, work_items: list[dict[str, str]] | None = None
) -> ApprovalAction:
    """Approve a valid spec and return the plan approval action."""

    spec_approval = _reach_spec_approval(repo)
    planner = submit_result(repo, "01", _approval_response(spec_approval, True))
    assert isinstance(planner, AgentAction)
    assert planner.role == "planner"
    plan = plan_data()
    if work_items is not None:
        plan["steps"][0]["work_items"] = work_items  # type: ignore[index]
    (repo / "planning" / "01" / "plan.json").write_text(
        json.dumps(plan), encoding="utf-8"
    )
    output = submit_result(
        repo,
        "01",
        _author_result(
            planner,
            status="ready_for_approval",
            files_written=list(planner.writable_paths),
        ),
    )
    assert isinstance(output, ApprovalAction)
    return output


def _reach_maker(repo: Path) -> AgentAction:
    """Approve a valid plan and return candidate 1's maker action."""

    approval = _reach_plan_approval(repo)
    output = submit_result(repo, "01", _approval_response(approval, True))
    assert isinstance(output, AgentAction)
    assert output.role == "maker"
    return output


def _submit_maker(
    repo: Path, maker: AgentAction, files_changed: list[str]
) -> AgentAction | BlockedOutput | CompleteOutput:
    """Submit an implemented maker or repair result that reports the given files."""

    return submit_result(
        repo,
        "01",
        {
            "task_id": "01",
            "action_id": maker.action_id,
            "status": "implemented",
            "files_changed": files_changed,
            "summary": "Implemented the approved behavior.",
            "blockers": [],
        },
    )


def _reach_checker(repo: Path, files_changed: list[str] | None = None) -> AgentAction:
    """Return the checker action for a candidate."""

    checker = _submit_maker(repo, _reach_maker(repo), files_changed or [])
    assert isinstance(checker, AgentAction)
    assert checker.role == "checker"
    return checker


def _checker_result(action: AgentAction, verdict: str = "pass") -> dict[str, object]:
    """Build a reviewed checker result with a pass or revise verdict."""

    findings: list[dict[str, object]] = []
    if verdict == "revise":
        findings.append(
            {
                "severity": "blocking",
                "path": "src/feature.py",
                "issue": "The behavior is incomplete.",
                "evidence": "The candidate returns the wrong value.",
                "recommended_correction": "Implement the approved behavior.",
            }
        )
    return {
        "task_id": action.task_id,
        "action_id": action.action_id,
        "result": {
            "status": "reviewed",
            "verdict": verdict,
            "findings": findings,
            "summary": "Review complete.",
            "blockers": [],
        },
    }


def _validation_record(
    attempt: int,
    status: str,
    *,
    launch_error: str | None = None,
) -> CandidateValidationRecord:
    """Build one fixed-command result for a patched supervisor test."""

    if status == "pass":
        exit_code = 0
    elif launch_error:
        exit_code = None
    else:
        exit_code = 1
    return CandidateValidationRecord(
        candidate_attempt=attempt,
        timestamp=datetime(2026, 8, 12, tzinfo=UTC),
        checks=(
            NamedCheckResult(
                name="ruff-lint",
                required=True,
                result=ValidationCommandResult(
                    status="skipped", stdout="", stderr="", timed_out=False
                ),
            ),
            NamedCheckResult(
                name="eslint",
                required=True,
                result=ValidationCommandResult(
                    status="skipped", stdout="", stderr="", timed_out=False
                ),
            ),
            NamedCheckResult(
                name="typecheck",
                required=True,
                result=ValidationCommandResult(
                    status="pass", exit_code=0, stdout="", stderr="", timed_out=False
                ),
            ),
        ),
        result=ValidationCommandResult(
            status=status,
            exit_code=exit_code,
            launch_error=launch_error,
            stdout="",
            stderr="tests failed" if status == "fail" else "",
            timed_out=False,
        ),
    )


def _patch_validation(
    monkeypatch: pytest.MonkeyPatch,
    records: dict[int, CandidateValidationRecord],
) -> list[tuple[int, int, bool]]:
    """Patch fixed validation while still writing the evidence read by gates.

    :return: The candidate, round, and harness-test flag of each call.
    """

    calls: list[tuple[int, int, bool]] = []

    def fake_run(
        repo_root: Path,
        task_id: str,
        attempt: int,
        validation_round: int = 1,
        *,
        include_harness_tests: bool,
        reference_digests: dict[str, str],
    ) -> CandidateValidationRecord:
        calls.append((attempt, validation_round, include_harness_tests))
        path = repo_root / "runs" / task_id / "validation" / "results.json"
        existing: list[dict[str, object]] = []
        if path.exists():
            existing = json.loads(path.read_text(encoding="utf-8"))["runs"]
        record = records[attempt].model_copy(
            update={"validation_round": validation_round}
        )
        existing.append(record.model_dump(mode="json"))
        path.write_text(
            json.dumps({"task_id": task_id, "runs": existing}), encoding="utf-8"
        )
        return record

    monkeypatch.setattr(routing_module, "run_tests", fake_run)
    return calls


def _harness_plan_items(path: str = "harness/gates.py") -> list[dict[str, str]]:
    return [
        {"path": "src/feature.py", "intent": "Implement."},
        {"path": path, "intent": "Adjust a gate."},
    ]


def _reach_harness_approval(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    path: str = "harness/gates.py",
    attempt: int = 1,
) -> ApprovalAction:
    """Change a planned protected file and return the approval of the given candidate."""

    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    (repo / path).write_text("GATE = 1\n", encoding="utf-8")
    _patch_validation(
        monkeypatch,
        {
            1: _validation_record(1, "pass" if attempt == 1 else "fail"),
            2: _validation_record(2, "pass"),
        },
    )
    approval = _reach_plan_approval(repo, _harness_plan_items(path))
    maker = submit_result(repo, "01", _approval_response(approval, True))
    assert isinstance(maker, AgentAction)
    (repo / path).write_text("GATE = 2\n", encoding="utf-8")
    checker = _submit_maker(repo, maker, [path])
    assert isinstance(checker, AgentAction)
    if attempt == 2:
        repair = submit_result(repo, "01", _checker_result(checker))
        assert isinstance(repair, AgentAction)
        checker = _submit_maker(repo, repair, [])
        assert isinstance(checker, AgentAction)
    pause = submit_result(repo, "01", _checker_result(checker))
    assert isinstance(pause, ApprovalAction)
    return pause


def _edit_spec(repo: Path) -> None:
    """Change the spec text while it stays valid."""

    (repo / "planning" / "01" / "SPEC.md").write_text(
        SPEC_TEXT.replace("Add one observable behavior.", "Add one visible behavior."),
        encoding="utf-8",
    )


def _blocked_reason(output: object) -> str:
    assert isinstance(output, BlockedOutput), output
    return output.reason
