import json
import subprocess
import sys
from pathlib import Path

import pytest

from harness.contracts.results import ErrorResponse

REPO_ROOT = Path(__file__).resolve().parents[2]


def _repo(tmp_path: Path) -> Path:
    """Create the fixed application directory required by a new run."""

    repo = tmp_path / "workflow-repo"
    (repo / "src").mkdir(parents=True)
    return repo


def _run_cli(
    operation: str,
    payload: object | None = None,
    *,
    payload_file: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the module in a fresh process with JSON from stdin or a file."""

    command = [sys.executable, "-m", "harness.execute", operation]
    standard_input: str | None = None
    if payload_file is None:
        standard_input = "" if payload is None else json.dumps(payload)
    else:
        payload_file.write_text(json.dumps(payload), encoding="utf-8")
        command.append(str(payload_file))
    return subprocess.run(
        command,
        cwd=REPO_ROOT,
        input=standard_input,
        capture_output=True,
        text=True,
        check=False,
    )


def _successful_json(completed: subprocess.CompletedProcess[str]) -> dict[str, object]:
    """Check one successful stdout-only response and decode it."""

    assert completed.returncode == 0
    assert completed.stderr == ""
    assert completed.stdout.count("\n") == 1
    value = json.loads(completed.stdout)
    assert isinstance(value, dict)
    return value


def _error_json(completed: subprocess.CompletedProcess[str]) -> ErrorResponse:
    """Check one failed stderr-only response and validate its error contract."""

    assert completed.returncode != 0
    assert completed.stdout == ""
    assert completed.stderr.count("\n") == 1
    return ErrorResponse.model_validate_json(completed.stderr)


def _start(repo: Path) -> dict[str, object]:
    """Start one workflow through stdin and return its spec-author action."""

    return _successful_json(
        _run_cli(
            "start",
            {"repo_root": str(repo), "original_request": "Add one behavior."},
        )
    )


def _spec_text() -> str:
    """Return a valid specification for the approval CLI test."""

    return """# Specification

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


def _plan_data(task_id: str) -> dict[str, object]:
    """Return one valid plan used to reach the maker through the CLI."""

    return {
        "schema_version": 1,
        "task_id": task_id,
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


def _ready_author(repo: Path, action: dict[str, object]) -> dict[str, object]:
    """Submit one ready author result and return its approval action."""

    return _successful_json(
        _run_cli(
            "submit",
            {
                "repo_root": str(repo),
                "task_id": action["task_id"],
                "result": {
                    "task_id": action["task_id"],
                    "action_id": action["action_id"],
                    "status": "ready_for_approval",
                    "files_written": action["writable_paths"],
                    "questions": [],
                    "blockers": [],
                },
            },
        )
    )


def _approve(repo: Path, action: dict[str, object]) -> dict[str, object]:
    """Submit a strict approval and return the next agent action."""

    return _successful_json(
        _run_cli(
            "submit",
            {
                "repo_root": str(repo),
                "task_id": action["task_id"],
                "result": {
                    "kind": "approval-response",
                    "task_id": action["task_id"],
                    "action_id": action["action_id"],
                    "artifact": action["artifact"],
                    "revision": action["revision"],
                    "approved": True,
                    "feedback": None,
                },
            },
        )
    )


def _reach_maker(repo: Path) -> dict[str, object]:
    """Run valid spec and plan approvals and return candidate 1's action."""

    author = _start(repo)
    task_id = str(author["task_id"])
    (repo / "planning" / task_id / "SPEC.md").write_text(_spec_text(), encoding="utf-8")
    planner = _approve(repo, _ready_author(repo, author))
    (repo / "planning" / task_id / "plan.json").write_text(
        json.dumps(_plan_data(task_id)), encoding="utf-8"
    )
    maker = _approve(repo, _ready_author(repo, planner))
    assert maker["role"] == "maker"
    return maker


def test_start_and_next_action_round_trip_across_processes_and_input_sources(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    started = _start(repo)
    payload_path = tmp_path / "next-action.json"

    resumed = _successful_json(
        _run_cli(
            "next-action",
            {"repo_root": str(repo), "task_id": started["task_id"]},
            payload_file=payload_path,
        )
    )

    assert started == resumed
    assert started["kind"] == "agent"
    assert started["role"] == "spec-author"
    assert (repo / "runs" / str(started["task_id"]) / "state.json").is_file()
    assert json.loads((repo / "runs" / "active.json").read_text(encoding="utf-8")) == {
        "task_id": started["task_id"]
    }


def test_submit_accepts_agent_and_human_results_through_one_operation(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    author = _start(repo)

    question = _successful_json(
        _run_cli(
            "submit",
            {
                "repo_root": str(repo),
                "task_id": author["task_id"],
                "result": {
                    "task_id": author["task_id"],
                    "action_id": author["action_id"],
                    "status": "questions",
                    "files_written": [],
                    "questions": ["Which output should be visible?"],
                    "blockers": [],
                },
            },
        )
    )
    assert question["kind"] == "question"

    next_author = _successful_json(
        _run_cli(
            "submit",
            {
                "repo_root": str(repo),
                "task_id": question["task_id"],
                "result": {
                    "kind": "question-response",
                    "task_id": question["task_id"],
                    "action_id": question["action_id"],
                    "artifact": question["artifact"],
                    "answers": [
                        {
                            "question": question["questions"][0],
                            "answer": "Show the saved todo in the list.",
                        }
                    ],
                },
            },
        )
    )
    assert next_author["kind"] == "agent"
    assert next_author["role"] == "spec-author"
    assert next_author["action_id"] != author["action_id"]
    assert json.loads((repo / "runs" / "active.json").read_text(encoding="utf-8")) == {
        "task_id": author["task_id"]
    }


def test_terminal_submit_clears_the_matching_active_task(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    maker = _reach_maker(repo)
    task_id = str(maker["task_id"])

    blocked = _successful_json(
        _run_cli(
            "submit",
            {
                "repo_root": str(repo),
                "task_id": task_id,
                "result": {
                    "task_id": task_id,
                    "action_id": maker["action_id"],
                    "status": "blocked",
                    "files_changed": [],
                    "summary": "",
                    "blockers": ["The implementation cannot continue."],
                },
            },
        )
    )

    assert blocked["kind"] == "blocked"
    assert not (repo / "runs" / "active.json").exists()
    resumed = _successful_json(
        _run_cli("next-action", {"repo_root": str(repo), "task_id": task_id})
    )
    assert resumed == blocked
    assert not (repo / "runs" / "active.json").exists()


def test_submit_uses_strict_human_approval_and_keeps_pause_on_error(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    author = _start(repo)
    task_id = str(author["task_id"])
    (repo / "planning" / task_id / "SPEC.md").write_text(_spec_text(), encoding="utf-8")
    payload_file = tmp_path / "author-result.json"
    approval = _successful_json(
        _run_cli(
            "submit",
            {
                "repo_root": str(repo),
                "task_id": task_id,
                "result": {
                    "task_id": task_id,
                    "action_id": author["action_id"],
                    "status": "ready_for_approval",
                    "files_written": author["writable_paths"],
                    "questions": [],
                    "blockers": [],
                },
            },
            payload_file=payload_file,
        )
    )
    assert approval["kind"] == "approval"

    rejected = _error_json(
        _run_cli(
            "submit",
            {
                "repo_root": str(repo),
                "task_id": task_id,
                "result": {
                    "kind": "approval-response",
                    "task_id": task_id,
                    "action_id": approval["action_id"],
                    "artifact": approval["artifact"],
                    "revision": approval["revision"],
                    "approved": "true",
                    "feedback": None,
                },
            },
        )
    )
    assert rejected.code == "invalid_submission"

    still_pending = _successful_json(
        _run_cli("next-action", {"repo_root": str(repo), "task_id": task_id})
    )
    assert still_pending == approval

    planner = _successful_json(
        _run_cli(
            "submit",
            {
                "repo_root": str(repo),
                "task_id": task_id,
                "result": {
                    "kind": "approval-response",
                    "task_id": task_id,
                    "action_id": approval["action_id"],
                    "artifact": approval["artifact"],
                    "revision": approval["revision"],
                    "approved": True,
                    "feedback": None,
                },
            },
        )
    )
    assert planner["kind"] == "agent"
    assert planner["role"] == "planner"


def test_cli_reports_malformed_json_arguments_input_and_saved_state_as_json(
    tmp_path: Path,
) -> None:
    malformed = subprocess.run(
        [sys.executable, "-m", "harness.execute", "start"],
        cwd=REPO_ROOT,
        input="{not json",
        capture_output=True,
        text=True,
        check=False,
    )
    assert _error_json(malformed).code == "invalid_json"

    unknown = _error_json(_run_cli("validate", {}))
    assert unknown.code == "invalid_arguments"

    missing_operation = subprocess.run(
        [sys.executable, "-m", "harness.execute"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert _error_json(missing_operation).code == "invalid_arguments"

    bad_input = _error_json(_run_cli("start", {"repo_root": str(tmp_path)}))
    assert bad_input.code == "invalid_input"

    repo = _repo(tmp_path)
    action = _start(repo)
    state_path = repo / "runs" / str(action["task_id"]) / "state.json"
    state_path.write_text("{not json", encoding="utf-8")
    invalid_state = _error_json(
        _run_cli(
            "next-action",
            {"repo_root": str(repo), "task_id": action["task_id"]},
        )
    )
    assert invalid_state.code == "invalid_state"
    assert json.loads((repo / "runs" / "active.json").read_text(encoding="utf-8")) == {
        "task_id": action["task_id"]
    }


def test_cli_exposes_no_command_or_validation_operation() -> None:
    for operation in ("run-command", "validation", "pytest"):
        error = _error_json(_run_cli(operation, {}))
        assert error.code == "invalid_arguments"


@pytest.mark.parametrize("payload", [7, ["not", "an", "object"]])
def test_cli_rejects_scalar_and_list_json(payload: object) -> None:
    error = _error_json(_run_cli("start", payload))

    assert error.code == "invalid_input"


def test_cli_rejects_extra_arguments_and_missing_or_invalid_input_files(
    tmp_path: Path,
) -> None:
    extra_arguments = subprocess.run(
        [sys.executable, "-m", "harness.execute", "start", "one.json", "two.json"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert _error_json(extra_arguments).code == "invalid_arguments"

    missing_file = subprocess.run(
        [
            sys.executable,
            "-m",
            "harness.execute",
            "start",
            str(tmp_path / "missing.json"),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert _error_json(missing_file).code == "io_error"

    empty_input = subprocess.run(
        [sys.executable, "-m", "harness.execute", "start"],
        cwd=REPO_ROOT,
        input="",
        capture_output=True,
        text=True,
        check=False,
    )
    assert _error_json(empty_input).code == "invalid_json"

    invalid_utf8 = tmp_path / "invalid-utf8.json"
    invalid_utf8.write_bytes(b"\xff\xfe")
    invalid_encoding = subprocess.run(
        [sys.executable, "-m", "harness.execute", "start", str(invalid_utf8)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert _error_json(invalid_encoding).code == "invalid_json"


def test_cli_rejects_extra_fields_and_non_string_task_ids(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    extra_field = _error_json(
        _run_cli(
            "start",
            {
                "repo_root": str(repo),
                "original_request": "Add one behavior.",
                "unexpected": "not allowed",
            },
        )
    )
    assert extra_field.code == "invalid_input"

    boolean_task = _error_json(
        _run_cli("next-action", {"repo_root": str(repo), "task_id": True})
    )
    assert boolean_task.code == "invalid_input"


def test_cli_rejects_a_missing_repository_without_creating_directories(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "missing-root"

    error = _error_json(
        _run_cli(
            "start",
            {"repo_root": str(repo), "original_request": "Do not write here."},
        )
    )

    assert error.code == "invalid_state"
    assert not repo.exists()


def test_cli_reports_missing_state_without_a_traceback(tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    error = _error_json(
        _run_cli("next-action", {"repo_root": str(repo), "task_id": "01"})
    )

    assert error.code == "invalid_state"
    assert "missing" in error.message


def test_cli_rejects_command_fields_without_executing_them(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    author = _start(repo)
    marker = tmp_path / "must-not-exist"

    error = _error_json(
        _run_cli(
            "submit",
            {
                "repo_root": str(repo),
                "task_id": author["task_id"],
                "result": {
                    "task_id": author["task_id"],
                    "action_id": author["action_id"],
                    "status": "questions",
                    "files_written": [],
                    "questions": ["Which behavior is required?"],
                    "blockers": [],
                    "command": {
                        "argv": ["touch", str(marker)],
                        "command_string": f"touch {marker}",
                    },
                },
            },
        )
    )

    assert error.code == "invalid_submission"
    assert not marker.exists()
    still_pending = _successful_json(
        _run_cli(
            "next-action",
            {"repo_root": str(repo), "task_id": author["task_id"]},
        )
    )
    assert still_pending == author


def test_cli_reports_invalid_artifacts_as_submission_errors(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    author = _start(repo)
    task_id = str(author["task_id"])
    (repo / "planning" / task_id / "SPEC.md").write_text(
        "# Not a valid specification\n", encoding="utf-8"
    )

    error = _error_json(
        _run_cli(
            "submit",
            {
                "repo_root": str(repo),
                "task_id": task_id,
                "result": {
                    "task_id": task_id,
                    "action_id": author["action_id"],
                    "status": "ready_for_approval",
                    "files_written": author["writable_paths"],
                    "questions": [],
                    "blockers": [],
                },
            },
        )
    )

    assert error.code == "invalid_submission"
    assert "specification artifact is invalid" in error.message


def test_cli_keeps_spec_approval_pending_when_spec_changes_before_approval(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    author = _start(repo)
    task_id = str(author["task_id"])
    spec_path = repo / "planning" / task_id / "SPEC.md"
    spec_path.write_text(_spec_text(), encoding="utf-8")
    approval = _ready_author(repo, author)
    state_path = repo / "runs" / task_id / "state.json"
    saved_state = state_path.read_text(encoding="utf-8")
    spec_path.write_text("# Corrupted after review started\n", encoding="utf-8")

    error = _error_json(
        _run_cli(
            "submit",
            {
                "repo_root": str(repo),
                "task_id": task_id,
                "result": {
                    "kind": "approval-response",
                    "task_id": task_id,
                    "action_id": approval["action_id"],
                    "artifact": approval["artifact"],
                    "revision": approval["revision"],
                    "approved": True,
                    "feedback": None,
                },
            },
        )
    )

    assert error.code == "invalid_submission"
    assert "changed after the approval was requested" in error.message
    assert state_path.read_text(encoding="utf-8") == saved_state
    assert (
        _successful_json(
            _run_cli("next-action", {"repo_root": str(repo), "task_id": task_id})
        )
        == approval
    )


def test_cli_blocks_when_plan_changes_during_implementation(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    maker = _reach_maker(repo)
    task_id = str(maker["task_id"])
    (repo / "planning" / task_id / "plan.json").write_text(
        "{not valid JSON", encoding="utf-8"
    )

    output = _successful_json(
        _run_cli(
            "submit",
            {
                "repo_root": str(repo),
                "task_id": task_id,
                "result": {
                    "task_id": task_id,
                    "action_id": maker["action_id"],
                    "status": "implemented",
                    "files_changed": maker["writable_paths"],
                    "summary": "Implemented the approved behavior and tests.",
                    "blockers": [],
                },
            },
        )
    )

    assert output["kind"] == "blocked"
    assert "outside the approved scopes" in str(output["reason"])
    assert f"planning/{task_id}/plan.json" in str(output["reason"])


def test_help_lists_every_operation_as_json() -> None:
    for flag in ("--help", "-h", "help"):
        output = _successful_json(_run_cli(flag, None))

        assert output["kind"] == "usage"
        assert set(output["operations"]) == {  # type: ignore[arg-type]
            "start",
            "next-action",
            "submit",
            "status",
            "resume",
        }


def test_status_and_resume_work_across_processes(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    maker = _reach_maker(repo)
    task_id = str(maker["task_id"])

    status = _successful_json(_run_cli("status", {"repo_root": str(repo)}))
    resumed = _successful_json(
        _run_cli("resume", {"repo_root": str(repo), "task_id": task_id})
    )

    assert status["kind"] == "status"
    assert status["task_id"] == task_id
    assert status["stage"] == "implementation"
    assert status["baseline_saved"] is True
    assert resumed == maker


def test_status_without_any_run_reports_a_clear_error(tmp_path: Path) -> None:
    error = _error_json(_run_cli("status", {"repo_root": str(_repo(tmp_path))}))

    assert error.code == "invalid_state"
    assert "no saved runs" in error.message


def test_start_explains_a_missing_uv_before_any_work_is_done(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import io

    import harness.execute.__main__ as cli

    repo = _repo(tmp_path)
    monkeypatch.setattr(cli.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        cli.sys,
        "stdin",
        io.StringIO(json.dumps({"repo_root": str(repo), "original_request": "Add it."})),
    )

    assert cli.main(["start"]) == 2

    error = ErrorResponse.model_validate_json(capsys.readouterr().err)
    assert error.code == "missing_dependency"
    assert "uv is not on PATH" in error.message
    assert not (repo / "runs").exists()
