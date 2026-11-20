import json
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from unittest.mock import Mock

import pytest

from harness.config import HARNESS_TEST_COMMAND, project_config
from harness.contracts.changes import ChangeRecord, ChangeSet, DriftRecord, FileDrift
from harness.contracts.core import load_task_paths
from harness.contracts.state import RunState
from harness.contracts.validation import (
    ValidationCommandResult,
    ValidationResultsDocument,
)
from harness.execute.routing import _current_validation_record
from harness.feedback.run_tests import run_tests
from harness.utils import atomic as atomic_module
from tests.harness.support import TASK_ID, plan_data, write_approved_plan

test_runner = import_module("harness.feedback.run_tests")
TEST_COMMAND = project_config().tests[0].command
PASS_RESULT = ValidationCommandResult(
    status="pass", exit_code=0, stdout="passed\n", stderr="", timed_out=False
)


def _make_repo(tmp_path: Path, *, approved_plan: bool = True) -> Path:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    if approved_plan:
        write_approved_plan(repo_root, plan_data())
    return repo_root


def _results_path(repo_root: Path) -> Path:
    return repo_root / "runs" / TASK_ID / "validation" / "results.json"


def _persisted(repo_root: Path) -> ValidationResultsDocument:
    return ValidationResultsDocument.model_validate_json(
        _results_path(repo_root).read_text(encoding="utf-8")
    )


def test_run_tests_uses_only_fixed_policy_and_persists_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = _make_repo(tmp_path)
    execute = Mock(return_value=PASS_RESULT)
    monkeypatch.setattr(test_runner, "_execute", execute)

    record = run_tests(
        repo_root, TASK_ID, 1, include_harness_tests=False, reference_digests={}
    )

    assert record.status == "pass"
    assert record.timestamp.utcoffset() == UTC.utcoffset(record.timestamp)
    assert _persisted(repo_root).runs == (record,)
    assert execute.call_count == 2
    execute.assert_any_call(
        repo_root.resolve(),
        TEST_COMMAND,
        test_runner.TEST_TIMEOUT_SECONDS,
    )


@pytest.mark.parametrize(
    ("path", "harness_tests"),
    [
        ("harness/gates.py", True),
        ("docs/AGENTS.md", True),
        ("tests/unit/conftest.py", True),
        (".harness/ruff.toml", True),
        ("pnpm-lock.yaml", True),
        ("src/feature.py", False),
    ],
)
def test_routing_adds_harness_tests_when_the_reference_holds_a_protected_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str, harness_tests: bool
) -> None:
    repo_root = _make_repo(tmp_path)
    record_path = repo_root / "runs" / TASK_ID / "changes" / "candidate-1.json"
    record_path.write_text(
        ChangeRecord(
            task_id=TASK_ID,
            candidate_attempt=1,
            phase="candidate",
            changes=ChangeSet(changed=(path,)),
            digests={path: "changed"},
            reported_paths=(path,),
            status="pass",
        ).model_dump_json(),
        encoding="utf-8",
    )
    state = RunState(
        task_id=TASK_ID,
        original_request="Add one behavior.",
        stage="validation",
        spec_revision=1,
        spec_approved=True,
        spec_digest="0" * 64,
        plan_revision=1,
        plan_approved=True,
        candidate_attempt=1,
        pending_action=None,
    )
    execute = Mock(return_value=PASS_RESULT)
    monkeypatch.setattr(test_runner, "_execute", execute)

    _current_validation_record(repo_root, state)

    commands = [call.args[1] for call in execute.call_args_list[:2]]
    assert commands[0] == TEST_COMMAND
    assert (HARNESS_TEST_COMMAND in commands) is harness_tests


def test_routing_passes_the_current_round_reference_to_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = _make_repo(tmp_path)
    task_paths = load_task_paths(repo_root, TASK_ID)
    task_paths.candidate_changes(1).write_text(
        ChangeRecord(
            task_id=TASK_ID,
            candidate_attempt=1,
            phase="candidate",
            changes=ChangeSet(changed=("old.py",)),
            digests={"old.py": "old"},
            reported_paths=("old.py",),
            status="pass",
        ).model_dump_json(),
        encoding="utf-8",
    )
    task_paths.drift_changes(1).write_text(
        DriftRecord(
            task_id=TASK_ID,
            candidate_attempt=1,
            validation_round=2,
            drift=(FileDrift(path="new.py", old_digest=None, new_digest="new"),),
            digests={"new.py": "new"},
            reported_paths=("old.py", "new.py"),
        ).model_dump_json(),
        encoding="utf-8",
    )
    state = RunState(
        task_id=TASK_ID,
        original_request="Add one behavior.",
        stage="validation",
        spec_revision=1,
        spec_approved=True,
        spec_digest="0" * 64,
        plan_revision=1,
        plan_approved=True,
        candidate_attempt=1,
        pending_action=None,
    )
    calls: list[tuple[str, ...]] = []

    def run(
        repo_root: Path, command: tuple[str, ...], timeout: int
    ) -> ValidationCommandResult:
        calls.append(command)
        return PASS_RESULT

    monkeypatch.setattr(test_runner, "_execute", run)
    _current_validation_record(repo_root, state)
    _current_validation_record(
        repo_root, state.model_copy(update={"validation_round": 2})
    )

    assert (*project_config().check("ruff-lint").command, "--", "old.py") in calls
    assert (*project_config().check("ruff-lint").command, "--", "new.py") in calls


def test_execute_records_success_and_nonzero_exit(tmp_path: Path) -> None:
    success = test_runner._execute(
        tmp_path,
        (sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr)"),
        5,
    )
    failure = test_runner._execute(
        tmp_path,
        (
            sys.executable,
            "-c",
            "import sys; print('failed', file=sys.stderr); sys.exit(7)",
        ),
        5,
    )

    assert success.status == "pass"
    assert success.exit_code == 0
    assert success.stdout == "out\n"
    assert success.stderr == "err\n"
    assert not success.timed_out
    assert failure.status == "fail"
    assert failure.exit_code == 7
    assert failure.stderr == "failed\n"
    assert not failure.timed_out


def test_execute_records_launch_failure(tmp_path: Path) -> None:
    result = test_runner._execute(tmp_path, (str(tmp_path / "missing-command"),), 5)

    assert result.status == "fail"
    assert result.exit_code is None
    assert result.launch_error is not None
    assert "missing-command" in result.launch_error
    assert not result.timed_out


def test_execute_surfaces_process_group_cleanup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = Mock(pid=12345)
    process.communicate.side_effect = [
        subprocess.TimeoutExpired(("command",), 1),
        ("", ""),
    ]
    launch = Mock(return_value=process)
    monkeypatch.setattr(test_runner.subprocess, "Popen", launch)

    def fail_killpg(pid: int, sig: signal.Signals) -> None:
        raise PermissionError("process group signal failed")

    monkeypatch.setattr(test_runner.os, "killpg", fail_killpg)

    with pytest.raises(PermissionError, match="process group signal failed"):
        test_runner._execute(tmp_path, ("command",), 1)

    launch.assert_called_once_with(
        ("command",),
        cwd=tmp_path,
        shell=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    process.kill.assert_called_once_with()
    assert process.communicate.call_count == 2


def test_execute_bounds_stdout_and_stderr(tmp_path: Path) -> None:
    size = test_runner.TEST_OUTPUT_LIMIT + 10
    result = test_runner._execute(
        tmp_path,
        (
            sys.executable,
            "-c",
            "import sys; sys.stdout.write('x' * int(sys.argv[1])); "
            "sys.stderr.write('y' * int(sys.argv[1]))",
            str(size),
        ),
        5,
    )

    assert result.status == "pass"
    assert len(result.stdout) == test_runner.TEST_OUTPUT_LIMIT
    assert len(result.stderr) == test_runner.TEST_OUTPUT_LIMIT
    assert result.stdout.endswith("...[truncated]...")
    assert result.stderr.endswith("...[truncated]...")


def test_execute_timeout_stops_child_and_records_output(tmp_path: Path) -> None:
    pid_path = tmp_path / "child.pid"
    script = (
        "import pathlib, subprocess, sys, time; "
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, "
        "stderr=subprocess.DEVNULL); "
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); "
        "print('partial stdout', flush=True); "
        "print('partial stderr', file=sys.stderr, flush=True); "
        "time.sleep(60)"
    )
    child_pid: int | None = None
    child_gone = False
    try:
        result = test_runner._execute(
            tmp_path, (sys.executable, "-c", script, str(pid_path)), 1
        )
        child_pid = int(pid_path.read_text())
        assert result.status == "fail"
        assert result.timed_out
        assert result.exit_code is None
        assert result.launch_error is None
        assert result.stdout == "partial stdout\n"
        assert result.stderr == "partial stderr\n"

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                child_gone = True
                break
            time.sleep(0.05)
        else:
            pytest.fail("the child process survived the timeout")
    finally:
        if child_pid is None and pid_path.exists():
            child_pid = int(pid_path.read_text())
        if child_pid is not None and not child_gone:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_candidates_must_be_recorded_exactly_once_in_order_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = _make_repo(tmp_path)
    execute = Mock(return_value=PASS_RESULT)
    monkeypatch.setattr(test_runner, "_execute", execute)

    with pytest.raises(ValueError, match="out of order"):
        run_tests(
            repo_root, TASK_ID, 2, include_harness_tests=False, reference_digests={}
        )
    first = run_tests(
        repo_root, TASK_ID, 1, include_harness_tests=False, reference_digests={}
    )
    with pytest.raises(ValueError, match="already been recorded"):
        run_tests(
            repo_root, TASK_ID, 1, include_harness_tests=False, reference_digests={}
        )
    with pytest.raises(ValueError, match="out of order"):
        run_tests(
            repo_root, TASK_ID, 1, 3, include_harness_tests=False, reference_digests={}
        )
    again = run_tests(
        repo_root, TASK_ID, 1, 2, include_harness_tests=False, reference_digests={}
    )
    second = run_tests(
        repo_root, TASK_ID, 2, include_harness_tests=False, reference_digests={}
    )
    with pytest.raises(ValueError, match="already been recorded"):
        run_tests(
            repo_root, TASK_ID, 2, include_harness_tests=False, reference_digests={}
        )

    assert execute.call_count == 6
    assert _persisted(repo_root).runs == (first, again, second)


@pytest.mark.parametrize(
    "scenario",
    [
        "malformed",
        "wrong-task",
        "skipped-history",
        "exit-and-timeout",
        "launch-and-timeout",
    ],
)
def test_invalid_existing_evidence_rejects_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    repo_root = _make_repo(tmp_path)
    results_path = _results_path(repo_root)
    results_path.parent.mkdir(parents=True)
    if scenario == "malformed":
        original = "not json"
    else:
        task_id = "02" if scenario == "wrong-task" else TASK_ID
        attempt = 2 if scenario == "skipped-history" else 1
        result = {
            "status": "pass",
            "exit_code": 0,
            "launch_error": None,
            "stdout": "",
            "stderr": "",
            "timed_out": False,
        }
        if scenario == "exit-and-timeout":
            result.update(status="fail", exit_code=7, timed_out=True)
        elif scenario == "launch-and-timeout":
            result.update(
                status="fail",
                exit_code=None,
                launch_error="uv unavailable",
                timed_out=True,
            )
        original = json.dumps(
            {
                "task_id": task_id,
                "runs": [
                    {
                        "candidate_attempt": attempt,
                        "timestamp": datetime(2026, 8, 11, tzinfo=UTC).isoformat(),
                        "result": result,
                    }
                ],
            }
        )
    results_path.write_text(original, encoding="utf-8")
    execute = Mock()
    monkeypatch.setattr(test_runner, "_execute", execute)

    with pytest.raises(ValueError):
        run_tests(
            repo_root, TASK_ID, 1, include_harness_tests=False, reference_digests={}
        )

    execute.assert_not_called()
    assert results_path.read_text(encoding="utf-8") == original


def test_atomic_replace_failure_preserves_prior_evidence_and_cleans_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = _make_repo(tmp_path)
    monkeypatch.setattr(test_runner, "_execute", Mock(return_value=PASS_RESULT))
    run_tests(repo_root, TASK_ID, 1, include_harness_tests=False, reference_digests={})
    destination = _results_path(repo_root)
    original = destination.read_text(encoding="utf-8")

    def fail_replace(source: os.PathLike[str], target: os.PathLike[str]) -> None:
        raise OSError("replace unavailable")

    monkeypatch.setattr(atomic_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace unavailable"):
        run_tests(
            repo_root, TASK_ID, 2, include_harness_tests=False, reference_digests={}
        )

    assert destination.read_text(encoding="utf-8") == original
    assert not tuple(destination.parent.glob(".results.json.*.tmp"))


@pytest.mark.parametrize("scenario", ["task-parent", "validation-parent", "result"])
def test_validation_rejects_parent_and_final_artifact_symlinks_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    repo_root = _make_repo(tmp_path, approved_plan=False)
    runs_dir = repo_root / "runs"
    runs_dir.mkdir()
    outside = repo_root.parent / f"outside-validation-{scenario}"
    outside.mkdir()
    results_path = _results_path(repo_root)
    candidate_attempt = 1
    if scenario == "task-parent":
        (runs_dir / TASK_ID).symlink_to(outside, target_is_directory=True)
    elif scenario == "validation-parent":
        task_dir = runs_dir / TASK_ID
        task_dir.mkdir()
        (task_dir / "validation").symlink_to(outside, target_is_directory=True)
    else:
        results_path.parent.mkdir(parents=True)
        external_results = outside / "results.json"
        external_results.write_text(
            ValidationResultsDocument(
                task_id=TASK_ID,
                runs=(
                    {
                        "candidate_attempt": 1,
                        "timestamp": datetime(2026, 8, 11, tzinfo=UTC),
                        "checks": [
                            {
                                "name": "ruff-lint",
                                "required": True,
                                "result": {
                                    "status": "skipped",
                                    "stdout": "",
                                    "stderr": "",
                                    "timed_out": False,
                                },
                            },
                            {
                                "name": "eslint",
                                "required": True,
                                "result": {
                                    "status": "skipped",
                                    "stdout": "",
                                    "stderr": "",
                                    "timed_out": False,
                                },
                            },
                            {
                                "name": "typecheck",
                                "required": True,
                                "result": {
                                    "status": "pass",
                                    "exit_code": 0,
                                    "stdout": "",
                                    "stderr": "",
                                    "timed_out": False,
                                },
                            },
                        ],
                        "result": {
                            "status": "pass",
                            "exit_code": 0,
                            "launch_error": None,
                            "stdout": "",
                            "stderr": "",
                            "timed_out": False,
                        },
                    },
                ),
            ).model_dump_json(),
            encoding="utf-8",
        )
        results_path.symlink_to(external_results)
        candidate_attempt = 2
    execute = Mock()
    monkeypatch.setattr(test_runner, "_execute", execute)

    with pytest.raises(ValueError, match="must not traverse a symlink"):
        run_tests(
            repo_root,
            TASK_ID,
            candidate_attempt,
            include_harness_tests=False,
            reference_digests={},
        )

    execute.assert_not_called()
