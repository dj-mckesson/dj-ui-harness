import json
import os
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from unittest.mock import Mock

import pytest

import harness.design.rendering as rendering
from harness import load_task_paths
from harness.contracts.models import CheckerEvidence, GateResult
from harness.contracts.validation import ValidationResultsDocument
from harness.feedback.evidence import (
    load_checker_evidence,
    load_validation_evidence,
    save_checker_evidence,
    validated_evidence_path,
)
from harness.gates import after_checker, before_complete, before_maker
from harness.utils import atomic as atomic_module
from tests.harness.support import SPEC_TEXT, TASK_ID, plan_data, write_approved_plan


def _make_repo(tmp_path: Path) -> Path:
    repo_root = tmp_path / "repo"
    planning_dir = repo_root / "planning" / TASK_ID
    app_dir = repo_root / "src"
    planning_dir.mkdir(parents=True)
    app_dir.mkdir(parents=True)
    (planning_dir / "SPEC.md").write_text(SPEC_TEXT, encoding="utf-8")
    (planning_dir / "plan.json").write_text(json.dumps(plan_data()), encoding="utf-8")
    (planning_dir / "PLAN.md").write_text("# Human-readable plan\n", encoding="utf-8")
    write_approved_plan(repo_root, plan_data())
    return repo_root


def _checker_result(
    status: Literal["reviewed", "blocked"],
    verdict: Literal["pass", "revise"] | None,
) -> dict[str, object]:
    if status == "blocked":
        return {
            "status": "blocked",
            "verdict": None,
            "findings": [],
            "summary": "",
            "blockers": ["The candidate could not be inspected."],
        }
    findings: list[dict[str, object]] = []
    if verdict == "revise":
        findings.append(
            {
                "severity": "blocking",
                "path": "src/feature.py",
                "issue": "The required behavior is missing.",
                "evidence": "The candidate returns the wrong value.",
                "recommended_correction": "Implement the required return value.",
            }
        )
    return {
        "status": "reviewed",
        "verdict": verdict,
        "findings": findings,
        "summary": "The candidate review is complete.",
        "blockers": [],
    }


def _write_checker(
    repo_root: Path,
    *,
    status: Literal["reviewed", "blocked"] = "reviewed",
    verdict: Literal["pass", "revise"] | None = "pass",
    task_id: str = TASK_ID,
    attempt: int = 1,
    filename_attempt: int | None = None,
) -> Path:
    filename_attempt = attempt if filename_attempt is None else filename_attempt
    path = repo_root / "runs" / TASK_ID / "reviews" / f"checker-{filename_attempt}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "task_id": task_id,
                "attempt": attempt,
                "role": "checker",
                "result": _checker_result(status, verdict),
            }
        ),
        encoding="utf-8",
    )
    return path


def _checker_evidence(*, verdict: Literal["pass", "revise"] = "pass") -> CheckerEvidence:
    """Build valid checker evidence for persistence tests."""

    return CheckerEvidence(
        task_id=TASK_ID,
        attempt=1,
        result=_checker_result("reviewed", verdict),
    )


def _validation_result(status: Literal["pass", "fail"]) -> dict[str, object]:
    return {
        "status": status,
        "exit_code": 0 if status == "pass" else 1,
        "launch_error": None,
        "stdout": "",
        "stderr": "" if status == "pass" else "tests failed",
        "timed_out": False,
    }


def _write_validation(
    repo_root: Path,
    entries: tuple[tuple[int, Literal["pass", "fail"]], ...] = ((1, "pass"),),
    *,
    task_id: str = TASK_ID,
) -> Path:
    path = repo_root / "runs" / TASK_ID / "validation" / "results.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "task_id": task_id,
                "runs": [
                    {
                        "candidate_attempt": attempt,
                        "timestamp": datetime(2026, 8, 11, tzinfo=UTC).isoformat(),
                        "result": _validation_result(status),
                        "checks": [
                            {
                                "name": "ruff-lint",
                                "required": True,
                                "result": _validation_result("pass"),
                            },
                            {
                                "name": "eslint",
                                "required": True,
                                "result": _validation_result("pass"),
                            },
                            {
                                "name": "typecheck",
                                "required": True,
                                "result": _validation_result("pass"),
                            },
                        ],
                    }
                    for attempt, status in entries
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _tree_snapshot(
    repo_root: Path,
) -> dict[str, tuple[str, int, int, int, int, bytes | str | None]]:
    snapshot: dict[str, tuple[str, int, int, int, int, bytes | str | None]] = {}
    for path in (repo_root, *repo_root.rglob("*")):
        file_stat = path.lstat()
        if path.is_symlink():
            kind = "symlink"
            value: bytes | str | None = os.readlink(path)
        elif path.is_dir():
            kind = "directory"
            value = None
        else:
            kind = "file"
            value = path.read_bytes()
        relative_path = (
            "." if path == repo_root else path.relative_to(repo_root).as_posix()
        )
        snapshot[relative_path] = (
            kind,
            file_stat.st_mode,
            file_stat.st_uid,
            file_stat.st_gid,
            file_stat.st_mtime_ns,
            value,
        )
    return snapshot


def test_before_maker_requires_both_current_approvals(tmp_path: Path) -> None:
    repo_root = _make_repo(tmp_path)

    assert before_maker(repo_root, TASK_ID, False, True).status == "fail"
    assert before_maker(repo_root, TASK_ID, True, False).status == "fail"

    result = before_maker(repo_root, TASK_ID, True, True)

    assert result.status == "pass"
    assert result.evidence_paths == [
        "planning/01/SPEC.md",
        "runs/01/changes/approved-plan.json",
    ]


@pytest.mark.parametrize(
    "artifact", ["planning/01/SPEC.md", "runs/01/changes/approved-plan.json"]
)
def test_before_maker_requires_the_spec_and_the_approved_plan(
    tmp_path: Path, artifact: str
) -> None:
    repo_root = _make_repo(tmp_path)
    (repo_root / artifact).unlink()

    result = before_maker(repo_root, TASK_ID, True, True)

    assert result.status == "fail"
    assert "planning evidence is invalid" in result.reason


@pytest.mark.parametrize("draft", ["plan.json", "PLAN.md"])
def test_before_maker_does_not_read_the_draft_plan(tmp_path: Path, draft: str) -> None:
    repo_root = _make_repo(tmp_path)
    (repo_root / "planning" / TASK_ID / draft).unlink()

    assert before_maker(repo_root, TASK_ID, True, True).status == "pass"


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        json.dumps({**plan_data(), "task_id": "02"}),
        json.dumps({**plan_data(), "test_intents": {}}),
    ],
)
def test_before_maker_fails_when_the_approved_plan_is_invalid(
    tmp_path: Path, content: str
) -> None:
    repo_root = _make_repo(tmp_path)
    load_task_paths(repo_root, TASK_ID).approved_plan.write_text(
        content, encoding="utf-8"
    )

    result = before_maker(repo_root, TASK_ID, True, True)

    assert result.status == "fail"
    assert "planning evidence is invalid" in result.reason
    assert result.evidence_paths == ["planning/01/SPEC.md"]


def test_before_maker_validates_json_but_does_not_parse_or_render_markdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = _make_repo(tmp_path)
    projection = repo_root / "planning" / TASK_ID / "PLAN.md"
    projection.write_text("This is intentionally not parsed.\n", encoding="utf-8")
    renderer = Mock(side_effect=AssertionError("gate must not render"))
    monkeypatch.setattr(rendering, "render_plan_markdown", renderer)

    result = before_maker(repo_root, TASK_ID, True, True)

    assert result.status == "pass"
    assert projection.read_text(encoding="utf-8") == "This is intentionally not parsed.\n"
    renderer.assert_not_called()


@pytest.mark.parametrize(
    ("status", "verdict", "expected_status", "reason_text"),
    [
        ("reviewed", "pass", "pass", "passed the candidate"),
        ("reviewed", "revise", "pass", "requested changes"),
        ("blocked", None, "fail", "was blocked"),
    ],
)
def test_after_checker_routes_each_valid_review_outcome(
    tmp_path: Path,
    status: Literal["reviewed", "blocked"],
    verdict: Literal["pass", "revise"] | None,
    expected_status: Literal["pass", "fail"],
    reason_text: str,
) -> None:
    repo_root = _make_repo(tmp_path)
    _write_checker(repo_root, status=status, verdict=verdict)

    result = after_checker(repo_root, TASK_ID, 1)

    assert result.status == expected_status
    assert reason_text in result.reason
    assert result.evidence_paths == ["runs/01/reviews/checker-1.json"]
    assert not (repo_root / "runs" / TASK_ID / "validation").exists()


@pytest.mark.parametrize("scenario", ["malformed", "wrong-task", "wrong-attempt"])
def test_after_checker_rejects_invalid_or_mismatched_review(
    tmp_path: Path, scenario: str
) -> None:
    repo_root = _make_repo(tmp_path)
    path = _write_checker(repo_root)
    if scenario == "malformed":
        path.write_text("not json", encoding="utf-8")
    elif scenario == "wrong-task":
        _write_checker(repo_root, task_id="02")
    else:
        _write_checker(repo_root, attempt=2, filename_attempt=1)

    result = after_checker(repo_root, TASK_ID, 1)

    assert result.status == "fail"
    assert result.evidence_paths == []


def test_save_checker_evidence_accepts_identical_retry_without_replacing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = _make_repo(tmp_path)
    task_paths = load_task_paths(repo_root, TASK_ID)
    evidence = _checker_evidence()
    first_path = save_checker_evidence(repo_root, task_paths, evidence)
    review_path = repo_root / first_path
    original_bytes = review_path.read_bytes()
    saved, loaded_path = load_checker_evidence(repo_root, task_paths, 1)

    assert saved == evidence
    assert loaded_path == first_path == "runs/01/reviews/checker-1.json"

    def reject_replace(_source: Path, _destination: Path) -> None:
        raise AssertionError("an identical retry must not replace the saved review")

    monkeypatch.setattr(atomic_module.os, "replace", reject_replace)

    assert save_checker_evidence(repo_root, task_paths, evidence) == first_path
    assert review_path.read_bytes() == original_bytes


def test_save_checker_evidence_rejects_conflicting_retry(
    tmp_path: Path,
) -> None:
    repo_root = _make_repo(tmp_path)
    task_paths = load_task_paths(repo_root, TASK_ID)
    review_path = repo_root / save_checker_evidence(
        repo_root, task_paths, _checker_evidence()
    )
    original_bytes = review_path.read_bytes()

    with pytest.raises(
        ValueError,
        match="already been recorded with different content",
    ):
        save_checker_evidence(
            repo_root,
            task_paths,
            _checker_evidence(verdict="revise"),
        )

    assert review_path.read_bytes() == original_bytes


def test_save_checker_evidence_preserves_malformed_existing_review(
    tmp_path: Path,
) -> None:
    repo_root = _make_repo(tmp_path)
    task_paths = load_task_paths(repo_root, TASK_ID)
    review_path = task_paths.checker_evidence(1)
    review_path.parent.mkdir(parents=True)
    original_bytes = b"not valid checker evidence\n"
    review_path.write_bytes(original_bytes)

    with pytest.raises(ValueError):
        save_checker_evidence(repo_root, task_paths, _checker_evidence())

    assert review_path.read_bytes() == original_bytes


def test_save_checker_evidence_rejects_non_regular_existing_path(
    tmp_path: Path,
) -> None:
    repo_root = _make_repo(tmp_path)
    task_paths = load_task_paths(repo_root, TASK_ID)
    review_path = task_paths.checker_evidence(1)
    review_path.mkdir(parents=True)

    with pytest.raises(ValueError, match="regular file"):
        save_checker_evidence(repo_root, task_paths, _checker_evidence())

    assert review_path.is_dir()


def test_save_checker_evidence_rejects_existing_symlink(
    tmp_path: Path,
) -> None:
    repo_root = _make_repo(tmp_path)
    task_paths = load_task_paths(repo_root, TASK_ID)
    review_path = task_paths.checker_evidence(1)
    review_path.parent.mkdir(parents=True)
    outside = tmp_path / "outside-checker.json"
    original_bytes = b"outside review must stay unchanged\n"
    outside.write_bytes(original_bytes)
    review_path.symlink_to(outside)

    with pytest.raises(ValueError, match="must not traverse a symlink"):
        save_checker_evidence(repo_root, task_paths, _checker_evidence())

    assert review_path.is_symlink()
    assert outside.read_bytes() == original_bytes


def test_before_complete_accepts_matching_latest_passes_without_human_input(
    tmp_path: Path,
) -> None:
    repo_root = _make_repo(tmp_path)
    _write_checker(repo_root, attempt=2)
    _write_validation(repo_root, ((1, "fail"), (2, "pass")))

    result = before_complete(repo_root, TASK_ID, 2)

    assert result.status == "pass"
    assert result.evidence_paths == [
        "runs/01/reviews/checker-2.json",
        "runs/01/validation/results.json",
    ]


@pytest.mark.parametrize(
    "scenario",
    [
        "missing-review",
        "malformed-review",
        "wrong-review-task",
        "wrong-review-attempt",
        "blocked-review",
        "revise-review",
        "missing-results",
        "malformed-results",
        "wrong-results-task",
        "empty-results",
        "skipped-history",
        "older-pass",
        "newer-pass",
        "latest-failed",
    ],
)
def test_before_complete_rejects_missing_malformed_stale_or_failed_evidence(
    tmp_path: Path, scenario: str
) -> None:
    repo_root = _make_repo(tmp_path)
    attempt = 1
    if scenario != "missing-review":
        checker_path = _write_checker(repo_root)
        if scenario == "malformed-review":
            checker_path.write_text("not json", encoding="utf-8")
        elif scenario == "wrong-review-task":
            _write_checker(repo_root, task_id="02")
        elif scenario == "wrong-review-attempt":
            _write_checker(repo_root, attempt=2, filename_attempt=1)
        elif scenario == "blocked-review":
            _write_checker(repo_root, status="blocked", verdict=None)
        elif scenario == "revise-review":
            _write_checker(repo_root, verdict="revise")

    if scenario not in {
        "missing-results",
        "missing-review",
        "malformed-review",
        "wrong-review-task",
        "wrong-review-attempt",
        "blocked-review",
        "revise-review",
    }:
        results_path = _write_validation(repo_root)
        if scenario == "malformed-results":
            results_path.write_text("not json", encoding="utf-8")
        elif scenario == "wrong-results-task":
            _write_validation(repo_root, task_id="02")
        elif scenario == "empty-results":
            _write_validation(repo_root, ())
        elif scenario == "skipped-history":
            _write_validation(repo_root, ((2, "pass"),))
            _write_checker(repo_root, attempt=2)
            attempt = 2
        elif scenario == "older-pass":
            _write_validation(repo_root, ((1, "pass"),))
            _write_checker(repo_root, attempt=2)
            attempt = 2
        elif scenario == "newer-pass":
            _write_validation(repo_root, ((1, "pass"), (2, "pass")))
        elif scenario == "latest-failed":
            _write_validation(repo_root, ((1, "fail"),))

    result = before_complete(repo_root, TASK_ID, attempt)

    assert result.status == "fail"


def test_evidence_loaders_reject_symlinks(tmp_path: Path) -> None:
    repo_root = _make_repo(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    review_path = repo_root / "runs" / TASK_ID / "reviews" / "checker-1.json"
    review_path.parent.mkdir(parents=True)
    review_path.symlink_to(outside)

    with pytest.raises(ValueError, match="must not traverse a symlink"):
        load_checker_evidence(repo_root, load_task_paths(repo_root, TASK_ID), 1)


def test_validation_loader_and_completion_gate_reject_results_symlink(
    tmp_path: Path,
) -> None:
    repo_root = _make_repo(tmp_path)
    _write_checker(repo_root)
    results_path = _write_validation(repo_root)
    outside = tmp_path / "outside-results.json"
    results_path.replace(outside)
    results_path.symlink_to(outside)
    task_paths = load_task_paths(repo_root, TASK_ID)

    with pytest.raises(ValueError, match="must not traverse a symlink"):
        load_validation_evidence(repo_root, task_paths)
    assert before_complete(repo_root, TASK_ID, 1).status == "fail"


def test_checker_loader_and_gate_reject_inconsistent_typed_review(
    tmp_path: Path,
) -> None:
    repo_root = _make_repo(tmp_path)
    review_path = _write_checker(repo_root)
    data = json.loads(review_path.read_text(encoding="utf-8"))
    data["result"]["findings"] = [  # type: ignore[index]
        {
            "severity": "blocking",
            "path": "src/feature.py",
            "issue": "This conflicts with a pass verdict.",
            "evidence": "The finding is marked as blocking.",
            "recommended_correction": "Return a revise verdict.",
        }
    ]
    review_path.write_text(json.dumps(data), encoding="utf-8")
    task_paths = load_task_paths(repo_root, TASK_ID)

    with pytest.raises(ValueError, match="passing review cannot contain"):
        load_checker_evidence(repo_root, task_paths, 1)
    assert after_checker(repo_root, TASK_ID, 1).status == "fail"


def test_validation_loader_and_completion_gate_reject_inconsistent_result(
    tmp_path: Path,
) -> None:
    repo_root = _make_repo(tmp_path)
    _write_checker(repo_root)
    results_path = _write_validation(repo_root)
    data = json.loads(results_path.read_text(encoding="utf-8"))
    data["runs"][0]["result"]["exit_code"] = 7  # type: ignore[index]
    results_path.write_text(json.dumps(data), encoding="utf-8")
    task_paths = load_task_paths(repo_root, TASK_ID)

    with pytest.raises(ValueError, match="passing command requires exit code 0"):
        load_validation_evidence(repo_root, task_paths)
    assert before_complete(repo_root, TASK_ID, 1).status == "fail"


def test_validation_loader_checks_task_and_candidate_sequence(tmp_path: Path) -> None:
    repo_root = _make_repo(tmp_path)
    task_paths = load_task_paths(repo_root, TASK_ID)
    _write_validation(repo_root, ((1, "fail"), (2, "pass")))

    results, relative_path = load_validation_evidence(repo_root, task_paths)

    assert isinstance(results, ValidationResultsDocument)
    assert relative_path == "runs/01/validation/results.json"
    _write_validation(repo_root, ((2, "pass"),))
    with pytest.raises(ValueError, match="must start at candidate 1, round 1"):
        load_validation_evidence(repo_root, task_paths)


@pytest.mark.parametrize(
    ("gate", "arguments"),
    [
        (before_maker, ("1", True, True)),
        (before_maker, (TASK_ID, 1, True)),
        (after_checker, (TASK_ID, True)),
        (before_complete, (TASK_ID, "1")),
    ],
)
def test_gate_boundaries_reject_non_strict_values(
    tmp_path: Path,
    gate: Callable[..., GateResult],
    arguments: tuple[object, ...],
) -> None:
    repo_root = _make_repo(tmp_path)

    with pytest.raises(ValueError):
        gate(repo_root, *arguments)


def test_before_maker_is_read_only_on_pass_and_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = _make_repo(tmp_path)
    process_launcher = Mock(side_effect=AssertionError("a gate launched a process"))
    monkeypatch.setattr(subprocess, "Popen", process_launcher)
    before = _tree_snapshot(repo_root)

    assert before_maker(repo_root, TASK_ID, True, True).status == "pass"
    assert before_maker(repo_root, TASK_ID, False, True).status == "fail"

    process_launcher.assert_not_called()
    assert _tree_snapshot(repo_root) == before


def test_after_checker_is_read_only_for_pass_revise_blocked_and_malformed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process_launcher = Mock(side_effect=AssertionError("a gate launched a process"))
    monkeypatch.setattr(subprocess, "Popen", process_launcher)
    cases: tuple[
        tuple[
            str,
            Literal["reviewed", "blocked"],
            Literal["pass", "revise"] | None,
            Literal["pass", "fail"],
        ],
        ...,
    ] = (
        ("pass", "reviewed", "pass", "pass"),
        ("revise", "reviewed", "revise", "pass"),
        ("blocked", "blocked", None, "fail"),
        ("malformed", "reviewed", "pass", "fail"),
    )
    for name, review_status, verdict, expected_gate_status in cases:
        repo_root = _make_repo(tmp_path / name)
        review_path = _write_checker(
            repo_root,
            status=review_status,
            verdict=verdict,
        )
        if name == "malformed":
            review_path.write_text("not json", encoding="utf-8")
        before = _tree_snapshot(repo_root)

        result = after_checker(repo_root, TASK_ID, 1)

        assert result.status == expected_gate_status
        assert _tree_snapshot(repo_root) == before
    process_launcher.assert_not_called()


def test_before_complete_is_read_only_for_pass_and_failure_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process_launcher = Mock(side_effect=AssertionError("a gate launched a process"))
    monkeypatch.setattr(subprocess, "Popen", process_launcher)
    cases: tuple[
        tuple[
            str,
            Literal["pass", "revise"],
            Literal["pass", "fail"],
            bool,
        ],
        ...,
    ] = (
        ("pass", "pass", "pass", False),
        ("review-revise", "revise", "pass", False),
        ("validation-fail", "pass", "fail", False),
        ("malformed-results", "pass", "pass", True),
    )
    for name, verdict, validation_status, malformed_results in cases:
        repo_root = _make_repo(tmp_path / name)
        _write_checker(repo_root, verdict=verdict)
        results_path = _write_validation(
            repo_root,
            ((1, validation_status),),
        )
        if malformed_results:
            results_path.write_text("not json", encoding="utf-8")
        before = _tree_snapshot(repo_root)

        result = before_complete(repo_root, TASK_ID, 1)

        expected = "pass" if name == "pass" else "fail"
        assert result.status == expected
        assert _tree_snapshot(repo_root) == before
    process_launcher.assert_not_called()


def test_validated_evidence_path_returns_a_safe_relative_path(tmp_path: Path) -> None:
    repo_root = _make_repo(tmp_path)
    spec_path = repo_root / "planning" / TASK_ID / "SPEC.md"

    assert validated_evidence_path(repo_root, spec_path) == "planning/01/SPEC.md"
