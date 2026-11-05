import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from harness.contracts.core import load_task_paths
from harness.contracts.models import (
    CheckerEvidence,
    ManualObservation,
    ReviewedCheckerResult,
)
from harness.contracts.plan import PlanDocument
from harness.contracts.results import CompletionEvidence
from harness.feedback.evidence import build_report, save_checker_evidence
from tests.harness.support import TASK_ID, write_approved_plan

RESULT_OK = {
    "status": "pass",
    "exit_code": 0,
    "launch_error": None,
    "stdout": "",
    "stderr": "",
    "timed_out": False,
}
RESULT_FAIL = {**RESULT_OK, "status": "fail", "exit_code": 1}


def _plan(manual: list[dict[str, str]]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "task_id": TASK_ID,
        "steps": [
            {
                "id": "STEP-01",
                "title": "Do it",
                "intent": "Do it.",
                "work_items": [{"path": "src/a.py", "intent": "Edit."}],
                "requirement_ids": ["REQ-01"],
                "acceptance_ids": ["AC-01", "AC-02"],
            }
        ],
        "test_intents": {"AC-01": "Test one.", "AC-02": "Docs read well."},
        "checks": [{"name": "ruff-format", "required": False}],
        "manual_reviews": manual,
    }


def _review(acceptance_id: str = "AC-02") -> dict[str, str]:
    return {
        "acceptance_id": acceptance_id,
        "inspect": "docs/harness.md",
        "how": "Read the new section.",
        "pass_if": "It names every check.",
    }


def _repo(
    tmp_path: Path,
    observations: list[dict[str, str]],
    *,
    tests: dict[str, object] = RESULT_OK,
    manual: list[dict[str, str]] | None = None,
    checker_blocked: bool = False,
) -> Path:
    repo_root = tmp_path / "repo"
    task_paths = load_task_paths(repo_root, TASK_ID)
    write_approved_plan(repo_root, _plan([_review()] if manual is None else manual))
    if checker_blocked:
        result: dict[str, object] = {
            "status": "blocked",
            "verdict": None,
            "findings": [],
            "summary": "",
            "blockers": ["no context"],
        }
    else:
        result = {
            "status": "reviewed",
            "verdict": "pass",
            "findings": [],
            "summary": "Fine.",
            "blockers": [],
            "observations": observations,
        }
    save_checker_evidence(
        repo_root,
        task_paths,
        CheckerEvidence.model_validate(
            {"task_id": TASK_ID, "attempt": 1, "result": result}
        ),
    )
    task_paths.validation_results.parent.mkdir(parents=True)
    record = {
        "candidate_attempt": 1,
        "timestamp": datetime.now(UTC).isoformat(),
        "result": tests,
        "checks": [
            {"name": "typecheck", "required": True, "result": RESULT_OK},
            {"name": "eslint", "required": True, "result": RESULT_OK},
            {"name": "ruff-lint", "required": True, "result": RESULT_OK},
            {"name": "ruff-format", "required": False, "result": RESULT_FAIL},
        ],
    }
    task_paths.validation_results.write_text(
        json.dumps({"task_id": TASK_ID, "runs": [record]}), encoding="utf-8"
    )
    return repo_root


def _report(repo_root: Path):
    return build_report(repo_root, load_task_paths(repo_root, TASK_ID), 1)


def test_report_maps_each_criterion_to_tests_and_each_check_to_its_result(
    tmp_path: Path,
) -> None:
    conditions, checks = _report(_repo(tmp_path, []))

    assert [(c.acceptance_id, c.method) for c in conditions] == [
        ("AC-01", "tests"),
        ("AC-02", "manual-review"),
    ]
    assert all(c.result == "pass" for c in conditions if c.method == "tests")
    assert [(c.name, c.required, c.result) for c in checks] == [
        ("typecheck", True, "pass"),
        ("eslint", True, "pass"),
        ("ruff-lint", True, "pass"),
        ("ruff-format", False, "fail"),
    ]


def test_report_preserves_a_saved_skipped_ruff_result(tmp_path: Path) -> None:
    repo_root = _repo(tmp_path, [])
    path = load_task_paths(repo_root, TASK_ID).validation_results
    data = json.loads(path.read_text(encoding="utf-8"))
    data["runs"][0]["checks"][2]["result"] = {
        "status": "skipped",
        "stdout": "",
        "stderr": "",
        "timed_out": False,
    }
    path.write_text(json.dumps(data), encoding="utf-8")

    _, checks = _report(repo_root)

    assert checks[2].name == "ruff-lint"
    assert checks[2].result == "skipped"


def test_manual_review_without_an_observation_is_pending_not_passed(
    tmp_path: Path,
) -> None:
    conditions, _ = _report(_repo(tmp_path, []))
    manual = next(c for c in conditions if c.method == "manual-review")

    assert manual.result == "pending"
    assert "Not verified" in manual.note
    assert "It names every check." in manual.note


def test_manual_review_uses_the_checker_observation(tmp_path: Path) -> None:
    observation = {
        "acceptance_id": "AC-02",
        "outcome": "pass",
        "note": "Section lists all.",
    }
    conditions, _ = _report(_repo(tmp_path, [observation]))
    manual = next(c for c in conditions if c.method == "manual-review")

    assert (manual.result, manual.note) == ("pass", "Section lists all.")


def test_blocked_checker_leaves_a_manual_review_pending(tmp_path: Path) -> None:
    conditions, _ = _report(_repo(tmp_path, [], checker_blocked=True))

    assert next(c for c in conditions if c.method == "manual-review").result == "pending"


def test_failed_suite_is_reported_for_every_criterion(tmp_path: Path) -> None:
    conditions, _ = _report(_repo(tmp_path, [], tests=RESULT_FAIL, manual=[]))

    assert {c.method for c in conditions} == {"tests"}
    assert all(c.result == "fail" for c in conditions)


def test_manual_reviews_come_from_the_approved_plan_not_the_edited_draft(
    tmp_path: Path,
) -> None:
    repo_root = _repo(tmp_path, [])
    task_paths = load_task_paths(repo_root, TASK_ID)
    task_paths.canonical_plan.parent.mkdir(parents=True)
    task_paths.canonical_plan.write_text(json.dumps(_plan([])), encoding="utf-8")

    conditions, _ = _report(repo_root)

    assert [(c.acceptance_id, c.method) for c in conditions] == [
        ("AC-01", "tests"),
        ("AC-02", "manual-review"),
    ]


def test_missing_approved_plan_is_an_error(tmp_path: Path) -> None:
    repo_root = _repo(tmp_path, [])
    load_task_paths(repo_root, TASK_ID).approved_plan.unlink()

    with pytest.raises(ValueError, match="approved plan could not be read"):
        _report(repo_root)


def test_observation_rules() -> None:
    base = {
        "status": "reviewed",
        "verdict": "pass",
        "findings": [],
        "summary": "ok",
        "blockers": [],
    }
    seen = {"acceptance_id": "AC-01", "outcome": "pass", "note": "Saw it."}
    assert ReviewedCheckerResult.model_validate(base).observations == []
    with pytest.raises(ValidationError, match="failed manual observation"):
        ReviewedCheckerResult.model_validate(
            {**base, "observations": [{**seen, "outcome": "fail"}]}
        )
    with pytest.raises(ValidationError, match="must not repeat"):
        ReviewedCheckerResult.model_validate({**base, "observations": [seen, seen]})


def test_plan_rejects_a_repeated_manual_review() -> None:
    with pytest.raises(ValidationError, match="manual review acceptance IDs"):
        PlanDocument.model_validate(_plan([_review(), _review()]))


def test_completion_evidence_saved_before_the_report_existed_still_loads() -> None:
    evidence = CompletionEvidence.model_validate(
        {
            "candidate_attempt": 1,
            "checker_evidence_path": "runs/01/reviews/checker-1.json",
            "validation_evidence_path": "runs/01/validation/results.json",
        }
    )
    assert evidence.conditions == () and evidence.checks == ()


def test_checker_skill_example_observation_is_valid() -> None:
    skill = (
        Path(__file__).resolve().parents[2]
        / ".agents"
        / "skills"
        / "checker"
        / "SKILL.md"
    )
    example = re.search(
        r'^\{"acceptance_id".*\}$', skill.read_text(encoding="utf-8"), re.M
    )
    assert example is not None
    ManualObservation.model_validate_json(example.group(0))
